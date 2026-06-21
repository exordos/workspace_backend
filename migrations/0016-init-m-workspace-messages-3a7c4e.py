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
        self._depends = ["0015-init-m-workspace-user-streams-and-sync-view-6f4f3b.py"]

    @property
    def migration_id(self):
        return "3a7c4e91-5b2f-4d8a-9c1e-7f6d3b2a0e85"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_messages" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "stream_uuid" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "payload" JSONB NOT NULL,
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_messages_stream_uuid_fkey"
                    FOREIGN KEY ("stream_uuid") REFERENCES "m_workspace_streams" ("uuid")
                    ON DELETE CASCADE
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_messages_project_id_idx"
                ON "m_workspace_messages" ("project_id");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_messages_stream_uuid_idx"
                ON "m_workspace_messages" ("stream_uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_messages_user_uuid_idx"
                ON "m_workspace_messages" ("user_uuid");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "m_workspace_messages")


migration_step = MigrationStep()
