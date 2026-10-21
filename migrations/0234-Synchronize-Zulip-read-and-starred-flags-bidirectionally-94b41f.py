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


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0233-Add-outgoing-Zulip-message-tasks-and-Workspace-outbox-subscription-23c3f1.py"
        ]

    @property
    def migration_id(self):
        return "94b41fef-fb8f-43a9-9539-79b23d5e2c86"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE messenger_user_message_states
                ADD COLUMN read_updated_at TIMESTAMP WITHOUT TIME ZONE,
                ADD COLUMN starred_updated_at TIMESTAMP WITHOUT TIME ZONE;
            CREATE TABLE bridge_zulip_v5_message_flags (
                project_id UUID NOT NULL,
                external_account_uuid UUID NOT NULL,
                message_id BIGINT NOT NULL CHECK (message_id >= 0),
                flag TEXT NOT NULL CHECK (flag IN ('read', 'starred')),
                value BOOLEAN NOT NULL,
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                event_id BIGINT NOT NULL DEFAULT -1,
                PRIMARY KEY (external_account_uuid, message_id, flag),
                CHECK (message_id > 0 OR (flag = 'read' AND value)),
                FOREIGN KEY (external_account_uuid, project_id)
                    REFERENCES m_external_accounts_v2(uuid, project_id) ON DELETE CASCADE
            );
            CREATE SEQUENCE bridge_zulip_v5_flag_change_revision_seq AS BIGINT;
            CREATE TABLE bridge_zulip_v5_flag_changes (
                project_id UUID NOT NULL,
                state_uuid UUID NOT NULL,
                revision BIGINT NOT NULL DEFAULT nextval('bridge_zulip_v5_flag_change_revision_seq'),
                PRIMARY KEY (project_id, state_uuid),
                FOREIGN KEY (project_id, state_uuid)
                    REFERENCES messenger_user_message_states(project_id, uuid) ON DELETE CASCADE
            );
            CREATE INDEX bridge_zulip_v5_flag_changes_revision_idx ON bridge_zulip_v5_flag_changes(revision);
            ALTER TABLE bridge_zulip_v5_tasks ADD COLUMN flag_state_uuid UUID
                GENERATED ALWAYS AS (CASE WHEN content->>'kind'='update_message_flags'
                    THEN (content->>'state_uuid')::uuid END) STORED;
            ALTER TABLE bridge_zulip_v5_tasks ADD CONSTRAINT bridge_zulip_v5_tasks_flag_state_fkey
                FOREIGN KEY (project_id, flag_state_uuid)
                    REFERENCES messenger_user_message_states(project_id, uuid) ON DELETE CASCADE;
            CREATE INDEX bridge_zulip_v5_pending_flag_tasks_idx
                ON bridge_zulip_v5_tasks(external_account_uuid, flag_state_uuid)
                WHERE content->>'kind'='update_message_flags' AND status IN ('new', 'retry');

            CREATE VIEW bridge_zulip_v5_flag_targets AS
            SELECT state.project_id, state.uuid AS state_uuid, state.user_uuid,
                   state.placement_uuid, placement.message_uuid, placement.stream_uuid,
                   placement.topic_uuid, account.uuid AS external_account_uuid,
                   account.external_account_id,
                   COALESCE(observed.message_id, delivery.zulip_message_id) AS message_id
            FROM messenger_user_message_states AS state
            JOIN messenger_message_placements AS placement
              ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
            JOIN messenger_messages AS canonical
              ON canonical.project_id=placement.project_id AND canonical.uuid=placement.message_uuid
             AND canonical.deleted_at IS NULL
            JOIN messenger_stream_bindings AS membership
              ON membership.project_id=state.project_id AND membership.stream_uuid=placement.stream_uuid
             AND membership.user_uuid=state.user_uuid AND membership.active
             AND membership.membership_generation=state.membership_generation
            JOIN messenger_user_message_bindings AS access
              ON access.project_id=state.project_id AND access.placement_uuid=state.placement_uuid
             AND access.user_uuid=state.user_uuid AND access.visibility='visible'
             AND access.membership_generation=state.membership_generation
            JOIN m_external_accounts_v2 AS account
              ON account.project_id=state.project_id AND account.owner_user_uuid=state.user_uuid
             AND account.provider='zulip' AND NOT account.disabled
             AND account.external_account_id IS NOT NULL
            JOIN bridge_zulip_v5_channels AS channel
              ON channel.external_account_uuid=account.uuid AND channel.stream_uuid=placement.stream_uuid
            LEFT JOIN bridge_zulip_v5_messages AS origin
              ON origin.uuid=state.zulip_source_uuid AND origin.project_id=state.project_id
             AND messenger_uuid_v5(origin.project_id, origin.message_uuid::text)=placement.message_uuid
            LEFT JOIN bridge_zulip_v5_messages AS observed
              ON observed.project_id=state.project_id AND observed.external_account_uuid=account.uuid
             AND observed.message_id=origin.message_id AND observed.message_uuid=origin.message_uuid
             AND observed.message_uuid=messenger_uuid_v5(
                 messenger_uuid_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid,
                     regexp_replace(account.external_account_id, ':[^:]+$', '')),
                 observed.message_id::text)
            LEFT JOIN bridge_zulip_v5_deliveries AS delivery
              ON delivery.project_id=state.project_id AND delivery.message_uuid=placement.message_uuid
             AND delivery.stream_uuid=placement.stream_uuid
             AND delivery.hostname=regexp_replace(account.external_account_id, ':[^:]+$', '')
            WHERE COALESCE(observed.message_id, delivery.zulip_message_id) IS NOT NULL;

            CREATE VIEW bridge_zulip_v5_flag_states AS
            SELECT target.*, local.flag, local.value AS local_value,
                   local.updated_at AS local_updated_at,
                   remote.value AS remote_value, remote.updated_at AS remote_updated_at
            FROM bridge_zulip_v5_flag_targets AS target
            JOIN messenger_user_message_states AS state
              ON state.project_id=target.project_id AND state.uuid=target.state_uuid
            CROSS JOIN LATERAL (VALUES
                ('read', state.read_at IS NOT NULL, state.read_updated_at),
                ('starred', state.starred, state.starred_updated_at)
            ) AS local(flag, value, updated_at)
            LEFT JOIN LATERAL (
                SELECT observed.value, observed.updated_at
                FROM bridge_zulip_v5_message_flags AS observed
                WHERE observed.external_account_uuid=target.external_account_uuid
                  AND observed.flag=local.flag AND observed.message_id IN (0, target.message_id)
                ORDER BY observed.updated_at DESC, observed.event_id DESC LIMIT 1
            ) AS remote ON true
            WHERE local.value IS DISTINCT FROM remote.value
               OR local.updated_at IS NULL
               OR remote.updated_at > local.updated_at;

            CREATE FUNCTION messenger_stamp_user_message_flags() RETURNS trigger AS $$
            BEGIN
                IF NEW.read_at IS DISTINCT FROM OLD.read_at
                   AND NEW.read_updated_at IS NOT DISTINCT FROM OLD.read_updated_at THEN
                    NEW.read_updated_at := statement_timestamp() AT TIME ZONE 'UTC';
                END IF;
                IF NEW.starred IS DISTINCT FROM OLD.starred
                   AND NEW.starred_updated_at IS NOT DISTINCT FROM OLD.starred_updated_at THEN
                    NEW.starred_updated_at := statement_timestamp() AT TIME ZONE 'UTC';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER messenger_stamp_user_message_flags
                BEFORE UPDATE ON messenger_user_message_states
                FOR EACH ROW EXECUTE FUNCTION messenger_stamp_user_message_flags();

            CREATE FUNCTION bridge_zulip_v5_track_local_flags() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                SELECT NEW.project_id, NEW.uuid
                WHERE EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                    JOIN messenger_streams AS stream
                      ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
                    WHERE placement.project_id=NEW.project_id AND placement.uuid=NEW.placement_uuid
                      AND stream.source_name='zulip')
                ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_track_local_flags
                AFTER INSERT OR UPDATE OF read_at, starred, read_updated_at, starred_updated_at
                ON messenger_user_message_states
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_local_flags();

            CREATE FUNCTION bridge_zulip_v5_track_remote_flags() RETURNS trigger AS $$
            DECLARE
                target_message_uuids UUID[];
                account_owner UUID;
                account_hostname TEXT;
            BEGIN
                IF NEW.message_id=0 THEN
                    INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                    SELECT DISTINCT target.project_id, target.state_uuid FROM bridge_zulip_v5_flag_targets AS target
                    JOIN messenger_user_message_states AS state
                      ON state.project_id=target.project_id AND state.uuid=target.state_uuid
                    WHERE target.external_account_uuid=NEW.external_account_uuid AND state.read_at IS NULL
                    ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                ELSE
                    SELECT owner_user_uuid, regexp_replace(external_account_id, ':[^:]+$', '')
                    INTO account_owner, account_hostname FROM m_external_accounts_v2
                    WHERE uuid=NEW.external_account_uuid;
                    SELECT array_agg(message_uuid) INTO target_message_uuids FROM (
                        SELECT messenger_uuid_v5(source.project_id, source.message_uuid::text) AS message_uuid
                        FROM bridge_zulip_v5_messages AS source
                        WHERE source.external_account_uuid=NEW.external_account_uuid AND source.message_id=NEW.message_id
                        UNION
                        SELECT delivery.message_uuid FROM bridge_zulip_v5_deliveries AS delivery
                        WHERE delivery.project_id=NEW.project_id AND delivery.zulip_message_id=NEW.message_id
                          AND delivery.hostname=account_hostname
                    ) AS messages;
                    INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                    SELECT state.project_id, state.uuid
                    FROM messenger_user_message_states AS state
                    JOIN messenger_message_placements AS placement
                      ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
                    WHERE state.project_id=NEW.project_id AND state.user_uuid=account_owner
                      AND placement.message_uuid=ANY(target_message_uuids)
                    ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_track_remote_flags
                AFTER INSERT OR UPDATE ON bridge_zulip_v5_message_flags
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_remote_flags();

            CREATE FUNCTION bridge_zulip_v5_observe_message_flags() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_message_flags
                    (project_id, external_account_uuid, message_id, flag, value, updated_at)
                SELECT NEW.project_id, NEW.external_account_uuid, NEW.message_id,
                       flags.flag, flags.value, NEW.updated_at AT TIME ZONE 'UTC'
                FROM (VALUES
                    ('read', NEW.read, TG_OP='INSERT' OR NEW.read IS DISTINCT FROM OLD.read),
                    ('starred', NEW.starred, TG_OP='INSERT' OR NEW.starred IS DISTINCT FROM OLD.starred)
                ) AS flags(flag, value, changed)
                WHERE flags.changed
                ON CONFLICT (external_account_uuid, message_id, flag) DO UPDATE
                    SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at, event_id=-1
                    WHERE TG_OP='UPDATE'
                      AND bridge_zulip_v5_message_flags.value IS DISTINCT FROM EXCLUDED.value
                      AND bridge_zulip_v5_message_flags.updated_at < EXCLUDED.updated_at;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_observe_message_flags
                AFTER INSERT OR UPDATE OF read, starred ON bridge_zulip_v5_messages
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_observe_message_flags();

            CREATE FUNCTION bridge_zulip_v5_track_delivery_flags() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                SELECT DISTINCT project_id, state_uuid FROM bridge_zulip_v5_flag_targets
                WHERE project_id=NEW.project_id AND message_uuid=NEW.message_uuid
                ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_track_delivery_flags
                AFTER INSERT OR UPDATE OF zulip_message_id ON bridge_zulip_v5_deliveries
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_delivery_flags();
            CREATE TRIGGER bridge_zulip_v5_flags_ready
                AFTER INSERT OR UPDATE ON bridge_zulip_v5_flag_changes
                FOR EACH STATEMENT EXECUTE FUNCTION bridge_zulip_v5_wake_realtime('flags');
            CREATE FUNCTION bridge_zulip_v5_track_account_flags() RETURNS trigger AS $$
            BEGIN
                IF (OLD.provider, OLD.external_account_id, OLD.settings->>'server_url')
                    IS DISTINCT FROM (NEW.provider, NEW.external_account_id, NEW.settings->>'server_url') THEN
                    DELETE FROM bridge_zulip_v5_message_flags WHERE external_account_uuid=NEW.uuid;
                    RETURN NULL;
                END IF;
                INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                SELECT DISTINCT project_id, state_uuid FROM bridge_zulip_v5_flag_states
                WHERE external_account_uuid=NEW.uuid
                  AND local_updated_at IS NOT NULL
                  AND local_value IS DISTINCT FROM remote_value
                ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_track_account_flags
                AFTER UPDATE OF provider, owner_user_uuid, disabled, external_account_id, settings ON m_external_accounts_v2
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_account_flags();

            ALTER TABLE bridge_zulip_v5_message_flags DISABLE TRIGGER bridge_zulip_v5_track_remote_flags;
            ALTER TABLE messenger_user_message_states DISABLE TRIGGER bridge_zulip_v5_track_local_flags;
            INSERT INTO bridge_zulip_v5_message_flags
                (project_id, external_account_uuid, message_id, flag, value, updated_at)
            SELECT observed.project_id, observed.external_account_uuid, observed.message_id,
                   flags.flag, flags.value, observed.updated_at AT TIME ZONE 'UTC'
            FROM bridge_zulip_v5_messages AS observed
            CROSS JOIN LATERAL (VALUES ('read', observed.read), ('starred', observed.starred))
                AS flags(flag, value);
            ANALYZE bridge_zulip_v5_message_flags;
            WITH changed AS (
                UPDATE messenger_user_message_states AS state
                SET read_updated_at=CASE WHEN observed.read IS DISTINCT FROM (state.read_at IS NOT NULL)
                                        THEN state.updated_at AT TIME ZONE 'UTC' END,
                    starred_updated_at=CASE WHEN observed.starred IS DISTINCT FROM state.starred
                                           THEN state.updated_at AT TIME ZONE 'UTC' END
                FROM bridge_zulip_v5_messages AS observed
                WHERE observed.project_id=state.project_id AND observed.uuid=state.zulip_source_uuid
                  AND (observed.read IS DISTINCT FROM (state.read_at IS NOT NULL)
                       OR observed.starred IS DISTINCT FROM state.starred)
                RETURNING state.project_id, state.uuid
            )
            INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
            SELECT project_id, uuid FROM changed;
            ALTER TABLE bridge_zulip_v5_message_flags ENABLE TRIGGER bridge_zulip_v5_track_remote_flags;
            ALTER TABLE messenger_user_message_states ENABLE TRIGGER bridge_zulip_v5_track_local_flags;
        """)

    def downgrade(self, session):
        session.execute("""
            DELETE FROM bridge_zulip_v5_tasks WHERE content->>'kind' IN ('update_message_flags', 'refresh_message_flags');
            DROP TRIGGER bridge_zulip_v5_track_account_flags ON m_external_accounts_v2;
            DROP FUNCTION bridge_zulip_v5_track_account_flags();
            DROP TRIGGER bridge_zulip_v5_flags_ready ON bridge_zulip_v5_flag_changes;
            DROP TRIGGER bridge_zulip_v5_track_delivery_flags ON bridge_zulip_v5_deliveries;
            DROP FUNCTION bridge_zulip_v5_track_delivery_flags();
            DROP TRIGGER bridge_zulip_v5_observe_message_flags ON bridge_zulip_v5_messages;
            DROP FUNCTION bridge_zulip_v5_observe_message_flags();
            DROP TRIGGER bridge_zulip_v5_track_remote_flags ON bridge_zulip_v5_message_flags;
            DROP FUNCTION bridge_zulip_v5_track_remote_flags();
            DROP TRIGGER bridge_zulip_v5_track_local_flags ON messenger_user_message_states;
            DROP FUNCTION bridge_zulip_v5_track_local_flags();
            DROP TRIGGER messenger_stamp_user_message_flags ON messenger_user_message_states;
            DROP FUNCTION messenger_stamp_user_message_flags();
            DROP VIEW bridge_zulip_v5_flag_states;
            DROP VIEW bridge_zulip_v5_flag_targets;
            ALTER TABLE bridge_zulip_v5_tasks DROP COLUMN flag_state_uuid;
            DROP TABLE bridge_zulip_v5_flag_changes;
            DROP SEQUENCE bridge_zulip_v5_flag_change_revision_seq;
            DROP TABLE bridge_zulip_v5_message_flags;
            ALTER TABLE messenger_user_message_states DROP COLUMN read_updated_at, DROP COLUMN starred_updated_at;
        """)


migration_step = MigrationStep()
