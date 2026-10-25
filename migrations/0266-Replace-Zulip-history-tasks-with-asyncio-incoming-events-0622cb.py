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

import re

from restalchemy.storage.sql import migrations


SCHEMA = """
CREATE TABLE zulip_incoming_subscriptions (
    external_account_uuid uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    provider_uuid uuid NOT NULL REFERENCES messenger_providers(uuid),
    owner_user_uuid uuid NOT NULL,
    account_identity varchar(255) NOT NULL,
    server_url text NOT NULL,
    queue_id text NOT NULL,
    last_event_id bigint NOT NULL,
    longpoll_timeout integer NOT NULL,
    FOREIGN KEY (external_account_uuid, project_id)
        REFERENCES m_external_accounts_v2(uuid, project_id) ON DELETE CASCADE
);
CREATE TABLE zulip_incoming_attachments (
    project_id uuid NOT NULL,
    provider_uuid uuid NOT NULL REFERENCES messenger_providers(uuid),
    attachment_id bigint NOT NULL,
    path text NOT NULL,
    name text NOT NULL,
    PRIMARY KEY (project_id, provider_uuid, attachment_id)
);
CREATE TABLE zulip_incoming_events (
    id bigserial PRIMARY KEY,
    uuid uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    project_id uuid NOT NULL,
    external_account_uuid uuid NOT NULL,
    provider_uuid uuid NOT NULL REFERENCES messenger_providers(uuid),
    owner_user_uuid uuid NOT NULL,
    account_identity varchar(255) NOT NULL,
    queue_id text NOT NULL,
    event_id bigint NOT NULL,
    payload jsonb NOT NULL,
    ordering_keys text[] NOT NULL DEFAULT ARRAY['*']::text[],
    processed_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    next_retry_at timestamptz NOT NULL DEFAULT now(),
    error text,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (external_account_uuid, queue_id, event_id),
    FOREIGN KEY (external_account_uuid, project_id)
        REFERENCES m_external_accounts_v2(uuid, project_id) ON DELETE CASCADE
);
CREATE INDEX zulip_incoming_pending ON zulip_incoming_events(project_id, provider_uuid, id)
    WHERE processed_at IS NULL;
INSERT INTO zulip_incoming_subscriptions
    (external_account_uuid, project_id, provider_uuid, owner_user_uuid,
     account_identity, server_url, queue_id, last_event_id, longpoll_timeout)
SELECT account.uuid, account.project_id, account.provider_uuid,
       account.owner_user_uuid, account.external_account_id,
       account.settings->>'server_url', subscription.queue_id,
       subscription.last_event_id, subscription.longpoll_timeout
FROM bridge_zulip_v5_subscriptions subscription
JOIN m_external_accounts_v2 account ON account.uuid=subscription.external_account_uuid
WHERE account.external_account_id IS NOT NULL AND account.provider_uuid IS NOT NULL;
INSERT INTO zulip_incoming_events
    (uuid, project_id, external_account_uuid, provider_uuid, owner_user_uuid,
     account_identity, queue_id, event_id, payload, processed_at, received_at)
SELECT event.uuid, event.project_id, account.uuid, account.provider_uuid,
       account.owner_user_uuid, account.external_account_id, event.queue_id,
       event.event_id, event.payload,
       CASE WHEN EXISTS (
           SELECT 1 FROM bridge_zulip_v5_tasks task
           WHERE task.realtime_event_uuid=event.uuid
             AND task.status IN ('new', 'waiting', 'retry', 'error')
       ) THEN NULL ELSE event.processed_at AT TIME ZONE 'UTC' END,
       event.created_at AT TIME ZONE 'UTC'
FROM bridge_zulip_v5_events event
JOIN m_external_accounts_v2 account ON account.uuid=event.external_account_uuid
WHERE event.source='zulip' AND event.event_id IS NOT NULL
  AND account.external_account_id IS NOT NULL AND account.provider_uuid IS NOT NULL
ORDER BY event.created_at, event.event_id
ON CONFLICT DO NOTHING;
DROP TABLE bridge_zulip_v5_tasks CASCADE;
ALTER TABLE bridge_zulip_v5_deliveries DROP COLUMN IF EXISTS placement_event_uuid CASCADE;
ALTER TABLE bridge_zulip_v5_messages DROP COLUMN IF EXISTS placement_event_uuid CASCADE;
DROP TABLE bridge_zulip_v5_events CASCADE;
DROP TABLE bridge_zulip_v5_subscriptions;
DROP TABLE bridge_zulip_v5_message_fingerprints;
DROP TABLE bridge_zulip_v5_history_batches CASCADE;
ALTER TABLE m_external_accounts_v2 DROP COLUMN has_completed_backfill;
ALTER TABLE m_external_accounts_v2 DROP CONSTRAINT m_external_accounts_v2_status_check;
UPDATE m_external_accounts_v2 SET status='syncing'
    WHERE status IN ('backfill', 'fast_backfill');
ALTER TABLE m_external_accounts_v2 ADD CONSTRAINT m_external_accounts_v2_status_check
    CHECK (status IN ('new', 'syncing', 'active', 'error'));
CREATE FUNCTION zulip_incoming_wakeup() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('workspace_zulip_incoming', '');
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS bridge_zulip_v5_accounts_changed ON m_external_accounts_v2;
CREATE TRIGGER zulip_incoming_accounts_changed
    AFTER INSERT OR UPDATE OR DELETE ON m_external_accounts_v2
    FOR EACH STATEMENT EXECUTE FUNCTION zulip_incoming_wakeup();
CREATE TRIGGER zulip_incoming_events_ready AFTER INSERT ON zulip_incoming_events
    FOR EACH STATEMENT EXECUTE FUNCTION zulip_incoming_wakeup();
"""

