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

from workspace.common import emoji


def _normalize_existing_reactions(session):
    names = list(emoji.CODE_BY_NAME)
    canonical = [emoji.name_for_code(emoji.CODE_BY_NAME[name], name) for name in names]
    session.execute(
        """
        WITH mapping AS (SELECT * FROM unnest(%s::text[],%s::text[]) AS names(alias,canonical)),
        ranked AS (
            SELECT fact.project_id,fact.uuid,
                   row_number() OVER (PARTITION BY fact.project_id,fact.canonical_message_uuid,fact.user_uuid,mapping.canonical
                       ORDER BY (fact.emoji_name=mapping.canonical) DESC,fact.updated_at DESC,fact.uuid) AS position
            FROM messenger_message_reaction_facts AS fact JOIN mapping ON mapping.alias=fact.emoji_name
            WHERE EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                JOIN messenger_streams AS stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
                WHERE placement.project_id=fact.project_id AND placement.message_uuid=fact.canonical_message_uuid
                  AND stream.source_name='zulip')
        ), changed AS (
            DELETE FROM messenger_message_reaction_facts AS fact USING ranked
            WHERE fact.project_id=ranked.project_id AND fact.uuid=ranked.uuid AND ranked.position>1
            RETURNING fact.*
        )
        INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
        SELECT messenger_uuid_v5(uuid,'unicode-reaction-normalization-235'),project_id,'reaction_snapshot','message',
               project_id::text || ':' || canonical_message_uuid::text,
               jsonb_build_object('source_kind','zulip.reconciled','placement_uuid',placement_uuid,'emit_reaction_event',false)
        FROM changed
    """,
        (names, canonical),
    )
    session.execute(
        """
        WITH mapping AS (SELECT * FROM unnest(%s::text[],%s::text[]) AS names(alias,canonical)),
        changed AS (
            UPDATE messenger_message_reaction_facts AS fact SET emoji_name=mapping.canonical FROM mapping
            WHERE fact.emoji_name=mapping.alias AND fact.emoji_name<>mapping.canonical
              AND EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                JOIN messenger_streams AS stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
                WHERE placement.project_id=fact.project_id AND placement.message_uuid=fact.canonical_message_uuid
                  AND stream.source_name='zulip')
            RETURNING fact.*
        )
        INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
        SELECT messenger_uuid_v5(uuid,'unicode-reaction-normalization-235'),project_id,'reaction_snapshot','message',
               project_id::text || ':' || canonical_message_uuid::text,
               jsonb_build_object('source_kind','zulip.reconciled','placement_uuid',placement_uuid,'emit_reaction_event',false)
        FROM changed
    """,
        (names, canonical),
    )


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0234-Synchronize-Zulip-read-and-starred-flags-bidirectionally-94b41f.py"
        ]

    @property
    def migration_id(self):
        return "19928b21-bee1-4843-83e6-f623f335d9ef"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Finish running history tasks before foreign-key DDL locks canonical
        # messages. Workers update messages while holding the tasks table.
        session.execute("LOCK TABLE bridge_zulip_v5_tasks IN ACCESS EXCLUSIVE MODE")
        _normalize_existing_reactions(session)
        session.execute("""
            CREATE SEQUENCE bridge_zulip_v5_reaction_revision_seq AS BIGINT;
            CREATE TABLE bridge_zulip_v5_reaction_messages (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                hostname TEXT NOT NULL,
                message_id BIGINT NOT NULL CHECK (message_id > 0),
                canonical_message_uuid UUID,
                observer_account_uuid UUID REFERENCES m_external_accounts_v2(uuid) ON DELETE SET NULL,
                snapshot_at TIMESTAMP WITHOUT TIME ZONE,
                next_retry_at TIMESTAMP WITHOUT TIME ZONE,
                UNIQUE (project_id, hostname, message_id),
                FOREIGN KEY (project_id, canonical_message_uuid)
                    REFERENCES messenger_messages(project_id, uuid) ON DELETE CASCADE
            );
            CREATE INDEX bridge_zulip_v5_reaction_messages_canonical_idx
                ON bridge_zulip_v5_reaction_messages(project_id, canonical_message_uuid);
            CREATE INDEX bridge_zulip_v5_reaction_messages_unbound_idx
                ON bridge_zulip_v5_reaction_messages(next_retry_at, uuid)
                WHERE canonical_message_uuid IS NULL;
            CREATE TABLE bridge_zulip_v5_reactions (
                uuid UUID PRIMARY KEY,
                reaction_message_uuid UUID NOT NULL
                    REFERENCES bridge_zulip_v5_reaction_messages(uuid) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                reaction_type TEXT NOT NULL,
                emoji_code TEXT NOT NULL,
                emoji_name TEXT NOT NULL,
                present BOOLEAN NOT NULL,
                observed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                queue_id TEXT,
                event_id BIGINT NOT NULL DEFAULT -1,
                fact_uuid UUID REFERENCES messenger_message_reaction_facts(uuid) ON DELETE SET NULL,
                UNIQUE (reaction_message_uuid, user_id, reaction_type, emoji_code)
            );
            CREATE INDEX bridge_zulip_v5_reactions_actor_idx
                ON bridge_zulip_v5_reactions(user_id, reaction_message_uuid);
            CREATE TABLE bridge_zulip_v5_reaction_intents (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                canonical_message_uuid UUID NOT NULL,
                user_uuid UUID NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
                emoji_name TEXT NOT NULL,
                present BOOLEAN NOT NULL,
                changed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                error_description TEXT,
                UNIQUE (project_id, canonical_message_uuid, user_uuid, emoji_name),
                FOREIGN KEY (project_id, canonical_message_uuid)
                    REFERENCES messenger_messages(project_id, uuid) ON DELETE CASCADE
            );
            CREATE TABLE bridge_zulip_v5_reaction_changes (
                project_id UUID NOT NULL,
                canonical_message_uuid UUID NOT NULL,
                revision BIGINT NOT NULL DEFAULT nextval('bridge_zulip_v5_reaction_revision_seq'),
                next_retry_at TIMESTAMP WITHOUT TIME ZONE,
                PRIMARY KEY (project_id, canonical_message_uuid),
                FOREIGN KEY (project_id, canonical_message_uuid)
                    REFERENCES messenger_messages(project_id, uuid) ON DELETE CASCADE
            );
            CREATE INDEX bridge_zulip_v5_reaction_intents_actor_idx
                ON bridge_zulip_v5_reaction_intents(project_id,user_uuid,canonical_message_uuid);
            CREATE INDEX bridge_zulip_v5_reaction_changes_revision_idx
                ON bridge_zulip_v5_reaction_changes(revision);
            ALTER TABLE bridge_zulip_v5_tasks ADD COLUMN reaction_intent_uuid UUID
                GENERATED ALWAYS AS (CASE WHEN content->>'kind'='update_reaction'
                    THEN (content->>'intent_uuid')::uuid END) STORED
                REFERENCES bridge_zulip_v5_reaction_intents(uuid) ON DELETE CASCADE;
            ALTER TABLE bridge_zulip_v5_tasks ADD COLUMN reaction_message_uuid UUID
                GENERATED ALWAYS AS (CASE WHEN content->>'kind'='update_reaction'
                    THEN (content->>'reaction_message_uuid')::uuid END) STORED
                REFERENCES bridge_zulip_v5_reaction_messages(uuid) ON DELETE CASCADE;

            CREATE FUNCTION bridge_zulip_v5_track_reaction_intents() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_reaction_changes(project_id, canonical_message_uuid)
                VALUES (NEW.project_id, NEW.canonical_message_uuid)
                ON CONFLICT (project_id, canonical_message_uuid) DO UPDATE
                    SET revision=EXCLUDED.revision, next_retry_at=NULL;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_track_reaction_intents
                AFTER INSERT OR UPDATE ON bridge_zulip_v5_reaction_intents
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_reaction_intents();

            CREATE FUNCTION bridge_zulip_v5_track_local_reactions() RETURNS trigger AS $$
            DECLARE
                fact messenger_message_reaction_facts;
                operation TEXT;
            BEGIN
                IF current_setting('workspace.zulip_reaction_import', true)='on' THEN
                    RETURN NULL;
                END IF;
                IF TG_OP='UPDATE' AND (OLD.canonical_message_uuid, OLD.user_uuid, OLD.emoji_name)
                    IS NOT DISTINCT FROM (NEW.canonical_message_uuid, NEW.user_uuid, NEW.emoji_name) THEN
                    RETURN NULL;
                END IF;
                FOREACH operation IN ARRAY (CASE WHEN TG_OP='UPDATE' THEN ARRAY['DELETE','INSERT']
                    ELSE ARRAY[TG_OP] END) LOOP
                    IF operation='DELETE' THEN fact := OLD; ELSE fact := NEW; END IF;
                    IF EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                        JOIN messenger_streams AS stream
                          ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
                        JOIN messenger_messages AS canonical
                          ON canonical.project_id=placement.project_id AND canonical.uuid=placement.message_uuid
                        WHERE placement.project_id=fact.project_id
                          AND placement.message_uuid=fact.canonical_message_uuid AND stream.source_name='zulip'
                          AND canonical.deleted_at IS NULL) THEN
                        INSERT INTO bridge_zulip_v5_reaction_intents
                            (uuid, project_id, canonical_message_uuid, user_uuid, emoji_name, present, changed_at)
                        VALUES (messenger_uuid_v5(fact.canonical_message_uuid,
                            fact.user_uuid::text || ':reaction:' || fact.emoji_name), fact.project_id,
                            fact.canonical_message_uuid, fact.user_uuid, fact.emoji_name, operation<>'DELETE',
                            statement_timestamp() AT TIME ZONE 'UTC')
                        ON CONFLICT (project_id, canonical_message_uuid, user_uuid, emoji_name) DO UPDATE
                            SET present=EXCLUDED.present, changed_at=EXCLUDED.changed_at, error_description=NULL;
                    END IF;
                END LOOP;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_track_local_reactions
                AFTER INSERT OR UPDATE OR DELETE ON messenger_message_reaction_facts
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_local_reactions();

            CREATE FUNCTION bridge_zulip_v5_track_remote_reactions() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_reaction_changes(project_id, canonical_message_uuid)
                SELECT project_id, canonical_message_uuid FROM bridge_zulip_v5_reaction_messages
                WHERE uuid=NEW.reaction_message_uuid AND canonical_message_uuid IS NOT NULL
                ON CONFLICT (project_id, canonical_message_uuid) DO UPDATE
                    SET revision=EXCLUDED.revision, next_retry_at=NULL;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_track_remote_reactions
                AFTER INSERT OR UPDATE OF present, observed_at ON bridge_zulip_v5_reactions
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_remote_reactions();
            CREATE TRIGGER bridge_zulip_v5_reactions_ready
                AFTER INSERT OR UPDATE ON bridge_zulip_v5_reaction_changes
                FOR EACH STATEMENT EXECUTE FUNCTION bridge_zulip_v5_wake_realtime('reactions');
            CREATE TRIGGER bridge_zulip_v5_unbound_reactions_ready
                AFTER INSERT ON bridge_zulip_v5_reaction_messages
                FOR EACH STATEMENT EXECUTE FUNCTION bridge_zulip_v5_wake_realtime('reactions');

            CREATE FUNCTION bridge_zulip_v5_requeue_reactions() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_reaction_changes(project_id, canonical_message_uuid)
                SELECT project_id, canonical_message_uuid FROM bridge_zulip_v5_reaction_intents
                WHERE project_id=NEW.project_id AND user_uuid IN (OLD.owner_user_uuid, NEW.owner_user_uuid)
                UNION
                SELECT message.project_id, message.canonical_message_uuid
                FROM bridge_zulip_v5_reactions AS reaction
                JOIN bridge_zulip_v5_reaction_messages AS message ON message.uuid=reaction.reaction_message_uuid
                WHERE message.project_id=NEW.project_id AND message.canonical_message_uuid IS NOT NULL
                  AND OLD.external_account_id=message.hostname || ':' || reaction.user_id::text
                  AND OLD.owner_user_uuid IS DISTINCT FROM NEW.owner_user_uuid
                ON CONFLICT (project_id, canonical_message_uuid) DO UPDATE
                    SET revision=EXCLUDED.revision, next_retry_at=NULL;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_requeue_reactions
                AFTER UPDATE OF provider, owner_user_uuid, disabled, external_account_id, settings ON m_external_accounts_v2
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_requeue_reactions();

            INSERT INTO bridge_zulip_v5_reaction_intents
                (uuid, project_id, canonical_message_uuid, user_uuid, emoji_name, present, changed_at)
            SELECT messenger_uuid_v5(fact.canonical_message_uuid,
                       fact.user_uuid::text || ':reaction:' || fact.emoji_name),
                   fact.project_id, fact.canonical_message_uuid, fact.user_uuid, fact.emoji_name,
                   true, fact.updated_at AT TIME ZONE 'UTC'
            FROM messenger_message_reaction_facts AS fact
            WHERE EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                JOIN messenger_streams AS stream
                  ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
                WHERE placement.project_id=fact.project_id AND placement.message_uuid=fact.canonical_message_uuid
                  AND stream.source_name='zulip');
        """)

    def downgrade(self, session):
        session.execute("""
            DELETE FROM bridge_zulip_v5_tasks WHERE content->>'kind'='update_reaction';
            DROP TRIGGER bridge_zulip_v5_requeue_reactions ON m_external_accounts_v2;
            DROP FUNCTION bridge_zulip_v5_requeue_reactions();
            DROP TRIGGER bridge_zulip_v5_unbound_reactions_ready ON bridge_zulip_v5_reaction_messages;
            DROP TRIGGER bridge_zulip_v5_reactions_ready ON bridge_zulip_v5_reaction_changes;
            DROP TRIGGER bridge_zulip_v5_track_remote_reactions ON bridge_zulip_v5_reactions;
            DROP FUNCTION bridge_zulip_v5_track_remote_reactions();
            DROP TRIGGER bridge_zulip_v5_track_local_reactions ON messenger_message_reaction_facts;
            DROP FUNCTION bridge_zulip_v5_track_local_reactions();
            DROP TRIGGER bridge_zulip_v5_track_reaction_intents ON bridge_zulip_v5_reaction_intents;
            DROP FUNCTION bridge_zulip_v5_track_reaction_intents();
            ALTER TABLE bridge_zulip_v5_tasks DROP COLUMN reaction_intent_uuid, DROP COLUMN reaction_message_uuid;
            DROP TABLE bridge_zulip_v5_reaction_changes;
            DROP TABLE bridge_zulip_v5_reaction_intents;
            DROP TABLE bridge_zulip_v5_reactions;
            DROP TABLE bridge_zulip_v5_reaction_messages;
            DROP SEQUENCE bridge_zulip_v5_reaction_revision_seq;
        """)


migration_step = MigrationStep()
