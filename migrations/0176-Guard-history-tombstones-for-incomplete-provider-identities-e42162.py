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
        self._depends = ["0175-Add-resumable-history-batch-imports-26c3b7.py"]

    @property
    def migration_id(self):
        return "e4216272-466f-494f-a083-e5748f5ba5d0"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE OR REPLACE FUNCTION workspace_history_remember_message_delete_v1()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    -- Also retain deletions made before this migration when the
                    -- normal retention worker eventually purges those rows.
                    IF OLD.deleted_at IS NOT NULL AND OLD.provider_realm_uuid IS NOT NULL
                       AND OLD.provider_message_id IS NOT NULL THEN
                        INSERT INTO m_external_history_tombstones_v1
                            (provider_realm_uuid, provider_message_id, deleted_at)
                        VALUES (OLD.provider_realm_uuid, OLD.provider_message_id, OLD.deleted_at)
                        ON CONFLICT DO NOTHING;
                    END IF;
                    RETURN OLD;
                END IF;
                IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL
                   AND NEW.provider_realm_uuid IS NOT NULL
                   AND NEW.provider_message_id IS NOT NULL THEN
                    INSERT INTO m_external_history_tombstones_v1
                        (provider_realm_uuid, provider_message_id, deleted_at)
                    VALUES (NEW.provider_realm_uuid, NEW.provider_message_id, NEW.deleted_at)
                    ON CONFLICT DO NOTHING;
                END IF;
                RETURN NEW;
            END;
            $$;
        """)

    def downgrade(self, session):
        session.execute("""
            CREATE OR REPLACE FUNCTION workspace_history_remember_message_delete_v1()
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
        """)


migration_step = MigrationStep()
