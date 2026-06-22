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
        self._depends = ["0025-add-unread-user-messages-view-3b9f1c.py"]

    @property
    def migration_id(self):
        return "7e2a4f83-c1d5-4b9e-a6f2-3d0c8e1b5a97"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute('DROP TABLE IF EXISTS "m_workspace_user_streams" CASCADE;')
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
        self._delete_view_if_exists(session, "m_workspace_user_streams")
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_user_streams" (
                "uuid" UUID NOT NULL,
                "name" VARCHAR(255) NOT NULL,
                "description" VARCHAR(255) NULL,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "last_synced_at" TIMESTAMP(6) NOT NULL,
                "source_name" VARCHAR(64) NOT NULL,
                "source" JSONB NOT NULL,
                "invite_only" BOOLEAN NOT NULL DEFAULT FALSE,
                "announce" BOOLEAN NOT NULL DEFAULT FALSE,
                "private" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_user_streams_pkey"
                    PRIMARY KEY ("uuid", "user_uuid")
            );
            """
        )


migration_step = MigrationStep()
