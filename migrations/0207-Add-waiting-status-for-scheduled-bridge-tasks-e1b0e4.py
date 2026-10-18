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
            "0206-Add-provider-account-identity-to-external-accounts-812e0e.py"
        ]

    @property
    def migration_id(self):
        return "e1b0e4fc-432c-4185-8999-1e46b3db6009"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_tasks
                DROP CONSTRAINT bridge_zulip_v5_tasks_status_check,
                ADD CONSTRAINT bridge_zulip_v5_tasks_status_check
                    CHECK (status IN ('new', 'waiting', 'retry', 'error', 'done', 'cancel'));
            UPDATE bridge_zulip_v5_tasks SET status = 'waiting'
                WHERE status = 'new' AND not_before_at > CURRENT_TIMESTAMP AT TIME ZONE 'UTC';
            DROP INDEX bridge_zulip_v5_tasks_ready_idx;
            CREATE INDEX bridge_zulip_v5_tasks_ready_idx
                ON bridge_zulip_v5_tasks (priority DESC, created_at, uuid)
                WHERE status IN ('new', 'waiting', 'retry');
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            UPDATE bridge_zulip_v5_tasks SET status = 'new' WHERE status = 'waiting';
            ALTER TABLE bridge_zulip_v5_tasks
                DROP CONSTRAINT bridge_zulip_v5_tasks_status_check,
                ADD CONSTRAINT bridge_zulip_v5_tasks_status_check
                    CHECK (status IN ('new', 'retry', 'error', 'done', 'cancel'));
            DROP INDEX bridge_zulip_v5_tasks_ready_idx;
            CREATE INDEX bridge_zulip_v5_tasks_ready_idx
                ON bridge_zulip_v5_tasks (priority DESC, created_at, uuid)
                WHERE status IN ('new', 'retry');
            """
        )


migration_step = MigrationStep()
