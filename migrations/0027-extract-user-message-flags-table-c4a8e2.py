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
        self._depends = ["0026-replace-m-workspace-user-streams-with-view-7e2a4f.py"]

    @property
    def migration_id(self):
        return "c4a8e2f3-9b1d-4e7a-b5c3-2d0f8a6e1c94"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_user_message_flags" (
                "uuid" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "read" BOOLEAN NOT NULL DEFAULT FALSE,
                "pinned" BOOLEAN NOT NULL DEFAULT FALSE,
                "starred" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("uuid", "user_uuid")
            );
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_message_flags_user_uuid_idx"
                ON "m_workspace_user_message_flags" ("user_uuid");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_message_flags_project_id_idx"
                ON "m_workspace_user_message_flags" ("project_id");
            """
        )
        session.execute(
            """
            INSERT INTO "m_workspace_user_message_flags"
                ("uuid", "user_uuid", "project_id", "read", "pinned", "starred",
                 "created_at", "updated_at")
            SELECT uuid, user_uuid, project_id, read, pinned, starred,
                   created_at, updated_at
            FROM "m_workspace_user_messages";
            """
        )
        session.execute('DROP VIEW IF EXISTS "m_workspace_user_streams" CASCADE;')
        session.execute('DROP VIEW IF EXISTS "m_unread_user_messages" CASCADE;')
        session.execute(
            """
            ALTER TABLE "m_workspace_user_messages"
                DROP COLUMN IF EXISTS "read",
                DROP COLUMN IF EXISTS "pinned",
                DROP COLUMN IF EXISTS "starred";
            """
        )
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_unread_user_messages" AS
            SELECT
                f.uuid AS uuid,
                f.user_uuid,
                f.project_id,
                COUNT(*) AS unread_count
            FROM "m_workspace_user_message_flags" AS f
            WHERE f.read = false
            GROUP BY f.uuid, f.user_uuid, f.project_id;
            """
        )
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_workspace_user_streams" AS
            SELECT
                s.uuid,
                CASE
                    WHEN s.private AND s.user_uuid <> b.user_uuid THEN
                        COALESCE(
                            NULLIF(
                                TRIM(
                                    COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')
                                ),
                                ''
                            ),
                            u.username
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
                s.updated_at
            FROM "m_workspace_streams" AS s
            JOIN "m_workspace_stream_bindings" AS b
                ON b.stream_uuid = s.uuid
                AND b.project_id = s.project_id
            LEFT JOIN "m_unread_user_messages" AS un
                ON un.uuid = s.uuid
                AND un.user_uuid = b.user_uuid
                AND un.project_id = s.project_id
            LEFT JOIN "m_workspace_users" AS u
                ON u.uuid = s.user_uuid;
            """
        )

    def downgrade(self, session):
        session.execute('DROP VIEW IF EXISTS "m_workspace_user_streams" CASCADE;')
        session.execute('DROP VIEW IF EXISTS "m_unread_user_messages" CASCADE;')
        session.execute(
            """
            ALTER TABLE "m_workspace_user_messages"
                ADD COLUMN IF NOT EXISTS "read" BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS "pinned" BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS "starred" BOOLEAN NOT NULL DEFAULT FALSE;
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_user_messages" AS um
            SET read    = f.read,
                pinned  = f.pinned,
                starred = f.starred
            FROM "m_workspace_user_message_flags" AS f
            WHERE f.uuid = um.uuid AND f.user_uuid = um.user_uuid;
            """
        )
        session.execute(
            'DROP TABLE IF EXISTS "m_workspace_user_message_flags";'
        )
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_unread_user_messages" AS
            SELECT
                um.stream_uuid AS uuid,
                um.user_uuid,
                um.project_id,
                COUNT(*) AS unread_count
            FROM "m_workspace_user_messages" AS um
            WHERE um.read = false
            GROUP BY um.user_uuid, um.stream_uuid, um.project_id;
            """
        )
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_workspace_user_streams" AS
            SELECT
                s.uuid,
                CASE
                    WHEN s.private AND s.user_uuid <> b.user_uuid THEN
                        COALESCE(
                            NULLIF(
                                TRIM(
                                    COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')
                                ),
                                ''
                            ),
                            u.username
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
                s.updated_at
            FROM "m_workspace_streams" AS s
            JOIN "m_workspace_stream_bindings" AS b
                ON b.stream_uuid = s.uuid
                AND b.project_id = s.project_id
            LEFT JOIN "m_unread_user_messages" AS un
                ON un.uuid = s.uuid
                AND un.user_uuid = b.user_uuid
                AND un.project_id = s.project_id
            LEFT JOIN "m_workspace_users" AS u
                ON u.uuid = s.user_uuid;
            """
        )


migration_step = MigrationStep()
