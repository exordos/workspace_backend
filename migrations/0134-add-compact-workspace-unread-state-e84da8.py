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

READ_STATE_SCHEMA_LOCK_KEY = "workspace-read-state-schema-v1"

USER_MESSAGES_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_messages_view" AS
SELECT
    message.uuid,
    message.stream_uuid,
    message.user_uuid AS author_uuid,
    message.topic_uuid,
    message.payload,
    message.created_at,
    message.updated_at,
    binding.user_uuid,
    message.project_id,
    CASE WHEN COALESCE(project.mode, 'legacy') IN ('compact', 'rollback') THEN
        COALESCE(
            get_bit(
                chunk.read_bits,
                (message.ingest_sequence % 4096)::integer
            ),
            0
        ) = 1
    ELSE COALESCE(flags.read, FALSE) END AS read,
    COALESCE(flags.pinned, FALSE) AS pinned,
    COALESCE(flags.starred, FALSE) AS starred,
    (message.user_uuid = binding.user_uuid) AS is_own,
    COALESCE(
        (
            SELECT jsonb_object_agg(
                reaction_counts.emoji_name,
                reaction_counts.reaction_count
            )
            FROM (
                SELECT reaction.emoji_name, COUNT(*) AS reaction_count
                FROM "m_workspace_message_reactions" AS reaction
                WHERE reaction.project_id = message.project_id
                  AND reaction.message_uuid = message.uuid
                GROUP BY reaction.emoji_name
            ) AS reaction_counts
        ),
        '{}'::jsonb
    ) AS reactions,
    message.source_name,
    message.source,
    CASE WHEN COALESCE(project.mode, 'legacy') IN ('compact', 'rollback') THEN
        mention.message_uuid IS NOT NULL
    ELSE POSITION(
        '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
        IN LOWER(COALESCE(message.payload->>'content', ''))
    ) > 0 END AS mentioned,
    message.reaction_users
FROM "m_workspace_messages" AS message
JOIN "m_workspace_stream_bindings" AS binding
  ON binding.stream_uuid = message.stream_uuid
 AND binding.project_id = message.project_id
JOIN "m_workspace_streams" AS stream
  ON stream.uuid = message.stream_uuid
 AND stream.project_id = message.project_id
LEFT JOIN "m_workspace_read_state_projects_v1" AS project
  ON project.project_id = message.project_id
LEFT JOIN "m_workspace_user_message_flags" AS flags
  ON flags.uuid = message.uuid
 AND flags.user_uuid = binding.user_uuid
 AND flags.project_id = message.project_id
LEFT JOIN "m_workspace_user_read_chunks_v1" AS chunk
  ON chunk.user_uuid = binding.user_uuid
 AND chunk.chunk_number = message.ingest_sequence / 4096
LEFT JOIN "m_workspace_message_mentions_v1" AS mention
  ON mention.message_uuid = message.uuid
 AND mention.user_uuid = binding.user_uuid
LEFT JOIN "m_confirmed_external_stream_access" AS access
  ON access.project_id = message.project_id
 AND access.user_uuid = binding.user_uuid
 AND access.stream_uuid = message.stream_uuid
WHERE stream.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


PREVIOUS_USER_MESSAGES_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_messages_view" AS
SELECT
    m.uuid, m.stream_uuid, m.user_uuid AS author_uuid, m.topic_uuid,
    m.payload, m.created_at, m.updated_at, b.user_uuid, m.project_id,
    COALESCE(f.read, FALSE) AS read,
    COALESCE(f.pinned, FALSE) AS pinned,
    COALESCE(f.starred, FALSE) AS starred,
    (m.user_uuid = b.user_uuid) AS is_own,
    COALESCE(
        (
            SELECT jsonb_object_agg(counts.emoji_name, counts.reaction_count)
            FROM (
                SELECT r.emoji_name, COUNT(*) AS reaction_count
                FROM "m_workspace_message_reactions" AS r
                WHERE r.project_id = m.project_id
                  AND r.message_uuid = m.uuid
                GROUP BY r.emoji_name
            ) AS counts
        ),
        '{}'::jsonb
    ) AS reactions,
    m.source_name,
    m.source,
    POSITION(
        '](' || 'urn:user:' || LOWER(b.user_uuid::text) || ')'
        IN LOWER(COALESCE(m.payload->>'content', ''))
    ) > 0 AS mentioned,
    m.reaction_users
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS b
  ON b.stream_uuid = m.stream_uuid AND b.project_id = m.project_id
JOIN "m_workspace_streams" AS stream
  ON stream.uuid = m.stream_uuid AND stream.project_id = m.project_id
LEFT JOIN "m_workspace_user_message_flags" AS f
  ON f.uuid = m.uuid AND f.user_uuid = b.user_uuid
 AND f.project_id = m.project_id
LEFT JOIN "m_confirmed_external_stream_access" AS access
  ON access.project_id = m.project_id
 AND access.user_uuid = b.user_uuid
 AND access.stream_uuid = m.stream_uuid
