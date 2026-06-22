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
        self._depends = [
            "0023-add-is-mentioned-to-user-messages-a8c4e5.py"
        ]

    @property
    def migration_id(self):
        return "90c43d35-010f-4cf8-abd7-35a05fc49b3e"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_users" (
                "uuid" UUID NOT NULL,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL,
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL,
                "username" VARCHAR(128) NOT NULL,
                "source" VARCHAR(16) NOT NULL,
                "status" VARCHAR(16) NOT NULL,
                "first_name" VARCHAR(128),
                "last_name" VARCHAR(128),
                "email" VARCHAR(256),
                "last_ping_at" TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY ("uuid"),
                UNIQUE ("username")
            );
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_users_username_idx"
                ON "m_workspace_users" ("username");
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP TABLE IF EXISTS "m_workspace_users";
            """
        )


migration_step = MigrationStep()
