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
CREATE TABLE telegram_account_state (
    external_account_uuid uuid PRIMARY KEY REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
    generation uuid NOT NULL DEFAULT gen_random_uuid(),
    auth_state text NOT NULL DEFAULT 'disconnected',
    auth_expires_at timestamp, attempt_expires_at timestamp,
    session_envelope jsonb, login_envelope jsonb, password_envelope jsonb,
    disconnect_requested boolean NOT NULL DEFAULT false,
    pts bigint, qts bigint, cursor_date timestamp,
    error_code text, next_attempt_at timestamp NOT NULL DEFAULT now(),
    authorized_at timestamp, updated_at timestamp NOT NULL DEFAULT now()
);
CREATE TABLE telegram_peer_state (
    external_account_uuid uuid NOT NULL REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
    peer_key text NOT NULL, title text NOT NULL,
    input_peer_envelope jsonb NOT NULL,
    peer_kind text NOT NULL CHECK(peer_kind IN ('user','chat','channel')),
    can_send boolean NOT NULL DEFAULT false, forum boolean NOT NULL DEFAULT false,
    selected boolean NOT NULL DEFAULT false,
    stream_uuid uuid, pts bigint, read_max_id bigint NOT NULL DEFAULT 0,
    history_before_id bigint NOT NULL DEFAULT 0, history_done boolean NOT NULL DEFAULT false,
    history_started_at timestamp, first_message_at timestamp, history_finished_at timestamp,
    imported_messages bigint NOT NULL DEFAULT 0, imported_files bigint NOT NULL DEFAULT 0,
    imported_bytes bigint NOT NULL DEFAULT 0, fetch_seconds double precision NOT NULL DEFAULT 0,
    merge_seconds double precision NOT NULL DEFAULT 0, file_seconds double precision NOT NULL DEFAULT 0,
    updated_at timestamp NOT NULL DEFAULT now(),
    PRIMARY KEY(external_account_uuid,peer_key)
);
CREATE INDEX telegram_selected_peers ON telegram_peer_state(external_account_uuid) WHERE selected;
CREATE TABLE telegram_incoming_events (
    id bigserial PRIMARY KEY,
    external_account_uuid uuid NOT NULL REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
    event_key text NOT NULL, payload jsonb NOT NULL,
    received_at timestamp NOT NULL DEFAULT now(), processed_at timestamp,
    UNIQUE(external_account_uuid,event_key)
);
CREATE INDEX telegram_pending_incoming ON telegram_incoming_events(external_account_uuid,received_at) WHERE processed_at IS NULL;
CREATE INDEX telegram_observed_identity ON telegram_incoming_events((payload->>'identity'),received_at);
CREATE TABLE telegram_outgoing_intents (
    id bigserial PRIMARY KEY, uuid uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL,
    external_account_uuid uuid NOT NULL REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
    peer_key text NOT NULL, entity_type text NOT NULL, entity_uuid uuid NOT NULL,
    operation text NOT NULL, data jsonb NOT NULL,
    random_id bigint NOT NULL DEFAULT ('x'||substr(md5(gen_random_uuid()::text),1,16))::bit(64)::bigint,
    status text NOT NULL DEFAULT 'new', request jsonb, error_code text,
    next_attempt_at timestamp NOT NULL DEFAULT now(), attempts integer NOT NULL DEFAULT 0,
    created_at timestamp NOT NULL DEFAULT now(), completed_at timestamp,
    UNIQUE(external_account_uuid,random_id)
);
CREATE INDEX telegram_pending_outgoing ON telegram_outgoing_intents(external_account_uuid,id)
    WHERE status IN ('new','checking');
"""


CAPTURE = """
CREATE FUNCTION telegram_capture_outgoing() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actor uuid := nullif(current_setting('workspace.outgoing_actor',true),'')::uuid;
    value jsonb := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    previous jsonb := CASE WHEN TG_OP='INSERT' THEN '{}'::jsonb ELSE to_jsonb(OLD) END;
    kind text := TG_ARGV[0];
    _message_uuid uuid; message_record record; target record; operation text;