WHERE stream.source_name = 'native' OR access.user_uuid IS NOT NULL;
"""


UNREAD_MESSAGE_BASE_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_unread_messages_base_v1" AS
SELECT
    message.uuid AS message_uuid,
    message.stream_uuid,
    message.topic_uuid,
    binding.user_uuid,
    message.project_id,
    CASE WHEN project.mode IN ('compact', 'rollback')
        THEN mention.message_uuid IS NOT NULL
    ELSE POSITION(
        '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
        IN LOWER(COALESCE(message.payload->>'content', ''))
    ) > 0 END AS mentioned,
    CASE COALESCE(topic_flags.notification_mode, 'default')
        WHEN 'mute' THEN FALSE
        WHEN 'follow' THEN TRUE
        WHEN 'unmute' THEN CASE WHEN project.mode IN ('compact', 'rollback') THEN
            mention.message_uuid IS NOT NULL
        ELSE POSITION(
            '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
            IN LOWER(COALESCE(message.payload->>'content', ''))
        ) > 0 END
        ELSE CASE binding.notification_mode
            WHEN 'all_messages' THEN TRUE
            WHEN 'mentions_only' THEN CASE
                WHEN project.mode IN ('compact', 'rollback') THEN
                mention.message_uuid IS NOT NULL
            ELSE POSITION(
                '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
                IN LOWER(COALESCE(message.payload->>'content', ''))
            ) > 0 END
            ELSE FALSE
        END
    END AS active
FROM "m_workspace_messages" AS message
JOIN "m_workspace_stream_bindings" AS binding
  ON binding.stream_uuid = message.stream_uuid
 AND binding.project_id = message.project_id
LEFT JOIN "m_workspace_read_state_projects_v1" AS project
  ON project.project_id = message.project_id
LEFT JOIN "m_workspace_user_message_flags" AS flags
  ON flags.uuid = message.uuid
 AND flags.user_uuid = binding.user_uuid
 AND flags.project_id = message.project_id
LEFT JOIN "m_workspace_user_read_chunks_v1" AS chunk
  ON chunk.user_uuid = binding.user_uuid
 AND chunk.chunk_number = message.ingest_sequence / 4096
LEFT JOIN "m_workspace_message_mentions_v1" AS mention
  ON mention.message_uuid = message.uuid
 AND mention.user_uuid = binding.user_uuid
LEFT JOIN "m_workspace_user_topic_flags" AS topic_flags
  ON topic_flags.uuid = message.topic_uuid
 AND topic_flags.project_id = message.project_id
 AND topic_flags.user_uuid = binding.user_uuid
WHERE CASE WHEN project.mode IN ('compact', 'rollback') THEN
    COALESCE(
        get_bit(chunk.read_bits, (message.ingest_sequence % 4096)::integer),
        0
    ) = 0
ELSE flags.read = FALSE END;
"""


PREVIOUS_UNREAD_MESSAGE_BASE_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_unread_messages_base_v1" AS
SELECT
    m.uuid AS message_uuid, m.stream_uuid, m.topic_uuid, binding.user_uuid,
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
  ON binding.stream_uuid = m.stream_uuid AND binding.project_id = m.project_id
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


TOPIC_UNREAD_COUNTS_VIEW_SQL = """
CREATE VIEW "m_workspace_user_topic_unread_counts_v1" AS
WITH legacy_counts AS (
    SELECT
        unread.stream_uuid,
        unread.topic_uuid,
        unread.user_uuid,
        unread.project_id,
        COUNT(*) AS unread_count,
        COUNT(*) FILTER (WHERE unread.active)::bigint AS active_unread_count
    FROM "m_workspace_user_unread_messages_base_v1" AS unread
    LEFT JOIN "m_workspace_read_state_projects_v1" AS project
      ON project.project_id = unread.project_id
    WHERE COALESCE(project.mode, 'legacy') NOT IN ('compact', 'rollback')
    GROUP BY unread.stream_uuid, unread.topic_uuid,
             unread.user_uuid, unread.project_id
), compact_counts AS (
    SELECT
        stats.stream_uuid,
        stats.topic_uuid,
        binding.user_uuid,
        stats.project_id,
        GREATEST(stats.message_count - COALESCE(reads.read_count, 0), 0)
            AS unread_count,
        CASE COALESCE(topic_flags.notification_mode, 'default')
            WHEN 'mute' THEN 0
            WHEN 'follow' THEN
                GREATEST(stats.message_count - COALESCE(reads.read_count, 0), 0)
            WHEN 'unmute' THEN COALESCE(mentions.unread_count, 0)
            ELSE CASE binding.notification_mode
                WHEN 'all_messages' THEN
                    GREATEST(
                        stats.message_count - COALESCE(reads.read_count, 0),
                        0
                    )
                WHEN 'mentions_only' THEN COALESCE(mentions.unread_count, 0)
                ELSE 0
            END
        END AS active_unread_count
    FROM "m_workspace_topic_message_stats_v1" AS stats
    JOIN "m_workspace_read_state_projects_v1" AS project
      ON project.project_id = stats.project_id
     AND project.mode IN ('compact', 'rollback')
    JOIN "m_workspace_stream_bindings" AS binding
      ON binding.project_id = stats.project_id
     AND binding.stream_uuid = stats.stream_uuid
    LEFT JOIN "m_workspace_user_topic_flags" AS topic_flags
      ON topic_flags.project_id = stats.project_id
     AND topic_flags.uuid = stats.topic_uuid
     AND topic_flags.user_uuid = binding.user_uuid
    LEFT JOIN LATERAL (
        SELECT topic_reads.read_count
        FROM "m_workspace_user_topic_read_stats_v1" AS topic_reads
        WHERE topic_reads.project_id = stats.project_id
          AND topic_reads.topic_uuid = stats.topic_uuid
          AND topic_reads.user_uuid = binding.user_uuid
    ) AS reads ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS unread_count
        FROM "m_workspace_message_mentions_v1" AS mention
        LEFT JOIN "m_workspace_user_read_chunks_v1" AS chunk
          ON chunk.user_uuid = mention.user_uuid
         AND chunk.chunk_number = mention.ingest_sequence / 4096
        WHERE mention.project_id = stats.project_id
          AND mention.topic_uuid = stats.topic_uuid
          AND mention.user_uuid = binding.user_uuid
          AND COALESCE(
                get_bit(
                    chunk.read_bits,
                    (mention.ingest_sequence % 4096)::integer
                ),
                0
              ) = 0
    ) AS mentions ON TRUE
)
SELECT
    stream_uuid, topic_uuid, user_uuid, project_id,
    unread_count::bigint,
    active_unread_count::integer,
    (unread_count - active_unread_count)::integer AS passive_unread_count
FROM legacy_counts
UNION ALL
SELECT
    stream_uuid, topic_uuid, user_uuid, project_id,
    unread_count::bigint,
    active_unread_count::integer,
    (unread_count - active_unread_count)::integer AS passive_unread_count
FROM compact_counts
WHERE unread_count > 0;
"""


