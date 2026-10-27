# Copyright 2016 Eugene Frolov <eugene@frolov.net.ru>
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from restalchemy.storage.sql import migrations


SCHEMA = """
CREATE OR REPLACE FUNCTION zulip_direct_message_recipients(
    project_ uuid, provider_ uuid, stream_ uuid, actor_ uuid
) RETURNS jsonb LANGUAGE sql STABLE AS $$
    WITH endpoints AS (
        SELECT endpoint.side, coalesce(mapped.owner_user_uuid, endpoint.user_uuid) AS user_uuid
        FROM messenger_streams stream
        CROSS JOIN LATERAL (VALUES ('owner',stream.owner_uuid),('peer',stream.direct_user_uuid)) endpoint(side,user_uuid)
        LEFT JOIN m_workspace_users legacy ON legacy.uuid=endpoint.user_uuid
        LEFT JOIN LATERAL (
            SELECT account.owner_user_uuid
            FROM m_external_accounts_v2 account
            WHERE account.project_id=project_ AND account.provider_uuid=provider_
              AND account.provider='zulip' AND account.external_account_id IS NOT NULL
              AND (account.owner_user_uuid=endpoint.user_uuid OR
                   (legacy.source IN ('zulip','hidden') AND
                    messenger_uuid_v5(provider_,'user:'||account.external_account_id)=endpoint.user_uuid))
            ORDER BY (account.owner_user_uuid=endpoint.user_uuid) DESC LIMIT 1
        ) mapped ON true
        WHERE stream.project_id=project_ AND stream.uuid=stream_
          AND stream.source_name='zulip' AND stream.provider_uuid=provider_
          AND stream.private AND stream.direct_user_uuid IS NOT NULL
    ), peer AS (
        SELECT CASE WHEN owner.user_uuid=actor_ THEN other.user_uuid
                    WHEN other.user_uuid=actor_ THEN owner.user_uuid END AS user_uuid
        FROM endpoints owner CROSS JOIN endpoints other
        WHERE owner.side='owner' AND other.side='peer'
    )
    SELECT jsonb_agg(DISTINCT account.external_account_id::bigint)
    FROM peer JOIN m_external_accounts_v2 account ON account.owner_user_uuid=peer.user_uuid
    WHERE account.project_id=project_ AND account.provider_uuid=provider_
      AND account.provider='zulip' AND account.external_account_id IS NOT NULL
$$;
CREATE OR REPLACE FUNCTION zulip_rebind_direct_user(
    project_ uuid, provider_ uuid, legacy_ uuid, canonical_ uuid
) RETURNS SETOF uuid LANGUAGE plpgsql AS $$
DECLARE affected_ uuid[];
BEGIN
    IF legacy_=canonical_ OR NOT EXISTS (
        SELECT 1 FROM m_workspace_users legacy WHERE legacy.uuid=legacy_
          AND legacy.source IN ('zulip','hidden')
    ) OR NOT EXISTS (
        SELECT 1 FROM m_external_accounts_v2 account
        WHERE account.project_id=project_ AND account.provider_uuid=provider_
          AND account.provider='zulip' AND account.owner_user_uuid=canonical_
          AND account.external_account_id IS NOT NULL
    ) THEN RETURN; END IF;
    SELECT array_agg(stream.uuid) INTO affected_ FROM messenger_streams stream
    WHERE stream.project_id=project_ AND stream.provider_uuid=provider_ AND stream.source_name='zulip'
      AND stream.private AND stream.direct_user_uuid IS NOT NULL
      AND (stream.owner_uuid=legacy_ OR stream.direct_user_uuid=legacy_);
    PERFORM messenger_v2_register_project_user(project_,canonical_);
    INSERT INTO messenger_stream_bindings (
        uuid,project_id,stream_uuid,user_uuid,who_uuid,active,role,notification_mode,notification_updated_at,
        source_name,provider_uuid,provider_identity,status
    )
    SELECT messenger_uuid_v5(stream.uuid,canonical_::text),project_,stream.uuid,canonical_,
           CASE WHEN binding.who_uuid=legacy_ THEN canonical_ ELSE binding.who_uuid END,
           binding.active,binding.role,binding.notification_mode,binding.notification_updated_at,
           binding.source_name,binding.provider_uuid,binding.provider_identity,binding.status
    FROM messenger_streams stream JOIN messenger_stream_bindings binding
      ON binding.project_id=stream.project_id AND binding.stream_uuid=stream.uuid
    WHERE stream.project_id=project_ AND stream.provider_uuid=provider_ AND stream.source_name='zulip'
      AND stream.private AND stream.direct_user_uuid IS NOT NULL
      AND (stream.owner_uuid=legacy_ OR stream.direct_user_uuid=legacy_) AND binding.user_uuid=legacy_
    ON CONFLICT (project_id,stream_uuid,user_uuid) DO NOTHING;
    INSERT INTO messenger_stream_bindings (
        uuid,project_id,stream_uuid,user_uuid,who_uuid,active,
        source_name,provider_uuid,provider_identity,status
    )
    SELECT messenger_uuid_v5(stream.uuid,canonical_::text),project_,stream.uuid,canonical_,
           CASE WHEN stream.owner_uuid=legacy_ THEN canonical_ ELSE stream.owner_uuid END,true,
           'zulip',provider_,stream.provider_identity||':'||account.external_account_id,'active'
    FROM messenger_streams stream JOIN m_external_accounts_v2 account
      ON account.project_id=stream.project_id AND account.provider_uuid=stream.provider_uuid
     AND account.owner_user_uuid=canonical_ AND account.provider='zulip'
    WHERE stream.project_id=project_ AND stream.provider_uuid=provider_ AND stream.source_name='zulip'
      AND stream.private AND stream.direct_user_uuid IS NOT NULL
      AND (stream.owner_uuid=legacy_ OR stream.direct_user_uuid=legacy_)
    ON CONFLICT (project_id,stream_uuid,user_uuid) DO NOTHING;
    INSERT INTO messenger_user_topic_bindings (
        uuid,project_id,topic_uuid,user_uuid,notification_mode,
        source_name,provider_uuid,provider_identity,status
    )
    SELECT messenger_uuid_v5(topic.uuid,canonical_::text),project_,topic.uuid,canonical_,
           coalesce(binding.notification_mode,'default'),
           'zulip',provider_,topic.provider_identity||':'||account.external_account_id,'active'
    FROM messenger_streams stream JOIN messenger_topics topic
      ON topic.project_id=stream.project_id AND topic.stream_uuid=stream.uuid
    JOIN m_external_accounts_v2 account
      ON account.project_id=stream.project_id AND account.provider_uuid=stream.provider_uuid
     AND account.owner_user_uuid=canonical_ AND account.provider='zulip'
    LEFT JOIN messenger_user_topic_bindings binding
      ON binding.project_id=topic.project_id AND binding.topic_uuid=topic.uuid AND binding.user_uuid=legacy_
    WHERE stream.project_id=project_ AND stream.provider_uuid=provider_ AND stream.source_name='zulip'
      AND stream.private AND stream.direct_user_uuid IS NOT NULL
      AND (stream.owner_uuid=legacy_ OR stream.direct_user_uuid=legacy_)
      AND (topic.uuid=stream.default_topic_uuid OR binding.uuid IS NOT NULL)
    ON CONFLICT (project_id,user_uuid,topic_uuid) DO NOTHING;
    UPDATE messenger_stream_bindings binding SET active=false
    FROM messenger_streams stream
    WHERE binding.project_id=project_ AND binding.stream_uuid=stream.uuid AND binding.user_uuid=legacy_
      AND binding.active AND stream.project_id=project_ AND stream.provider_uuid=provider_
      AND stream.source_name='zulip' AND stream.private AND stream.direct_user_uuid IS NOT NULL
      AND (stream.owner_uuid=legacy_ OR stream.direct_user_uuid=legacy_);
    RETURN QUERY
    UPDATE messenger_streams stream
    SET owner_uuid=CASE WHEN owner_uuid=legacy_ THEN canonical_ ELSE owner_uuid END,
        direct_user_uuid=CASE WHEN direct_user_uuid=legacy_ THEN canonical_ ELSE direct_user_uuid END
    WHERE stream.project_id=project_ AND stream.provider_uuid=provider_ AND stream.source_name='zulip'
      AND stream.private AND stream.direct_user_uuid IS NOT NULL
      AND (stream.owner_uuid=legacy_ OR stream.direct_user_uuid=legacy_)
    RETURNING stream.uuid;
    UPDATE zulip_outgoing_intents intent
    SET data=intent.data || jsonb_build_object(
            'direct_user_uuid',stream.direct_user_uuid,'channel_type','private',
            'recipient_ids',zulip_direct_message_recipients(project_,provider_,
                (intent.data->>'stream_uuid')::uuid,account.owner_user_uuid)),
        error=NULL,next_attempt_at=now()
    FROM m_external_accounts_v2 account, messenger_streams stream
    WHERE intent.project_id=project_ AND intent.external_account_uuid=account.uuid
      AND account.project_id=project_ AND account.provider_uuid=provider_ AND account.provider='zulip'
      AND intent.status='new' AND intent.request IS NULL
      AND intent.entity_type='message' AND intent.operation='insert'
      AND stream.project_id=project_ AND stream.uuid=(intent.data->>'stream_uuid')::uuid
      AND stream.provider_uuid=provider_ AND stream.source_name='zulip'
      AND stream.private AND stream.direct_user_uuid IS NOT NULL
      AND (intent.data->>'direct_user_uuid'=legacy_::text OR stream.uuid=ANY(affected_));
END $$;
"""

