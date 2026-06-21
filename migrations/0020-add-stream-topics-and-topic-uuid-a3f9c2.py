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
        self._depends = ["0019-add-m-message-to-sync-view-d4f7b2.py"]

    @property
    def migration_id(self):
        return "a3f9c2e1-4b7d-4f8a-9e2c-6d1b5f0a3c78"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_stream_topics" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "name" VARCHAR(128) NOT NULL,
                "stream_uuid" UUID NOT NULL,
                "default_for_stream_uuid" UUID DEFAULT NULL,
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_stream_topics_stream_uuid_fkey"
                    FOREIGN KEY ("stream_uuid") REFERENCES "m_workspace_streams" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_stream_topics_default_for_stream_uuid_unique"
                    UNIQUE ("default_for_stream_uuid")
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_stream_topics_project_id_idx"
                ON "m_workspace_stream_topics" ("project_id");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_stream_topics_stream_uuid_idx"
                ON "m_workspace_stream_topics" ("stream_uuid");
            """,
            """
            INSERT INTO "m_workspace_stream_topics"
                ("uuid", "project_id", "name", "stream_uuid", "default_for_stream_uuid",
                 "created_at", "updated_at")
            SELECT
                gen_random_uuid(),
                s."project_id",
                'General Chat',
                s."uuid",
                s."uuid",
                NOW(),
                NOW()
            FROM "m_workspace_streams" AS s
            WHERE NOT EXISTS (
                SELECT 1 FROM "m_workspace_stream_topics" t
                WHERE t."default_for_stream_uuid" = s."uuid"
            );
            """,
            """
            ALTER TABLE "m_workspace_messages"
                ADD COLUMN IF NOT EXISTS "topic_uuid" UUID DEFAULT NULL,
                ADD CONSTRAINT "m_workspace_messages_topic_uuid_fkey"
                    FOREIGN KEY ("topic_uuid") REFERENCES "m_workspace_stream_topics" ("uuid")
                    ON DELETE SET NULL;
            """,
            """
            UPDATE "m_workspace_messages" AS msg
            SET "topic_uuid" = t."uuid"
            FROM "m_workspace_stream_topics" AS t
            WHERE t."default_for_stream_uuid" = msg."stream_uuid"
              AND msg."topic_uuid" IS NULL;
            """,
            """
            ALTER TABLE "m_workspace_user_messages"
                ADD COLUMN IF NOT EXISTS "topic_uuid" UUID DEFAULT NULL,
                ADD CONSTRAINT "m_workspace_user_messages_topic_uuid_fkey"
                    FOREIGN KEY ("topic_uuid") REFERENCES "m_workspace_stream_topics" ("uuid")
                    ON DELETE SET NULL;
            """,
            """
            UPDATE "m_workspace_user_messages" AS um
            SET "topic_uuid" = t."uuid"
            FROM "m_workspace_stream_topics" AS t
            WHERE t."default_for_stream_uuid" = um."stream_uuid"
              AND um."topic_uuid" IS NULL;
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_user_messages"
                DROP CONSTRAINT IF EXISTS "m_workspace_user_messages_topic_uuid_fkey",
                DROP COLUMN IF EXISTS "topic_uuid";
            """,
            """
            ALTER TABLE "m_workspace_messages"
                DROP CONSTRAINT IF EXISTS "m_workspace_messages_topic_uuid_fkey",
                DROP COLUMN IF EXISTS "topic_uuid";
            """,
        ]
        for expression in expressions:
            session.execute(expression)
        self._delete_table_if_exists(session, "m_workspace_stream_topics")


migration_step = MigrationStep()
