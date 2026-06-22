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
        self._depends = ["0032-add-m-folder-all-items-view-2b5e9d.py"]

    @property
    def migration_id(self):
        return "3c6a1f4d-8b2e-4c7a-d9f1-5e0a3b2c6d87"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_folder_all_view" AS
            SELECT
                '00000000-0000-0000-0000-000000000000'::uuid AS uuid,
                i.project_id,
                i.user_uuid,
                'All chats'::varchar                         AS title,
                11184810::bigint                             AS background_color_value,
                'all'::varchar                               AS system_type,
                COALESCE(SUM(i.unread_count), 0)::integer    AS unread_count,
                json_agg(
                    json_build_object(
                        'uuid',         i.uuid,
                        'folder',       i.folder,
                        'project_id',   i.project_id,
                        'user_uuid',    i.user_uuid,
                        'stream_uuid',  i.stream_uuid,
                        'order_index',  i.order_index,
                        'pinned_at',    i.pinned_at,
                        'chat_type',    i.chat_type,
                        'unread_count', i.unread_count,
                        'created_at',   i.created_at,
                        'updated_at',   i.updated_at
                    )
                )                                            AS folder_items,
                '2000-01-01 00:00:00'::timestamp             AS created_at,
                '2000-01-01 00:00:00'::timestamp             AS updated_at
            FROM "m_folder_all_items_view" AS i
            GROUP BY i.project_id, i.user_uuid;
            """
        )

    def downgrade(self, session):
        session.execute('DROP VIEW IF EXISTS "m_folder_all_view";')


migration_step = MigrationStep()
