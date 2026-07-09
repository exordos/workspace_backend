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


EXTERNAL_ACCOUNT_ACCESS_STATUSES = (
    "missing_credentials",
    "confirmed",
    "invalid_credentials",
    "unavailable",
)


CONFIRMED_EXTERNAL_ACCOUNT_ACCESS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_confirmed_external_account_access" AS
SELECT
    "project_id",
    "user_uuid",
    "account_type",
    COALESCE("source_scope", "server_url") AS "source_scope"
FROM "m_external_accounts"
WHERE "access_status" = 'confirmed'
  AND "account_settings"->'credentials' IS NOT NULL
  AND "account_settings"->'credentials' <> 'null'::jsonb
  AND COALESCE("source_scope", "server_url") IS NOT NULL;
"""


UNREAD_USER_MESSAGES_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_unread_user_messages" AS
SELECT
    m.stream_uuid                   AS uuid,
    f.user_uuid,
    f.project_id,
    COUNT(*)                        AS unread_count
FROM "m_workspace_user_message_flags" AS f
JOIN "m_workspace_messages" AS m
    ON  m.uuid       = f.uuid
    AND m.project_id = f.project_id
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = m.project_id
    AND access.user_uuid    = f.user_uuid
    AND access.account_type = m.source_name
    AND access.source_scope = COALESCE(
        m.source->>'source_scope',
        m.source->>'server_url'
    )
WHERE f.read = false
  AND (
      m.source_name = 'native'
      OR access.user_uuid IS NOT NULL
  )
GROUP BY m.stream_uuid, f.user_uuid, f.project_id;
"""


USER_MESSAGES_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_messages_view" AS
SELECT
    m.uuid                          AS uuid,
    m.stream_uuid,
    m.user_uuid                     AS author_uuid,
    m.topic_uuid,
    m.payload,
    m.created_at,
    m.updated_at,
    b.user_uuid                     AS user_uuid,
    m.project_id,
    COALESCE(f.read,    FALSE)      AS read,
    COALESCE(f.pinned,  FALSE)      AS pinned,
    COALESCE(f.starred, FALSE)      AS starred,
    (m.user_uuid = b.user_uuid)     AS is_own,
    COALESCE(
        (
            SELECT jsonb_object_agg(
                reaction_counts.emoji_name,
                reaction_counts.reaction_count
            )
            FROM (
                SELECT
                    r.emoji_name,
                    COUNT(*) AS reaction_count
                FROM "m_workspace_message_reactions" AS r
                WHERE r.project_id = m.project_id
                    AND r.message_uuid = m.uuid
                GROUP BY r.emoji_name
            ) AS reaction_counts
        ),
        '{}'::jsonb
    )                               AS reactions,
    m.source_name,
    m.source
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid  = m.stream_uuid
    AND b.project_id   = m.project_id
LEFT JOIN "m_workspace_user_message_flags" AS f
    ON  f.uuid       = m.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = m.project_id
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = m.project_id
    AND access.user_uuid    = b.user_uuid
    AND access.account_type = m.source_name
    AND access.source_scope = COALESCE(
        m.source->>'source_scope',
        m.source->>'server_url'
    )
WHERE m.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


