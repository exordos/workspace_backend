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


CANONICAL_STREAM_ACCESS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_confirmed_external_stream_access" AS
WITH candidate_rows AS (
    SELECT
        chat.project_id,
        account.owner_user_uuid AS user_uuid,
        account.provider::varchar(32) AS account_type,
        account.uuid::text::varchar(2048) AS source_scope,
        COALESCE(
            NULLIF(chat.source->>'provider_realm_uuid', ''),
            NULLIF(account.settings->>'server_url', ''),
            account.uuid::text
        )::varchar(2048) AS provider_realm_id,
        chat.provider_chat_id,
        chat.projection_stream_uuid AS stream_uuid,
        1 AS owner_priority
    FROM "m_external_accounts_v2" AS account
    JOIN "m_external_chats_v2" AS chat
      ON chat.external_account_uuid = account.uuid
    WHERE chat.selected
      AND chat.project_id IS NOT NULL
      AND chat.projection_stream_uuid IS NOT NULL
      AND account.credential_present
      AND account.status NOT IN ('disconnected', 'suspended')
    UNION ALL
    SELECT
        chat.project_id,
        binding.user_uuid,
        account.provider::varchar(32) AS account_type,
        account.uuid::text::varchar(2048) AS source_scope,
        COALESCE(
            NULLIF(chat.source->>'provider_realm_uuid', ''),
            NULLIF(account.settings->>'server_url', ''),
            account.uuid::text
        )::varchar(2048) AS provider_realm_id,
        chat.provider_chat_id,
        chat.projection_stream_uuid AS stream_uuid,
        CASE
            WHEN account.owner_user_uuid = binding.user_uuid THEN 1
            ELSE 0
        END AS owner_priority
    FROM "m_external_accounts_v2" AS account
    JOIN "m_external_chats_v2" AS chat
      ON chat.external_account_uuid = account.uuid
    JOIN "m_workspace_stream_bindings" AS binding
      ON binding.project_id = chat.project_id
     AND binding.stream_uuid = chat.projection_stream_uuid
    WHERE chat.selected
      AND chat.project_id IS NOT NULL
      AND chat.projection_stream_uuid IS NOT NULL
      AND account.credential_present
      AND account.status NOT IN ('disconnected', 'suspended')
),
deduplicated_candidates AS (
    SELECT
        candidate.project_id,
        candidate.user_uuid,
        candidate.account_type,
        candidate.source_scope,
        candidate.provider_realm_id,
        candidate.provider_chat_id,
        candidate.stream_uuid,
        MAX(candidate.owner_priority) AS owner_priority
    FROM candidate_rows AS candidate
    WHERE NOT EXISTS (
        SELECT 1
        FROM "m_workspace_external_chat_membership_revocations" AS revocation
        WHERE revocation.project_id = candidate.project_id
          AND revocation.user_uuid = candidate.user_uuid
          AND revocation.provider = candidate.account_type
          AND revocation.provider_realm_id = candidate.provider_realm_id
          AND revocation.provider_chat_id = candidate.provider_chat_id
    )
    GROUP BY
        candidate.project_id,
        candidate.user_uuid,
        candidate.account_type,
        candidate.source_scope,
        candidate.provider_realm_id,
        candidate.provider_chat_id,
        candidate.stream_uuid
),
ranked_candidates AS (
    SELECT
        candidate.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                candidate.project_id,
                candidate.user_uuid,
                candidate.account_type,
                candidate.provider_realm_id,
                candidate.provider_chat_id
            ORDER BY
                candidate.owner_priority DESC,
                candidate.source_scope,
                candidate.stream_uuid
        ) AS projection_rank
    FROM deduplicated_candidates AS candidate
)
SELECT
    project_id,
    user_uuid,
    account_type,
    source_scope,
    stream_uuid
FROM ranked_candidates
WHERE projection_rank = 1;
"""


def _unread_user_messages_view(stream_scoped):
    if stream_scoped:
        stream_join = """
JOIN "m_workspace_streams" AS stream
    ON  stream.uuid       = m.stream_uuid
    AND stream.project_id = m.project_id
"""
        access_join = """
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON  access.project_id = m.project_id
    AND access.user_uuid  = f.user_uuid
    AND access.stream_uuid = m.stream_uuid
"""
        visibility = """
      stream.source_name = 'native'
      OR access.user_uuid IS NOT NULL
"""
    else:
        stream_join = ""
        access_join = """
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = m.project_id
    AND access.user_uuid    = f.user_uuid
    AND access.account_type = m.source_name
    AND access.source_scope = COALESCE(
        m.source->>'source_scope',
        m.source->>'server_url'
    )
"""
        visibility = """
      m.source_name = 'native'
      OR access.user_uuid IS NOT NULL
"""
    return f"""
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
{stream_join}
{access_join}
WHERE f.read = false
  AND ({visibility})
GROUP BY m.stream_uuid, f.user_uuid, f.project_id;
"""


def _user_messages_view(stream_scoped):
    if stream_scoped:
        stream_join = """
