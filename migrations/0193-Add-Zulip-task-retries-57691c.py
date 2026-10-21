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
        self._depends = ["0192-Add-Zulip-history-tasks-d80af5.py"]

    @property
    def migration_id(self):
        return "57691ceb-2b88-4df8-ab71-2769135c7f8b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_tasks
                ADD COLUMN next_retry_at TIMESTAMP WITHOUT TIME ZONE,
                DROP CONSTRAINT bridge_zulip_v5_tasks_status_check,
                ADD CONSTRAINT bridge_zulip_v5_tasks_status_check
                    CHECK (status IN ('new', 'retry', 'error', 'done'));
            DROP INDEX bridge_zulip_v5_tasks_ready_idx;
            CREATE INDEX bridge_zulip_v5_tasks_ready_idx
                ON bridge_zulip_v5_tasks (priority DESC, created_at, uuid)
                WHERE status IN ('new', 'retry');
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            UPDATE bridge_zulip_v5_tasks SET status = 'error' WHERE status = 'retry';
            ALTER TABLE bridge_zulip_v5_tasks
                DROP COLUMN next_retry_at,
                DROP CONSTRAINT bridge_zulip_v5_tasks_status_check,
                ADD CONSTRAINT bridge_zulip_v5_tasks_status_check
                    CHECK (status IN ('new', 'error', 'done'));
            DROP INDEX bridge_zulip_v5_tasks_ready_idx;
            CREATE INDEX bridge_zulip_v5_tasks_ready_idx
                ON bridge_zulip_v5_tasks (priority DESC, created_at, uuid)
                WHERE status = 'new';
            """
        )


migration_step = MigrationStep()
