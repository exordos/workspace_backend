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
        self._depends = ["0091-add-integration-bridge-event-cursors-c0679b.py"]

    @property
    def migration_id(self):
        return "6e0ebecc-28bd-4811-870b-0d3ca8441496"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_accounts_project_user_account_type_idx";
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_external_accounts_project_user_account_type_server_idx"
                ON "m_external_accounts"
                    ("project_id", "user_uuid", "account_type", "server_url");
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_accounts_project_user_account_type_server_idx";
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_external_accounts_project_user_account_type_idx"
                ON "m_external_accounts"
                    ("project_id", "user_uuid", "account_type");
            """
        )


migration_step = MigrationStep()
