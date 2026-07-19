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
        self._depends = ["0031-add-unread-count-to-folders-and-folder-items-1a3f7c.py"]

    @property
    def migration_id(self):
        return "2b5e9d3f-7a1c-4f8b-d6e0-3c4a2f1b8e95"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_folder_all_items_view" AS
            SELECT
                ('00' || substr(b.stream_uuid::text, 3))::uuid AS uuid,
                '00000000-0000-0000-0000-000000000000'::uuid AS folder,
                b.project_id,
                b.user_uuid,
                b.stream_uuid,
                NULL::integer                                AS order_index,
                NULL::timestamp                              AS pinned_at,
                CASE
                    WHEN s.private THEN 'private'::varchar
                    ELSE 'stream'::varchar
                END                                          AS chat_type,
                COALESCE(un.unread_count, 0)                 AS unread_count,
                '2000-01-01 00:00:00'::timestamp             AS created_at,
                '2000-01-01 00:00:00'::timestamp             AS updated_at
            FROM "m_workspace_stream_bindings" AS b
            JOIN "m_workspace_streams" AS s
                ON  s.uuid       = b.stream_uuid
                AND s.project_id = b.project_id
            LEFT JOIN "m_unread_user_messages" AS un
                ON  un.uuid       = b.stream_uuid
                AND un.user_uuid  = b.user_uuid
                AND un.project_id = b.project_id;
            """
        )

    def downgrade(self, session):
        session.execute('DROP VIEW IF EXISTS "m_folder_all_items_view";')


migration_step = MigrationStep()
