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


USER_MESSAGES_VIEW_WITH_REACTIONS_SQL = """
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

USER_MESSAGES_VIEW_WITHOUT_REACTIONS_SQL = """
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
    (m.user_uuid = b.user_uuid)     AS is_own
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
        self._depends = ["0057-drop-message-reaction-status-707278.py"]

    @property
    def migration_id(self):
        return "fa37f909-b49b-4dd8-9493-2fc8641c3ce9"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(USER_MESSAGES_VIEW_WITH_REACTIONS_SQL)

    def downgrade(self, session):
        session.execute(USER_MESSAGES_VIEW_WITHOUT_REACTIONS_SQL)


migration_step = MigrationStep()
