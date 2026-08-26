# Copyright 2026 Genesis Corporation.
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


READ_STATE_SCHEMA_LOCK_KEY = "workspace-read-state-schema-v1"


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0145-index-read-state-maintenance-scheduler-8e2468.py",
        ]

    @property
    def migration_id(self):
        return "4c8dc326-40db-4045-addb-bb8ac4d472c5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        # Old backends do not know the advisory fence. Block their stream
        # inserts while the one-time gap is repaired and the trigger is added.
        session.execute(
            "LOCK TABLE m_workspace_streams IN SHARE ROW EXCLUSIVE MODE"
        )
        session.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (
                project_id, mode, created_at, updated_at
            )
            SELECT DISTINCT stream.project_id, 'legacy', NOW(), NOW()
            FROM m_workspace_streams AS stream
            ON CONFLICT (project_id) DO NOTHING;

            CREATE OR REPLACE FUNCTION
                m_workspace_register_read_state_project_v1()
            RETURNS TRIGGER AS $$
            BEGIN
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode, created_at, updated_at
                ) VALUES (NEW.project_id, 'legacy', NOW(), NOW())
                ON CONFLICT (project_id) DO NOTHING;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS
                m_workspace_register_read_state_project_v1
                ON m_workspace_streams;
            CREATE TRIGGER m_workspace_register_read_state_project_v1
            BEFORE INSERT OR UPDATE OF project_id ON m_workspace_streams
            FOR EACH ROW
            EXECUTE FUNCTION m_workspace_register_read_state_project_v1();
            """
        )

    def downgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute(
            "LOCK TABLE m_workspace_streams IN SHARE ROW EXCLUSIVE MODE"
        )
        session.execute(
            """
            DROP TRIGGER IF EXISTS
                m_workspace_register_read_state_project_v1
                ON m_workspace_streams;
            DROP FUNCTION IF EXISTS
                m_workspace_register_read_state_project_v1();
            """
        )


migration_step = MigrationStep()