USER_STREAMS_VIEW_SQL = """
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
    last_message.uuid AS last_message_uuid
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
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = s.project_id
    AND access.user_uuid    = b.user_uuid
    AND access.account_type = s.source_name
    AND access.source_scope = COALESCE(
        s.source->>'source_scope',
        s.source->>'server_url'
    )
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


USER_TOPICS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_topics_view" AS
SELECT
    t.uuid,
    t.name,
    t.stream_uuid,
    t.project_id,
    t.created_at,
    t.updated_at,
    (t.default_for_stream_uuid IS NOT NULL) AS is_default,
    b.user_uuid,
    COALESCE(uc.unread_count, 0) AS unread_count,
    COALESCE(f.is_done, FALSE) AS is_done,
    COALESCE(f.notification_mode, 'default') AS notification_mode,
    t.color,
    last_message.uuid AS last_message_uuid,
    t.source_name,
    t.source
FROM "m_workspace_stream_topics" AS t
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid = t.stream_uuid
    AND b.project_id  = t.project_id
LEFT JOIN (
    SELECT
        m.topic_uuid,
        m.user_uuid,
        m.project_id,
        COUNT(*) AS unread_count
    FROM "m_workspace_user_messages_view" AS m
    WHERE m.read = false
      AND m.topic_uuid IS NOT NULL
    GROUP BY m.topic_uuid, m.user_uuid, m.project_id
) AS uc
    ON  uc.topic_uuid = t.uuid
    AND uc.user_uuid  = b.user_uuid
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
    ON  f.uuid       = t.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = t.project_id
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = t.project_id
    AND access.user_uuid    = b.user_uuid
    AND access.account_type = t.source_name
    AND access.source_scope = COALESCE(
        t.source->>'source_scope',
        t.source->>'server_url'
    )
WHERE t.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
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
JOIN "m_workspace_user_streams" AS s
    ON  s.uuid = fi.stream_uuid
    AND s.project_id = fi.project_id
    AND s.user_uuid = fi.user_uuid
    AND s.is_archived = false
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid = fi.stream_uuid
    AND un.user_uuid = fi.user_uuid
    AND un.project_id = fi.project_id;
"""


FOLDER_ALL_ITEMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_folder_all_items_view" AS
SELECT
    ('00' || substr(s.uuid::text, 3))::uuid AS uuid,
    '00000000-0000-0000-0000-000000000000'::uuid AS folder,
    s.project_id,
    s.user_uuid,
    s.uuid                                         AS stream_uuid,
    fi.order_index,
    fi.pinned_at::timestamp                    AS pinned_at,
    CASE
        WHEN s.private THEN 'private'::varchar
        ELSE 'stream'::varchar
    END                                          AS chat_type,
    COALESCE(un.unread_count, 0)                 AS unread_count,
    COALESCE(
        fi.created_at::timestamp,
        '2000-01-01 00:00:00'::timestamp
    )                                            AS created_at,
    COALESCE(
        fi.updated_at::timestamp,
        '2000-01-01 00:00:00'::timestamp
    )                                            AS updated_at
FROM "m_workspace_user_streams" AS s
LEFT JOIN "m_folder_items" AS fi
    ON  fi.folder_uuid = '00000000-0000-0000-0000-000000000000'::uuid
    AND fi.stream_uuid = s.uuid
    AND fi.project_id  = s.project_id
    AND fi.user_uuid   = s.user_uuid
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid       = s.uuid
    AND un.user_uuid  = s.user_uuid
    AND un.project_id = s.project_id
WHERE s.is_archived = false;
"""


FOLDER_PRIVATE_ITEMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_folder_private_items_view" AS
SELECT
    ('11' || substr(s.uuid::text, 3))::uuid AS uuid,
    '00000000-0000-0000-0000-000000000001'::uuid AS folder,
    s.project_id,
    s.user_uuid,
    s.uuid                                         AS stream_uuid,
    fi.order_index,
    fi.pinned_at::timestamp                    AS pinned_at,
    'private'::varchar                           AS chat_type,
    COALESCE(un.unread_count, 0)                 AS unread_count,
    COALESCE(
        fi.created_at::timestamp,
        '2000-01-01 00:00:01'::timestamp
    )                                            AS created_at,
    COALESCE(
        fi.updated_at::timestamp,
        '2000-01-01 00:00:01'::timestamp
    )                                            AS updated_at
FROM "m_workspace_user_streams" AS s
LEFT JOIN "m_folder_items" AS fi
    ON  fi.folder_uuid = '00000000-0000-0000-0000-000000000001'::uuid
    AND fi.stream_uuid = s.uuid
    AND fi.project_id  = s.project_id
    AND fi.user_uuid   = s.user_uuid
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid       = s.uuid
    AND un.user_uuid  = s.user_uuid
    AND un.project_id = s.project_id
WHERE s.private = true
    AND s.is_archived = false;
"""


