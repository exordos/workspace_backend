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
        self._depends = ["0174-suppress-legacy-backfill-counters-a2cd99.py"]

    @property
    def migration_id(self):
        return "26c3b7d6-cfea-427b-9c08-5f41eacb2dea"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TABLE m_external_history_scopes_v1 (
                bridge_uuid uuid NOT NULL,
                project_uuid uuid NOT NULL,
                provider_realm_uuid uuid NOT NULL,
                generation bigint NOT NULL CHECK (generation > 0),
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (bridge_uuid, project_uuid, provider_realm_uuid)
            );
            CREATE TABLE m_external_history_imports_v1 (
                uuid uuid PRIMARY KEY,
                bridge_uuid uuid NOT NULL,
                project_uuid uuid NOT NULL,
                provider_realm_uuid uuid NOT NULL,
                generation bigint NOT NULL CHECK (generation > 0),
                identity_generation bigint NOT NULL,
                from_id bigint NOT NULL,
                to_id bigint NOT NULL,
                body_hash char(64) NOT NULL,
                storage jsonb NOT NULL,
                sources jsonb NOT NULL,
                status varchar(24) NOT NULL DEFAULT 'pending',
                file_cursor integer NOT NULL DEFAULT 0,
                files_discovered boolean NOT NULL DEFAULT false,
                directory_cursor integer NOT NULL DEFAULT 0,
                next_message_id bigint NOT NULL DEFAULT 0,
                notification_cursor integer NOT NULL DEFAULT 0,
                total_messages integer NOT NULL,
                applied_messages integer NOT NULL DEFAULT 0,
                inserted_messages integer NOT NULL DEFAULT 0,
                skipped_messages integer NOT NULL DEFAULT 0,
                part_size integer NOT NULL DEFAULT 100,
                worker_uuid uuid,
                lease_token bigint NOT NULL DEFAULT 0,
                lease_until timestamptz,
                available_at timestamptz NOT NULL DEFAULT now(),
                attempts integer NOT NULL DEFAULT 0,
                safe_error varchar(128),
                write_seconds double precision NOT NULL DEFAULT 0,
                max_write_seconds double precision NOT NULL DEFAULT 0,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                completed_at timestamptz,
                CHECK (status IN (
                    'pending', 'running', 'waiting_files', 'complete',
                    'failed', 'superseded'
                )),
                CHECK ((from_id - 1) % 5000 = 0 AND to_id = from_id + 4999),
                CHECK (part_size BETWEEN 1 AND 100),
                FOREIGN KEY (bridge_uuid, project_uuid, provider_realm_uuid)
                    REFERENCES m_external_history_scopes_v1 (
                        bridge_uuid, project_uuid, provider_realm_uuid
                    ) ON DELETE CASCADE
            );
            CREATE INDEX m_external_history_imports_claim_idx
                ON m_external_history_imports_v1 (available_at, created_at, uuid)
                WHERE status IN ('pending', 'running', 'waiting_files');
            CREATE INDEX m_external_history_imports_scope_idx
                ON m_external_history_imports_v1 (
                    bridge_uuid, project_uuid, provider_realm_uuid, generation
                ) WHERE status NOT IN ('complete', 'superseded');
            CREATE INDEX m_external_history_imports_capacity_idx
                ON m_external_history_imports_v1 (bridge_uuid)
                WHERE status IN ('pending', 'running', 'waiting_files');
            CREATE TABLE m_external_history_files_v1 (
                job_uuid uuid NOT NULL REFERENCES m_external_history_imports_v1(uuid)
                    ON DELETE CASCADE,
                uuid uuid NOT NULL,
                source_path varchar(4096) NOT NULL,
                account_uuids jsonb NOT NULL,
                status varchar(16) NOT NULL DEFAULT 'missing',
                name varchar(256),
                content_type varchar(256),
                size_bytes bigint,
                sha256 char(64),
                storage jsonb,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (job_uuid, uuid),
                CHECK (status IN ('missing', 'ready', 'unavailable'))
            );
            CREATE INDEX m_external_history_files_pending_idx
                ON m_external_history_files_v1 (job_uuid, uuid)
                WHERE status = 'missing';
            CREATE TABLE m_external_history_message_hashes_v1 (
                provider_realm_uuid uuid NOT NULL,
                provider_message_id varchar(32) NOT NULL,
                message_uuid uuid NOT NULL REFERENCES messenger_messages(uuid)
                    ON DELETE CASCADE,
                source_hash char(64) NOT NULL,
                job_uuid uuid NOT NULL REFERENCES m_external_history_imports_v1(uuid),
                PRIMARY KEY (provider_realm_uuid, provider_message_id)
            );
            CREATE TABLE m_external_history_notifications_v1 (
                job_uuid uuid NOT NULL REFERENCES m_external_history_imports_v1(uuid)
                    ON DELETE CASCADE,
                uuid uuid NOT NULL,
                user_uuid uuid NOT NULL,
                stream_uuid uuid NOT NULL,
                topic_uuid uuid,
                PRIMARY KEY (job_uuid, uuid)
            );
            CREATE TABLE m_external_history_tombstones_v1 (
                provider_realm_uuid uuid NOT NULL,
                provider_message_id varchar(32) NOT NULL,
                deleted_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (provider_realm_uuid, provider_message_id)
            );
            CREATE FUNCTION workspace_history_remember_message_delete_v1()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    -- Also retain deletions made before this migration when the
                    -- normal retention worker eventually purges those rows.
                    IF OLD.deleted_at IS NOT NULL AND OLD.provider_realm_uuid IS NOT NULL THEN
                        INSERT INTO m_external_history_tombstones_v1
                            (provider_realm_uuid, provider_message_id, deleted_at)
                        VALUES (OLD.provider_realm_uuid, OLD.provider_message_id, OLD.deleted_at)
                        ON CONFLICT DO NOTHING;
                    END IF;
                    RETURN OLD;
                END IF;
                IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL
                   AND NEW.provider_realm_uuid IS NOT NULL THEN
                    INSERT INTO m_external_history_tombstones_v1
                        (provider_realm_uuid, provider_message_id, deleted_at)
                    VALUES (NEW.provider_realm_uuid, NEW.provider_message_id, NEW.deleted_at)
                    ON CONFLICT DO NOTHING;
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER workspace_history_remember_message_delete_v1
                AFTER UPDATE OF deleted_at ON messenger_messages FOR EACH ROW
                EXECUTE FUNCTION workspace_history_remember_message_delete_v1();
            CREATE TRIGGER workspace_history_remember_message_purge_v1
                AFTER DELETE ON messenger_messages FOR EACH ROW
                EXECUTE FUNCTION workspace_history_remember_message_delete_v1();
        """)

    def downgrade(self, session):
        session.execute("""
            DROP TRIGGER workspace_history_remember_message_purge_v1 ON messenger_messages;
            DROP TRIGGER workspace_history_remember_message_delete_v1 ON messenger_messages;
            DROP FUNCTION workspace_history_remember_message_delete_v1();
            DROP TABLE m_external_history_tombstones_v1;
            DROP TABLE m_external_history_notifications_v1;
            DROP TABLE m_external_history_message_hashes_v1;
            DROP TABLE m_external_history_files_v1;
            DROP TABLE m_external_history_imports_v1;
            DROP TABLE m_external_history_scopes_v1;
        """)


migration_step = MigrationStep()
