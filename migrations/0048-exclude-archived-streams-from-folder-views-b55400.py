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


FOLDER_ALL_ITEMS_VIEW_SQL = """
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
    AND un.project_id = b.project_id
WHERE s.is_archived = false;
"""


FOLDER_PRIVATE_ITEMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_folder_private_items_view" AS
SELECT
    ('11' || substr(b.stream_uuid::text, 3))::uuid AS uuid,
    '00000000-0000-0000-0000-000000000001'::uuid AS folder,
    b.project_id,
    b.user_uuid,
    b.stream_uuid,
    NULL::integer                                AS order_index,
    NULL::timestamp                              AS pinned_at,
    'private'::varchar                           AS chat_type,
    COALESCE(un.unread_count, 0)                 AS unread_count,
    '2000-01-01 00:00:01'::timestamp             AS created_at,
    '2000-01-01 00:00:01'::timestamp             AS updated_at
FROM "m_workspace_stream_bindings" AS b
JOIN "m_workspace_streams" AS s
    ON  s.uuid       = b.stream_uuid
    AND s.project_id = b.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid       = b.stream_uuid
    AND un.user_uuid  = b.user_uuid
    AND un.project_id = b.project_id
WHERE s.private = true
    AND s.is_archived = false;
"""


FOLDER_CHANNEL_ITEMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_folder_channel_items_view" AS
SELECT
    ('22' || substr(b.stream_uuid::text, 3))::uuid AS uuid,
    '00000000-0000-0000-0000-000000000002'::uuid AS folder,
    b.project_id,
    b.user_uuid,
    b.stream_uuid,
    NULL::integer                                AS order_index,
    NULL::timestamp                              AS pinned_at,
    'stream'::varchar                            AS chat_type,
    COALESCE(un.unread_count, 0)                 AS unread_count,
    '2000-01-01 00:00:02'::timestamp             AS created_at,
    '2000-01-01 00:00:02'::timestamp             AS updated_at
FROM "m_workspace_stream_bindings" AS b
JOIN "m_workspace_streams" AS s
    ON  s.uuid       = b.stream_uuid
    AND s.project_id = b.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid       = b.stream_uuid
    AND un.user_uuid  = b.user_uuid
    AND un.project_id = b.project_id
WHERE s.private = false
    AND s.is_archived = false;
"""


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
JOIN "m_workspace_streams" AS s
    ON  s.uuid        = fi.stream_uuid
    AND s.project_id  = fi.project_id
    AND s.is_archived = false
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid       = fi.stream_uuid
    AND un.user_uuid  = fi.user_uuid
    AND un.project_id = fi.project_id;
"""


PREVIOUS_FOLDER_ALL_ITEMS_VIEW_SQL = """
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


PREVIOUS_FOLDER_PRIVATE_ITEMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_folder_private_items_view" AS
SELECT
    ('11' || substr(b.stream_uuid::text, 3))::uuid AS uuid,
    '00000000-0000-0000-0000-000000000001'::uuid AS folder,
    b.project_id,
    b.user_uuid,
    b.stream_uuid,
    NULL::integer                                AS order_index,
    NULL::timestamp                              AS pinned_at,
    'private'::varchar                           AS chat_type,
    COALESCE(un.unread_count, 0)                 AS unread_count,
    '2000-01-01 00:00:01'::timestamp             AS created_at,
    '2000-01-01 00:00:01'::timestamp             AS updated_at
FROM "m_workspace_stream_bindings" AS b
JOIN "m_workspace_streams" AS s
    ON  s.uuid       = b.stream_uuid
    AND s.project_id = b.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid       = b.stream_uuid
    AND un.user_uuid  = b.user_uuid
    AND un.project_id = b.project_id
WHERE s.private = true;
"""


PREVIOUS_FOLDER_CHANNEL_ITEMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_folder_channel_items_view" AS
SELECT
    ('22' || substr(b.stream_uuid::text, 3))::uuid AS uuid,
    '00000000-0000-0000-0000-000000000002'::uuid AS folder,
    b.project_id,
    b.user_uuid,
    b.stream_uuid,
    NULL::integer                                AS order_index,
    NULL::timestamp                              AS pinned_at,
    'stream'::varchar                            AS chat_type,
    COALESCE(un.unread_count, 0)                 AS unread_count,
    '2000-01-01 00:00:02'::timestamp             AS created_at,
    '2000-01-01 00:00:02'::timestamp             AS updated_at
FROM "m_workspace_stream_bindings" AS b
JOIN "m_workspace_streams" AS s
    ON  s.uuid       = b.stream_uuid
    AND s.project_id = b.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid       = b.stream_uuid
    AND un.user_uuid  = b.user_uuid
    AND un.project_id = b.project_id
WHERE s.private = false;
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
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid       = fi.stream_uuid
    AND un.user_uuid  = fi.user_uuid
    AND un.project_id = fi.project_id;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0047-add-stream-is-archived-2def0d.py"]

    @property
    def migration_id(self):
        return "b55400f1-4536-4bf3-9888-85b68a208bdc"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            FOLDER_ALL_ITEMS_VIEW_SQL,
            FOLDER_PRIVATE_ITEMS_VIEW_SQL,
            FOLDER_CHANNEL_ITEMS_VIEW_SQL,
            FOLDER_ITEMS_CREATED_VIEW_SQL,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            PREVIOUS_FOLDER_ALL_ITEMS_VIEW_SQL,
            PREVIOUS_FOLDER_PRIVATE_ITEMS_VIEW_SQL,
            PREVIOUS_FOLDER_CHANNEL_ITEMS_VIEW_SQL,
            PREVIOUS_FOLDER_ITEMS_CREATED_VIEW_SQL,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
