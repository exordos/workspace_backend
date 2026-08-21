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
EXTERNAL_CHAT_PROJECTION_INDEX = "m_external_chats_v2_selected_projection_idx"


UNREAD_MESSAGE_BASE_VIEW_SQL = """
CREATE VIEW "m_workspace_user_unread_messages_base_v1" AS
SELECT
    m.uuid AS message_uuid,
    m.stream_uuid,
    m.topic_uuid,
    binding.user_uuid,
    m.project_id,
    POSITION(
        '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
        IN LOWER(COALESCE(m.payload->>'content', ''))
    ) > 0 AS mentioned,
    CASE COALESCE(topic_flags.notification_mode, 'default')
        WHEN 'mute' THEN FALSE
        WHEN 'follow' THEN TRUE
        WHEN 'unmute' THEN POSITION(
            '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
            IN LOWER(COALESCE(m.payload->>'content', ''))
        ) > 0
        ELSE CASE binding.notification_mode
            WHEN 'all_messages' THEN TRUE
            WHEN 'mentions_only' THEN POSITION(
                '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
                IN LOWER(COALESCE(m.payload->>'content', ''))
            ) > 0
            ELSE FALSE
        END
    END AS active
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS binding
    ON binding.stream_uuid = m.stream_uuid
    AND binding.project_id = m.project_id
JOIN "m_workspace_user_message_flags" AS message_flags
    ON message_flags.uuid = m.uuid
    AND message_flags.project_id = m.project_id
    AND message_flags.user_uuid = binding.user_uuid
LEFT JOIN "m_workspace_user_topic_flags" AS topic_flags
    ON topic_flags.uuid = m.topic_uuid
    AND topic_flags.project_id = m.project_id
    AND topic_flags.user_uuid = binding.user_uuid
WHERE message_flags.read = FALSE;
"""


PROTECTED_UNREAD_MESSAGE_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_unread_messages_view" AS
SELECT unread.*
FROM "m_workspace_user_unread_messages_base_v1" AS unread
JOIN "m_workspace_streams" AS stream
    ON stream.uuid = unread.stream_uuid
    AND stream.project_id = unread.project_id
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON access.project_id = unread.project_id
    AND access.user_uuid = unread.user_uuid
    AND access.stream_uuid = unread.stream_uuid
WHERE stream.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


PREVIOUS_PROTECTED_UNREAD_MESSAGE_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_unread_messages_view" AS
SELECT
    m.uuid AS message_uuid,
    m.stream_uuid,
    m.topic_uuid,
    binding.user_uuid,
    m.project_id,
    POSITION(
        '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
        IN LOWER(COALESCE(m.payload->>'content', ''))
    ) > 0 AS mentioned,
    CASE COALESCE(topic_flags.notification_mode, 'default')
        WHEN 'mute' THEN FALSE
        WHEN 'follow' THEN TRUE
        WHEN 'unmute' THEN POSITION(
            '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
            IN LOWER(COALESCE(m.payload->>'content', ''))
        ) > 0
        ELSE CASE binding.notification_mode
            WHEN 'all_messages' THEN TRUE
            WHEN 'mentions_only' THEN POSITION(
                '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
                IN LOWER(COALESCE(m.payload->>'content', ''))
            ) > 0
            ELSE FALSE
        END
    END AS active
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS binding
    ON binding.stream_uuid = m.stream_uuid
    AND binding.project_id = m.project_id
JOIN "m_workspace_streams" AS stream
    ON stream.uuid = m.stream_uuid
    AND stream.project_id = m.project_id
JOIN "m_workspace_user_message_flags" AS message_flags
    ON message_flags.uuid = m.uuid
    AND message_flags.project_id = m.project_id
    AND message_flags.user_uuid = binding.user_uuid
