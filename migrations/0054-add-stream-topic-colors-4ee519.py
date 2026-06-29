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


USER_STREAMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_streams" AS
SELECT
    s.uuid,
    CASE
        WHEN s.private THEN
            COALESCE(
                NULLIF(
                    TRIM(
                        COALESCE(peer_user.first_name, '') || ' ' ||
                        COALESCE(peer_user.last_name, '')
                    ),
                    ''
                ),
                peer_user.username,
                s.name
            )
        ELSE s.name
    END AS name,
    s.description,
    s.project_id,
    s.source_name,
    s.source,
    s.user_uuid AS owner,
    b.user_uuid AS user_uuid,
    b.role AS role,
    COALESCE(un.unread_count, 0) AS unread_count,
    s.invite_only,
    s.announce,
    s.private,
    s.created_at,
    s.updated_at,
    CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        ELSE NULL
    END AS direct_user_uuid,
    s.private_index,
    s.is_archived,
    b.notification_mode,
    s.color
FROM "m_workspace_streams" AS s
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = s.uuid
    AND b.project_id = s.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON un.uuid = s.uuid
    AND un.user_uuid = b.user_uuid
    AND un.project_id = s.project_id
LEFT JOIN "m_workspace_users" AS peer_user
    ON peer_user.uuid = CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        WHEN s.private AND s.user_uuid <> b.user_uuid THEN s.user_uuid
        ELSE NULL
    END;
"""


PREVIOUS_USER_STREAMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_streams" AS
SELECT
    s.uuid,
    CASE
        WHEN s.private THEN
            COALESCE(
                NULLIF(
                    TRIM(
                        COALESCE(peer_user.first_name, '') || ' ' ||
                        COALESCE(peer_user.last_name, '')
                    ),
                    ''
                ),
                peer_user.username,
                s.name
            )
        ELSE s.name
    END AS name,
    s.description,
    s.project_id,
    s.source_name,
    s.source,
    s.user_uuid AS owner,
    b.user_uuid AS user_uuid,
    b.role AS role,
    COALESCE(un.unread_count, 0) AS unread_count,
    s.invite_only,
    s.announce,
    s.private,
    s.created_at,
    s.updated_at,
    CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        ELSE NULL
    END AS direct_user_uuid,
    s.private_index,
    s.is_archived,
    b.notification_mode
FROM "m_workspace_streams" AS s
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = s.uuid
    AND b.project_id = s.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON un.uuid = s.uuid
    AND un.user_uuid = b.user_uuid
    AND un.project_id = s.project_id
LEFT JOIN "m_workspace_users" AS peer_user
    ON peer_user.uuid = CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        WHEN s.private AND s.user_uuid <> b.user_uuid THEN s.user_uuid
        ELSE NULL
    END;
"""


USER_TOPICS_VIEW_SQL = """
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
    t.color
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
"""


PREVIOUS_USER_TOPICS_VIEW_SQL = """
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
    COALESCE(f.notification_mode, 'default') AS notification_mode
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
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0053-topic-notification-mode-72f564.py"]

    @property
    def migration_id(self):
        return "4ee51940-e59b-4ff2-8b5f-eb206edcad86"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_streams"
                ADD COLUMN IF NOT EXISTS "color" BIGINT NULL;
            """,
            """
            UPDATE "m_workspace_streams"
            SET "color" = FLOOR(random() * 16777216)::bigint
            WHERE "color" IS NULL;
            """,
            """
            ALTER TABLE "m_workspace_streams"
                ALTER COLUMN "color"
                    SET DEFAULT (FLOOR(random() * 16777216)::bigint),
                ALTER COLUMN "color" SET NOT NULL;
            """,
            """
            ALTER TABLE "m_workspace_streams"
                DROP CONSTRAINT IF EXISTS "m_workspace_streams_color_rgb_check",
                ADD CONSTRAINT "m_workspace_streams_color_rgb_check"
                    CHECK ("color" BETWEEN 0 AND 16777215);
            """,
            """
            ALTER TABLE "m_workspace_stream_topics"
                ADD COLUMN IF NOT EXISTS "color" BIGINT NULL;
            """,
            """
            UPDATE "m_workspace_stream_topics"
            SET "color" = FLOOR(random() * 16777216)::bigint
            WHERE "color" IS NULL;
            """,
            """
            ALTER TABLE "m_workspace_stream_topics"
                ALTER COLUMN "color"
                    SET DEFAULT (FLOOR(random() * 16777216)::bigint),
                ALTER COLUMN "color" SET NOT NULL;
            """,
            """
            ALTER TABLE "m_workspace_stream_topics"
                DROP CONSTRAINT IF EXISTS "m_workspace_stream_topics_color_rgb_check",
                ADD CONSTRAINT "m_workspace_stream_topics_color_rgb_check"
                    CHECK ("color" BETWEEN 0 AND 16777215);
            """,
            USER_STREAMS_VIEW_SQL,
            USER_TOPICS_VIEW_SQL,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            'DROP VIEW IF EXISTS "m_workspace_user_topics_view";',
            PREVIOUS_USER_TOPICS_VIEW_SQL,
            """
            DROP VIEW IF EXISTS "m_workspace_user_streams" CASCADE;
            """,
            PREVIOUS_USER_STREAMS_VIEW_SQL,
            """
            ALTER TABLE "m_workspace_stream_topics"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_stream_topics_color_rgb_check",
                DROP COLUMN IF EXISTS "color";
            """,
            """
            ALTER TABLE "m_workspace_streams"
                DROP CONSTRAINT IF EXISTS "m_workspace_streams_color_rgb_check",
                DROP COLUMN IF EXISTS "color";
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