UNREAD_USER_MESSAGES_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_unread_user_messages" AS
SELECT
    counts.stream_uuid AS uuid,
    counts.user_uuid,
    counts.project_id,
    SUM(counts.unread_count)::bigint AS unread_count,
    SUM(counts.active_unread_count)::integer AS active_unread_count,
    SUM(counts.passive_unread_count)::integer AS passive_unread_count
FROM "m_workspace_user_topic_unread_counts_v1" AS counts
JOIN "m_workspace_streams" AS stream
  ON stream.uuid = counts.stream_uuid
 AND stream.project_id = counts.project_id
LEFT JOIN "m_confirmed_external_stream_access" AS access
  ON access.project_id = counts.project_id
 AND access.user_uuid = counts.user_uuid
 AND access.stream_uuid = counts.stream_uuid
WHERE stream.source_name = 'native' OR access.user_uuid IS NOT NULL
GROUP BY counts.stream_uuid, counts.user_uuid, counts.project_id;
"""


PREVIOUS_UNREAD_USER_MESSAGES_VIEW_SQL = """
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


def _user_streams_view_sql(*, compact_counts):
    unread_source = (
        '"m_unread_user_messages" AS unread'
        if compact_counts
        else """
        (
            SELECT
                item.stream_uuid AS uuid,
                item.user_uuid,
                item.project_id,
                COUNT(*) AS unread_count,
                COUNT(*) FILTER (WHERE item.active)::integer
                    AS active_unread_count,
                COUNT(*) FILTER (WHERE NOT item.active)::integer
                    AS passive_unread_count
            FROM "m_workspace_user_unread_messages_base_v1" AS item
            GROUP BY item.stream_uuid, item.user_uuid, item.project_id
        ) AS unread
        """
    )
    return f"""
CREATE OR REPLACE VIEW "m_workspace_user_streams" AS
SELECT
    stream.uuid,
    CASE WHEN stream.private THEN
        COALESCE(
            NULLIF(
                TRIM(
                    COALESCE(peer.first_name, '') || ' ' ||
                    COALESCE(peer.last_name, '')
                ),
                ''
            ),
            peer.username,
            stream.name
        )
    ELSE stream.name END AS name,
    stream.description,
    stream.project_id,
    stream.source_name,
    stream.source,
    stream.user_uuid AS owner,
    binding.user_uuid,
    binding.role,
    COALESCE(unread.unread_count, 0) AS unread_count,
    stream.invite_only,
    stream.announce,
    stream.private,
    stream.created_at,
    stream.updated_at,
    CASE
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
             AND stream.user_uuid = binding.user_uuid
            THEN stream.direct_user_uuid
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
            THEN stream.user_uuid
        ELSE NULL
    END AS direct_user_uuid,
    stream.private_index,
    stream.is_archived,
    binding.notification_mode,
    stream.color,
    last_message.uuid AS last_message_uuid,
    stream.default_topic_uuid,
    COALESCE(unread.active_unread_count, 0) AS active_unread_count,
    COALESCE(unread.passive_unread_count, 0) AS passive_unread_count
FROM "m_workspace_streams" AS stream
JOIN "m_workspace_stream_bindings" AS binding
  ON binding.stream_uuid = stream.uuid
 AND binding.project_id = stream.project_id
LEFT JOIN {unread_source}
  ON unread.uuid = stream.uuid
 AND unread.user_uuid = binding.user_uuid
 AND unread.project_id = stream.project_id
LEFT JOIN LATERAL (
    SELECT message.uuid
    FROM "m_workspace_messages" AS message
    WHERE message.project_id = stream.project_id
      AND message.stream_uuid = stream.uuid
    ORDER BY message.created_at DESC, message.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_users" AS peer
  ON peer.uuid = CASE
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
             AND stream.user_uuid = binding.user_uuid
            THEN stream.direct_user_uuid
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
            THEN stream.user_uuid
        WHEN stream.private AND stream.user_uuid <> binding.user_uuid
            THEN stream.user_uuid
        ELSE NULL
    END
LEFT JOIN "m_confirmed_external_stream_access" AS access
  ON access.project_id = stream.project_id
 AND access.user_uuid = binding.user_uuid
 AND access.stream_uuid = stream.uuid
WHERE stream.source_name = 'native' OR access.user_uuid IS NOT NULL;
"""


