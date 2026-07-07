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


NATIVE_SOURCE = """'{"kind": "native"}'::jsonb"""


USER_TOPICS_VIEW_WITH_SOURCE_SQL = """
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
    COALESCE(f.is_done, FALSE) AS is_done,
    COALESCE(f.notification_mode, 'default') AS notification_mode,
    t.color,
    last_message.uuid AS last_message_uuid,
    t.source_name,
    t.source
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
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM "m_workspace_messages" AS m
    WHERE m.project_id = t.project_id
      AND m.topic_uuid = t.uuid
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_user_topic_flags" AS f
    ON  f.uuid       = t.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = t.project_id;
"""


USER_TOPICS_VIEW_WITHOUT_SOURCE_SQL = """
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
    COALESCE(f.is_done, FALSE) AS is_done,
    COALESCE(f.notification_mode, 'default') AS notification_mode,
    t.color,
    last_message.uuid AS last_message_uuid
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
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM "m_workspace_messages" AS m
    WHERE m.project_id = t.project_id
      AND m.topic_uuid = t.uuid
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_user_topic_flags" AS f
    ON  f.uuid       = t.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = t.project_id;
"""


USER_MESSAGES_VIEW_WITH_SOURCE_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_messages_view" AS
SELECT
    m.uuid                          AS uuid,
    m.stream_uuid,
    m.user_uuid                     AS author_uuid,
    m.topic_uuid,
    m.payload,
    m.created_at,
    m.updated_at,
    b.user_uuid                     AS user_uuid,
    m.project_id,
    COALESCE(f.read,    FALSE)      AS read,
    COALESCE(f.pinned,  FALSE)      AS pinned,
    COALESCE(f.starred, FALSE)      AS starred,
    (m.user_uuid = b.user_uuid)     AS is_own,
    COALESCE(
        (
            SELECT jsonb_object_agg(
                reaction_counts.emoji_name,
                reaction_counts.reaction_count
            )
            FROM (
                SELECT
                    r.emoji_name,
                    COUNT(*) AS reaction_count
                FROM "m_workspace_message_reactions" AS r
                WHERE r.project_id = m.project_id
                    AND r.message_uuid = m.uuid
                GROUP BY r.emoji_name
            ) AS reaction_counts
        ),
        '{}'::jsonb
    )                               AS reactions,
    m.source_name,
    m.source
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid  = m.stream_uuid
    AND b.project_id   = m.project_id
LEFT JOIN "m_workspace_user_message_flags" AS f
    ON  f.uuid       = m.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = m.project_id;
