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
        self._depends = ["0098-allow-global-workspace-avatar-files-a09c95.py"]

    @property
    def migration_id(self):
        return "9e185829-3670-4cd4-804a-d28d75dfded0"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_messages_project_created_uuid_idx"
                ON "m_workspace_messages" (
                    "project_id",
                    "created_at",
                    "uuid"
                );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_workspace_messages_project_created_uuid_idx";
            """
        )


migration_step = MigrationStep()
