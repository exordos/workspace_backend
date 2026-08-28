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


LEGACY_UNREAD_SELECT_SQL = """
SELECT
    message.uuid AS message_uuid,
    message.stream_uuid,
    message.topic_uuid,
    message_flags.user_uuid,
    message_flags.project_id,
    POSITION(
        '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
        IN LOWER(COALESCE(message.payload->>'content', ''))
    ) > 0 AS mentioned,
    CASE COALESCE(topic_flags.notification_mode, 'default')
        WHEN 'mute' THEN FALSE
        WHEN 'follow' THEN TRUE
        WHEN 'unmute' THEN POSITION(
            '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
            IN LOWER(COALESCE(message.payload->>'content', ''))
        ) > 0
        ELSE CASE binding.notification_mode
            WHEN 'all_messages' THEN TRUE
            WHEN 'mentions_only' THEN POSITION(
                '](' || 'urn:user:' || LOWER(binding.user_uuid::text) || ')'
                IN LOWER(COALESCE(message.payload->>'content', ''))
            ) > 0
            ELSE FALSE
        END
    END AS active
FROM "m_workspace_user_message_flags" AS message_flags
JOIN LATERAL (
    SELECT TRUE AS allowed
    WHERE NOT EXISTS (
        SELECT 1
        FROM "m_workspace_read_state_projects_v1" AS project
        WHERE project.project_id = message_flags.project_id
          AND project.mode IN ('compact', 'rollback')
    )
    OFFSET 0
) AS legacy_mode ON TRUE
JOIN LATERAL (
    SELECT
        stored_message.uuid,
        stored_message.stream_uuid,
        stored_message.topic_uuid,
        stored_message.project_id,
        stored_message.payload
    FROM "m_workspace_messages" AS stored_message
    WHERE stored_message.uuid = message_flags.uuid
      AND stored_message.project_id = message_flags.project_id
      AND legacy_mode.allowed
    OFFSET 0
) AS message ON TRUE
JOIN "m_workspace_stream_bindings" AS binding
  ON binding.stream_uuid = message.stream_uuid
 AND binding.project_id = message.project_id
 AND binding.user_uuid = message_flags.user_uuid
LEFT JOIN "m_workspace_user_topic_flags" AS topic_flags
  ON topic_flags.uuid = message.topic_uuid
 AND topic_flags.project_id = message.project_id
 AND topic_flags.user_uuid = binding.user_uuid
WHERE message_flags.read = FALSE
"""


COMPACT_UNREAD_SELECT_SQL = """
SELECT
    message.uuid AS message_uuid,
    message.stream_uuid,
    message.topic_uuid,
    binding.user_uuid,
    message.project_id,
    mention.message_uuid IS NOT NULL AS mentioned,
    CASE COALESCE(topic_flags.notification_mode, 'default')
        WHEN 'mute' THEN FALSE
        WHEN 'follow' THEN TRUE
        WHEN 'unmute' THEN mention.message_uuid IS NOT NULL
        ELSE CASE binding.notification_mode
            WHEN 'all_messages' THEN TRUE
            WHEN 'mentions_only' THEN mention.message_uuid IS NOT NULL
            ELSE FALSE
        END
    END AS active
FROM "m_workspace_read_state_projects_v1" AS project
JOIN "m_workspace_messages" AS message
  ON message.project_id = project.project_id
JOIN "m_workspace_stream_bindings" AS binding
  ON binding.stream_uuid = message.stream_uuid
 AND binding.project_id = message.project_id
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
WHERE project.mode IN ('compact', 'rollback')
  AND COALESCE(
        get_bit(
            chunk.read_bits,
            (message.ingest_sequence % 4096)::integer
        ),
        0
      ) = 0
"""


UNREAD_MESSAGE_BASE_VIEW_SQL = f"""
CREATE OR REPLACE VIEW "m_workspace_user_unread_messages_base_v1" AS
{LEGACY_UNREAD_SELECT_SQL}
UNION ALL
{COMPACT_UNREAD_SELECT_SQL};
"""


PREVIOUS_UNREAD_MESSAGE_BASE_VIEW_SQL = """
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


def _topic_unread_counts_view_sql(*, split_legacy_branch):
    legacy_source = (
        f"({LEGACY_UNREAD_SELECT_SQL}) AS unread"
        if split_legacy_branch
        else '"m_workspace_user_unread_messages_base_v1" AS unread'
    )
    legacy_mode_join = (
        ""
        if split_legacy_branch
        else """
    LEFT JOIN "m_workspace_read_state_projects_v1" AS project
      ON project.project_id = unread.project_id
        """
    )
    legacy_mode_filter = (
        ""
        if split_legacy_branch
        else "WHERE COALESCE(project.mode, 'legacy') NOT IN ('compact', 'rollback')"
    )
    return f"""
CREATE OR REPLACE VIEW "m_workspace_user_topic_unread_counts_v1" AS
WITH legacy_counts AS (
    SELECT
        unread.stream_uuid,
        unread.topic_uuid,
        unread.user_uuid,
        unread.project_id,
        COUNT(*) AS unread_count,
        COUNT(*) FILTER (WHERE unread.active)::bigint AS active_unread_count
    FROM {legacy_source}
    {legacy_mode_join}
    {legacy_mode_filter}
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


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0148-join-topic-summary-reasoning-head-4588d6.py"]

    @property
    def migration_id(self):
        return "c84ae9cb-d3c1-4385-88b8-0b2c156d2cb5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UNREAD_MESSAGE_BASE_VIEW_SQL)
        session.execute(_topic_unread_counts_view_sql(split_legacy_branch=True))

    def downgrade(self, session):
        session.execute(PREVIOUS_UNREAD_MESSAGE_BASE_VIEW_SQL)
        session.execute(_topic_unread_counts_view_sql(split_legacy_branch=False))


migration_step = MigrationStep()
