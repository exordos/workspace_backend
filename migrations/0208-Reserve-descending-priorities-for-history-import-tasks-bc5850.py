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
        self._depends = ["0207-Add-waiting-status-for-scheduled-bridge-tasks-e1b0e4.py"]

    @property
    def migration_id(self):
        return "bc585096-3f8c-436b-8c55-d76e61062eac"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            WITH history_priorities AS (
                SELECT uuid, GREATEST(10, 10000 - (
                    MAX((content->>'first_message_id')::BIGINT) OVER (
                        PARTITION BY project_id, external_account_uuid
                    ) - (content->>'first_message_id')::BIGINT
                ) / 5000) AS priority
                FROM bridge_zulip_v5_tasks
                WHERE content->>'kind' = 'import_history'
            )
            UPDATE bridge_zulip_v5_tasks AS task
                SET priority = history.priority
                FROM history_priorities AS history
                WHERE task.uuid = history.uuid
                    AND task.status IN ('new', 'waiting', 'retry');
            UPDATE bridge_zulip_v5_tasks
                SET priority = CASE content->>'kind'
                    WHEN 'initialize_connection' THEN 300000
                    WHEN 'import_users' THEN 200000
                    WHEN 'full_backfill' THEN 100000
                    WHEN 'import_channels' THEN 50000
                END
                WHERE status IN ('new', 'waiting', 'retry')
                    AND content->>'kind' IN (
                        'initialize_connection', 'import_users',
                        'full_backfill', 'import_channels'
                    );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            UPDATE bridge_zulip_v5_tasks
                SET priority = CASE content->>'kind'
                    WHEN 'initialize_connection' THEN 300
                    WHEN 'import_users' THEN 200
                    WHEN 'full_backfill' THEN 100
                    WHEN 'import_channels' THEN 50
                    WHEN 'import_history' THEN 20
                END
                WHERE status IN ('new', 'waiting', 'retry')
                    AND content->>'kind' IN (
                        'initialize_connection', 'import_users',
                        'full_backfill', 'import_channels', 'import_history'
                    );
            """
        )


migration_step = MigrationStep()
