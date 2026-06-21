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
        self._depends = ["0016-init-m-workspace-messages-3a7c4e.py"]

    @property
    def migration_id(self):
        return "b9d2f1a3-7c4e-4b8d-a2f5-1e6d9c3b0f47"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_user_messages" (
                "uuid" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "user_stream_uuid" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "payload" JSONB NOT NULL,
                "last_synced_at" TIMESTAMP(6) NOT NULL,
                "read" BOOLEAN NOT NULL DEFAULT FALSE,
                "pinned" BOOLEAN NOT NULL DEFAULT FALSE,
                "starred" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("uuid", "user_uuid"),
                CONSTRAINT "m_workspace_user_messages_user_stream_uuid_fkey"
                    FOREIGN KEY ("user_stream_uuid")
                    REFERENCES "m_workspace_user_streams" ("uuid")
                    ON DELETE CASCADE
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_messages_project_id_idx"
                ON "m_workspace_user_messages" ("project_id");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_messages_user_uuid_idx"
                ON "m_workspace_user_messages" ("user_uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_messages_user_stream_uuid_idx"
                ON "m_workspace_user_messages" ("user_stream_uuid");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "m_workspace_user_messages")


migration_step = MigrationStep()
