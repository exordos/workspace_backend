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


USER_UNREAD_MESSAGES_VIEW_SQL = """
CREATE VIEW "m_workspace_user_unread_messages_view" AS
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
    ON  binding.stream_uuid = m.stream_uuid
    AND binding.project_id = m.project_id
JOIN "m_workspace_streams" AS stream
    ON  stream.uuid = m.stream_uuid
    AND stream.project_id = m.project_id
JOIN "m_workspace_user_message_flags" AS message_flags
    ON message_flags.uuid = m.uuid
    AND message_flags.project_id = m.project_id
    AND message_flags.user_uuid = binding.user_uuid
LEFT JOIN "m_workspace_user_topic_flags" AS topic_flags
    ON  topic_flags.uuid = m.topic_uuid
    AND topic_flags.project_id = m.project_id
    AND topic_flags.user_uuid = binding.user_uuid
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON access.project_id = m.project_id
    AND access.user_uuid = binding.user_uuid
    AND access.stream_uuid = m.stream_uuid
WHERE message_flags.read = FALSE
  AND (stream.source_name = 'native' OR access.user_uuid IS NOT NULL);
"""


UNREAD_USER_MESSAGES_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_unread_user_messages" AS
SELECT
    unread.stream_uuid AS uuid,
    unread.user_uuid,
    unread.project_id,
    COUNT(*) AS unread_count,
    COUNT(*) FILTER (WHERE unread.active)::integer AS active_unread_count,
    COUNT(*) FILTER (WHERE NOT unread.active)::integer AS passive_unread_count
FROM "m_workspace_user_unread_messages_view" AS unread
GROUP BY unread.stream_uuid, unread.user_uuid, unread.project_id;
"""


PREVIOUS_UNREAD_USER_MESSAGES_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_unread_user_messages" AS
SELECT
    m.stream_uuid AS uuid,
    f.user_uuid,
    f.project_id,
    COUNT(*) AS unread_count
FROM "m_workspace_user_message_flags" AS f
JOIN "m_workspace_messages" AS m
    ON  m.uuid = f.uuid
    AND m.project_id = f.project_id
JOIN "m_workspace_streams" AS stream
    ON  stream.uuid = m.stream_uuid
    AND stream.project_id = m.project_id
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON  access.project_id = m.project_id
    AND access.user_uuid = f.user_uuid
    AND access.stream_uuid = m.stream_uuid
WHERE f.read = FALSE
  AND (stream.source_name = 'native' OR access.user_uuid IS NOT NULL)
GROUP BY m.stream_uuid, f.user_uuid, f.project_id;
"""


