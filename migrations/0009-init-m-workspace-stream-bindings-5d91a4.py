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
        self._depends = ["0008-add-m-workspace-stream-private-8a7e2c.py"]

    @property
    def migration_id(self):
        return "5d91a45d-03fd-4925-a9d5-8b024253d0be"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_stream_bindings" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "stream" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "who" UUID NOT NULL,
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_stream_bindings_stream_fkey"
                    FOREIGN KEY ("stream") REFERENCES "m_workspace_streams" ("uuid")
                    ON DELETE CASCADE
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_stream_bindings_project_id_idx"
                ON "m_workspace_stream_bindings" ("project_id");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_stream_bindings_stream_idx"
                ON "m_workspace_stream_bindings" ("stream");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_stream_bindings_user_uuid_idx"
                ON "m_workspace_stream_bindings" ("user_uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_stream_bindings_who_idx"
                ON "m_workspace_stream_bindings" ("who");
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_workspace_stream_bindings_unique_idx"
                ON "m_workspace_stream_bindings"
                ("project_id", "stream", "user_uuid", "who");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "m_workspace_stream_bindings")


migration_step = MigrationStep()
