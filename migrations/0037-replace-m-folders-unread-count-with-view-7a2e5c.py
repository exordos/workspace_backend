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
        self._depends = ["0036-replace-m-folder-items-with-view-6f1d4b.py"]

    @property
    def migration_id(self):
        return "7a2e5c8b-3f1d-4e9a-b6c2-1d5f8a3e7c04"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_folders"
                DROP COLUMN IF EXISTS "unread_count";
            """
        )
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_folder_created_view" AS
            SELECT
                f.uuid,
                f.project_id,
                f.user_uuid,
                f.title,
                f.background_color_value,
                f.system_type,
                COALESCE(SUM(COALESCE(fi.unread_count, 0)), 0)::integer AS unread_count,
                COALESCE(
                    json_agg(
                        json_build_object(
                            'uuid',         fi.uuid,
                            'folder_uuid',  fi.folder_uuid,
                            'project_id',   fi.project_id,
                            'user_uuid',    fi.user_uuid,
                            'stream_uuid',  fi.stream_uuid,
                            'order_index',  fi.order_index,
                            'pinned_at',    fi.pinned_at,
                            'chat_type',    fi.chat_type,
                            'unread_count', fi.unread_count,
                            'created_at',   fi.created_at,
                            'updated_at',   fi.updated_at
                        )
                    ) FILTER (WHERE fi.uuid IS NOT NULL),
                    '[]'::json
                ) AS folder_items,
                f.created_at,
                f.updated_at
            FROM "m_folders" AS f
            LEFT JOIN "m_folder_items_created_view" AS fi
                ON  fi.folder_uuid = f.uuid
                AND fi.user_uuid   = f.user_uuid
                AND fi.project_id  = f.project_id
            GROUP BY
                f.uuid,
                f.project_id,
                f.user_uuid,
                f.title,
                f.background_color_value,
                f.system_type,
                f.created_at,
                f.updated_at;
            """
        )

    def downgrade(self, session):
        session.execute('DROP VIEW IF EXISTS "m_folder_created_view";')
        session.execute(
            """
            ALTER TABLE "m_folders"
                ADD COLUMN IF NOT EXISTS "unread_count" INTEGER NOT NULL DEFAULT 0;
            """
        )


migration_step = MigrationStep()
