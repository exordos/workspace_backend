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
        self._depends = ["0027-extract-user-message-flags-table-c4a8e2.py"]

    @property
    def migration_id(self):
        return "5f3a9b2c-1d4e-4f8a-b6c7-3e0d9a7f2b85"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            'DROP VIEW IF EXISTS "m_message_to_sync";'
        )
        session.execute(
            'DROP TABLE IF EXISTS "m_workspace_user_messages" CASCADE;'
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_bindings"
                DROP COLUMN IF EXISTS "status";
            """
        )
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_unread_user_messages" AS
            SELECT
                m.stream_uuid                   AS uuid,
                f.user_uuid,
                f.project_id,
                COUNT(*)                        AS unread_count
            FROM "m_workspace_user_message_flags" AS f
            JOIN "m_workspace_messages" AS m
                ON  m.uuid       = f.uuid
                AND m.project_id = f.project_id
            WHERE f.read = false
            GROUP BY m.stream_uuid, f.user_uuid, f.project_id;
            """
        )
        session.execute(
            """
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
        )

    def downgrade(self, session):
        session.execute(
            'DROP VIEW IF EXISTS "m_workspace_user_messages_view";'
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_bindings"
                ADD COLUMN IF NOT EXISTS "status" VARCHAR(16) NOT NULL
                    DEFAULT 'new'
                    CHECK ("status" IN ('new', 'in_progress', 'active'));
            """
        )


migration_step = MigrationStep()