def _user_topics_view_sql(*, compact_counts):
    unread_source = (
        '"m_workspace_user_topic_unread_counts_v1" AS unread'
        if compact_counts
        else """
        (
            SELECT
                item.stream_uuid,
                item.topic_uuid,
                item.user_uuid,
                item.project_id,
                COUNT(*) AS unread_count,
                COUNT(*) FILTER (WHERE item.active)::integer
                    AS active_unread_count,
                COUNT(*) FILTER (WHERE NOT item.active)::integer
                    AS passive_unread_count
            FROM "m_workspace_user_unread_messages_base_v1" AS item
            WHERE item.topic_uuid IS NOT NULL
            GROUP BY item.stream_uuid, item.topic_uuid,
                     item.user_uuid, item.project_id
        ) AS unread
        """
    )
    return f"""
CREATE OR REPLACE VIEW "m_workspace_user_topics_view" AS
SELECT
    topic.uuid,
    topic.name,
    topic.stream_uuid,
    topic.project_id,
    topic.created_at,
    topic.updated_at,
    COALESCE(topic.uuid = stream.default_topic_uuid, FALSE) AS is_default,
    binding.user_uuid,
    COALESCE(unread.unread_count, 0) AS unread_count,
    COALESCE(flags.is_done, FALSE) AS is_done,
    COALESCE(flags.notification_mode, 'default') AS notification_mode,
    topic.color,
    last_message.uuid AS last_message_uuid,
    topic.source_name,
    topic.source,
    topic.summary,
    topic.summary_last_message_uuid,
    CASE WHEN topic.summary IS NULL THEN NULL
         ELSE topic.summary_last_message_uuid IS DISTINCT FROM last_message.uuid
    END AS summary_has_new_messages,
    topic.summary_system_prompt,
    topic.summary_reasoning_effort,
    topic.summary_enabled,
    COALESCE(unread.active_unread_count, 0) AS active_unread_count,
    COALESCE(unread.passive_unread_count, 0) AS passive_unread_count
FROM "m_workspace_stream_topics" AS topic
JOIN "m_workspace_streams" AS stream
  ON stream.uuid = topic.stream_uuid
 AND stream.project_id = topic.project_id
JOIN "m_workspace_stream_bindings" AS binding
  ON binding.stream_uuid = topic.stream_uuid
 AND binding.project_id = topic.project_id
LEFT JOIN {unread_source}
  ON unread.topic_uuid = topic.uuid
 AND unread.stream_uuid = topic.stream_uuid
 AND unread.user_uuid = binding.user_uuid
 AND unread.project_id = topic.project_id
LEFT JOIN LATERAL (
    SELECT message.uuid
    FROM "m_workspace_messages" AS message
    WHERE message.project_id = topic.project_id
      AND message.topic_uuid = topic.uuid
    ORDER BY message.created_at DESC, message.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_user_topic_flags" AS flags
  ON flags.uuid = topic.uuid
 AND flags.user_uuid = binding.user_uuid
 AND flags.project_id = topic.project_id
LEFT JOIN "m_confirmed_external_stream_access" AS access
  ON access.project_id = topic.project_id
 AND access.user_uuid = binding.user_uuid
 AND access.stream_uuid = topic.stream_uuid
WHERE stream.source_name = 'native' OR access.user_uuid IS NOT NULL;
"""


DOWNGRADE_BATCH_SIZE = 10_000


def _prepare_downgrade_progress(session):
    session.execute(
        """
        CREATE TABLE IF NOT EXISTS "m_workspace_read_state_downgrade_v1" (
            "project_id" UUID PRIMARY KEY,
            "last_created_at" TIMESTAMPTZ,
            "last_ingest_sequence" BIGINT,
            "last_message_uuid" UUID,
            "last_user_uuid" UUID,
            "processed_rows" BIGINT NOT NULL DEFAULT 0,
            "completed_at" TIMESTAMPTZ,
            "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT "m_workspace_read_state_downgrade_count_check"
                CHECK ("processed_rows" >= 0)
        )
        """
    )
    session.commit()


def _lock_read_state_project(session, project_id):
    session.execute(
        """
        SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))
        """,
        (READ_STATE_SCHEMA_LOCK_KEY,),
    )
    session.execute(
        """
        SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))
        """,
        (project_id,),
    )