BEGIN
    IF actor IS NULL OR pg_trigger_depth()>1 THEN RETURN NULL; END IF;
    operation := lower(TG_OP);
    IF kind='message' AND TG_TABLE_NAME='messenger_message_placements' THEN
        _message_uuid := (value->>'message_uuid')::uuid;
    ELSIF kind='message' THEN
        IF value->'content_uuid' IS NOT DISTINCT FROM previous->'content_uuid'
           AND value->'deleted_at' IS NOT DISTINCT FROM previous->'deleted_at' THEN RETURN NULL; END IF;
        _message_uuid := (value->>'uuid')::uuid;
    ELSIF kind='reaction' THEN
        _message_uuid := (value->>'canonical_message_uuid')::uuid;
    ELSIF kind='read' THEN
        IF TG_OP<>'DELETE' OR actor<>(value->>'user_uuid')::uuid THEN RETURN NULL; END IF;
        SELECT placement.message_uuid INTO _message_uuid FROM messenger_message_placements placement
        WHERE placement.project_id=(value->>'project_id')::uuid AND placement.uuid=(value->>'placement_uuid')::uuid;
    ELSE
        RETURN NULL;
    END IF;
    SELECT message.*,contents.workspace_content AS payload INTO message_record
    FROM messenger_messages message JOIN messenger_message_contents contents ON contents.uuid=message.content_uuid
    WHERE message.project_id=(value->>'project_id')::uuid AND message.uuid=_message_uuid;
    IF NOT FOUND OR message_record.source_name<>'telegram' THEN RETURN NULL; END IF;
    IF kind='message' AND message_record.deleted_at IS NOT NULL THEN operation := 'delete'; END IF;
    FOR target IN
        SELECT account.uuid,peer.peer_key,topic.provider_identity AS topic_identity
        FROM messenger_message_placements placement
        JOIN telegram_peer_state peer ON peer.stream_uuid=placement.stream_uuid AND peer.selected
        JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid
        JOIN m_external_accounts_v2 account ON account.uuid=peer.external_account_uuid
        JOIN messenger_stream_bindings member ON member.project_id=placement.project_id
          AND member.stream_uuid=placement.stream_uuid AND member.user_uuid=actor AND member.active
        WHERE placement.project_id=message_record.project_id AND placement.message_uuid=_message_uuid
          AND account.project_id=message_record.project_id AND account.owner_user_uuid=actor
          AND account.provider='telegram' AND NOT account.disabled
          AND account.provider_uuid=message_record.provider_uuid
          AND (TG_TABLE_NAME<>'messenger_message_placements' OR placement.uuid=(value->>'uuid')::uuid)
    LOOP
        INSERT INTO telegram_outgoing_intents(project_id,external_account_uuid,peer_key,entity_type,entity_uuid,operation,data)
        VALUES(message_record.project_id,target.uuid,target.peer_key,kind,_message_uuid,operation,
            jsonb_build_object('provider_identity',message_record.provider_identity,'payload',message_record.payload,
                               'actor_uuid',actor,'row',value,'topic_identity',target.topic_identity));
    END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER telegram_capture_message_create AFTER INSERT ON messenger_message_placements
    FOR EACH ROW EXECUTE FUNCTION telegram_capture_outgoing('message');
CREATE TRIGGER telegram_capture_message_update AFTER UPDATE ON messenger_messages
    FOR EACH ROW EXECUTE FUNCTION telegram_capture_outgoing('message');
CREATE TRIGGER telegram_capture_read AFTER DELETE ON messenger_user_message_unreads
    FOR EACH ROW EXECUTE FUNCTION telegram_capture_outgoing('read');
CREATE TRIGGER telegram_capture_reaction AFTER INSERT OR UPDATE OR DELETE ON messenger_message_reaction_facts
    FOR EACH ROW EXECUTE FUNCTION telegram_capture_outgoing('reaction');
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0287-Cache-successful-Zulip-history-blocks-c0769e.py"]

    @property
    def migration_id(self):
        return "b504836f-85dc-4687-b756-34e5bcd00b81"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        constraints = session.execute("""
            SELECT conrelid::regclass::text AS table_name,conname,
                   pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE contype='c' AND connamespace='public'::regnamespace
              AND (pg_get_constraintdef(oid) LIKE '%%source_name%%'
                   OR conrelid='m_workspace_users'::regclass)
        """).fetchall()
        for constraint in constraints:
            definition = constraint["definition"]
            if "'zulip'" not in definition:
                continue
            parsed = re.fullmatch(r"CHECK \((.*)\)( NOT VALID)?", definition, re.DOTALL)
            assert parsed is not None
            expression, suffix = parsed.group(1), parsed.group(2) or ""
            # Preserve PostgreSQL's complete cast expression. In older schemas
            # one array element can be ('zulip'::varchar)::text; inserting a
            # comma inside that cast would turn it into a row expression.
            telegram_expression = expression.replace("'zulip'", "'telegram'")
            definition = f"CHECK (({expression}) OR ({telegram_expression})){suffix}"
            session.execute(
                f'ALTER TABLE "{constraint["table_name"]}" DROP CONSTRAINT "{constraint["conname"]}"'
            )
            session.execute(
                f'ALTER TABLE "{constraint["table_name"]}" ADD CONSTRAINT "{constraint["conname"]}" {definition}'
            )
        session.execute(SCHEMA)
        session.execute(CAPTURE)

    def downgrade(self, session):
        raise RuntimeError(
            "Remove Telegram accounts and restore a matching database backup to downgrade"
        )


migration_step = MigrationStep()
