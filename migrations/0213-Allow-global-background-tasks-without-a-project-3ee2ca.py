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
        self._depends = ["0212-Allow-tasks-without-an-external-account-0d0ddd.py"]

    @property
    def migration_id(self):
        return "3ee2ca51-b2be-4f69-99e9-463aefb8dd3d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_tasks
                ALTER COLUMN project_id DROP NOT NULL,
                ADD CONSTRAINT bridge_zulip_v5_tasks_account_requires_project_check
                    CHECK (external_account_uuid IS NULL OR project_id IS NOT NULL);
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_tasks
                DROP CONSTRAINT bridge_zulip_v5_tasks_account_requires_project_check,
                ALTER COLUMN project_id SET NOT NULL;
            """
        )


migration_step = MigrationStep()
