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


TABLES = {
    "stream": "messenger_streams",
    "topic": "messenger_topics",
    "message": "messenger_messages",
    "stream_binding": "messenger_stream_bindings",
    "topic_binding": "messenger_user_topic_bindings",
    "message_state": "messenger_user_message_states",
    "reaction": "messenger_message_reaction_facts",
    "file": "m_workspace_files",
}

CAPTURE = """
CREATE OR REPLACE FUNCTION zulip_capture_outgoing() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actor uuid := nullif(current_setting('workspace.outgoing_actor',true),'')::uuid;
    row_ jsonb := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    old_ jsonb := CASE WHEN TG_OP='INSERT' THEN '{}'::jsonb ELSE to_jsonb(OLD) END;
    kind_ text := TG_ARGV[0];
    operation_ text := lower(TG_OP);
    stream_ uuid;
    message_ uuid;
    entity_ uuid := (row_->>'uuid')::uuid;
    placement_ record;
    target_ record;
    data_ jsonb;
BEGIN
    IF actor IS NULL OR pg_trigger_depth()>1 THEN RETURN NULL; END IF;
    IF kind_='message' THEN
        IF TG_TABLE_NAME='messenger_message_placements' THEN
            message_ := (row_->>'message_uuid')::uuid;
            entity_ := message_;
            SELECT to_jsonb(m) INTO data_ FROM messenger_messages m WHERE m.project_id=(row_->>'project_id')::uuid AND m.uuid=message_;
            row_ := data_ || jsonb_build_object('placement_uuid',row_->>'uuid','stream_uuid',row_->>'stream_uuid','topic_uuid',row_->>'topic_uuid');
        ELSE
            IF row_->'payload' IS NOT DISTINCT FROM old_->'payload' AND row_->'deleted_at' IS NOT DISTINCT FROM old_->'deleted_at' THEN RETURN NULL; END IF;
            message_ := entity_;
            IF row_->>'deleted_at' IS NOT NULL THEN operation_ := 'delete'; END IF;
        END IF;
        IF row_->'payload'->>'kind'<>'markdown' THEN RETURN NULL; END IF;
    ELSIF kind_='reaction' THEN
        message_ := (row_->>'canonical_message_uuid')::uuid;
        IF TG_OP='UPDATE' AND row_->'emoji_name'=old_->'emoji_name' AND row_->'canonical_message_uuid'=old_->'canonical_message_uuid' THEN RETURN NULL; END IF;
    ELSIF kind_='message_state' THEN
        IF TG_OP='DELETE' THEN RETURN NULL; END IF;
        IF TG_OP='INSERT' AND NOT (row_->>'starred')::boolean THEN RETURN NULL; END IF;
        IF TG_OP='UPDATE' AND (row_->>'read_at' IS NOT NULL)=(old_->>'read_at' IS NOT NULL) AND row_->'starred'=old_->'starred' THEN RETURN NULL; END IF;
        SELECT message_uuid INTO message_ FROM messenger_message_placements WHERE project_id=(row_->>'project_id')::uuid AND uuid=(row_->>'placement_uuid')::uuid;
    ELSIF kind_='stream_binding' THEN
        IF TG_OP='UPDATE' AND row_->'active'=old_->'active' AND row_->'notification_mode'=old_->'notification_mode' THEN RETURN NULL; END IF;
    ELSIF kind_='topic_binding' THEN
        IF TG_OP='INSERT' AND row_->>'notification_mode'='default' THEN RETURN NULL; END IF;
        IF TG_OP='UPDATE' AND row_->'notification_mode'=old_->'notification_mode' THEN RETURN NULL; END IF;
        SELECT stream_uuid INTO stream_ FROM messenger_topics WHERE project_id=(row_->>'project_id')::uuid AND uuid=(row_->>'topic_uuid')::uuid;
    ELSIF kind_='stream' THEN
        stream_ := entity_;
        IF TG_OP='UPDATE' AND ROW(row_->'name',row_->'description',row_->'invite_only',row_->'is_archived',row_->'deleted_at') IS NOT DISTINCT FROM ROW(old_->'name',old_->'description',old_->'invite_only',old_->'is_archived',old_->'deleted_at') THEN RETURN NULL; END IF;
    ELSIF kind_='topic' THEN
        IF TG_OP='UPDATE' AND ROW(row_->'name',row_->'is_done',row_->'deleted_at') IS NOT DISTINCT FROM ROW(old_->'name',old_->'is_done',old_->'deleted_at') THEN RETURN NULL; END IF;
    ELSIF kind_='file' AND TG_OP='UPDATE' THEN
        -- File metadata is local; Zulip only supports uploading/removing content.
        RETURN NULL;
    END IF;
    IF row_->>'source_name'<>'zulip' OR row_->>'provider_uuid' IS NULL THEN RETURN NULL; END IF;
    stream_ := coalesce(stream_,(row_->>'stream_uuid')::uuid);
    IF message_ IS NOT NULL THEN
        SELECT stream_uuid,topic_uuid,uuid INTO placement_ FROM messenger_message_placements
        WHERE project_id=(row_->>'project_id')::uuid AND message_uuid=message_
        AND (stream_ IS NULL OR stream_uuid=stream_) ORDER BY uuid LIMIT 1;
        stream_ := coalesce(stream_,placement_.stream_uuid);
        row_ := row_ || jsonb_build_object('message_uuid',message_,'stream_uuid',stream_,'topic_uuid',placement_.topic_uuid);
    END IF;
    IF stream_ IS NULL THEN RETURN NULL; END IF;
    row_ := row_ || jsonb_build_object('stream_uuid',stream_);
    SELECT row_ || jsonb_build_object('stream_name',stream.name,'topic_name',coalesce(
        CASE WHEN topic.uuid=messenger_uuid_v5(stream_,'') THEN ''
             WHEN topic.is_done AND topic.name NOT LIKE '✔ %' THEN '✔ '||topic.name
             ELSE topic.name END,'')) INTO data_
    FROM messenger_streams stream LEFT JOIN messenger_topics topic
      ON topic.project_id=stream.project_id AND topic.uuid=(row_->>'topic_uuid')::uuid
    WHERE stream.project_id=(row_->>'project_id')::uuid AND stream.uuid=stream_;

    row_ := coalesce(data_,row_);
    IF row_->>'deleted_at' IS NOT NULL THEN operation_ := 'delete'; END IF;
    IF kind_='reaction' AND TG_OP='UPDATE' THEN
        operation_ := 'insert';
        INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
        SELECT (old_->>'project_id')::uuid,'reaction',entity_,
            'reaction:'||(old_->>'canonical_message_uuid')||':'||(old_->>'user_uuid')||':'||(old_->>'emoji_name'),
            account.uuid,'delete',old_||jsonb_build_object('message_uuid',old_->>'canonical_message_uuid','stream_uuid',placement.stream_uuid,
              'before','{}'::jsonb,'account_identity',account.external_account_id,'account_provider_uuid',account.provider_uuid,
              'actor_uuid',actor,'remote_message_identity',message.provider_identity)
        FROM messenger_message_placements placement JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
        JOIN bridge_zulip_v5_channels channel ON channel.stream_uuid=placement.stream_uuid
        JOIN m_external_accounts_v2 account ON account.uuid=channel.external_account_uuid AND account.project_id=placement.project_id
        WHERE placement.uuid=(old_->>'placement_uuid')::uuid AND placement.project_id=(old_->>'project_id')::uuid AND account.owner_user_uuid=actor AND NOT account.disabled;
    END IF;
    FOR target_ IN
        SELECT account.uuid, account.provider_uuid, account.external_account_id, channel.uuid AS channel_uuid, CASE WHEN stream.private OR stream.provider_identity LIKE 'dm:%' THEN NULL ELSE stream.provider_identity::bigint END AS stream_id, coalesce(channel.channel_type,CASE WHEN stream.private THEN 'group' ELSE 'channel' END) AS channel_type, channel.direct_user_uuid
        FROM m_external_accounts_v2 account
        LEFT JOIN bridge_zulip_v5_channels channel ON channel.external_account_uuid=account.uuid AND channel.stream_uuid=stream_
        JOIN messenger_streams stream ON stream.project_id=account.project_id AND stream.uuid=stream_
        WHERE account.project_id=(row_->>'project_id')::uuid AND account.owner_user_uuid=actor AND account.provider='zulip'
        AND NOT account.disabled AND account.status<>'new' AND account.settings->>'api_key' IS NOT NULL
        AND stream.source_name='zulip' AND stream.provider_uuid=account.provider_uuid
        AND (row_->>'provider_uuid')::uuid=account.provider_uuid
    LOOP
        data_ := row_ || jsonb_build_object('before',old_,'actor_uuid',actor,'account_identity',target_.external_account_id,'account_provider_uuid',target_.provider_uuid,'stream_id',target_.stream_id,'channel_type',target_.channel_type,'direct_user_uuid',target_.direct_user_uuid);
        IF kind_='reaction' THEN
            SELECT data_ || jsonb_build_object('remote_message_identity',(SELECT provider_identity FROM messenger_messages WHERE project_id=(row_->>'project_id')::uuid AND uuid=message_ AND provider_uuid=target_.provider_uuid),
                'emoji_observations',(SELECT coalesce(jsonb_agg(jsonb_build_object('emoji_name',r.emoji_name,'emoji_code',r.emoji_code,'reaction_type',r.reaction_type)),'[]'::jsonb)
                    FROM bridge_zulip_v5_reactions r JOIN bridge_zulip_v5_reaction_messages m ON m.uuid=r.reaction_message_uuid WHERE m.project_id=(row_->>'project_id')::uuid AND m.canonical_message_uuid=message_)) INTO data_;
        END IF;
        IF kind_='stream_binding' AND row_->'notification_mode' IS DISTINCT FROM old_->'notification_mode' THEN
            UPDATE bridge_zulip_v5_channels SET notification_mode=row_->>'notification_mode' WHERE uuid=target_.channel_uuid;
        ELSIF kind_='topic_binding' THEN
            INSERT INTO bridge_zulip_v5_topic_notifications(uuid,channel_uuid,topic_name,visibility_policy,last_updated)
            SELECT gen_random_uuid(),target_.channel_uuid,lower(row_->>'topic_name'),
                CASE row_->>'notification_mode' WHEN 'default' THEN 0 WHEN 'mute' THEN 1 WHEN 'unmute' THEN 2 WHEN 'follow' THEN 3 END,0
            WHERE target_.channel_uuid IS NOT NULL
            ON CONFLICT(channel_uuid,topic_name) DO UPDATE SET visibility_policy=EXCLUDED.visibility_policy,last_updated=0;
        END IF;
        -- Save known provider IDs now, so deletes never depend on a surviving row.
        data_ := data_ || jsonb_build_object('remote_identity',row_->>'provider_identity');
        INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
        VALUES((row_->>'project_id')::uuid,kind_,entity_,CASE WHEN kind_='reaction' THEN 'reaction:'||message_::text||':'||(row_->>'user_uuid')||':'||(row_->>'emoji_name') ELSE kind_||':'||entity_::text END,target_.uuid,operation_,data_);
    END LOOP;
    IF FOUND AND TG_OP<>'DELETE' AND row_->>'source_name'<>'native' THEN
        EXECUTE format('UPDATE %I SET status=''new'' WHERE project_id=$1 AND uuid=$2 AND status<>''new''',
            CASE WHEN TG_TABLE_NAME='messenger_message_placements' THEN 'messenger_messages' ELSE TG_TABLE_NAME END)
        USING (row_->>'project_id')::uuid,entity_;
    END IF;
    RETURN NULL;
END $$;
"""


