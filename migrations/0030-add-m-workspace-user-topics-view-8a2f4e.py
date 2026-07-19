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
        self._depends = ["0029-fix-m-unread-user-messages-view-3c7d1e.py"]

    @property
    def migration_id(self):
        return "8a2f4e1c-3b7d-4a9f-c6e2-5d0b1f8e3a74"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
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
            """
        )

    def downgrade(self, session):
        session.execute(
            'DROP VIEW IF EXISTS "m_workspace_user_topics_view";'
        )


migration_step = MigrationStep()
