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


ZERO_UUID = "00000000-0000-0000-0000-000000000000"
ALL_CHATS_FOLDER_UUID = "00000000-0000-0000-0000-000000000000"
PERSONAL_FOLDER_UUID = "00000000-0000-0000-0000-000000000001"
CHANNELS_FOLDER_UUID = "00000000-0000-0000-0000-000000000002"


def _background_color_sql(value):
    if value is None:
        return "NULL::bigint"
    return f"{value}::bigint"


def _system_folder_view_sql(view, item_view, folder_uuid, title,
                            background_color_value, created_at):
    return f"""
            CREATE OR REPLACE VIEW "{view}" AS
            WITH project_users AS (
                SELECT project_id, user_uuid
                FROM "m_workspace_stream_bindings"
                UNION
                SELECT project_id, user_uuid
                FROM "m_external_accounts"
                UNION
                SELECT project_id, user_uuid
                FROM "m_folders"
                WHERE project_id != '{ZERO_UUID}'::uuid
                    AND user_uuid != '{ZERO_UUID}'::uuid
            )
            SELECT
                '{folder_uuid}'::uuid                         AS uuid,
                pu.project_id,
                pu.user_uuid,
                '{title}'::varchar                            AS title,
                {_background_color_sql(background_color_value)}
                                                               AS background_color_value,
                'all'::varchar                                AS system_type,
                COALESCE(SUM(i.unread_count), 0)::integer     AS unread_count,
                COALESCE(
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
                        ORDER BY i.created_at, i.uuid
                    ) FILTER (WHERE i.uuid IS NOT NULL),
                    '[]'::json
                )                                             AS folder_items,
                '{created_at}'::timestamp                     AS created_at,
                '{created_at}'::timestamp                     AS updated_at
            FROM project_users AS pu
            LEFT JOIN "{item_view}" AS i
                ON  i.project_id = pu.project_id
                AND i.user_uuid = pu.user_uuid
            GROUP BY pu.project_id, pu.user_uuid;
            """


def _previous_system_folder_view_sql(view, item_view, folder_uuid, title,
                                     background_color_value, created_at):
    return f"""
            CREATE OR REPLACE VIEW "{view}" AS
            SELECT
                '{folder_uuid}'::uuid                         AS uuid,
                i.project_id,
                i.user_uuid,
                '{title}'::varchar                            AS title,
                {_background_color_sql(background_color_value)}
                                                               AS background_color_value,
                'all'::varchar                                AS system_type,
                COALESCE(SUM(i.unread_count), 0)::integer     AS unread_count,
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
                )                                             AS folder_items,
                '{created_at}'::timestamp                     AS created_at,
                '{created_at}'::timestamp                     AS updated_at
            FROM "{item_view}" AS i
            GROUP BY i.project_id, i.user_uuid;
            """


SYSTEM_FOLDER_VIEWS = (
    (
        "m_folder_all_view",
        "m_folder_all_items_view",
        ALL_CHATS_FOLDER_UUID,
        "All chats",
        11184810,
        "2000-01-01 00:00:00",
    ),
    (
        "m_folder_personal_view",
        "m_folder_private_items_view",
        PERSONAL_FOLDER_UUID,
        "Personal",
        None,
        "2000-01-01 00:00:01",
    ),
    (
        "m_folder_channels_view",
        "m_folder_channel_items_view",
        CHANNELS_FOLDER_UUID,
        "Channels",
        None,
        "2000-01-01 00:00:02",
    ),
)


FOLDER_ITEMS_CREATED_VIEW_SQL = """
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
            JOIN "m_workspace_stream_bindings" AS b
                ON  b.stream_uuid = fi.stream_uuid
                AND b.project_id = fi.project_id
                AND b.user_uuid = fi.user_uuid
            JOIN "m_workspace_streams" AS s
                ON  s.uuid = fi.stream_uuid
                AND s.project_id = fi.project_id
                AND s.is_archived = false
            LEFT JOIN "m_unread_user_messages" AS un
                ON  un.uuid = fi.stream_uuid
                AND un.user_uuid = fi.user_uuid
                AND un.project_id = fi.project_id;
            """


PREVIOUS_FOLDER_ITEMS_CREATED_VIEW_SQL = """
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
            JOIN "m_workspace_streams" AS s
                ON  s.uuid = fi.stream_uuid
                AND s.project_id = fi.project_id
                AND s.is_archived = false
            LEFT JOIN "m_unread_user_messages" AS un
                ON  un.uuid = fi.stream_uuid
                AND un.user_uuid = fi.user_uuid
                AND un.project_id = fi.project_id;
            """


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0078-drop-workspace-iam-users-9486e9.py"]

    @property
    def migration_id(self):
        return "c57845f4-1514-4344-912f-db738308c5d5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(FOLDER_ITEMS_CREATED_VIEW_SQL)
        for args in SYSTEM_FOLDER_VIEWS:
            session.execute(_system_folder_view_sql(*args))

    def downgrade(self, session):
        for args in SYSTEM_FOLDER_VIEWS:
            session.execute(_previous_system_folder_view_sql(*args))
        session.execute(PREVIOUS_FOLDER_ITEMS_CREATED_VIEW_SQL)


migration_step = MigrationStep()