def _inherit(session, table, parent_table, relation):
    session.execute(f"""
        UPDATE {table} child SET source_name=parent.source_name,
            provider_uuid=parent.provider_uuid,
            provider_identity=CASE WHEN parent.source_name='native' THEN NULL
                                  ELSE child.provider_identity END,
            status=CASE WHEN parent.source_name='native' THEN 'active' ELSE child.status END
        FROM {parent_table} parent
        WHERE child.project_id=parent.project_id AND {relation}
    """)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0266-Replace-Zulip-history-tasks-with-asyncio-incoming-events-0622cb.py"
        ]

    @property
    def migration_id(self):
        return "06fad543-62a8-494d-b7e6-02aa735856a8"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        triggers = session.execute(
            """SELECT tgrelid::regclass::text AS table_name,
            tgname,tgenabled FROM pg_trigger WHERE tgrelid=ANY(%s::regclass[])
            AND NOT tgisinternal""",
            (list(TABLES.values()),),
        ).fetchall()
        for table in TABLES.values():
            session.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            session.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT {table}_provider_pair_check"
            )
        ambiguous = session.execute("""SELECT 1 FROM zulip_entity_links
            WHERE entity_type IN ('stream','topic','message','file')
            GROUP BY project_id,entity_type,entity_uuid
            HAVING count(DISTINCT provider_uuid)>1 LIMIT 1""").fetchone()
        if ambiguous:
            raise RuntimeError("An entity has copies in multiple providers")
        for kind, table in TABLES.items():
            session.execute(
                f"""UPDATE {table} entity SET source_name='zulip',
                provider_uuid=link.provider_uuid,provider_identity=link.provider_identity
                FROM (SELECT DISTINCT ON(project_id,entity_uuid) * FROM zulip_entity_links
                      WHERE entity_type=%s ORDER BY project_id,entity_uuid,external_account_uuid) link
                WHERE entity.project_id=link.project_id AND entity.uuid=link.entity_uuid
            """,
                (kind,),
            )
        session.execute("""UPDATE messenger_streams stream SET source_name='zulip',
            provider_uuid=account.provider_uuid,
            provider_identity=coalesce(stream.provider_identity,channel.stream_id::text,
                                       'dm:'||stream.uuid::text)
            FROM bridge_zulip_v5_channels channel JOIN m_external_accounts_v2 account
              ON account.uuid=channel.external_account_uuid
            WHERE stream.project_id=account.project_id AND stream.uuid=channel.stream_uuid
              AND account.provider_uuid IS NOT NULL""")
        session.execute("""UPDATE messenger_streams SET source_name='native',
            provider_identity=NULL,status='active' WHERE provider_uuid IS NULL""")
        _inherit(
            session,
            "messenger_topics",
            "messenger_streams",
            "child.stream_uuid=parent.uuid",
        )
        _inherit(
            session,
            "messenger_stream_bindings",
            "messenger_streams",
            "child.stream_uuid=parent.uuid",
        )
        _inherit(
            session,
            "m_workspace_files",
            "messenger_streams",
            "child.stream_uuid=parent.uuid",
        )
        ambiguous = session.execute("""SELECT 1 FROM messenger_message_placements placement
            JOIN messenger_streams stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
            GROUP BY placement.project_id,placement.message_uuid
            HAVING count(DISTINCT (stream.source_name,stream.provider_uuid))>1 LIMIT 1""").fetchone()
        if ambiguous:
            raise RuntimeError("A canonical message has placements in multiple sources")
        session.execute("""UPDATE messenger_messages message SET source_name=stream.source_name,
            provider_uuid=stream.provider_uuid,
            provider_identity=CASE WHEN stream.source_name='native' THEN NULL ELSE coalesce(
                message.provider_identity,(SELECT delivery.zulip_message_id::text
                  FROM bridge_zulip_v5_deliveries delivery
                  JOIN messenger_providers provider ON provider.hostname=delivery.hostname
                  WHERE delivery.project_id=message.project_id AND delivery.message_uuid=message.uuid
                    AND provider.uuid=stream.provider_uuid AND delivery.zulip_message_id IS NOT NULL
                  ORDER BY delivery.uuid LIMIT 1)) END,
            status=CASE WHEN stream.source_name='native' THEN 'active' ELSE message.status END
            FROM messenger_message_placements placement JOIN messenger_streams stream
              ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
            WHERE message.project_id=placement.project_id AND message.uuid=placement.message_uuid""")
        _inherit(
            session,
            "messenger_user_topic_bindings",
            "messenger_topics",
            "child.topic_uuid=parent.uuid",
        )
        _inherit(
            session,
            "messenger_message_reaction_facts",
            "messenger_messages",
            "child.canonical_message_uuid=parent.uuid",
        )
        session.execute("""UPDATE messenger_user_message_states state SET
            source_name=message.source_name,provider_uuid=message.provider_uuid,
            provider_identity=CASE WHEN message.source_name='native' THEN NULL ELSE state.provider_identity END,
            status=CASE WHEN message.source_name='native' THEN 'active' ELSE state.status END
            FROM messenger_message_placements placement JOIN messenger_messages message
              ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
            WHERE state.project_id=placement.project_id AND state.placement_uuid=placement.uuid""")
        for table in TABLES.values():
            session.execute(
                f"UPDATE {table} SET status='new' WHERE source_name='zulip' AND provider_identity IS NULL"
            )
        for kind, table in TABLES.items():
            session.execute(
                f"""UPDATE zulip_outgoing_intents intent SET data=intent.data||
                jsonb_build_object('source_name',entity.source_name,'provider_uuid',entity.provider_uuid,
                                   'provider_identity',entity.provider_identity)
                FROM {table} entity WHERE intent.entity_type=%s
                  AND intent.project_id=entity.project_id AND intent.entity_uuid=entity.uuid""",
                (kind,),
            )
            session.execute(f"""ALTER TABLE {table} ADD CONSTRAINT {table}_provider_pair_check
                CHECK ((source_name='native' AND provider_uuid IS NULL AND provider_identity IS NULL)
                    OR (source_name='zulip' AND provider_uuid IS NOT NULL))""")
        session.execute(
            "DELETE FROM zulip_outgoing_intents WHERE data->>'source_name'='native'"
        )
        session.execute(
            "DELETE FROM zulip_entity_links WHERE entity_type NOT IN ('reaction','file')"
        )
        # A pending upload is visible to its uploader before the provider receipt
        # creates the account-scoped access observation.
        definition = session.execute(
            "SELECT pg_get_viewdef('m_workspace_visible_files_v1'::regclass) AS sql"
        ).fetchone()["sql"]
        definition = definition.replace(
            "(file.source_name)::text <> 'zulip'::text",
            "((file.source_name)::text <> 'zulip'::text OR (file.provider_identity IS NULL AND file.user_uuid=visible.viewer_user_uuid))",
        )
        session.execute(
            "CREATE OR REPLACE VIEW m_workspace_visible_files_v1 AS " + definition
        )
        session.execute(CAPTURE)
        for trigger in triggers:
            state = trigger["tgenabled"]
            if state == "D":
                continue
            mode = {"O": "ENABLE", "A": "ENABLE ALWAYS", "R": "ENABLE REPLICA"}[state]
            session.execute(
                f"ALTER TABLE {trigger['table_name']} {mode} TRIGGER {trigger['tgname']}"
            )

    def downgrade(self, session):
        raise NotImplementedError("The Zulip provenance cutover is irreversible")


migration_step = MigrationStep()
