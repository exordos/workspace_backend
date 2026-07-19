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
        self._depends = ["0073-add-unique-workspace-user-email-index-53be84.py"]

    @property
    def migration_id(self):
        return "18e26aff-0952-4eee-861e-ba7c75982373"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                ADD COLUMN IF NOT EXISTS "server_url" VARCHAR(2048);
            """
        )
        session.execute(
            """
            UPDATE "m_external_accounts"
                SET "server_url" = COALESCE(
                    "server_url",
                    "account_settings"->'credentials'->>'server_url',
                    "account_settings"->'user_info'->>'server_url',
                    "account_settings"->>'server_url'
                )
                WHERE "server_url" IS NULL;
            """
        )
        session.execute(
            """
            UPDATE "m_external_accounts"
                SET "account_settings" = jsonb_set(
                    "account_settings",
                    '{credentials}',
                    ("account_settings"->'credentials') - 'server_url'::text
                )
                WHERE "account_settings" ? 'credentials'
                  AND jsonb_typeof("account_settings"->'credentials') =
                      'object';
            """
        )
        session.execute(
            """
            UPDATE "m_external_accounts"
                SET "account_settings" = jsonb_set(
                    "account_settings",
                    '{user_info}',
                    ("account_settings"->'user_info') - 'server_url'::text
                )
                WHERE "account_settings" ? 'user_info'
                  AND jsonb_typeof("account_settings"->'user_info') =
                      'object';
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                ALTER COLUMN "server_url" SET NOT NULL;
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_external_accounts_server_url_idx"
                ON "m_external_accounts" ("server_url");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_external_accounts_project_type_server_url_idx"
                ON "m_external_accounts"
                    ("project_id", "account_type", "server_url");
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_accounts_project_type_server_url_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS "m_external_accounts_server_url_idx";
            """
        )
        session.execute(
            """
            UPDATE "m_external_accounts"
                SET "account_settings" = jsonb_set(
                    "account_settings",
                    '{credentials}',
                    COALESCE(
                        "account_settings"->'credentials',
                        jsonb_build_object('kind', "account_type")
                    ) || jsonb_build_object('server_url', "server_url")
                )
                WHERE "account_settings" ? 'credentials';
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                DROP COLUMN IF EXISTS "server_url";
            """
        )


migration_step = MigrationStep()