LEFT JOIN "m_workspace_user_topic_flags" AS topic_flags
    ON topic_flags.uuid = m.topic_uuid
    AND topic_flags.project_id = m.project_id
    AND topic_flags.user_uuid = binding.user_uuid
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON access.project_id = m.project_id
    AND access.user_uuid = binding.user_uuid
    AND access.stream_uuid = m.stream_uuid
WHERE message_flags.read = FALSE
  AND (stream.source_name = 'native' OR access.user_uuid IS NOT NULL);
"""


def _user_streams_view_sql(*, reuse_outer_visibility):
    last_message_source = (
        '"m_workspace_messages"'
        if reuse_outer_visibility
        else '"m_workspace_user_messages_view"'
    )
    last_message_user_filter = (
        "" if reuse_outer_visibility else "\n      AND m.user_uuid = b.user_uuid"
    )
    unread_source = (
        '"m_workspace_user_unread_messages_base_v1"'
        if reuse_outer_visibility
        else '"m_workspace_user_unread_messages_view"'
    )
    return f"""
CREATE OR REPLACE VIEW "m_workspace_user_streams" AS
SELECT
    s.uuid,
    CASE
        WHEN s.private THEN
            COALESCE(
                NULLIF(
                    TRIM(
                        COALESCE(peer_user.first_name, '') || ' ' ||
                        COALESCE(peer_user.last_name, '')
                    ),
                    ''
                ),
                peer_user.username,
                s.name
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
    s.updated_at,
    CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        ELSE NULL
    END AS direct_user_uuid,
    s.private_index,
    s.is_archived,
    b.notification_mode,
    s.color,
    last_message.uuid AS last_message_uuid,
    s.default_topic_uuid,
    COALESCE(un.active_unread_count, 0) AS active_unread_count,
    COALESCE(un.passive_unread_count, 0) AS passive_unread_count
FROM "m_workspace_streams" AS s
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = s.uuid
    AND b.project_id = s.project_id
LEFT JOIN (
    SELECT
        unread.stream_uuid AS uuid,
        unread.user_uuid,
        unread.project_id,
        COUNT(*) AS unread_count,
        COUNT(*) FILTER (WHERE unread.active)::integer AS active_unread_count,
        COUNT(*) FILTER (WHERE NOT unread.active)::integer
            AS passive_unread_count
    FROM {unread_source} AS unread
    GROUP BY unread.stream_uuid, unread.user_uuid, unread.project_id
) AS un
    ON un.uuid = s.uuid
    AND un.user_uuid = b.user_uuid
    AND un.project_id = s.project_id
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM {last_message_source} AS m
    WHERE m.project_id = s.project_id
      AND m.stream_uuid = s.uuid{last_message_user_filter}
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_users" AS peer_user
    ON peer_user.uuid = CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        WHEN s.private AND s.user_uuid <> b.user_uuid THEN s.user_uuid
        ELSE NULL
    END
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON access.project_id = s.project_id
    AND access.user_uuid = b.user_uuid
    AND access.stream_uuid = s.uuid
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


def _user_topics_view_sql(*, reuse_outer_visibility):
    last_message_source = (
        '"m_workspace_messages"'
        if reuse_outer_visibility
        else '"m_workspace_user_messages_view"'
    )
    last_message_user_filter = (
        "" if reuse_outer_visibility else "\n      AND m.user_uuid = b.user_uuid"
    )
    unread_source = (
        '"m_workspace_user_unread_messages_base_v1"'
        if reuse_outer_visibility
        else '"m_workspace_user_unread_messages_view"'
    )
    return f"""
CREATE OR REPLACE VIEW "m_workspace_user_topics_view" AS
SELECT
    t.uuid,
    t.name,
    t.stream_uuid,
    t.project_id,
    t.created_at,
    t.updated_at,
    (t.uuid = s.default_topic_uuid) AS is_default,
    b.user_uuid,
    COALESCE(uc.unread_count, 0) AS unread_count,
    COALESCE(f.is_done, FALSE) AS is_done,
    COALESCE(f.notification_mode, 'default') AS notification_mode,
    t.color,
    last_message.uuid AS last_message_uuid,
    t.source_name,
    t.source,
    t.summary,
    t.summary_last_message_uuid,
    CASE
        WHEN t.summary IS NULL THEN NULL
        ELSE t.summary_last_message_uuid IS DISTINCT FROM last_message.uuid
    END AS summary_has_new_messages,
    t.summary_system_prompt,
    t.summary_reasoning_effort,
    t.summary_enabled,
    COALESCE(uc.active_unread_count, 0) AS active_unread_count,
    COALESCE(uc.passive_unread_count, 0) AS passive_unread_count
FROM "m_workspace_stream_topics" AS t
JOIN "m_workspace_streams" AS s
    ON s.uuid = t.stream_uuid
    AND s.project_id = t.project_id
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = t.stream_uuid
    AND b.project_id = t.project_id
LEFT JOIN (
    SELECT
        unread.stream_uuid,
        unread.topic_uuid,
        unread.user_uuid,
        unread.project_id,
        COUNT(*) AS unread_count,
        COUNT(*) FILTER (WHERE unread.active)::integer
            AS active_unread_count,
        COUNT(*) FILTER (WHERE NOT unread.active)::integer
            AS passive_unread_count
    FROM {unread_source} AS unread
    WHERE unread.topic_uuid IS NOT NULL
    GROUP BY
        unread.stream_uuid,
        unread.topic_uuid,
        unread.user_uuid,
        unread.project_id
) AS uc
    ON uc.topic_uuid = t.uuid
    AND uc.stream_uuid = t.stream_uuid
    AND uc.user_uuid = b.user_uuid
    AND uc.project_id = t.project_id
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM {last_message_source} AS m
    WHERE m.project_id = t.project_id
      AND m.topic_uuid = t.uuid{last_message_user_filter}
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_user_topic_flags" AS f
    ON f.uuid = t.uuid
    AND f.user_uuid = b.user_uuid
    AND f.project_id = t.project_id
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON access.project_id = t.project_id
    AND access.user_uuid = b.user_uuid
    AND access.stream_uuid = t.stream_uuid
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


FOLDERS_VIEW_SQL = f"""
CREATE OR REPLACE VIEW "m_folders_view" AS
WITH project_users AS (
    SELECT project_id, user_uuid
    FROM "m_workspace_stream_bindings"
    UNION
    SELECT project_id, owner_user_uuid AS user_uuid
    FROM "m_external_chats_v2"
    WHERE selected AND project_id IS NOT NULL
    UNION
    SELECT project_id, user_uuid
    FROM "m_folders"
    WHERE project_id != '{ZERO_UUID}'::uuid
      AND user_uuid != '{ZERO_UUID}'::uuid
), system_folder_templates AS (
    SELECT *
    FROM (
        VALUES
            (
                '00000000-0000-0000-0000-000000000000'::uuid,
                '00'::text,
                'All chats'::varchar,
                '2000-01-01 00:00:00'::timestamp,
                NULL::boolean,
                NULL::varchar
            ),
            (
                '00000000-0000-0000-0000-000000000001'::uuid,
                '11'::text,
                'Personal'::varchar,
                '2000-01-01 00:00:01'::timestamp,
                TRUE,
                'private'::varchar
            ),
            (
                '00000000-0000-0000-0000-000000000002'::uuid,
                '22'::text,
                'Channels'::varchar,
                '2000-01-01 00:00:02'::timestamp,
                FALSE,
                'stream'::varchar
            )
    ) AS template(
        uuid, item_uuid_prefix, title, created_at, private_filter, chat_type
    )
), folder_definitions AS (
    SELECT
        template.uuid,
        users.project_id,
        users.user_uuid,
        template.title,
        11184810::bigint AS background_color_value,
        'all'::varchar AS system_type,
        template.created_at,
        template.created_at AS updated_at,
        TRUE AS system_folder,
        template.item_uuid_prefix,
        template.private_filter,
        template.chat_type
    FROM project_users AS users
    CROSS JOIN system_folder_templates AS template
    UNION ALL
    SELECT
        folder.uuid,
        folder.project_id,
        folder.user_uuid,
        folder.title,
        folder.background_color_value,
        folder.system_type,
        folder.created_at,
        folder.updated_at,
        FALSE AS system_folder,
        NULL::text AS item_uuid_prefix,
        NULL::boolean AS private_filter,
        NULL::varchar AS chat_type
    FROM "m_folders" AS folder
), visible_streams AS (
    SELECT *
    FROM "m_workspace_user_streams"
    WHERE is_archived = FALSE
), folder_items AS (
    SELECT
        definition.uuid AS folder_uuid,
        definition.project_id,
        definition.user_uuid,
        definition.system_folder,
        CASE
            WHEN definition.system_folder THEN (
                definition.item_uuid_prefix || substr(stream.uuid::text, 3)
            )::uuid
            ELSE item.uuid
        END AS uuid,
        stream.uuid AS stream_uuid,
        item.order_index,
        item.pinned_at::timestamp AS pinned_at,
        CASE
            WHEN definition.system_folder THEN COALESCE(
                definition.chat_type,
                CASE
                    WHEN stream.private THEN 'private'::varchar
                    ELSE 'stream'::varchar
                END
            )
            ELSE item.chat_type
        END AS chat_type,
        COALESCE(stream.unread_count, 0) AS unread_count,
        COALESCE(stream.active_unread_count, 0) AS active_unread_count,
        COALESCE(stream.passive_unread_count, 0) AS passive_unread_count,
        CASE
            WHEN definition.system_folder THEN
                COALESCE(item.created_at::timestamp, definition.created_at)
            ELSE item.created_at::timestamp
        END AS created_at,
        CASE
            WHEN definition.system_folder THEN
                COALESCE(item.updated_at::timestamp, definition.created_at)
            ELSE item.updated_at::timestamp
        END AS updated_at
    FROM folder_definitions AS definition
    JOIN visible_streams AS stream
      ON stream.project_id = definition.project_id
     AND stream.user_uuid = definition.user_uuid
    LEFT JOIN "m_folder_items" AS item
      ON item.folder_uuid = definition.uuid
     AND item.stream_uuid = stream.uuid
     AND item.project_id = definition.project_id
     AND item.user_uuid = definition.user_uuid
    WHERE (
            definition.system_folder
            AND (
                definition.private_filter IS NULL
                OR stream.private = definition.private_filter
            )
          )
       OR (NOT definition.system_folder AND item.uuid IS NOT NULL)
)
SELECT
    definition.uuid,
    definition.project_id,
    definition.user_uuid,
    definition.title,
    definition.background_color_value,
    definition.system_type,
    COALESCE(SUM(item.active_unread_count), 0)::integer AS unread_count,
    COALESCE(
        json_agg(
            CASE
                WHEN item.system_folder THEN json_build_object(
                    'uuid', item.uuid,
                    'folder', item.folder_uuid,
                    'project_id', item.project_id,
                    'user_uuid', item.user_uuid,
                    'stream_uuid', item.stream_uuid,
                    'order_index', item.order_index,
                    'pinned_at', item.pinned_at,
                    'chat_type', item.chat_type,
                    'unread_count', item.unread_count,
                    'active_unread_count', item.active_unread_count,
                    'passive_unread_count', item.passive_unread_count,
                    'created_at', item.created_at,
                    'updated_at', item.updated_at
                )
                ELSE json_build_object(
                    'uuid', item.uuid,
                    'folder_uuid', item.folder_uuid,
                    'project_id', item.project_id,
                    'user_uuid', item.user_uuid,
                    'stream_uuid', item.stream_uuid,
                    'order_index', item.order_index,
                    'pinned_at', item.pinned_at,
                    'chat_type', item.chat_type,
                    'unread_count', item.unread_count,
                    'active_unread_count', item.active_unread_count,
                    'passive_unread_count', item.passive_unread_count,
                    'created_at', item.created_at,
                    'updated_at', item.updated_at
                )
            END
            ORDER BY item.created_at, item.uuid
        ) FILTER (WHERE item.uuid IS NOT NULL),
        '[]'::json
    ) AS folder_items,
    definition.created_at,
    definition.updated_at
FROM folder_definitions AS definition
LEFT JOIN folder_items AS item
  ON item.folder_uuid = definition.uuid
 AND item.project_id = definition.project_id
 AND item.user_uuid = definition.user_uuid
GROUP BY
    definition.uuid,
    definition.project_id,
    definition.user_uuid,
    definition.title,
    definition.background_color_value,
    definition.system_type,
    definition.created_at,
    definition.updated_at;
"""


PREVIOUS_FOLDERS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_folders_view" AS
SELECT * FROM "m_folder_all_view"
UNION ALL
SELECT * FROM "m_folder_channels_view"
UNION ALL
SELECT * FROM "m_folder_personal_view"
UNION ALL
SELECT * FROM "m_folder_created_view";
"""


CREATE_EXTERNAL_CHAT_PROJECTION_INDEX_SQL = f"""
CREATE INDEX CONCURRENTLY IF NOT EXISTS "{EXTERNAL_CHAT_PROJECTION_INDEX}"
ON "m_external_chats_v2" (
    "project_id", "projection_stream_uuid", "external_account_uuid"
)
INCLUDE ("provider_chat_id")
WHERE "selected"
  AND "project_id" IS NOT NULL
  AND "projection_stream_uuid" IS NOT NULL;
"""


def _run_online_projection_index_ddl(session, *, create):
    # Keep external chat projection writes available while a large installation
    # builds this supporting index. PostgreSQL concurrent index DDL must run
    # outside the migration transaction.
    session.commit()
    connection = session.engine.get_connection()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            if create:
                cursor.execute(
                    """
                    SELECT target_index.indisvalid
                    FROM pg_index AS target_index
                    WHERE target_index.indexrelid = to_regclass(%s)
                    """,
                    (EXTERNAL_CHAT_PROJECTION_INDEX,),
                )
                row = cursor.fetchone()
                if row is not None and not row[0]:
                    cursor.execute(
                        f"DROP INDEX CONCURRENTLY IF EXISTS "
                        f'"{EXTERNAL_CHAT_PROJECTION_INDEX}"'
                    )
                cursor.execute(CREATE_EXTERNAL_CHAT_PROJECTION_INDEX_SQL)
            else:
                cursor.execute(
                    f"DROP INDEX CONCURRENTLY IF EXISTS "
                    f'"{EXTERNAL_CHAT_PROJECTION_INDEX}"'
                )
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0131-index-reusable-external-file-content-0bb3ca.py"]

    @property
    def migration_id(self):
        return "93849688-bd14-40b1-8703-12e5ebe13e6b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _run_online_projection_index_ddl(session, create=True)
        session.execute(UNREAD_MESSAGE_BASE_VIEW_SQL)
        session.execute(PROTECTED_UNREAD_MESSAGE_VIEW_SQL)
        session.execute(_user_streams_view_sql(reuse_outer_visibility=True))
        session.execute(_user_topics_view_sql(reuse_outer_visibility=True))
        session.execute(FOLDERS_VIEW_SQL)

    def downgrade(self, session):
        session.execute(PREVIOUS_FOLDERS_VIEW_SQL)
        session.execute(_user_topics_view_sql(reuse_outer_visibility=False))
        session.execute(_user_streams_view_sql(reuse_outer_visibility=False))
        session.execute(PREVIOUS_PROTECTED_UNREAD_MESSAGE_VIEW_SQL)
        session.execute('DROP VIEW "m_workspace_user_unread_messages_base_v1";')
        _run_online_projection_index_ddl(session, create=False)


migration_step = MigrationStep()
