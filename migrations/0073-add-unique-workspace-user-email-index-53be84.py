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
        self._depends = ["0072-nest-external-account-settings-8f211a.py"]

    @property
    def migration_id(self):
        return "53be8489-2b33-474f-9194-d78300fd436e"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_workspace_users_email_unique_idx"
                ON "m_workspace_users" ("email")
                WHERE "email" IS NOT NULL;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS "m_workspace_users_email_unique_idx";
            """
        )


migration_step = MigrationStep()
