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
        self._depends = ["0005-init-workspace-iam-users-3f6a1c.py"]

    @property
    def migration_id(self):
        return "a2f110d3-e5cf-4ab9-8bf1-a58bed41bede"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_folders" (
                "uuid" UUID PRIMARY KEY,
                "title" VARCHAR(64) NOT NULL,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "background_color_value" BIGINT NULL,
                "unread_messages" JSONB[] NOT NULL,
                "system_type" VARCHAR(16) NULL
                    CHECK (system_type IN ('all', 'created')),
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW()
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_folders_title_idx"
                ON "m_folders" ("title");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_folders_project_id_idx"
                ON "m_folders" ("project_id");
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_folders_one_all_per_project_user_uuid_idx"
                ON "m_folders" ("project_id", "user_uuid")
                WHERE (system_type = 'all');
            """,
            """
            CREATE TABLE IF NOT EXISTS "m_folder_items" (
                "uuid" UUID PRIMARY KEY,
                "folder" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "stream_uuid" UUID NOT NULL,
                "order_index" INTEGER NULL,
                "pinned_at" TIMESTAMP(6) NULL,
                "chat_type" VARCHAR(16) NOT NULL
                    CHECK (chat_type IN ('stream', 'group', 'private')),
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_folder_items_folder_uuid_fkey"
                    FOREIGN KEY ("folder") REFERENCES "m_folders" ("uuid")
                    ON DELETE CASCADE
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_folder_items_project_id_idx"
                ON "m_folder_items" ("project_id");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_folder_items_folder_idx"
                ON "m_folder_items" ("folder");
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS "m_stream_uuid_folder_idx"
                ON "m_folder_items" ("stream_uuid", "folder");
            """,
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_streams" (
                "uuid" UUID PRIMARY KEY,
                "name" VARCHAR(255) NOT NULL,
                "description" VARCHAR(255) NULL,
                "source_name" VARCHAR(64) NOT NULL,
                "source" JSONB NOT NULL,
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW()
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_streams_source_name_idx"
                ON "m_workspace_streams" ("source_name");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        tables = [
            "m_folder_items",
            "m_folders",
            "m_workspace_streams",
        ]

        for table in tables:
            self._delete_table_if_exists(session, table)


migration_step = MigrationStep()
