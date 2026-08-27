# Copyright 2026 Genesis Corporation.
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
PROJECT_DENSE_READ_SEQUENCE_MIGRATION_UUID = (
    "1bca8f2b-147f-4af8-b6e4-8078a3be253b"
)


def _published_dense_sequence_state(session):
    applied = session.execute(
        """
        SELECT applied
        FROM ra_migrations
        WHERE uuid = %s
        """,
        (PROJECT_DENSE_READ_SEQUENCE_MIGRATION_UUID,),
    ).fetchone()
    schema_exists = (
        session.execute(
            """
            SELECT to_regclass('m_workspace_project_ingest_ranges_v2')
                AS relation
            """
        ).fetchone()["relation"]
        is not None
    )
    is_applied = applied is not None and applied["applied"] is True
    if is_applied != schema_exists:
        raise RuntimeError(
            "Published project-dense migration metadata and schema disagree"
        )
    return is_applied


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0138-harden-lazy-provider-read-rolling-fences-1b0b01.py"]

    @property
    def migration_id(self):
        return "0c93a123-8205-43cf-93dc-29031e06f2a7"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Published databases already use stable project-dense coordinates and
        # must not have their compact bitmaps rewritten.
        if _published_dense_sequence_state(session):
            return

        # This lock is retained by the outer RestAlchemy migration transaction
        # through 0139 and 0143. It fences all read-state writers while the
        # coordinate-bound rows are staged, messages are renumbered, and the
        # compact representation is rebuilt.
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute("LOCK TABLE m_workspace_messages IN ACCESS EXCLUSIVE MODE")

        snapshot = session.execute(
            """
            SELECT 1
            FROM m_external_provider_read_snapshots_v1
            LIMIT 1
            """
        ).fetchone()
        if snapshot is not None:
            raise RuntimeError(
                "Project-dense read sequence migration requires provider read "
                "snapshots to be drained first"
            )
        transitional = session.execute(
            """
            SELECT project_id
            FROM m_workspace_read_state_projects_v1
            WHERE mode NOT IN ('legacy', 'compact')
            LIMIT 1
            """
        ).fetchone()
        if transitional is not None:
            raise RuntimeError(
                "Project-dense read sequence migration requires every read-state "
                "project to be stable"
            )
        incomplete = session.execute(
            """
            SELECT project.project_id
            FROM m_workspace_read_state_projects_v1 AS project
            LEFT JOIN m_workspace_read_state_compaction_v1 AS progress
              ON progress.project_id = project.project_id
            WHERE project.mode = 'compact'
              AND (progress.project_id IS NULL OR progress.completed_at IS NULL)
            LIMIT 1
            """
        ).fetchone()
        if incomplete is not None:
            raise RuntimeError(
                "Project-dense read sequence migration requires compact "
                "projects to have completed compaction metadata"
            )

        non_prefix = session.execute(
            """
            SELECT 1
            FROM m_workspace_read_memberships_v1 AS membership
            JOIN LATERAL (
                SELECT message.created_at, message.uuid
                FROM m_workspace_messages AS message
                WHERE message.project_id = membership.project_id
                  AND message.stream_uuid = membership.stream_uuid
                  AND message.ingest_sequence <=
                        membership.last_detached_sequence
                ORDER BY message.created_at DESC, message.uuid DESC
                LIMIT 1
            ) AS prefix_tail ON TRUE
            JOIN LATERAL (
                SELECT message.created_at, message.uuid
                FROM m_workspace_messages AS message
                WHERE message.project_id = membership.project_id
                  AND message.stream_uuid = membership.stream_uuid
                  AND message.ingest_sequence >
                        membership.last_detached_sequence
                ORDER BY message.created_at, message.uuid
                LIMIT 1
            ) AS suffix_head ON TRUE
            WHERE membership.last_detached_sequence IS NOT NULL
              AND (suffix_head.created_at, suffix_head.uuid) <
                    (prefix_tail.created_at, prefix_tail.uuid)
            LIMIT 1
            """
        ).fetchone()
        if non_prefix is not None:
            raise RuntimeError(
                "Project-dense read sequence migration cannot preserve a "
                "detached membership boundary across reordered backfill"
            )

        session.execute(
            """
            CREATE TABLE m_workspace_dense_compact_projects_v1 AS
            SELECT project_id
            FROM m_workspace_read_state_projects_v1
            WHERE mode = 'compact';
            ALTER TABLE m_workspace_dense_compact_projects_v1
                ADD PRIMARY KEY (project_id);

            CREATE TABLE m_workspace_dense_membership_boundaries_v1 (
                project_id UUID NOT NULL,
                stream_uuid UUID NOT NULL,
                user_uuid UUID NOT NULL,
                boundary_message_uuid UUID,
                PRIMARY KEY (project_id, stream_uuid, user_uuid)
            );
            INSERT INTO m_workspace_dense_membership_boundaries_v1 (
                project_id, stream_uuid, user_uuid, boundary_message_uuid
            )
            SELECT
                membership.project_id,
                membership.stream_uuid,
                membership.user_uuid,
                boundary.uuid
            FROM m_workspace_read_memberships_v1 AS membership
            LEFT JOIN LATERAL (
                SELECT message.uuid
                FROM m_workspace_messages AS message
                WHERE message.project_id = membership.project_id
                  AND message.stream_uuid = membership.stream_uuid
                  AND message.ingest_sequence <=
                        membership.last_detached_sequence
                ORDER BY message.created_at DESC, message.uuid DESC
                LIMIT 1
            ) AS boundary ON TRUE
            WHERE membership.last_detached_sequence IS NOT NULL;

            -- Enumerate only set bits. A compact project with 500,000 unread
            -- messages therefore stages no per-message unread rows; work is
            -- proportional to the number of logical reads, not user x message.
            CREATE TABLE m_workspace_dense_read_messages_v1 AS
            SELECT chunk.user_uuid, message.uuid AS message_uuid
            FROM m_workspace_user_read_chunks_v1 AS chunk
            CROSS JOIN LATERAL (
                WITH RECURSIVE set_bits(position) AS (
                    SELECT strpos(chunk.read_bits::text, '1')
                    UNION ALL
                    SELECT
                        position + strpos(
                            substr(chunk.read_bits::text, position + 1),
                            '1'
                        )
                    FROM set_bits
                    WHERE position > 0
                      AND strpos(
                            substr(chunk.read_bits::text, position + 1),
                            '1'
                          ) > 0
                )
                SELECT position - 1 AS bit_offset
                FROM set_bits
                WHERE position > 0
            ) AS bit
            JOIN m_workspace_messages AS message
              ON message.ingest_sequence =
                    chunk.chunk_number * 4096 + bit.bit_offset
            JOIN m_workspace_dense_compact_projects_v1 AS project
              ON project.project_id = message.project_id;
            ALTER TABLE m_workspace_dense_read_messages_v1
                ADD PRIMARY KEY (user_uuid, message_uuid);

            CREATE TABLE m_workspace_dense_message_mentions_v1 AS
            SELECT mention.*
            FROM m_workspace_message_mentions_v1 AS mention
            JOIN m_workspace_dense_compact_projects_v1 AS project
              ON project.project_id = mention.project_id;

            CREATE TABLE m_workspace_dense_user_topic_read_stats_v1 AS
            SELECT stats.*
            FROM m_workspace_user_topic_read_stats_v1 AS stats
            JOIN m_workspace_dense_compact_projects_v1 AS project
              ON project.project_id = stats.project_id;

            CREATE TABLE m_workspace_dense_topic_message_stats_v1 AS
            SELECT stats.*
            FROM m_workspace_topic_message_stats_v1 AS stats
            JOIN m_workspace_dense_compact_projects_v1 AS project
              ON project.project_id = stats.project_id;

            CREATE TABLE m_workspace_dense_read_state_compaction_v1 AS
            SELECT progress.*
            FROM m_workspace_read_state_compaction_v1 AS progress
            JOIN m_workspace_dense_compact_projects_v1 AS project
              ON project.project_id = progress.project_id;

            UPDATE m_workspace_read_state_projects_v1 AS state
            SET mode = 'legacy', updated_at = NOW()
            FROM m_workspace_dense_compact_projects_v1 AS project
            WHERE state.project_id = project.project_id;

            DELETE FROM m_workspace_read_state_compaction_v1;
            DELETE FROM m_workspace_topic_message_stats_v1;
            DELETE FROM m_workspace_message_mentions_v1;
            DELETE FROM m_workspace_user_topic_read_stats_v1;
            DELETE FROM m_workspace_user_read_chunks_v1;
            """
        )

    def downgrade(self, session):
        # RestAlchemy may visit this sibling on either side of 0139 and older
        # dependency downgrades commit internal batches. There is no safe,
        # atomic post-renumber hook, so this compatibility fork is forward-only.
        raise RuntimeError(
            "Compact dense sequence compatibility migration is forward-only; "
            "restore a database backup to roll it back"
        )


migration_step = MigrationStep()