"""


USER_MESSAGES_VIEW_WITHOUT_SOURCE_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_messages_view" AS
SELECT
    m.uuid                          AS uuid,
    m.stream_uuid,
    m.user_uuid                     AS author_uuid,
    m.topic_uuid,
    m.payload,
    m.created_at,
    m.updated_at,
    b.user_uuid                     AS user_uuid,
    m.project_id,
    COALESCE(f.read,    FALSE)      AS read,
    COALESCE(f.pinned,  FALSE)      AS pinned,
    COALESCE(f.starred, FALSE)      AS starred,
    (m.user_uuid = b.user_uuid)     AS is_own,
    COALESCE(
        (
            SELECT jsonb_object_agg(
                reaction_counts.emoji_name,
                reaction_counts.reaction_count
            )
            FROM (
                SELECT
                    r.emoji_name,
                    COUNT(*) AS reaction_count
                FROM "m_workspace_message_reactions" AS r
                WHERE r.project_id = m.project_id
                    AND r.message_uuid = m.uuid
                GROUP BY r.emoji_name
            ) AS reaction_counts
        ),
        '{}'::jsonb
    )                               AS reactions
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid  = m.stream_uuid
    AND b.project_id   = m.project_id
LEFT JOIN "m_workspace_user_message_flags" AS f
    ON  f.uuid       = m.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = m.project_id;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0080-set-system-folder-colors-afbbe6.py"]

    @property
    def migration_id(self):
        return "7295ec9f-3ba5-4526-ad07-90fa3fc100ce"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            f"""
            ALTER TABLE "m_workspace_stream_topics"
                ADD COLUMN IF NOT EXISTS "source_name" VARCHAR(64)
                    NOT NULL DEFAULT 'native',
                ADD COLUMN IF NOT EXISTS "source" JSONB
                    NOT NULL DEFAULT {NATIVE_SOURCE};
            """,
            f"""
            ALTER TABLE "m_workspace_messages"
                ADD COLUMN IF NOT EXISTS "source_name" VARCHAR(64)
                    NOT NULL DEFAULT 'native',
                ADD COLUMN IF NOT EXISTS "source" JSONB
                    NOT NULL DEFAULT {NATIVE_SOURCE};
            """,
            """
            UPDATE "m_workspace_streams" AS s
            SET "source" = s."source" || jsonb_build_object(
                'server_url',
                ea."server_url"
            )
            FROM (
                SELECT "project_id", MIN("server_url") AS "server_url"
                FROM "m_external_accounts"
                WHERE "account_type" = 'zulip'
                GROUP BY "project_id"
            ) AS ea
            WHERE s."project_id" = ea."project_id"
              AND s."source_name" = 'zulip'
              AND NOT (s."source" ? 'server_url');
            """,
            """
            UPDATE "m_workspace_stream_topics" AS t
            SET
                "source_name" = 'zulip',
                "source" = jsonb_build_object(
                    'kind', 'zulip',
                    'stream_id', (s."source"->>'stream_id')::bigint,
                    'server_url', s."source"->>'server_url',
                    'topic_name', t."name"
                )
            FROM "m_workspace_streams" AS s
            WHERE t."project_id" = s."project_id"
              AND t."stream_uuid" = s."uuid"
              AND s."source_name" = 'zulip';
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_stream_topics_source_name_idx"
                ON "m_workspace_stream_topics" ("source_name");
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_workspace_stream_topics_zulip_topic_unique_idx"
                ON "m_workspace_stream_topics" (
                    "project_id",
                    "stream_uuid",
                    ("source"->>'topic_name')
                )
                WHERE "source_name" = 'zulip'
                  AND "source" ? 'topic_name';
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_messages_source_name_idx"
                ON "m_workspace_messages" ("source_name");
            """,
            """
            CREATE TABLE IF NOT EXISTS "m_zulip_processed_entities" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "server_url" VARCHAR(2048) NOT NULL,
                "entity_type" VARCHAR(32) NOT NULL,
                "entity_id" VARCHAR(256) NOT NULL,
                "workspace_uuid" UUID NOT NULL,
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_zulip_processed_entities_unique"
                    UNIQUE (
                        "project_id",
                        "server_url",
                        "entity_type",
                        "entity_id"
                    )
            );
            """,
            """
            INSERT INTO "m_zulip_processed_entities"
                (
                    "uuid",
                    "project_id",
                    "server_url",
                    "entity_type",
                    "entity_id",
                    "workspace_uuid"
                )
            SELECT
                gen_random_uuid(),
                s."project_id",
                s."source"->>'server_url',
                CASE
                    WHEN s."private" THEN 'private_stream'
                    ELSE 'stream'
                END,
                s."source"->>'stream_id',
                s."uuid"
            FROM "m_workspace_streams" AS s
            WHERE s."source_name" = 'zulip'
              AND s."source" ? 'server_url'
              AND s."source" ? 'stream_id'
            ON CONFLICT (
                "project_id",
                "server_url",
                "entity_type",
                "entity_id"
            ) DO NOTHING;
            """,
            """
            INSERT INTO "m_zulip_processed_entities"
                (
                    "uuid",
                    "project_id",
                    "server_url",
                    "entity_type",
                    "entity_id",
                    "workspace_uuid"
                )
            SELECT
                gen_random_uuid(),
                t."project_id",
                t."source"->>'server_url',
                'topic',
                (t."source"->>'stream_id') || '/' ||
                    (t."source"->>'topic_name'),
                t."uuid"
            FROM "m_workspace_stream_topics" AS t
            WHERE t."source_name" = 'zulip'
              AND t."source" ? 'server_url'
              AND t."source" ? 'stream_id'
              AND t."source" ? 'topic_name'
            ON CONFLICT (
                "project_id",
                "server_url",
                "entity_type",
                "entity_id"
            ) DO NOTHING;
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_zulip_processed_entities_project_idx"
                ON "m_zulip_processed_entities" ("project_id");
            """,
            USER_TOPICS_VIEW_WITH_SOURCE_SQL,
            USER_MESSAGES_VIEW_WITH_SOURCE_SQL,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            'DROP VIEW IF EXISTS "m_workspace_user_messages_view";',
            USER_MESSAGES_VIEW_WITHOUT_SOURCE_SQL,
            'DROP VIEW IF EXISTS "m_workspace_user_topics_view";',
            USER_TOPICS_VIEW_WITHOUT_SOURCE_SQL,
            'DROP TABLE IF EXISTS "m_zulip_processed_entities";',
            """
            DROP INDEX IF EXISTS "m_workspace_messages_source_name_idx";
            """,
            """
            DROP INDEX IF EXISTS
                "m_workspace_stream_topics_zulip_topic_unique_idx";
            """,
            """
            DROP INDEX IF EXISTS "m_workspace_stream_topics_source_name_idx";
            """,
            """
            ALTER TABLE "m_workspace_messages"
                DROP COLUMN IF EXISTS "source",
                DROP COLUMN IF EXISTS "source_name";
            """,
            """
            ALTER TABLE "m_workspace_stream_topics"
                DROP COLUMN IF EXISTS "source",
                DROP COLUMN IF EXISTS "source_name";
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
