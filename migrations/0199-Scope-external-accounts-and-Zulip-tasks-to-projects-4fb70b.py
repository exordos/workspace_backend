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
        self._depends = ["0198-Add-task-cancellation-status-452cb8.py"]

    @property
    def migration_id(self):
        return "4fb70bb4-b710-47f7-8295-ce92d7359d55"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_zulip_v5_legacy_state (
                kind text NOT NULL,
                identity_key text NOT NULL,
                data jsonb NOT NULL,
                PRIMARY KEY (kind, identity_key)
            );
            ALTER TABLE m_external_accounts_v2 ADD COLUMN project_id UUID;
            UPDATE m_external_accounts_v2 AS account
            SET project_id = COALESCE(
                (
                    SELECT NULLIF(data #>> '{settings,default_project_id}', '')::uuid
                    FROM workspace_zulip_v5_legacy_state
                    WHERE kind='account' AND identity_key=account.uuid::text
                ),
                (
                    SELECT (array_agg(DISTINCT (data->>'project_id')::uuid))[1]
                    FROM workspace_zulip_v5_legacy_state
                    WHERE kind='chat' AND data->>'external_account_uuid'=account.uuid::text
                        AND data->>'project_id' IS NOT NULL
                    HAVING count(DISTINCT data->>'project_id')=1
                ),
                (
                    SELECT event.project_id FROM m_workspace_events AS event
                    WHERE event.user_uuid = account.owner_user_uuid
                        AND event.object_type = 'external_account'
                        AND event.action = 'created'
                        AND event.payload->>'uuid' = account.uuid::text
                    ORDER BY event.epoch_version
                    LIMIT 1
                ),
                (
                    SELECT (array_agg(cursor.project_id))[1]
                    FROM m_workspace_event_cursors AS cursor
                    WHERE cursor.user_uuid = account.owner_user_uuid
                    HAVING COUNT(*) = 1
                )
            );
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM m_external_accounts_v2 WHERE project_id IS NULL
                ) THEN
                    RAISE EXCEPTION 'Cannot infer project_id for existing external accounts';
                END IF;
            END;
            $$;
            ALTER TABLE m_external_accounts_v2
                ALTER COLUMN project_id SET NOT NULL,
                DROP CONSTRAINT m_external_accounts_v2_owner_provider_key,
                ADD CONSTRAINT m_external_accounts_v2_project_owner_provider_key
                    UNIQUE (project_id, owner_user_uuid, provider),
                ADD CONSTRAINT m_external_accounts_v2_uuid_project_key
                    UNIQUE (uuid, project_id);
            ALTER TABLE bridge_zulip_v5_tasks ADD COLUMN project_id UUID;
            UPDATE bridge_zulip_v5_tasks AS task
            SET project_id = account.project_id
            FROM m_external_accounts_v2 AS account
            WHERE task.external_account_uuid = account.uuid;
            ALTER TABLE bridge_zulip_v5_tasks
                ALTER COLUMN project_id SET NOT NULL,
                DROP CONSTRAINT bridge_zulip_v5_tasks_account_uuid_fkey,
                ADD CONSTRAINT bridge_zulip_v5_tasks_account_project_fkey
                    FOREIGN KEY (external_account_uuid, project_id)
                    REFERENCES m_external_accounts_v2 (uuid, project_id)
                    ON DELETE CASCADE;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_tasks
                DROP CONSTRAINT bridge_zulip_v5_tasks_account_project_fkey,
                DROP COLUMN project_id,
                ADD CONSTRAINT bridge_zulip_v5_tasks_account_uuid_fkey
                    FOREIGN KEY (external_account_uuid)
                    REFERENCES m_external_accounts_v2 (uuid) ON DELETE CASCADE;
            ALTER TABLE m_external_accounts_v2
                DROP CONSTRAINT m_external_accounts_v2_project_owner_provider_key,
                DROP CONSTRAINT m_external_accounts_v2_uuid_project_key,
                DROP COLUMN project_id,
                ADD CONSTRAINT m_external_accounts_v2_owner_provider_key
                    UNIQUE (owner_user_uuid, provider);
            """
        )


migration_step = MigrationStep()
