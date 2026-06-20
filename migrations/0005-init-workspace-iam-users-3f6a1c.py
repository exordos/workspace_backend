#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
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
        self._depends = ["0004-init-workspace-streams-9d8e7f.py"]

    @property
    def migration_id(self):
        return "3f6a1cf0-0c2f-4f0d-9ce4-2c86fa19e7cb"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE SEQUENCE IF NOT EXISTS "workspace_iam_users_user_id_seq"
                AS INTEGER
                MINVALUE 0
                MAXVALUE 2147483647
                START WITH 1073741824
                INCREMENT BY 1;
            """,
            """
            CREATE TABLE IF NOT EXISTS "workspace_iam_users" (
                "uuid" UUID PRIMARY KEY,
                "platform_user_uuid" UUID NOT NULL,
                "user_id" INTEGER NOT NULL
                    DEFAULT nextval('workspace_iam_users_user_id_seq'),
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                CONSTRAINT "workspace_iam_users_user_id_check"
                    CHECK ("user_id" BETWEEN 0 AND 2147483647)
            );
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "workspace_iam_users_platform_user_uuid_idx"
                ON "workspace_iam_users" ("platform_user_uuid");
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "workspace_iam_users_user_id_idx"
                ON "workspace_iam_users" ("user_id");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "workspace_iam_users")
        session.execute(
            'DROP SEQUENCE IF EXISTS "workspace_iam_users_user_id_seq";'
        )


migration_step = MigrationStep()
