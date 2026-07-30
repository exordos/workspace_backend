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


def _user_messages_view(reaction_users):
    return f"""
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
        '{{}}'::jsonb
    )                               AS reactions,
    m.source_name,
    m.source,
    POSITION(
        '](' || 'urn:user:' || LOWER(b.user_uuid::text) || ')'
        IN LOWER(COALESCE(m.payload->>'content', ''))
    ) > 0                           AS mentioned,
    {reaction_users}                AS reaction_users
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid  = m.stream_uuid
    AND b.project_id   = m.project_id
JOIN "m_workspace_streams" AS stream
    ON  stream.uuid       = m.stream_uuid
    AND stream.project_id = m.project_id
LEFT JOIN "m_workspace_user_message_flags" AS f
    ON  f.uuid       = m.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = m.project_id
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON  access.project_id = m.project_id
    AND access.user_uuid  = b.user_uuid
    AND access.stream_uuid = m.stream_uuid
WHERE stream.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0126-index-topic-read-boundaries-20ae22.py"]

    @property
    def migration_id(self):
        return "547d747d-c9f1-4583-80d9-b932c1a5df2a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_messages"
                ADD COLUMN "reaction_users" JSONB NOT NULL DEFAULT '{}'::jsonb;
            """
        )
        session.execute(_user_messages_view('m."reaction_users"'))

    def downgrade(self, session):
        # PostgreSQL cannot remove a trailing view column with CREATE OR
        # REPLACE. Keep a harmless constant column for old readers while
        # severing the dependency before dropping canonical storage.
        session.execute(_user_messages_view("'{}'::jsonb"))
        session.execute(
            """
            ALTER TABLE "m_workspace_messages"
                DROP COLUMN "reaction_users";
            """
        )


migration_step = MigrationStep()