PREVIOUS_RECIPIENTS = """CASE WHEN target_.channel_type='private' THEN
                (SELECT jsonb_agg(DISTINCT peer.external_account_id::bigint)
                 FROM m_external_accounts_v2 peer
                 WHERE peer.project_id=(row_->>'project_id')::uuid
                   AND peer.owner_user_uuid=target_.direct_user_uuid
                   AND peer.provider='zulip'
                   AND peer.provider_uuid=target_.provider_uuid
                   AND peer.external_account_id IS NOT NULL)
                ELSE (SELECT jsonb_agg(DISTINCT peer.external_account_id::bigint)
                FROM messenger_stream_bindings member LEFT JOIN m_external_accounts_v2 peer
                  ON peer.project_id=member.project_id AND peer.owner_user_uuid=member.user_uuid
                 AND peer.provider='zulip' AND peer.provider_uuid=target_.provider_uuid
                WHERE member.project_id=(row_->>'project_id')::uuid AND member.stream_uuid=stream_
                  AND member.active) END"""
RECIPIENTS = """CASE WHEN target_.channel_type='private' THEN zulip_direct_message_recipients((row_->>'project_id')::uuid,target_.provider_uuid,stream_,actor) ELSE (SELECT jsonb_agg(DISTINCT peer.external_account_id::bigint)
                FROM messenger_stream_bindings member LEFT JOIN m_external_accounts_v2 peer
                  ON peer.project_id=member.project_id AND peer.owner_user_uuid=member.user_uuid
                 AND peer.provider='zulip' AND peer.provider_uuid=target_.provider_uuid
                WHERE member.project_id=(row_->>'project_id')::uuid AND member.stream_uuid=stream_
                  AND member.active) END"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0282-Allow-full-provider-identities-for-imported-files-78fba4.py"
        ]

    @property
    def migration_id(self):
        return "6b50aef5-2695-43a9-8454-02f968b3c47b"

    @property
    def is_manual(self):
        return False

    def _capture(self, session, old, new):
        capture = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS sql"
        ).fetchone()["sql"]
        if (
            "'recipient_ids'," + old not in capture
            and "'recipient_ids'," + new not in capture
        ):
            raise RuntimeError("Unrecognized Zulip recipient capture definition")
        session.execute(
            capture.replace("'recipient_ids'," + old, "'recipient_ids'," + new)
        )

    def upgrade(self, session):
        session.execute(SCHEMA)
        self._capture(session, PREVIOUS_RECIPIENTS, RECIPIENTS)
        previous = session.execute(
            "SELECT current_setting('workspace.zulip_incoming',true) AS value"
        ).fetchone()["value"]
        session.execute("SELECT set_config('workspace.zulip_incoming','on',true)")
        session.execute("""
            SELECT zulip_rebind_direct_user(account.project_id,account.provider_uuid,
                       legacy.uuid,account.owner_user_uuid)
            FROM m_external_accounts_v2 account
            JOIN m_workspace_users legacy
              ON legacy.uuid=messenger_uuid_v5(account.provider_uuid,'user:'||account.external_account_id)
            WHERE account.provider='zulip' AND account.external_account_id IS NOT NULL
              AND account.owner_user_uuid<>legacy.uuid AND legacy.source IN ('zulip','hidden')
        """)
        session.execute(
            "SELECT set_config('workspace.zulip_incoming',%s,true)", (previous or "",)
        )

    def downgrade(self, session):
        # Canonical identity references remain valid and must not be reverted.
        self._capture(session, RECIPIENTS, PREVIOUS_RECIPIENTS)
        session.execute("DROP FUNCTION zulip_rebind_direct_user(uuid,uuid,uuid,uuid)")
        session.execute(
            "DROP FUNCTION zulip_direct_message_recipients(uuid,uuid,uuid,uuid)"
        )


migration_step = MigrationStep()
