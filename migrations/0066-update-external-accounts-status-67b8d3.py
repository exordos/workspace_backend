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
        self._depends = ["0065-add-external-accounts-1f7d6e.py"]

    @property
    def migration_id(self):
        return "67b8d33d-7cf5-4907-a88a-96dd30099914"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            DO $$
            BEGIN
                IF to_regclass('public.m_external_accounts') IS NULL
                    AND to_regclass('public.external_accounts') IS NOT NULL
                THEN
                    ALTER TABLE "external_accounts"
                        RENAME TO "m_external_accounts";
                END IF;
            END $$;
            """
        )
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_external_accounts" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "account_type" VARCHAR(32) NOT NULL,
                "status" VARCHAR(16) NOT NULL DEFAULT 'new',
                "account_settings" JSONB NOT NULL,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_accounts_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_accounts_account_type_check"
                    CHECK ("account_type" IN ('zulip')),
                CONSTRAINT "m_external_accounts_status_check"
                    CHECK ("status" IN ('new', 'active'))
            );
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_accounts_project_type_external_user_id_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS
                "external_accounts_project_type_external_user_id_idx";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                DROP COLUMN IF EXISTS "external_user_id",
                ADD COLUMN IF NOT EXISTS "status" VARCHAR(16) DEFAULT 'new';
            """
        )
        session.execute(
            """
            UPDATE "m_external_accounts"
                SET "status" = 'new'
                WHERE "status" IS NULL;
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                ALTER COLUMN "status" SET DEFAULT 'new',
                ALTER COLUMN "status" SET NOT NULL,
                DROP CONSTRAINT IF EXISTS "m_external_accounts_status_check",
                ADD CONSTRAINT "m_external_accounts_status_check"
                    CHECK ("status" IN ('new', 'active'));
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
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "m_external_accounts_project_id_idx"
                ON "m_external_accounts" ("project_id");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "m_external_accounts_user_uuid_idx"
                ON "m_external_accounts" ("user_uuid");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "m_external_accounts_account_type_idx"
                ON "m_external_accounts" ("account_type");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "m_external_accounts_status_idx"
                ON "m_external_accounts" ("status");
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS "m_external_accounts_status_idx";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                DROP CONSTRAINT IF EXISTS "m_external_accounts_status_check",
                DROP COLUMN IF EXISTS "status",
                ADD COLUMN IF NOT EXISTS
                    "external_user_id" VARCHAR(128) NOT NULL DEFAULT '0';
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                ALTER COLUMN "external_user_id" DROP DEFAULT;
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_external_accounts_project_type_external_user_id_idx"
                ON "m_external_accounts"
                    ("project_id", "account_type", "external_user_id");
            """
        )


migration_step = MigrationStep()
