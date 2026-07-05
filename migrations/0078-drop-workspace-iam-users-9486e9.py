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
        self._depends = ["0077-allow-iam-external-account-type-ffe9dc.py"]

    @property
    def migration_id(self):
        return "9486e94d-439b-4e13-b275-0ae1570dccca"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            DROP TABLE IF EXISTS "workspace_iam_users";
            """
        )
        session.execute(
            """
            DROP SEQUENCE IF EXISTS "workspace_iam_users_user_id_seq";
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            CREATE SEQUENCE IF NOT EXISTS "workspace_iam_users_user_id_seq"
                AS INTEGER
                MINVALUE 0
                MAXVALUE 2147483647
                START WITH 1073741824
                INCREMENT BY 1;
            """
        )
        session.execute(
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
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "workspace_iam_users_platform_user_uuid_idx"
                ON "workspace_iam_users" ("platform_user_uuid");
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "workspace_iam_users_user_id_idx"
                ON "workspace_iam_users" ("user_id");
            """
        )


migration_step = MigrationStep()