def _user_streams_view_sql(with_split_counters):
    split_counters = (
        """,
    COALESCE(un.active_unread_count, 0) AS active_unread_count,
    COALESCE(un.passive_unread_count, 0) AS passive_unread_count"""
        if with_split_counters
        else ""
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
    s.default_topic_uuid{split_counters}
FROM "m_workspace_streams" AS s
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = s.uuid
    AND b.project_id = s.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON un.uuid = s.uuid
    AND un.user_uuid = b.user_uuid
    AND un.project_id = s.project_id
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM "m_workspace_user_messages_view" AS m
    WHERE m.project_id = s.project_id
      AND m.stream_uuid = s.uuid
      AND m.user_uuid = b.user_uuid
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


def _user_topics_view_sql(with_split_counters):
    split_counts = (
        """,
        COUNT(*) FILTER (WHERE unread.active)::integer
            AS active_unread_count,
        COUNT(*) FILTER (WHERE NOT unread.active)::integer
            AS passive_unread_count"""
        if with_split_counters
        else ""
    )
    selected_split_counts = (
        """,
    COALESCE(uc.active_unread_count, 0) AS active_unread_count,
    COALESCE(uc.passive_unread_count, 0) AS passive_unread_count"""
        if with_split_counters
        else ""
    )
    unread_source = (
        '"m_workspace_user_unread_messages_view" AS unread'
        if with_split_counters
        else '"m_workspace_user_messages_view" AS unread'
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
    t.summary_enabled{selected_split_counts}
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
        COUNT(*) AS unread_count{split_counts}
    FROM {unread_source}
    WHERE unread.topic_uuid IS NOT NULL
      {"" if with_split_counters else "AND unread.read = FALSE"}
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
    FROM "m_workspace_user_messages_view" AS m
    WHERE m.project_id = t.project_id
      AND m.topic_uuid = t.uuid
      AND m.user_uuid = b.user_uuid
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


def _folder_items_created_view_sql(with_split_counters):
    split_counters = (
        """,
    COALESCE(s.active_unread_count, 0) AS active_unread_count,
    COALESCE(s.passive_unread_count, 0) AS passive_unread_count"""
        if with_split_counters
        else ""
    )
    return f"""
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
    COALESCE(s.unread_count, 0) AS unread_count,
    fi.created_at,
    fi.updated_at{split_counters}
FROM "m_folder_items" AS fi
JOIN "m_workspace_user_streams" AS s
    ON s.uuid = fi.stream_uuid
    AND s.project_id = fi.project_id
    AND s.user_uuid = fi.user_uuid
    AND s.is_archived = FALSE;
"""


def _system_folder_item_view_sql(
    view,
    item_uuid_prefix,
    folder_uuid,
    created_at,
    private_filter,
    chat_type,
    with_split_counters,
):
    split_counters = (
        """,
    COALESCE(s.active_unread_count, 0) AS active_unread_count,
    COALESCE(s.passive_unread_count, 0) AS passive_unread_count"""
        if with_split_counters
        else ""
    )
    private_filter_sql = (
        f"\n  AND s.private = {'TRUE' if private_filter else 'FALSE'}"
        if private_filter is not None
        else ""
    )
    chat_type_sql = (
        "CASE WHEN s.private THEN 'private'::varchar ELSE 'stream'::varchar END"
        if chat_type is None
        else f"'{chat_type}'::varchar"
    )
    return f"""
CREATE OR REPLACE VIEW "{view}" AS
SELECT
    ('{item_uuid_prefix}' || substr(s.uuid::text, 3))::uuid AS uuid,
    '{folder_uuid}'::uuid AS folder,
    s.project_id,
    s.user_uuid,
    s.uuid AS stream_uuid,
    fi.order_index,
    fi.pinned_at::timestamp AS pinned_at,
    {chat_type_sql} AS chat_type,
    COALESCE(s.unread_count, 0) AS unread_count,
    COALESCE(fi.created_at::timestamp, '{created_at}'::timestamp) AS created_at,
    COALESCE(fi.updated_at::timestamp, '{created_at}'::timestamp) AS updated_at{split_counters}
FROM "m_workspace_user_streams" AS s
LEFT JOIN "m_folder_items" AS fi
    ON fi.folder_uuid = '{folder_uuid}'::uuid
    AND fi.stream_uuid = s.uuid
    AND fi.project_id = s.project_id
    AND fi.user_uuid = s.user_uuid
WHERE s.is_archived = FALSE{private_filter_sql};
"""


SYSTEM_FOLDER_ITEMS = (
    (
        "m_folder_all_items_view",
        "00",
        "00000000-0000-0000-0000-000000000000",
        "2000-01-01 00:00:00",
        None,
        None,
    ),
    (
        "m_folder_private_items_view",
        "11",
        "00000000-0000-0000-0000-000000000001",
        "2000-01-01 00:00:01",
        True,
        "private",
    ),
    (
        "m_folder_channel_items_view",
        "22",
        "00000000-0000-0000-0000-000000000002",
        "2000-01-01 00:00:02",
        False,
        "stream",
    ),
)


def _folder_created_view_sql(with_split_counters):
    aggregate = "fi.active_unread_count" if with_split_counters else "fi.unread_count"
    split_json = (
        """,
                    'active_unread_count', fi.active_unread_count,
                    'passive_unread_count', fi.passive_unread_count"""
        if with_split_counters
        else ""
    )
    return f"""
CREATE OR REPLACE VIEW "m_folder_created_view" AS
SELECT
    f.uuid,
    f.project_id,
    f.user_uuid,
    f.title,
    f.background_color_value,
    f.system_type,
    COALESCE(SUM(COALESCE({aggregate}, 0)), 0)::integer AS unread_count,
    COALESCE(
        json_agg(
            json_build_object(
                'uuid', fi.uuid,
                'folder_uuid', fi.folder_uuid,
                'project_id', fi.project_id,
                'user_uuid', fi.user_uuid,
                'stream_uuid', fi.stream_uuid,
                'order_index', fi.order_index,
                'pinned_at', fi.pinned_at,
                'chat_type', fi.chat_type,
                'unread_count', fi.unread_count{split_json},
                'created_at', fi.created_at,
                'updated_at', fi.updated_at
            )
        ) FILTER (WHERE fi.uuid IS NOT NULL),
        '[]'::json
    ) AS folder_items,
    f.created_at,
    f.updated_at
FROM "m_folders" AS f
LEFT JOIN "m_folder_items_created_view" AS fi
    ON fi.folder_uuid = f.uuid
    AND fi.user_uuid = f.user_uuid
    AND fi.project_id = f.project_id
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


SYSTEM_FOLDERS = (
    (
        "m_folder_all_view",
        "m_folder_all_items_view",
        "00000000-0000-0000-0000-000000000000",
        "All chats",
        "2000-01-01 00:00:00",
    ),
    (
        "m_folder_personal_view",
        "m_folder_private_items_view",
        "00000000-0000-0000-0000-000000000001",
        "Personal",
        "2000-01-01 00:00:01",
    ),
    (
        "m_folder_channels_view",
        "m_folder_channel_items_view",
        "00000000-0000-0000-0000-000000000002",
        "Channels",
        "2000-01-01 00:00:02",
    ),
)


def _system_folder_view_sql(
    view, item_view, folder_uuid, title, created_at, with_split_counters
):
    aggregate = "i.active_unread_count" if with_split_counters else "i.unread_count"
    split_json = (
        """,
                        'active_unread_count', i.active_unread_count,
                        'passive_unread_count', i.passive_unread_count"""
        if with_split_counters
        else ""
    )
    return f"""
CREATE OR REPLACE VIEW "{view}" AS
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
)
SELECT
    '{folder_uuid}'::uuid AS uuid,
    pu.project_id,
    pu.user_uuid,
    '{title}'::varchar AS title,
    11184810::bigint AS background_color_value,
    'all'::varchar AS system_type,
    COALESCE(SUM({aggregate}), 0)::integer AS unread_count,
    COALESCE(
        json_agg(
            json_build_object(
                'uuid', i.uuid,
                'folder', i.folder,
                'project_id', i.project_id,
                'user_uuid', i.user_uuid,
                'stream_uuid', i.stream_uuid,
                'order_index', i.order_index,
                'pinned_at', i.pinned_at,
                'chat_type', i.chat_type,
                'unread_count', i.unread_count{split_json},
                'created_at', i.created_at,
                'updated_at', i.updated_at
            )
            ORDER BY i.created_at, i.uuid
        ) FILTER (WHERE i.uuid IS NOT NULL),
        '[]'::json
    ) AS folder_items,
    '{created_at}'::timestamp AS created_at,
    '{created_at}'::timestamp AS updated_at
FROM project_users AS pu
LEFT JOIN "{item_view}" AS i
    ON i.project_id = pu.project_id
    AND i.user_uuid = pu.user_uuid
GROUP BY pu.project_id, pu.user_uuid;
"""


FOLDERS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_folders_view" AS
SELECT * FROM "m_folder_all_view"
UNION ALL
SELECT * FROM "m_folder_channels_view"
UNION ALL
SELECT * FROM "m_folder_personal_view"
UNION ALL
SELECT * FROM "m_folder_created_view";
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0129-add-topic-summary-worker-and-LLM-endpoint-registry-22b3a6.py"
        ]

    @property
    def migration_id(self):
        return "36e14b04-23c3-412c-bc87-34a7ccc79d0e"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(USER_UNREAD_MESSAGES_VIEW_SQL)
        session.execute(UNREAD_USER_MESSAGES_VIEW_SQL)
        session.execute(_user_streams_view_sql(with_split_counters=True))
        session.execute(_user_topics_view_sql(with_split_counters=True))
        session.execute(_folder_items_created_view_sql(with_split_counters=True))
        for args in SYSTEM_FOLDER_ITEMS:
            session.execute(
                _system_folder_item_view_sql(*args, with_split_counters=True)
            )
        session.execute(_folder_created_view_sql(with_split_counters=True))
        for args in SYSTEM_FOLDERS:
            session.execute(_system_folder_view_sql(*args, with_split_counters=True))

    def downgrade(self, session):
        for view in (
            "m_folders_view",
            "m_folder_all_view",
            "m_folder_personal_view",
            "m_folder_channels_view",
            "m_folder_created_view",
            "m_folder_all_items_view",
            "m_folder_private_items_view",
            "m_folder_channel_items_view",
            "m_folder_items_created_view",
            "m_workspace_user_topics_view",
            "m_workspace_user_streams",
            "m_unread_user_messages",
            "m_workspace_user_unread_messages_view",
        ):
            session.execute(f'DROP VIEW IF EXISTS "{view}";')

        session.execute(PREVIOUS_UNREAD_USER_MESSAGES_VIEW_SQL)
        session.execute(_user_streams_view_sql(with_split_counters=False))
        session.execute(_user_topics_view_sql(with_split_counters=False))
        session.execute(_folder_items_created_view_sql(with_split_counters=False))
        for args in SYSTEM_FOLDER_ITEMS:
            session.execute(
                _system_folder_item_view_sql(*args, with_split_counters=False)
            )
        session.execute(_folder_created_view_sql(with_split_counters=False))
        for args in SYSTEM_FOLDERS:
            session.execute(_system_folder_view_sql(*args, with_split_counters=False))
        session.execute(FOLDERS_VIEW_SQL)


migration_step = MigrationStep()
