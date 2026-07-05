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
        self._depends = ["0064-unify-workspace-event-contract-587ffd.py"]

    @property
    def migration_id(self):
        return "1f7d6e04-a07c-4c1a-8ccf-360165f27d2b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "external_accounts" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "external_user_id" VARCHAR(128) NOT NULL,
                "account_type" VARCHAR(32) NOT NULL,
                "account_settings" JSONB NOT NULL,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "external_accounts_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "external_accounts_account_type_check"
                    CHECK ("account_type" IN ('zulip'))
            );
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "external_accounts_project_user_account_type_idx"
                ON "external_accounts"
                    ("project_id", "user_uuid", "account_type");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "external_accounts_project_id_idx"
                ON "external_accounts" ("project_id");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "external_accounts_user_uuid_idx"
                ON "external_accounts" ("user_uuid");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "external_accounts_account_type_idx"
                ON "external_accounts" ("account_type");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "external_accounts_project_type_external_user_id_idx"
                ON "external_accounts"
                    ("project_id", "account_type", "external_user_id");
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP TABLE IF EXISTS "external_accounts";
            """
        )


migration_step = MigrationStep()
