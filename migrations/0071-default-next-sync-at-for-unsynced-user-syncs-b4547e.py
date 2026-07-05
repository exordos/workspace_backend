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
        self._depends = ["0070-set-null-external-account-user-sync-account-c12b06.py"]

    @property
    def migration_id(self):
        return "b4547ebf-4ca7-4ec1-804c-03d672ec99f3"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            UPDATE "m_external_account_user_syncs"
                SET "next_sync_at" = NOW()
                WHERE "last_synced_at" IS NULL
                  AND "next_sync_at" IS NULL;
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_account_user_syncs"
                ALTER COLUMN "next_sync_at" SET DEFAULT CURRENT_TIMESTAMP;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_account_user_syncs"
                ALTER COLUMN "next_sync_at" DROP DEFAULT;
            """
        )


migration_step = MigrationStep()
