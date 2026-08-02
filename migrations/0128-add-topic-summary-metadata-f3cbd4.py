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
    t.source,
    t.summary,
    t.summary_last_message_uuid,
    CASE
        WHEN t.summary IS NULL THEN NULL
        ELSE t.summary_last_message_uuid IS DISTINCT FROM last_message.uuid
    END AS summary_has_new_messages,
    t.summary_system_prompt
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
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON  access.project_id = t.project_id
    AND access.user_uuid  = b.user_uuid
    AND access.stream_uuid = t.stream_uuid
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


PREVIOUS_USER_TOPICS_VIEW_SQL = """
CREATE VIEW "m_workspace_user_topics_view" AS
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
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON  access.project_id = t.project_id
    AND access.user_uuid  = b.user_uuid
    AND access.stream_uuid = t.stream_uuid
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0127-persist-bounded-reaction-user-snapshots-547d74.py"
        ]

    @property
    def migration_id(self):
        return "f3cbd414-4eba-4db1-8f1d-fc3c7eeb7f96"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_topics"
                ADD COLUMN "summary" VARCHAR(4096),
                ADD COLUMN "summary_last_message_uuid" UUID,
                ADD COLUMN "summary_system_prompt" VARCHAR(16384),
                ADD CONSTRAINT "m_workspace_topic_summary_state_check"
                    CHECK (
                        "summary" IS NOT NULL
                        OR "summary_last_message_uuid" IS NULL
                    ),
                ADD CONSTRAINT "m_workspace_topic_summary_message_fkey"
                    FOREIGN KEY ("summary_last_message_uuid")
                    REFERENCES "m_workspace_messages" ("uuid")
                    ON DELETE SET NULL
            """
        )
        session.execute(USER_TOPICS_VIEW_SQL)

    def downgrade(self, session):
        session.execute('DROP VIEW IF EXISTS "m_workspace_user_topics_view"')
        session.execute(PREVIOUS_USER_TOPICS_VIEW_SQL)
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_topics"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_topic_summary_message_fkey",
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_topic_summary_state_check",
                DROP COLUMN IF EXISTS "summary_system_prompt",
                DROP COLUMN IF EXISTS "summary_last_message_uuid",
                DROP COLUMN IF EXISTS "summary"
            """
        )


migration_step = MigrationStep()