CAPTURE_ROUTES_SQL = """
    FOR target_ IN
        SELECT account.uuid, account.provider_uuid, account.external_account_id,
               CASE WHEN identity.value ~ '^[0-9]+$'
                    THEN identity.value::bigint END AS stream_id,
               CASE WHEN stream.private THEN 'private'
                    WHEN identity.value LIKE 'dm:%' THEN 'group'
                    ELSE 'channel' END AS channel_type,
               stream.direct_user_uuid
        FROM m_external_accounts_v2 account
        JOIN messenger_streams stream
          ON stream.project_id=account.project_id AND stream.uuid=stream_
        LEFT JOIN LATERAL (
            SELECT COALESCE(stream.provider_identity, link.provider_identity) AS value
            FROM (SELECT 1) seed LEFT JOIN zulip_entity_links link
              ON link.entity_type='stream' AND link.entity_uuid=stream_
             AND link.external_account_uuid=account.uuid
             AND link.provider_uuid=account.provider_uuid LIMIT 1
        ) identity ON true
        WHERE account.project_id=(row_->>'project_id')::uuid
          AND account.owner_user_uuid=actor AND account.provider='zulip'
          AND NOT account.disabled AND account.status<>'new'
          AND account.settings->>'api_key' IS NOT NULL
          AND ((stream.source_name='zulip' AND stream.provider_uuid=account.provider_uuid)
               OR EXISTS (
                   SELECT 1 FROM zulip_entity_links link
                   WHERE link.entity_type='stream' AND link.entity_uuid=stream_
                     AND link.external_account_uuid=account.uuid
                     AND link.provider_uuid=account.provider_uuid
               ))
    LOOP
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0265-Replace-outbound-Zulip-tasks-with-SQL-intents-9cafda.py"]

    @property
    def migration_id(self):
        return "0622cb0d-703f-4a93-8f7d-a15f8a04ef4a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        triggers = session.execute(
            """
            SELECT trigger.tgname AS name, trigger.tgrelid::regclass::text AS table_name
            FROM pg_trigger trigger
            JOIN pg_proc function ON function.oid=trigger.tgfoid
            JOIN pg_class table_ ON table_.oid=trigger.tgrelid
            WHERE NOT trigger.tgisinternal AND (
                ((trigger.tgtype & 2)=0 AND (
                    table_.relname LIKE 'bridge_zulip_v5_%%'
                    OR function.proname LIKE 'bridge_zulip_v5_%%'
                    OR function.proname LIKE 'messenger_%%zulip%%'
                    OR function.proname='messenger_track_message_changes'
                )) OR function.proname='messenger_assign_provider_origin'
            )
        """,
            (),
        ).fetchall()
        for trigger in triggers:
            session.execute(
                f'DROP TRIGGER "{trigger["name"]}" ON {trigger["table_name"]}'
            )
        session.execute(SCHEMA)
        capture = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS sql"
        ).fetchone()["sql"]
        capture = re.sub(
            r"FOR target_ IN.*?\n    LOOP",
            lambda match: CAPTURE_ROUTES_SQL,
            capture,
            count=1,
            flags=re.DOTALL,
        )
        capture = re.sub(
            r"        IF kind_='stream_binding' AND row_->'notification_mode'.*?\n        END IF;",
            "",
            capture,
            count=1,
            flags=re.DOTALL,
        )
        capture = capture.replace(
            "JOIN bridge_zulip_v5_channels channel ON channel.stream_uuid=placement.stream_uuid\n"
            "        JOIN m_external_accounts_v2 account ON account.uuid=channel.external_account_uuid AND account.project_id=placement.project_id",
            "JOIN messenger_streams stream ON stream.uuid=placement.stream_uuid AND stream.project_id=placement.project_id\n"
            "        JOIN m_external_accounts_v2 account ON account.provider_uuid=stream.provider_uuid AND account.project_id=placement.project_id",
        )
        capture = capture.replace(
            "'direct_user_uuid',target_.direct_user_uuid)",
            "'direct_user_uuid',target_.direct_user_uuid,'recipient_ids', "
            "(SELECT jsonb_agg(DISTINCT peer.external_account_id::bigint) "
            "FROM messenger_stream_bindings member JOIN m_external_accounts_v2 peer "
            "ON peer.project_id=member.project_id AND peer.owner_user_uuid=member.user_uuid "
            "WHERE member.project_id=(row_->>'project_id')::uuid AND member.stream_uuid=stream_ "
            "AND member.active AND peer.provider_uuid=target_.provider_uuid "
            "AND peer.external_account_id IS NOT NULL))",
        )
        session.execute(capture)
        session.execute("""
            INSERT INTO zulip_entity_links (
                project_id, entity_type, entity_uuid, provider_uuid,
                external_account_uuid, provider_identity, data
            )
            SELECT file.project_id, 'file', file.uuid, file.provider_uuid,
                   account.uuid, file.provider_identity,
                   jsonb_build_object('account_identity', account.external_account_id)
            FROM m_workspace_files file
            JOIN messenger_stream_bindings member
              ON member.project_id=file.project_id AND member.stream_uuid=file.stream_uuid
             AND member.active
            JOIN m_external_accounts_v2 account
              ON account.project_id=member.project_id
             AND account.owner_user_uuid=member.user_uuid
             AND account.provider_uuid=file.provider_uuid
            WHERE file.source_name='zulip' AND file.provider_identity IS NOT NULL
            ON CONFLICT DO NOTHING
        """)
        files = (
            session.execute(
                "SELECT pg_get_viewdef('m_workspace_visible_files_v1'::regclass, true) AS sql"
            )
            .fetchone()["sql"]
            .rstrip()
            .removesuffix(";")
        )
        session.execute(f"""
            CREATE OR REPLACE VIEW m_workspace_visible_files_v1 AS
            SELECT visible.* FROM ({files}) visible
            JOIN m_workspace_files file ON file.uuid=visible.uuid
            WHERE file.source_name<>'zulip' OR file.acl_mode='public' OR EXISTS (
                SELECT 1 FROM zulip_entity_links link
                JOIN m_external_accounts_v2 account ON account.uuid=link.external_account_uuid
                WHERE link.project_id=file.project_id AND link.entity_type='file'
                  AND link.entity_uuid=file.uuid AND link.provider_uuid=file.provider_uuid
                  AND account.provider_uuid=link.provider_uuid
                  AND account.owner_user_uuid=visible.viewer_user_uuid
                  AND link.data->>'account_identity'=account.external_account_id
            )
        """)
        # Only viewer-specific resources inject a recipient into their payload.
        # Reaction actors, file owners and changed membership users must survive.
        definition = session.execute(
            "SELECT pg_get_viewdef('m_workspace_visible_events'::regclass, true) AS sql"
        ).fetchone()["sql"]
        definition = definition.replace(
            "b.object_type = 'user'::text",
            "b.object_type NOT IN ('message', 'stream', 'topic', 'folder', 'folder_item')",
        )
        session.execute(
            f"CREATE OR REPLACE VIEW m_workspace_visible_events AS {definition}"
        )

    def downgrade(self, session):
        raise RuntimeError("History task removal requires restoring a database backup")


migration_step = MigrationStep()
