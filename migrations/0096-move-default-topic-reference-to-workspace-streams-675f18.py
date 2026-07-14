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
    s.color,
    last_message.uuid AS last_message_uuid,
    s.default_topic_uuid
FROM "m_workspace_streams" AS s
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = s.uuid
    AND b.project_id = s.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON un.uuid = s.uuid
    AND un.user_uuid = b.user_uuid
    AND un.project_id = s.project_id
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM "m_workspace_user_messages_view" AS m
    WHERE m.project_id = s.project_id
      AND m.stream_uuid = s.uuid
      AND m.user_uuid = b.user_uuid
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_users" AS peer_user
    ON peer_user.uuid = CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        WHEN s.private AND s.user_uuid <> b.user_uuid THEN s.user_uuid
        ELSE NULL
    END
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = s.project_id
    AND access.user_uuid    = b.user_uuid
    AND access.account_type = s.source_name
    AND access.source_scope = COALESCE(
        s.source->>'source_scope',
        s.source->>'server_url'
    )
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


DOWNGRADE_USER_STREAMS_VIEW_SQL = USER_STREAMS_VIEW_SQL.replace(
    "s.default_topic_uuid\nFROM",
    "NULL::uuid AS default_topic_uuid\nFROM",
)


USER_TOPICS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_topics_view" AS
SELECT
    t.uuid,
    t.name,
    t.stream_uuid,
    t.project_id,
    t.created_at,
    t.updated_at,
    (t.uuid = s.default_topic_uuid) AS is_default,
    b.user_uuid,
    COALESCE(uc.unread_count, 0) AS unread_count,
    COALESCE(f.is_done, FALSE) AS is_done,
    COALESCE(f.notification_mode, 'default') AS notification_mode,
    t.color,
    last_message.uuid AS last_message_uuid,
    t.source_name,
    t.source
FROM "m_workspace_stream_topics" AS t
JOIN "m_workspace_streams" AS s
    ON  s.uuid = t.stream_uuid
    AND s.project_id = t.project_id
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid = t.stream_uuid
    AND b.project_id  = t.project_id
LEFT JOIN (
    SELECT
        m.topic_uuid,
        m.user_uuid,
        m.project_id,
        COUNT(*) AS unread_count
    FROM "m_workspace_user_messages_view" AS m
    WHERE m.read = false
      AND m.topic_uuid IS NOT NULL
    GROUP BY m.topic_uuid, m.user_uuid, m.project_id
) AS uc
    ON  uc.topic_uuid = t.uuid
    AND uc.user_uuid  = b.user_uuid
    AND uc.project_id = t.project_id
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM "m_workspace_user_messages_view" AS m
    WHERE m.project_id = t.project_id
      AND m.topic_uuid = t.uuid
      AND m.user_uuid = b.user_uuid
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_user_topic_flags" AS f
    ON  f.uuid       = t.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = t.project_id
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = t.project_id
    AND access.user_uuid    = b.user_uuid
    AND access.account_type = t.source_name
    AND access.source_scope = COALESCE(
        t.source->>'source_scope',
        t.source->>'server_url'
    )
WHERE t.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


DOWNGRADE_USER_TOPICS_VIEW_SQL = USER_TOPICS_VIEW_SQL.replace(
    "(t.uuid = s.default_topic_uuid) AS is_default",
    "(t.default_for_stream_uuid IS NOT NULL) AS is_default",
).replace(
    "JOIN \"m_workspace_streams\" AS s\n"
    "    ON  s.uuid = t.stream_uuid\n"
    "    AND s.project_id = t.project_id\n",
    "",
)


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0095-fix-workspace-gravatar-avatar-urn-f75679.py"]

    @property
    def migration_id(self):
        return "675f18d8-bc07-49cf-b306-5e96b54467d4"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_streams"
                ADD COLUMN "default_topic_uuid" UUID DEFAULT NULL;
            """,
            """
            UPDATE "m_workspace_streams" AS s
            SET "default_topic_uuid" = t."uuid"
            FROM "m_workspace_stream_topics" AS t
            WHERE t."default_for_stream_uuid" = s."uuid"
              AND t."stream_uuid" = s."uuid"
              AND t."project_id" = s."project_id";
            """,
            """
            ALTER TABLE "m_workspace_stream_topics"
                ADD CONSTRAINT
                    "m_workspace_stream_topics_default_target_unique"
                UNIQUE ("uuid", "stream_uuid", "project_id");
            """,
            """
            ALTER TABLE "m_workspace_streams"
                ADD CONSTRAINT "m_workspace_streams_default_topic_fkey"
                FOREIGN KEY ("default_topic_uuid", "uuid", "project_id")
                REFERENCES "m_workspace_stream_topics"
                    ("uuid", "stream_uuid", "project_id")
                DEFERRABLE INITIALLY DEFERRED;
            """,
            USER_STREAMS_VIEW_SQL,
            USER_TOPICS_VIEW_SQL,
            """
            ALTER TABLE "m_workspace_stream_topics"
                DROP CONSTRAINT
                    "m_workspace_stream_topics_default_for_stream_uuid_unique",
                DROP COLUMN "default_for_stream_uuid";
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_stream_topics"
                ADD COLUMN "default_for_stream_uuid" UUID DEFAULT NULL;
            """,
            """
            UPDATE "m_workspace_stream_topics" AS t
            SET "default_for_stream_uuid" = s."uuid"
            FROM "m_workspace_streams" AS s
            WHERE s."default_topic_uuid" = t."uuid"
              AND s."uuid" = t."stream_uuid"
              AND s."project_id" = t."project_id";
            """,
            """
            ALTER TABLE "m_workspace_stream_topics"
                ADD CONSTRAINT
                    "m_workspace_stream_topics_default_for_stream_uuid_unique"
                UNIQUE ("default_for_stream_uuid");
            """,
            DOWNGRADE_USER_STREAMS_VIEW_SQL,
            DOWNGRADE_USER_TOPICS_VIEW_SQL,
            """
            ALTER TABLE "m_workspace_streams"
                DROP CONSTRAINT "m_workspace_streams_default_topic_fkey",
                DROP COLUMN "default_topic_uuid";
            """,
            """
            ALTER TABLE "m_workspace_stream_topics"
                DROP CONSTRAINT
                    "m_workspace_stream_topics_default_target_unique";
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