FOLDER_CHANNEL_ITEMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_folder_channel_items_view" AS
SELECT
    ('22' || substr(s.uuid::text, 3))::uuid AS uuid,
    '00000000-0000-0000-0000-000000000002'::uuid AS folder,
    s.project_id,
    s.user_uuid,
    s.uuid                                         AS stream_uuid,
    fi.order_index,
    fi.pinned_at::timestamp                    AS pinned_at,
    'stream'::varchar                            AS chat_type,
    COALESCE(un.unread_count, 0)                 AS unread_count,
    COALESCE(
        fi.created_at::timestamp,
        '2000-01-01 00:00:02'::timestamp
    )                                            AS created_at,
    COALESCE(
        fi.updated_at::timestamp,
        '2000-01-01 00:00:02'::timestamp
    )                                            AS updated_at
FROM "m_workspace_user_streams" AS s
LEFT JOIN "m_folder_items" AS fi
    ON  fi.folder_uuid = '00000000-0000-0000-0000-000000000002'::uuid
    AND fi.stream_uuid = s.uuid
    AND fi.project_id  = s.project_id
    AND fi.user_uuid   = s.user_uuid
LEFT JOIN "m_unread_user_messages" AS un
    ON  un.uuid       = s.uuid
    AND un.user_uuid  = s.user_uuid
    AND un.project_id = s.project_id
WHERE s.private = false
    AND s.is_archived = false;
"""


WORKSPACE_VISIBLE_EVENTS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_visible_events" AS
SELECT e.*
FROM "m_workspace_events" AS e
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = e.project_id
    AND access.user_uuid    = e.user_uuid
    AND access.account_type = e.payload->>'source_name'
    AND access.source_scope = COALESCE(
        e.payload->'source'->>'source_scope',
        e.payload->'source'->>'server_url'
    )
LEFT JOIN "m_confirmed_external_account_access" AS old_access
    ON  old_access.project_id   = e.project_id
    AND old_access.user_uuid    = e.user_uuid
    AND old_access.account_type = e.payload->>'old_source_name'
    AND old_access.source_scope = COALESCE(
        e.payload->'old_source'->>'source_scope',
        e.payload->'old_source'->>'server_url'
    )
WHERE (
        COALESCE(e.payload->>'source_name', 'native') = 'native'
        OR access.user_uuid IS NOT NULL
    )
  AND (
        e.payload->>'old_source_name' IS NULL
        OR e.payload->>'old_source_name' = 'native'
        OR old_access.user_uuid IS NOT NULL
    );
"""


PREVIOUS_UNREAD_USER_MESSAGES_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_unread_user_messages" AS
SELECT
    m.stream_uuid                   AS uuid,
    f.user_uuid,
    f.project_id,
    COUNT(*)                        AS unread_count
FROM "m_workspace_user_message_flags" AS f
JOIN "m_workspace_messages" AS m
    ON  m.uuid       = f.uuid
    AND m.project_id = f.project_id
WHERE f.read = false
GROUP BY m.stream_uuid, f.user_uuid, f.project_id;
"""


PREVIOUS_USER_MESSAGES_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_messages_view" AS
SELECT
    m.uuid                          AS uuid,
    m.stream_uuid,
    m.user_uuid                     AS author_uuid,
    m.topic_uuid,
    m.payload,
    m.created_at,
    m.updated_at,
    b.user_uuid                     AS user_uuid,
    m.project_id,
    COALESCE(f.read,    FALSE)      AS read,
    COALESCE(f.pinned,  FALSE)      AS pinned,
    COALESCE(f.starred, FALSE)      AS starred,
    (m.user_uuid = b.user_uuid)     AS is_own,
    COALESCE(
        (
            SELECT jsonb_object_agg(
                reaction_counts.emoji_name,
                reaction_counts.reaction_count
            )
            FROM (
                SELECT
                    r.emoji_name,
                    COUNT(*) AS reaction_count
                FROM "m_workspace_message_reactions" AS r
                WHERE r.project_id = m.project_id
                    AND r.message_uuid = m.uuid
                GROUP BY r.emoji_name
            ) AS reaction_counts
        ),
        '{}'::jsonb
    )                               AS reactions,
    m.source_name,
    m.source
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid  = m.stream_uuid
    AND b.project_id   = m.project_id
LEFT JOIN "m_workspace_user_message_flags" AS f
    ON  f.uuid       = m.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = m.project_id;
"""


