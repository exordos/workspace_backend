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
        self._depends = ["0035-add-m-folder-personal-and-channels-views-5e8c3a.py"]

    @property
    def migration_id(self):
        return "6f1d4b7a-2e5c-4f8d-a0b3-8c3e6a1d5f20"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_folder_items"
                DROP COLUMN IF EXISTS "unread_count";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_folder_items"
                RENAME COLUMN "folder" TO "folder_uuid";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_folder_items"
                DROP CONSTRAINT IF EXISTS "m_folder_items_pkey",
                ADD PRIMARY KEY ("uuid", "user_uuid");
            """
        )
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_folder_items_created_view" AS
            SELECT
                fi.uuid,
                fi.folder_uuid,
                fi.project_id,
                fi.user_uuid,
                fi.stream_uuid,
                fi.order_index,
                fi.pinned_at,
                fi.chat_type,
                COALESCE(un.unread_count, 0) AS unread_count,
                fi.created_at,
                fi.updated_at
            FROM "m_folder_items" AS fi
            LEFT JOIN "m_unread_user_messages" AS un
                ON  un.uuid       = fi.stream_uuid
                AND un.user_uuid  = fi.user_uuid
                AND un.project_id = fi.project_id;
            """
        )

    def downgrade(self, session):
        session.execute('DROP VIEW IF EXISTS "m_folder_items_created_view";')
        session.execute(
            """
            ALTER TABLE "m_folder_items"
                DROP CONSTRAINT IF EXISTS "m_folder_items_pkey",
                ADD PRIMARY KEY ("uuid");
            """
        )
        session.execute(
            """
            ALTER TABLE "m_folder_items"
                RENAME COLUMN "folder_uuid" TO "folder";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_folder_items"
                ADD COLUMN IF NOT EXISTS "unread_count" INTEGER NOT NULL DEFAULT 0;
            """
        )


migration_step = MigrationStep()