JOIN "m_workspace_streams" AS stream
    ON  stream.uuid       = m.stream_uuid
    AND stream.project_id = m.project_id
"""
        access_join = """
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON  access.project_id = m.project_id
    AND access.user_uuid  = b.user_uuid
    AND access.stream_uuid = m.stream_uuid
"""
        visibility = """
stream.source_name = 'native'
   OR access.user_uuid IS NOT NULL
"""
    else:
        stream_join = ""
        access_join = """
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = m.project_id
    AND access.user_uuid    = b.user_uuid
    AND access.account_type = m.source_name
    AND access.source_scope = COALESCE(
        m.source->>'source_scope',
        m.source->>'server_url'
    )
"""
        visibility = """
m.source_name = 'native'
   OR access.user_uuid IS NOT NULL
"""
    return f"""
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
        '{{}}'::jsonb
    )                               AS reactions,
    m.source_name,
    m.source,
    POSITION(
        '](' || 'urn:user:' || LOWER(b.user_uuid::text) || ')'
        IN LOWER(COALESCE(m.payload->>'content', ''))
    ) > 0                           AS mentioned
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid  = m.stream_uuid
    AND b.project_id   = m.project_id
{stream_join}
LEFT JOIN "m_workspace_user_message_flags" AS f
    ON  f.uuid       = m.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = m.project_id
{access_join}
WHERE {visibility};
"""


def _user_streams_view(stream_scoped):
    if stream_scoped:
        access_join = """
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON  access.project_id = s.project_id
    AND access.user_uuid  = b.user_uuid
    AND access.stream_uuid = s.uuid
"""
    else:
        access_join = """
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = s.project_id
    AND access.user_uuid    = b.user_uuid
    AND access.account_type = s.source_name
    AND access.source_scope = COALESCE(
        s.source->>'source_scope',
        s.source->>'server_url'
    )
"""
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
    s.default_topic_uuid
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
{access_join}
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


def _user_topics_view(stream_scoped):
    if stream_scoped:
        access_join = """
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON  access.project_id = t.project_id
    AND access.user_uuid  = b.user_uuid
    AND access.stream_uuid = t.stream_uuid
"""
        visibility = """
s.source_name = 'native'
   OR access.user_uuid IS NOT NULL
"""
    else:
        access_join = """
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = t.project_id
    AND access.user_uuid    = b.user_uuid
    AND access.account_type = t.source_name
    AND access.source_scope = COALESCE(
        t.source->>'source_scope',
        t.source->>'server_url'
    )
"""
        visibility = """
t.source_name = 'native'
   OR access.user_uuid IS NOT NULL
"""
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
    t.source
FROM "m_workspace_stream_topics" AS t
JOIN "m_workspace_streams" AS s
    ON  s.uuid = t.stream_uuid
    AND s.project_id = t.project_id
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
{access_join}
WHERE {visibility};
"""


