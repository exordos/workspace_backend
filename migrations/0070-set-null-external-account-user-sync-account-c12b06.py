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
        self._depends = ["0069-allow-null-external-account-user-sync-account-45f892.py"]

    @property
    def migration_id(self):
        return "c12b0621-1aae-403f-b886-6b2a4d15202b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_account_user_syncs"
                DROP CONSTRAINT IF EXISTS
                    "m_external_account_user_syncs_account_fkey",
                ADD CONSTRAINT "m_external_account_user_syncs_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON DELETE SET NULL;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_account_user_syncs"
                DROP CONSTRAINT IF EXISTS
                    "m_external_account_user_syncs_account_fkey",
                ADD CONSTRAINT "m_external_account_user_syncs_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON DELETE CASCADE;
            """
        )


migration_step = MigrationStep()
