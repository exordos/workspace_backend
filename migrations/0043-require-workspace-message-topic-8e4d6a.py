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
        self._depends = ["0042-add-workspace-events-outbox-2f6a9c.py"]

    @property
    def migration_id(self):
        return "8e4d6a2b-1f30-4d28-8b7a-09d2f1c3e4a5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            INSERT INTO "m_workspace_stream_topics"
                ("uuid", "project_id", "name", "stream_uuid",
                 "default_for_stream_uuid", "created_at", "updated_at")
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
                SELECT 1 FROM "m_workspace_stream_topics" AS t
                WHERE t."default_for_stream_uuid" = s."uuid"
            );
            """,
            """
            UPDATE "m_workspace_messages" AS msg
            SET "topic_uuid" = t."uuid"
            FROM "m_workspace_stream_topics" AS t
            WHERE t."default_for_stream_uuid" = msg."stream_uuid"
              AND msg."topic_uuid" IS NULL;
            """,
            """
            ALTER TABLE "m_workspace_messages"
                DROP CONSTRAINT IF EXISTS "m_workspace_messages_topic_uuid_fkey",
                ALTER COLUMN "topic_uuid" SET NOT NULL,
                ADD CONSTRAINT "m_workspace_messages_topic_uuid_fkey"
                    FOREIGN KEY ("topic_uuid")
                    REFERENCES "m_workspace_stream_topics" ("uuid");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_messages"
                DROP CONSTRAINT IF EXISTS "m_workspace_messages_topic_uuid_fkey",
                ALTER COLUMN "topic_uuid" DROP NOT NULL,
                ADD CONSTRAINT "m_workspace_messages_topic_uuid_fkey"
                    FOREIGN KEY ("topic_uuid")
                    REFERENCES "m_workspace_stream_topics" ("uuid")
                    ON DELETE SET NULL;
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