def _visible_events_view(stream_scoped):
    if stream_scoped:
        stream_resolution_join = """
LEFT JOIN LATERAL (
    SELECT COALESCE(
        NULLIF(e."payload"->>'stream_uuid', '')::uuid,
        CASE
            WHEN e."object_type" = 'stream'
            THEN NULLIF(e."payload"->>'uuid', '')::uuid
        END,
        (
            SELECT message."stream_uuid"
            FROM "m_workspace_messages" AS message
            WHERE message."project_id" = e."project_id"
              AND message."uuid" =
                    NULLIF(e."payload"->>'message_uuid', '')::uuid
        )
    ) AS stream_uuid
) AS event_stream ON TRUE
"""
        stream_visibility = """
          AND (
                (
                    e."object_type" = 'stream'
                    AND e."action" = 'deleted'
                )
                OR event_stream.stream_uuid IS NULL
                OR NOT EXISTS (
                    SELECT 1
                    FROM "m_workspace_streams" AS external_stream
                    WHERE external_stream."project_id" = e."project_id"
                      AND external_stream."uuid" = event_stream.stream_uuid
                      AND external_stream."source_name" <> 'native'
                )
                OR EXISTS (
                    SELECT 1
                    FROM "m_confirmed_external_stream_access"
                        AS stream_access
                    WHERE stream_access."project_id" = e."project_id"
                      AND stream_access."user_uuid" = e."user_uuid"
                      AND stream_access."stream_uuid" =
                            event_stream.stream_uuid
                )
            )
"""
    else:
        stream_resolution_join = ""
        stream_visibility = """
          AND (
                e."object_type" <> 'message'
                OR e."payload"->>'stream_uuid' IS NULL
                OR NOT EXISTS (
                    SELECT 1
                    FROM "m_workspace_streams" AS external_stream
                    WHERE external_stream."project_id" = e."project_id"
                      AND external_stream."uuid" =
                          (e."payload"->>'stream_uuid')::uuid
                      AND external_stream."source_name" <> 'native'
                )
                OR EXISTS (
                    SELECT 1
                    FROM "m_workspace_streams" AS external_stream
                    JOIN "m_confirmed_external_account_access"
                        AS stream_access
                      ON stream_access."project_id" =
                            external_stream."project_id"
                     AND stream_access."user_uuid" = e."user_uuid"
                     AND stream_access."account_type" =
                            external_stream."source_name"
                     AND stream_access."source_scope" = COALESCE(
                            external_stream."source"->>'source_scope',
                            external_stream."source"->>'server_url'
                         )
                    WHERE external_stream."project_id" = e."project_id"
                      AND external_stream."uuid" =
                          (e."payload"->>'stream_uuid')::uuid
                )
            )
"""
    return f"""
CREATE OR REPLACE VIEW "m_workspace_visible_events" AS
WITH event_rows AS (
    SELECT
        e."epoch_version", e."uuid", e."project_id", e."user_uuid",
        e."payload", e."created_at", e."updated_at",
        e."schema_version", e."object_type", e."action"
    FROM "m_workspace_events" AS e
    UNION ALL
    SELECT
        b."epoch_version", b."uuid", b."project_id",
        recipient."user_uuid",
        b."payload" || COALESCE(override."payload", '{{}}'::jsonb)
            || CASE
                WHEN b."object_type" = 'user' THEN '{{}}'::jsonb
                ELSE jsonb_build_object(
                    'user_uuid', recipient."user_uuid"
                )
            END AS "payload",
        b."created_at", b."updated_at", b."schema_version",
        b."object_type", b."action"
    FROM "m_workspace_broadcast_message_events_v1" AS b
    JOIN "m_workspace_event_audience_members_v1" AS recipient
      ON recipient."audience_snapshot_uuid" = b."audience_snapshot_uuid"
    LEFT JOIN "m_workspace_event_recipient_payloads_v1" AS override
      ON override."event_uuid" = b."uuid"
     AND override."user_uuid" = recipient."user_uuid"
)
SELECT e.*
FROM event_rows AS e
LEFT JOIN "m_confirmed_external_account_access" AS access
  ON access.project_id = e.project_id
 AND access.user_uuid = e.user_uuid
 AND access.account_type = e.payload->>'source_name'
 AND access.source_scope = COALESCE(
        e.payload->'source'->>'source_scope',
        e.payload->'source'->>'server_url'
     )
LEFT JOIN "m_confirmed_external_account_access" AS old_access
  ON old_access.project_id = e.project_id
 AND old_access.user_uuid = e.user_uuid
 AND old_access.account_type = e.payload->>'old_source_name'
 AND old_access.source_scope = COALESCE(
        e.payload->'old_source'->>'source_scope',
        e.payload->'old_source'->>'server_url'
     )
{stream_resolution_join}
WHERE (
        COALESCE(e.payload->>'source_name', 'native') = 'native'
        OR access.user_uuid IS NOT NULL
        OR (
            e."object_type" = 'stream'
            AND e."action" = 'deleted'
        )
    )
  AND (
        e.payload->>'old_source_name' IS NULL
        OR e.payload->>'old_source_name' = 'native'
        OR old_access.user_uuid IS NOT NULL
    )
  AND (
        e."object_type" <> 'message'
        OR e."payload"->>'stream_uuid' IS NULL
        OR EXISTS (
            SELECT 1
            FROM "m_workspace_stream_bindings" AS binding
            WHERE binding."project_id" = e."project_id"
              AND binding."stream_uuid" =
                  (e."payload"->>'stream_uuid')::uuid
              AND binding."user_uuid" = e."user_uuid"
        )
    )
{stream_visibility};
"""


RESET_EXTERNAL_EVENT_CURSORS_SQL = """
UPDATE "m_workspace_event_cursors" AS cursor
SET "epoch_generation" = gen_random_uuid(),
    "pruned_through_epoch_version" = GREATEST(
        cursor."pruned_through_epoch_version",
        cursor."current_epoch_version"
    ),
    "updated_at" = NOW()
WHERE EXISTS (
    SELECT 1
    FROM "m_external_chats_v2" AS chat
    WHERE chat."project_id" = cursor."project_id"
);
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0124-deduplicate-external-account-access-78c745.py"]

    @property
    def migration_id(self):
        return "e82c027f-2481-4447-85fb-8648b335a6cd"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(CANONICAL_STREAM_ACCESS_VIEW_SQL)
        session.execute(_unread_user_messages_view(stream_scoped=True))
        session.execute(_user_messages_view(stream_scoped=True))
        session.execute(_user_streams_view(stream_scoped=True))
        session.execute(_user_topics_view(stream_scoped=True))
        session.execute(_visible_events_view(stream_scoped=True))
        session.execute(RESET_EXTERNAL_EVENT_CURSORS_SQL)

    def downgrade(self, session):
        session.execute(_visible_events_view(stream_scoped=False))
        session.execute(_user_topics_view(stream_scoped=False))
        session.execute(_user_streams_view(stream_scoped=False))
        session.execute(_user_messages_view(stream_scoped=False))
        session.execute(_unread_user_messages_view(stream_scoped=False))
        session.execute('DROP VIEW IF EXISTS "m_confirmed_external_stream_access";')
        session.execute(RESET_EXTERNAL_EVENT_CURSORS_SQL)


migration_step = MigrationStep()