PREVIOUS_USER_STREAMS_VIEW_SQL = """
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
    last_message.uuid AS last_message_uuid
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
    FROM "m_workspace_messages" AS m
    WHERE m.project_id = s.project_id
      AND m.stream_uuid = s.uuid
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
    END;
"""


PREVIOUS_USER_TOPICS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_topics_view" AS
SELECT
    t.uuid,
    t.name,
    t.stream_uuid,
    t.project_id,
    t.created_at,
    t.updated_at,
    (t.default_for_stream_uuid IS NOT NULL) AS is_default,
    b.user_uuid,
    COALESCE(uc.unread_count, 0) AS unread_count,
    COALESCE(f.is_done, FALSE) AS is_done,
    COALESCE(f.notification_mode, 'default') AS notification_mode,
    t.color,
    last_message.uuid AS last_message_uuid,
    t.source_name,
    t.source
FROM "m_workspace_stream_topics" AS t
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid = t.stream_uuid
    AND b.project_id  = t.project_id
LEFT JOIN (
    SELECT
        m.topic_uuid,
        f.user_uuid,
        f.project_id,
        COUNT(*) AS unread_count
    FROM "m_workspace_user_message_flags" AS f
    JOIN "m_workspace_messages" AS m
        ON  m.uuid       = f.uuid
        AND m.project_id = f.project_id
    WHERE f.read = false
      AND m.topic_uuid IS NOT NULL
    GROUP BY m.topic_uuid, f.user_uuid, f.project_id
) AS uc
    ON  uc.topic_uuid = t.uuid
    AND uc.user_uuid  = b.user_uuid
    AND uc.project_id = t.project_id
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM "m_workspace_messages" AS m
    WHERE m.project_id = t.project_id
      AND m.topic_uuid = t.uuid
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_user_topic_flags" AS f
    ON  f.uuid       = t.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = t.project_id;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0087-add-zulip-history-sync-tasks-d556e0.py"]

    @property
    def migration_id(self):
        return "6ea38864-f5f7-4a2b-9db1-3fa14897504d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_external_accounts"
                ADD COLUMN IF NOT EXISTS "source_scope" VARCHAR(2048),
                ADD COLUMN IF NOT EXISTS "access_status" VARCHAR(32)
                    NOT NULL DEFAULT 'missing_credentials',
                ADD COLUMN IF NOT EXISTS "access_checked_at"
                    TIMESTAMP WITH TIME ZONE,
                ADD COLUMN IF NOT EXISTS "access_confirmed_at"
                    TIMESTAMP WITH TIME ZONE,
                ADD COLUMN IF NOT EXISTS "access_next_check_at"
                    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                ADD COLUMN IF NOT EXISTS "access_last_error" TEXT;
            """,
            """
            ALTER TABLE "m_external_accounts"
                DROP CONSTRAINT IF EXISTS
                    "m_external_accounts_access_status_check",
                ADD CONSTRAINT "m_external_accounts_access_status_check"
                    CHECK (
                        "access_status" IN (
                            'missing_credentials',
                            'confirmed',
                            'invalid_credentials',
                            'unavailable'
                        )
                    );
            """,
            """
            UPDATE "m_external_accounts"
            SET "source_scope" = "server_url"
            WHERE "source_scope" IS NULL;
            """,
            """
            UPDATE "m_external_accounts"
            SET
                "access_status" = CASE
                    WHEN "account_settings"->'credentials' IS NOT NULL
                     AND "account_settings"->'credentials' <> 'null'::jsonb
                    THEN 'confirmed'
                    ELSE 'missing_credentials'
                END,
                "access_checked_at" = NOW(),
                "access_confirmed_at" = CASE
                    WHEN "account_settings"->'credentials' IS NOT NULL
                     AND "account_settings"->'credentials' <> 'null'::jsonb
                    THEN NOW()
                    ELSE NULL
                END,
                "access_last_error" = CASE
                    WHEN "account_settings"->'credentials' IS NOT NULL
                     AND "account_settings"->'credentials' <> 'null'::jsonb
                    THEN NULL
                    ELSE 'External account credentials are missing'
                END;
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_external_accounts_access_check_idx"
                ON "m_external_accounts"
                    ("access_next_check_at", "account_type", "access_status");
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_external_accounts_access_gate_idx"
                ON "m_external_accounts"
                    (
                        "project_id",
                        "user_uuid",
                        "account_type",
                        COALESCE("source_scope", "server_url")
                    )
                WHERE "access_status" = 'confirmed';
            """,
            CONFIRMED_EXTERNAL_ACCOUNT_ACCESS_VIEW_SQL,
            UNREAD_USER_MESSAGES_VIEW_SQL,
            USER_MESSAGES_VIEW_SQL,
            USER_STREAMS_VIEW_SQL,
            USER_TOPICS_VIEW_SQL,
            FOLDER_ITEMS_CREATED_VIEW_SQL,
            FOLDER_ALL_ITEMS_VIEW_SQL,
            FOLDER_PRIVATE_ITEMS_VIEW_SQL,
            FOLDER_CHANNEL_ITEMS_VIEW_SQL,
            WORKSPACE_VISIBLE_EVENTS_VIEW_SQL,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            'DROP VIEW IF EXISTS "m_workspace_visible_events";',
            PREVIOUS_UNREAD_USER_MESSAGES_VIEW_SQL,
            PREVIOUS_USER_MESSAGES_VIEW_SQL,
            PREVIOUS_USER_STREAMS_VIEW_SQL,
            PREVIOUS_USER_TOPICS_VIEW_SQL,
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
            """,
            """
            CREATE OR REPLACE VIEW "m_folder_all_items_view" AS
            SELECT
                ('00' || substr(b.stream_uuid::text, 3))::uuid AS uuid,
                '00000000-0000-0000-0000-000000000000'::uuid AS folder,
                b.project_id,
                b.user_uuid,
                b.stream_uuid,
                fi.order_index,
                fi.pinned_at::timestamp                    AS pinned_at,
                CASE
                    WHEN s.private THEN 'private'::varchar
                    ELSE 'stream'::varchar
                END                                          AS chat_type,
                COALESCE(un.unread_count, 0)                 AS unread_count,
                COALESCE(
                    fi.created_at::timestamp,
                    '2000-01-01 00:00:00'::timestamp
                )                                            AS created_at,
                COALESCE(
                    fi.updated_at::timestamp,
                    '2000-01-01 00:00:00'::timestamp
                )                                            AS updated_at
            FROM "m_workspace_stream_bindings" AS b
            JOIN "m_workspace_streams" AS s
                ON  s.uuid       = b.stream_uuid
                AND s.project_id = b.project_id
            LEFT JOIN "m_folder_items" AS fi
                ON  fi.folder_uuid =
                    '00000000-0000-0000-0000-000000000000'::uuid
                AND fi.stream_uuid = b.stream_uuid
                AND fi.project_id  = b.project_id
                AND fi.user_uuid   = b.user_uuid
            LEFT JOIN "m_unread_user_messages" AS un
                ON  un.uuid       = b.stream_uuid
                AND un.user_uuid  = b.user_uuid
                AND un.project_id = b.project_id
            WHERE s.is_archived = false;
            """,
            """
            CREATE OR REPLACE VIEW "m_folder_private_items_view" AS
            SELECT
                ('11' || substr(b.stream_uuid::text, 3))::uuid AS uuid,
                '00000000-0000-0000-0000-000000000001'::uuid AS folder,
                b.project_id,
                b.user_uuid,
                b.stream_uuid,
                fi.order_index,
                fi.pinned_at::timestamp                    AS pinned_at,
                'private'::varchar                           AS chat_type,
                COALESCE(un.unread_count, 0)                 AS unread_count,
                COALESCE(
                    fi.created_at::timestamp,
                    '2000-01-01 00:00:01'::timestamp
                )                                            AS created_at,
                COALESCE(
                    fi.updated_at::timestamp,
                    '2000-01-01 00:00:01'::timestamp
                )                                            AS updated_at
            FROM "m_workspace_stream_bindings" AS b
            JOIN "m_workspace_streams" AS s
                ON  s.uuid       = b.stream_uuid
                AND s.project_id = b.project_id
            LEFT JOIN "m_folder_items" AS fi
                ON  fi.folder_uuid =
                    '00000000-0000-0000-0000-000000000001'::uuid
                AND fi.stream_uuid = b.stream_uuid
                AND fi.project_id  = b.project_id
                AND fi.user_uuid   = b.user_uuid
            LEFT JOIN "m_unread_user_messages" AS un
                ON  un.uuid       = b.stream_uuid
                AND un.user_uuid  = b.user_uuid
                AND un.project_id = b.project_id
            WHERE s.private = true
                AND s.is_archived = false;
            """,
            """
            CREATE OR REPLACE VIEW "m_folder_channel_items_view" AS
            SELECT
                ('22' || substr(b.stream_uuid::text, 3))::uuid AS uuid,
                '00000000-0000-0000-0000-000000000002'::uuid AS folder,
                b.project_id,
                b.user_uuid,
                b.stream_uuid,
                fi.order_index,
                fi.pinned_at::timestamp                    AS pinned_at,
                'stream'::varchar                            AS chat_type,
                COALESCE(un.unread_count, 0)                 AS unread_count,
                COALESCE(
                    fi.created_at::timestamp,
                    '2000-01-01 00:00:02'::timestamp
                )                                            AS created_at,
                COALESCE(
                    fi.updated_at::timestamp,
                    '2000-01-01 00:00:02'::timestamp
                )                                            AS updated_at
            FROM "m_workspace_stream_bindings" AS b
            JOIN "m_workspace_streams" AS s
                ON  s.uuid       = b.stream_uuid
                AND s.project_id = b.project_id
            LEFT JOIN "m_folder_items" AS fi
                ON  fi.folder_uuid =
                    '00000000-0000-0000-0000-000000000002'::uuid
                AND fi.stream_uuid = b.stream_uuid
                AND fi.project_id  = b.project_id
                AND fi.user_uuid   = b.user_uuid
            LEFT JOIN "m_unread_user_messages" AS un
                ON  un.uuid       = b.stream_uuid
                AND un.user_uuid  = b.user_uuid
                AND un.project_id = b.project_id
            WHERE s.private = false
                AND s.is_archived = false;
            """,
            'DROP VIEW IF EXISTS "m_confirmed_external_account_access";',
            """
            DROP INDEX IF EXISTS "m_external_accounts_access_gate_idx";
            """,
            """
            DROP INDEX IF EXISTS "m_external_accounts_access_check_idx";
            """,
            """
            ALTER TABLE "m_external_accounts"
                DROP CONSTRAINT IF EXISTS
                    "m_external_accounts_access_status_check",
                DROP COLUMN IF EXISTS "access_last_error",
                DROP COLUMN IF EXISTS "access_next_check_at",
                DROP COLUMN IF EXISTS "access_confirmed_at",
                DROP COLUMN IF EXISTS "access_checked_at",
                DROP COLUMN IF EXISTS "access_status",
                DROP COLUMN IF EXISTS "source_scope";
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
