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
        self._depends = ["0066-update-external-accounts-status-67b8d3.py"]

    @property
    def migration_id(self):
        return "5e4f5631-44af-4745-b679-a838f9ed7304"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_external_account_user_syncs" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "is_synced" BOOLEAN NOT NULL DEFAULT FALSE,
                "last_synced_at" TIMESTAMP WITH TIME ZONE NULL,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_account_user_syncs_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON DELETE CASCADE
            );
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_external_account_user_syncs_account_uuid_idx"
                ON "m_external_account_user_syncs" ("external_account_uuid");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_external_account_user_syncs_project_id_idx"
                ON "m_external_account_user_syncs" ("project_id");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_external_account_user_syncs_is_synced_idx"
                ON "m_external_account_user_syncs" ("is_synced");
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP TABLE IF EXISTS "m_external_account_user_syncs";
            """
        )


migration_step = MigrationStep()