def _lock_read_state_projects(session):
    # Every current application writer takes the shared side before any
    # per-project lock.  The exclusive side therefore fences existing writes
    # and future project-row inserts without an enumerate-then-lock TOCTOU or
    # an unsorted cross-project lock acquisition.
    try:
        session.execute(
            """
            SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))
            """,
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        return []
    except Exception:
        session.rollback()
        raise


def _ensure_no_active_aggregate_provider_reads(session):
    aggregate = session.execute(
        """
        SELECT external_operation_uuid
        FROM m_external_provider_operations_v1
        GROUP BY external_operation_uuid
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if aggregate is not None:
        raise RuntimeError(
            "Compact unread downgrade requires aggregate provider read "
            "history to be drained first"
        )


def _hydrate_legacy_flags_batch(session, project_id, batch_size):
    result = session.execute(
        """
        WITH recipient_streams AS MATERIALIZED (
            SELECT binding.user_uuid, binding.stream_uuid,
                   NULL::BIGINT AS last_detached_sequence
            FROM m_workspace_stream_bindings AS binding
            WHERE binding.project_id = %s
            UNION ALL
            SELECT membership.user_uuid, membership.stream_uuid,
                   membership.last_detached_sequence
            FROM m_workspace_read_memberships_v1 AS membership
            WHERE membership.project_id = %s
              AND NOT EXISTS (
                    SELECT 1
                    FROM m_workspace_stream_bindings AS current_binding
                    WHERE current_binding.project_id = membership.project_id
                      AND current_binding.stream_uuid = membership.stream_uuid
                      AND current_binding.user_uuid = membership.user_uuid
                )
        ), recipient_users AS MATERIALIZED (
            SELECT DISTINCT user_uuid
            FROM recipient_streams
        ), next_user AS MATERIALIZED (
            SELECT recipient.user_uuid
            FROM recipient_users AS recipient
            JOIN m_workspace_read_state_downgrade_v1 AS progress
              ON progress.project_id = %s
            WHERE (
                    progress.last_user_uuid IS NULL
                    OR recipient.user_uuid >= progress.last_user_uuid
                  )
              AND EXISTS (
                    SELECT 1
                    FROM recipient_streams AS stream
                    JOIN m_workspace_messages AS message
                      ON message.project_id = %s
                     AND message.stream_uuid = stream.stream_uuid
                    WHERE stream.user_uuid = recipient.user_uuid
                      AND (
                            stream.last_detached_sequence IS NULL
                            OR message.ingest_sequence
                                <= stream.last_detached_sequence
                          )
                      AND (
                            progress.last_user_uuid IS NULL
                            OR recipient.user_uuid > progress.last_user_uuid
                            OR message.created_at > progress.last_created_at
                            OR (
                                message.created_at = progress.last_created_at
                                AND message.uuid > progress.last_message_uuid
                            )
                          )
                )
            ORDER BY recipient.user_uuid
            LIMIT 1
        ), candidates AS MATERIALIZED (
            SELECT
                message.uuid AS message_uuid,
                message.created_at,
                message.ingest_sequence,
                message.project_id,
                next_user.user_uuid
            FROM next_user
            JOIN recipient_streams AS recipient
              ON recipient.user_uuid = next_user.user_uuid
            JOIN m_workspace_messages AS message
              ON message.project_id = %s
             AND message.stream_uuid = recipient.stream_uuid
            JOIN m_workspace_read_state_downgrade_v1 AS progress
              ON progress.project_id = message.project_id
            WHERE (
                    recipient.last_detached_sequence IS NULL
                    OR message.ingest_sequence
                        <= recipient.last_detached_sequence
                  )
              AND (
                    progress.last_user_uuid IS NULL
                    OR next_user.user_uuid > progress.last_user_uuid
                    OR message.created_at > progress.last_created_at
                    OR (
                        message.created_at = progress.last_created_at
                        AND message.uuid > progress.last_message_uuid
                    )
              )
            ORDER BY message.created_at, message.uuid
            LIMIT %s
        ), hydrated AS (
            INSERT INTO m_workspace_user_message_flags AS legacy_flags (
                uuid, user_uuid, project_id, read, pinned, starred,
                created_at, updated_at
            )
            SELECT
                candidate.message_uuid,
                candidate.user_uuid,
                candidate.project_id,
                COALESCE(
                    get_bit(
                        chunk.read_bits,
                        (candidate.ingest_sequence %% 4096)::integer
                    ),
                    0
                ) = 1,
                COALESCE(existing.pinned, FALSE),
                COALESCE(existing.starred, FALSE),
                COALESCE(existing.created_at, NOW()),
                NOW()
            FROM candidates AS candidate
            LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
              ON chunk.user_uuid = candidate.user_uuid
             AND chunk.chunk_number = candidate.ingest_sequence / 4096
            LEFT JOIN m_workspace_user_message_flags AS existing
              ON existing.uuid = candidate.message_uuid
             AND existing.user_uuid = candidate.user_uuid
             AND existing.project_id = candidate.project_id
            ON CONFLICT (uuid, user_uuid) DO UPDATE
            SET project_id = EXCLUDED.project_id,
                read = EXCLUDED.read,
                updated_at = NOW()
            RETURNING 1
        ), tail AS (
            SELECT
                created_at,
                ingest_sequence,
                message_uuid,
                user_uuid
            FROM candidates
            ORDER BY created_at DESC, message_uuid DESC
            LIMIT 1
        ), batch AS (
            SELECT COUNT(*)::BIGINT AS processed_rows
            FROM hydrated
        )
        UPDATE m_workspace_read_state_downgrade_v1 AS progress
        SET last_created_at = tail.created_at,
            last_ingest_sequence = tail.ingest_sequence,
            last_message_uuid = tail.message_uuid,
            last_user_uuid = tail.user_uuid,
            processed_rows = progress.processed_rows + batch.processed_rows,
            updated_at = NOW()
        FROM tail, batch
        WHERE progress.project_id = %s
        RETURNING batch.processed_rows
        """,
        (
            project_id,
            project_id,
            project_id,
            project_id,
            project_id,
            batch_size,
            project_id,
        ),
    ).fetchone()
    if result is None:
        session.execute(
            """
            UPDATE m_workspace_read_state_downgrade_v1
            SET completed_at = COALESCE(completed_at, NOW()),
                updated_at = NOW()
            WHERE project_id = %s
            """,
            (project_id,),
        )
        return 0
    return result["processed_rows"]


def _prepare_rollback_projects(session):
    while True:
        project = session.execute(
            """
            SELECT project_id
            FROM m_workspace_read_state_projects_v1
            WHERE mode = 'compact'
            ORDER BY project_id
            LIMIT 1
            """
        ).fetchone()
        if project is None:
            session.execute(
                """
                INSERT INTO m_workspace_read_state_downgrade_v1 (project_id)
                SELECT project_id
                FROM m_workspace_read_state_projects_v1
                WHERE mode = 'rollback'
                ON CONFLICT (project_id) DO NOTHING
                """
            )
            session.commit()
            return
        project_id = project["project_id"]
        try:
            _lock_read_state_project(session, project_id)
            current = session.execute(
                """
                SELECT mode
                FROM m_workspace_read_state_projects_v1
                WHERE project_id = %s
                FOR UPDATE
                """,
                (project_id,),
            ).fetchone()
            if current is None or current["mode"] != "compact":
                session.commit()
                continue
            session.execute(
                """
                INSERT INTO m_workspace_read_state_downgrade_v1 (project_id)
                VALUES (%s)
                ON CONFLICT (project_id) DO NOTHING
                """,
                (project_id,),
            )
            # A completed cursor belongs to an earlier downgrade attempt if the
            # project became compact again. Reset it before enabling dual writes.
            session.execute(
                """
                UPDATE m_workspace_read_state_downgrade_v1
                SET last_created_at = NULL,
                    last_ingest_sequence = NULL,
                    last_message_uuid = NULL,
                    last_user_uuid = NULL,
                    processed_rows = 0,
                    completed_at = NULL,
                    updated_at = NOW()
                WHERE project_id = %s
                  AND completed_at IS NOT NULL
                """,
                (project_id,),
            )
            session.execute(
                """
                UPDATE m_workspace_read_state_projects_v1
                SET mode = 'rollback', updated_at = NOW()
                WHERE project_id = %s AND mode = 'compact'
                """,
                (project_id,),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise


def _hydrate_legacy_flags(session):
    _prepare_rollback_projects(session)
    while True:
        project = session.execute(
            """
            SELECT project.project_id
            FROM m_workspace_read_state_projects_v1 AS project
            JOIN m_workspace_read_state_downgrade_v1 AS progress
              ON progress.project_id = project.project_id
            WHERE project.mode = 'rollback'
              AND progress.completed_at IS NULL
            ORDER BY project.project_id
            LIMIT 1
            """
        ).fetchone()
        if project is None:
            return
        project_id = project["project_id"]
        try:
            # Application writers take the same transaction-scoped lock.  The
            # rollback mode keeps reads on bitmaps while every new write is
            # mirrored into legacy flags, so this lock can be released after
            # each bounded hydration batch without losing exactness.
            _lock_read_state_project(session, project_id)
            progress = session.execute(
                """
                SELECT progress.completed_at
                FROM m_workspace_read_state_projects_v1 AS project
                JOIN m_workspace_read_state_downgrade_v1 AS progress
                  ON progress.project_id = project.project_id
                WHERE project.project_id = %s
                  AND project.mode = 'rollback'
                FOR UPDATE OF project, progress
                """,
                (project_id,),
            ).fetchone()
            if progress is None or progress["completed_at"] is not None:
                session.commit()
                continue
            _hydrate_legacy_flags_batch(
                session,
                project_id,
                DOWNGRADE_BATCH_SIZE,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise


def _lock_hydrated_read_state_projects(session):
    while True:
        _hydrate_legacy_flags(session)
        locked_project_ids = _lock_read_state_projects(session)
        try:
            unfinished_project = session.execute(
                """
                SELECT project.project_id
                FROM m_workspace_read_state_projects_v1 AS project
                LEFT JOIN m_workspace_read_state_downgrade_v1 AS progress
                  ON progress.project_id = project.project_id
                WHERE project.mode = 'compact'
                   OR (
                        project.mode = 'rollback'
                        AND progress.completed_at IS NULL
                      )
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            session.rollback()
            raise
        if unfinished_project is None:
            return locked_project_ids
        session.rollback()


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0133-add-notification-setting-timestamps-52d0f8.py"]

    @property
    def migration_id(self):
        return "e84da8dc-97f6-4b10-bce7-f9652c0207a3"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_messages"
                ADD COLUMN "ingest_sequence" BIGINT;

            CREATE SEQUENCE "m_workspace_messages_ingest_sequence_v1_seq"
                AS BIGINT
                START WITH 281474976710656;
            ALTER SEQUENCE "m_workspace_messages_ingest_sequence_v1_seq"
                OWNED BY "m_workspace_messages"."ingest_sequence";
            CREATE SEQUENCE "m_workspace_messages_legacy_ingest_sequence_v1_seq"
                AS BIGINT
                START WITH 1
                MAXVALUE 281474976710655;
            ALTER TABLE "m_workspace_messages"
                ALTER COLUMN "ingest_sequence"
                SET DEFAULT nextval(
                    'm_workspace_messages_ingest_sequence_v1_seq'
                );

            CREATE FUNCTION "m_workspace_assign_ingest_sequence_v1"()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW."ingest_sequence" IS NULL THEN
                    NEW."ingest_sequence" := nextval(
                        'm_workspace_messages_ingest_sequence_v1_seq'
                    );
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER "m_workspace_assign_ingest_sequence_v1"
            BEFORE INSERT ON "m_workspace_messages"
            FOR EACH ROW
            EXECUTE FUNCTION "m_workspace_assign_ingest_sequence_v1"();

            CREATE TABLE "m_workspace_read_state_projects_v1" (
                "project_id" UUID PRIMARY KEY,
                "mode" VARCHAR(16) NOT NULL DEFAULT 'legacy',
                "structure_revision" BIGINT NOT NULL DEFAULT 0,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_read_state_projects_mode_check"
                    CHECK (
                        "mode" IN (
                            'legacy', 'preparing', 'dual', 'compact', 'rollback'
                        )
                    )
            );

            INSERT INTO "m_workspace_read_state_projects_v1" (
                "project_id", "mode"
            )
            SELECT DISTINCT "project_id", 'legacy'
            FROM "m_workspace_streams";

            CREATE TABLE "m_workspace_user_read_chunks_v1" (
                "user_uuid" UUID NOT NULL,
                "chunk_number" BIGINT NOT NULL,
                "read_bits" BIT(4096) NOT NULL DEFAULT B'0'::bit(4096),
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("user_uuid", "chunk_number"),
                CONSTRAINT "m_workspace_user_read_chunks_user_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_workspace_user_read_chunks_number_check"
                    CHECK ("chunk_number" >= 0)
            );

            CREATE TABLE "m_workspace_user_topic_read_stats_v1" (
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "topic_uuid" UUID NOT NULL,
                "read_count" BIGINT NOT NULL DEFAULT 0,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("project_id", "user_uuid", "topic_uuid"),
                CONSTRAINT "m_workspace_user_topic_read_stats_user_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_workspace_user_topic_read_stats_topic_fkey"
                    FOREIGN KEY ("topic_uuid")
                    REFERENCES "m_workspace_stream_topics" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_user_topic_read_stats_count_check"
                    CHECK ("read_count" >= 0)
            );

            CREATE TABLE "m_workspace_read_memberships_v1" (
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "stream_uuid" UUID NOT NULL,
                "last_detached_sequence" BIGINT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("project_id", "user_uuid", "stream_uuid"),
                CONSTRAINT "m_workspace_read_memberships_user_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_workspace_read_memberships_stream_fkey"
                    FOREIGN KEY ("stream_uuid")
                    REFERENCES "m_workspace_streams" ("uuid")
                    ON DELETE CASCADE
            );

            CREATE TABLE "m_workspace_message_mentions_v1" (
                "message_uuid" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "stream_uuid" UUID NOT NULL,
                "topic_uuid" UUID NOT NULL,
                "ingest_sequence" BIGINT NOT NULL,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("message_uuid", "user_uuid"),
                CONSTRAINT "m_workspace_message_mentions_message_fkey"
                    FOREIGN KEY ("message_uuid")
                    REFERENCES "m_workspace_messages" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_message_mentions_user_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE
            );

            CREATE INDEX "m_workspace_message_mentions_user_topic_idx"
                ON "m_workspace_message_mentions_v1" (
                    "project_id", "user_uuid", "topic_uuid", "ingest_sequence"
                );

            CREATE TABLE "m_workspace_topic_message_stats_v1" (
                "topic_uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "stream_uuid" UUID NOT NULL,
                "message_count" BIGINT NOT NULL DEFAULT 0,
                "last_ingest_sequence" BIGINT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_topic_message_stats_topic_fkey"
                    FOREIGN KEY ("topic_uuid")
                    REFERENCES "m_workspace_stream_topics" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_topic_message_stats_count_check"
                    CHECK ("message_count" >= 0)
            );

            CREATE INDEX "m_workspace_topic_message_stats_stream_idx"
                ON "m_workspace_topic_message_stats_v1" (
                    "project_id", "stream_uuid", "topic_uuid"
                );

            CREATE TABLE "m_workspace_read_state_compaction_v1" (
                "project_id" UUID PRIMARY KEY,
                "phase" VARCHAR(32) NOT NULL DEFAULT 'flags',
                "last_message_uuid" UUID,
                "last_user_uuid" UUID,
                "last_ingest_sequence" BIGINT NOT NULL DEFAULT 0,
                "target_ingest_sequence" BIGINT,
                "processed_rows" BIGINT NOT NULL DEFAULT 0,
                "completed_at" TIMESTAMPTZ,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_read_state_compaction_phase_check"
                    CHECK (
                        "phase" IN (
                            'sequences', 'memberships', 'flags', 'stats', 'mentions',
                            'verify', 'verify_chunks',
                            'verify_stats', 'verify_read_stats',
                            'verify_mentions'
                        )
                    ),
                CONSTRAINT "m_workspace_read_state_compaction_count_check"
                    CHECK ("processed_rows" >= 0)
            );

            ALTER TABLE "m_external_provider_operations_v1"
                DROP CONSTRAINT
                    "m_external_provider_operations_v1_external_operation_uuid_key",
                ADD COLUMN "public_result_status" TEXT,
                ADD COLUMN "terminal_result" JSONB,
                ADD CONSTRAINT "m_external_provider_operations_public_result_check"
                    CHECK (
                        "public_result_status" IS NULL
                        OR "public_result_status" IN (
                            'succeeded', 'failed',
                            'manual_reconciliation_required'
                        )
                    );
            CREATE INDEX "m_external_provider_operations_external_operation_idx"
                ON "m_external_provider_operations_v1" (
                    "external_operation_uuid", "sequence"
                );

            """
        )
        session.execute(USER_MESSAGES_VIEW_SQL)
        session.execute(UNREAD_MESSAGE_BASE_VIEW_SQL)
        session.execute(TOPIC_UNREAD_COUNTS_VIEW_SQL)
        session.execute(UNREAD_USER_MESSAGES_VIEW_SQL)
        session.execute(_user_streams_view_sql(compact_counts=True))
        session.execute(_user_topics_view_sql(compact_counts=True))

    def downgrade(self, session):
        schema_exists = session.execute(
            """
            SELECT to_regclass('m_workspace_read_state_projects_v1')
                AS relation
            """
        ).fetchone()["relation"]
        # The schema swap is committed inside this migration so its bounded
        # hydration progress survives restarts.  RestAlchemy records the
        # migration as unapplied only after downgrade() returns; if the process
        # dies in that narrow window, a retry must finish as a no-op.
        if schema_exists is None:
            return
        _ensure_no_active_aggregate_provider_reads(session)
        _prepare_downgrade_progress(session)
        _lock_hydrated_read_state_projects(session)
        try:
            _ensure_no_active_aggregate_provider_reads(session)
            session.execute(
                """
                UPDATE m_workspace_read_state_projects_v1
                SET mode = 'legacy', updated_at = NOW()
                WHERE mode <> 'legacy'
                """
            )
            session.execute(_user_topics_view_sql(compact_counts=False))
            session.execute(_user_streams_view_sql(compact_counts=False))
            session.execute(PREVIOUS_UNREAD_USER_MESSAGES_VIEW_SQL)
            session.execute(
                'DROP VIEW IF EXISTS "m_workspace_user_topic_unread_counts_v1";'
            )
            session.execute(PREVIOUS_UNREAD_MESSAGE_BASE_VIEW_SQL)
            session.execute(PREVIOUS_USER_MESSAGES_VIEW_SQL)
            session.execute(
                """
                DROP TABLE IF EXISTS "m_workspace_read_state_downgrade_v1";
                DROP INDEX IF EXISTS
                    "m_external_provider_operations_external_operation_idx";
                ALTER TABLE "m_external_provider_operations_v1"
                    DROP COLUMN IF EXISTS "public_result_status",
                    DROP COLUMN IF EXISTS "terminal_result",
                    ADD CONSTRAINT
                        "m_external_provider_operations_v1_external_operation_uuid_key"
                        UNIQUE ("external_operation_uuid");
                DROP TABLE IF EXISTS "m_workspace_read_state_compaction_v1";
                DROP TABLE IF EXISTS "m_workspace_topic_message_stats_v1";
                DROP TABLE IF EXISTS "m_workspace_message_mentions_v1";
                DROP TABLE IF EXISTS "m_workspace_read_memberships_v1";
                DROP TABLE IF EXISTS "m_workspace_user_topic_read_stats_v1";
                DROP TABLE IF EXISTS "m_workspace_user_read_chunks_v1";
                DROP TABLE IF EXISTS "m_workspace_read_state_projects_v1";
                DROP TRIGGER IF EXISTS "m_workspace_assign_ingest_sequence_v1"
                    ON "m_workspace_messages";
                DROP FUNCTION IF EXISTS
                    "m_workspace_assign_ingest_sequence_v1"();
                DROP INDEX IF EXISTS
                    "m_workspace_read_flags_project_message_idx";
                DROP INDEX IF EXISTS
                    "m_workspace_flags_project_message_user_idx";
                DROP INDEX IF EXISTS
                    "m_workspace_messages_stream_ingest_sequence_idx";
                DROP INDEX IF EXISTS
                    "m_workspace_messages_topic_read_page_idx";
                DROP INDEX IF EXISTS
                    "m_workspace_messages_stream_read_page_idx";
                DROP INDEX IF EXISTS
                    "m_workspace_messages_topic_ingest_sequence_idx";
                DROP INDEX IF EXISTS
                    "m_workspace_messages_project_ingest_sequence_idx";
                DROP INDEX IF EXISTS
                    "m_workspace_messages_ingest_sequence_idx";
                ALTER TABLE "m_workspace_messages"
                    DROP COLUMN IF EXISTS "ingest_sequence";
                DROP SEQUENCE IF EXISTS
                    "m_workspace_messages_ingest_sequence_v1_seq";
                DROP SEQUENCE IF EXISTS
                    "m_workspace_messages_legacy_ingest_sequence_v1_seq";
                """
            )
            session.commit()
        except Exception:
            session.rollback()
            raise


migration_step = MigrationStep()
