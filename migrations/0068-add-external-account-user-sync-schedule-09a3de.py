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
        self._depends = ["0067-add-external-account-user-syncs-5e4f56.py"]

    @property
    def migration_id(self):
        return "09a3de57-f68a-4dbd-92a0-59715dcea035"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_account_user_syncs"
                ADD COLUMN IF NOT EXISTS
                    "account_type" VARCHAR(32) DEFAULT 'zulip',
                ADD COLUMN IF NOT EXISTS
                    "server_url" VARCHAR(2048),
                ADD COLUMN IF NOT EXISTS
                    "next_sync_at" TIMESTAMP WITH TIME ZONE NULL;
            """
        )
        session.execute(
            """
            UPDATE "m_external_account_user_syncs" AS sync
                SET
                    "account_type" = COALESCE(
                        sync."account_type",
                        account."account_type"
                    ),
                    "server_url" = COALESCE(
                        sync."server_url",
                        account."account_settings"->>'server_url'
                    )
                FROM "m_external_accounts" AS account
                WHERE account."uuid" = sync."external_account_uuid";
            """
        )
        session.execute(
            """
            UPDATE "m_external_account_user_syncs"
                SET "account_type" = 'zulip'
                WHERE "account_type" IS NULL;
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_account_user_syncs"
                ALTER COLUMN "account_type" SET DEFAULT 'zulip',
                ALTER COLUMN "account_type" SET NOT NULL,
                ALTER COLUMN "server_url" SET NOT NULL,
                DROP CONSTRAINT IF EXISTS
                    "m_external_account_user_syncs_account_type_check",
                ADD CONSTRAINT "m_external_account_user_syncs_account_type_check"
                    CHECK ("account_type" IN ('zulip'));
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_external_account_user_syncs_server_url_idx"
                ON "m_external_account_user_syncs" ("server_url");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_external_account_user_syncs_account_type_idx"
                ON "m_external_account_user_syncs" ("account_type");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_external_account_user_syncs_next_sync_at_idx"
                ON "m_external_account_user_syncs" ("next_sync_at");
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_account_user_syncs_next_sync_at_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_account_user_syncs_account_type_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_account_user_syncs_server_url_idx";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_account_user_syncs"
                DROP CONSTRAINT IF EXISTS
                    "m_external_account_user_syncs_account_type_check",
                DROP COLUMN IF EXISTS "next_sync_at",
                DROP COLUMN IF EXISTS "server_url",
                DROP COLUMN IF EXISTS "account_type";
            """
        )


migration_step = MigrationStep()
