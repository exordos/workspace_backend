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
        self._depends = ["0040-create-m-folders-view-c3d4e5.py"]

    @property
    def migration_id(self):
        return "f8e7d6c5-b4a3-4921-9f0e-1a2b3c4d5e6f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_user_topic_flags" (
                "uuid" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "is_done" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("uuid", "user_uuid"),
                CONSTRAINT "m_workspace_user_topic_flags_uuid_fkey"
                    FOREIGN KEY ("uuid") REFERENCES "m_workspace_stream_topics" ("uuid")
                    ON DELETE CASCADE
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_topic_flags_user_uuid_idx"
                ON "m_workspace_user_topic_flags" ("user_uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_topic_flags_project_id_idx"
                ON "m_workspace_user_topic_flags" ("project_id");
            """,
            """
            INSERT INTO "m_workspace_user_topic_flags"
                ("uuid", "user_uuid", "project_id", "is_done",
                 "created_at", "updated_at")
            SELECT
                t."uuid",
                b."user_uuid",
                t."project_id",
                FALSE,
                NOW(),
                NOW()
            FROM "m_workspace_stream_topics" AS t
            JOIN "m_workspace_stream_bindings" AS b
                ON b."stream_uuid" = t."stream_uuid"
                AND b."project_id"  = t."project_id"
            ON CONFLICT ("uuid", "user_uuid") DO NOTHING;
            """,
            'DROP VIEW IF EXISTS "m_workspace_user_topics_view";',
            """
            CREATE OR REPLACE VIEW "m_workspace_user_topics_view" AS
            SELECT
                t.uuid,
                t.name,
                t.stream_uuid,
                t.project_id,
                t.created_at,
                t.updated_at,
                (t.default_for_stream_uuid IS NOT NULL) AS is_default,
                b.user_uuid,
                COALESCE(uc.unread_count, 0) AS unread_count,
                COALESCE(f.is_done, FALSE) AS is_done
            FROM "m_workspace_stream_topics" AS t
            JOIN "m_workspace_stream_bindings" AS b
                ON  b.stream_uuid = t.stream_uuid
                AND b.project_id  = t.project_id
            LEFT JOIN (
                SELECT
                    m.topic_uuid,
                    f.user_uuid,
                    f.project_id,
                    COUNT(*) AS unread_count
                FROM "m_workspace_user_message_flags" AS f
                JOIN "m_workspace_messages" AS m
                    ON  m.uuid       = f.uuid
                    AND m.project_id = f.project_id
                WHERE f.read = false
                  AND m.topic_uuid IS NOT NULL
                GROUP BY m.topic_uuid, f.user_uuid, f.project_id
            ) AS uc
                ON  uc.topic_uuid = t.uuid
                AND uc.user_uuid  = b.user_uuid
                AND uc.project_id = t.project_id
            LEFT JOIN "m_workspace_user_topic_flags" AS f
                ON  f.uuid       = t.uuid
                AND f.user_uuid  = b.user_uuid
                AND f.project_id = t.project_id;
            """,
        ]
        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            'DROP VIEW IF EXISTS "m_workspace_user_topics_view";',
            'DROP TABLE IF EXISTS "m_workspace_user_topic_flags";',
            """
            CREATE OR REPLACE VIEW "m_workspace_user_topics_view" AS
            SELECT
                t.uuid,
                t.name,
                t.stream_uuid,
                t.project_id,
                t.created_at,
                t.updated_at,
                (t.default_for_stream_uuid IS NOT NULL) AS is_default,
                b.user_uuid,
                COALESCE(uc.unread_count, 0) AS unread_count
            FROM "m_workspace_stream_topics" AS t
            JOIN "m_workspace_stream_bindings" AS b
                ON  b.stream_uuid = t.stream_uuid
                AND b.project_id  = t.project_id
            LEFT JOIN (
                SELECT
                    m.topic_uuid,
                    f.user_uuid,
                    f.project_id,
                    COUNT(*) AS unread_count
                FROM "m_workspace_user_message_flags" AS f
                JOIN "m_workspace_messages" AS m
                    ON  m.uuid       = f.uuid
                    AND m.project_id = f.project_id
                WHERE f.read = false
                  AND m.topic_uuid IS NOT NULL
                GROUP BY m.topic_uuid, f.user_uuid, f.project_id
            ) AS uc
                ON  uc.topic_uuid = t.uuid
                AND uc.user_uuid  = b.user_uuid
                AND uc.project_id = t.project_id;
            """,
        ]
        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
