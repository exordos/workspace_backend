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


PROJECT_SEQUENCE_RANGE_SIZE = 4_294_967_296


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        # Dependency order is part of the forward-only compatibility contract:
        # stage compact 0137/0138 databases, run the frozen 0139 rewrite, then
        # rebuild coordinate-bound compact state before the transaction commits.
        self._depends = [
            "0142-prepare-compact-dense-sequence-upgrade-0c93a1.py",
            "0141-forward-correct-published-read-state-migrations-60f5ca.py",
        ]

    @property
    def migration_id(self):
        return "1ce3ae70-7ad1-447b-a7ca-e14318e38f98"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        staged = session.execute(
            """
            SELECT to_regclass('m_workspace_dense_compact_projects_v1')
                AS relation
            """
        ).fetchone()["relation"]
        if staged is None:
            return

        session.execute(
            f"""
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number, read_bits, created_at, updated_at
            )
            SELECT
                staged_read.user_uuid,
                message.ingest_sequence / 4096,
                bit_or(
                    set_bit(
                        B'0'::bit(4096),
                        (message.ingest_sequence % 4096)::integer,
                        1
                    )
                ),
                NOW(),
                NOW()
            FROM m_workspace_dense_read_messages_v1 AS staged_read
            JOIN m_workspace_messages AS message
              ON message.uuid = staged_read.message_uuid
            GROUP BY staged_read.user_uuid, message.ingest_sequence / 4096;

            INSERT INTO m_workspace_message_mentions_v1 (
                message_uuid, user_uuid, project_id, stream_uuid, topic_uuid,
                ingest_sequence, created_at
            )
            SELECT
                staged_mention.message_uuid,
                staged_mention.user_uuid,
                staged_mention.project_id,
                staged_mention.stream_uuid,
                staged_mention.topic_uuid,
                message.ingest_sequence,
                staged_mention.created_at
            FROM m_workspace_dense_message_mentions_v1 AS staged_mention
            JOIN m_workspace_messages AS message
              ON message.uuid = staged_mention.message_uuid;

            INSERT INTO m_workspace_user_topic_read_stats_v1
            SELECT *
            FROM m_workspace_dense_user_topic_read_stats_v1;

            INSERT INTO m_workspace_topic_message_stats_v1 (
                topic_uuid, project_id, stream_uuid, message_count,
                last_ingest_sequence, created_at, updated_at
            )
            SELECT
                staged_stats.topic_uuid,
                staged_stats.project_id,
                staged_stats.stream_uuid,
                staged_stats.message_count,
                MAX(message.ingest_sequence),
                staged_stats.created_at,
                staged_stats.updated_at
            FROM m_workspace_dense_topic_message_stats_v1 AS staged_stats
            LEFT JOIN m_workspace_messages AS message
              ON message.project_id = staged_stats.project_id
             AND message.topic_uuid = staged_stats.topic_uuid
            GROUP BY
                staged_stats.topic_uuid,
                staged_stats.project_id,
                staged_stats.stream_uuid,
                staged_stats.message_count,
                staged_stats.created_at,
                staged_stats.updated_at;

            INSERT INTO m_workspace_read_state_compaction_v1 (
                project_id, phase, last_message_uuid, last_user_uuid,
                last_ingest_sequence, target_ingest_sequence, processed_rows,
                completed_at, created_at, updated_at
            )
            SELECT
                staged_progress.project_id,
                staged_progress.phase,
                staged_progress.last_message_uuid,
                staged_progress.last_user_uuid,
                COALESCE(MAX(message.ingest_sequence), 0),
                COALESCE(MAX(message.ingest_sequence), 0),
                staged_progress.processed_rows,
                staged_progress.completed_at,
                staged_progress.created_at,
                staged_progress.updated_at
            FROM m_workspace_dense_read_state_compaction_v1 AS staged_progress
            LEFT JOIN m_workspace_messages AS message
              ON message.project_id = staged_progress.project_id
            GROUP BY
                staged_progress.project_id,
                staged_progress.phase,
                staged_progress.last_message_uuid,
                staged_progress.last_user_uuid,
                staged_progress.processed_rows,
                staged_progress.completed_at,
                staged_progress.created_at,
                staged_progress.updated_at;

            UPDATE m_workspace_read_memberships_v1 AS membership
            SET last_detached_sequence = COALESCE(
                    boundary_message.ingest_sequence,
                    project_range.range_number * {PROJECT_SEQUENCE_RANGE_SIZE},
                    0
                )
            FROM m_workspace_dense_membership_boundaries_v1 AS staged_boundary
            LEFT JOIN m_workspace_messages AS boundary_message
              ON boundary_message.uuid = staged_boundary.boundary_message_uuid
             AND boundary_message.project_id = staged_boundary.project_id
             AND boundary_message.stream_uuid = staged_boundary.stream_uuid
            LEFT JOIN m_workspace_project_ingest_ranges_v2 AS project_range
              ON project_range.project_id = staged_boundary.project_id
            WHERE membership.project_id = staged_boundary.project_id
              AND membership.stream_uuid = staged_boundary.stream_uuid
              AND membership.user_uuid = staged_boundary.user_uuid;

            UPDATE m_workspace_read_state_projects_v1 AS state
            SET mode = 'compact', updated_at = NOW()
            FROM m_workspace_dense_compact_projects_v1 AS project
            WHERE state.project_id = project.project_id;

            DROP TABLE m_workspace_dense_read_state_compaction_v1;
            DROP TABLE m_workspace_dense_topic_message_stats_v1;
            DROP TABLE m_workspace_dense_user_topic_read_stats_v1;
            DROP TABLE m_workspace_dense_message_mentions_v1;
            DROP TABLE m_workspace_dense_read_messages_v1;
            DROP TABLE m_workspace_dense_membership_boundaries_v1;
            DROP TABLE m_workspace_dense_compact_projects_v1;
            """
        )

    def downgrade(self, session):
        # Refuse even a direct HEAD rollback: RestAlchemy does not expose the
        # root target to this hook, so it cannot distinguish that case from a
        # recursive rollback that will later renumber the frozen 0139 state.
        raise RuntimeError(
            "Compact dense sequence compatibility migration is forward-only; "
            "restore a database backup to roll it back"
        )


migration_step = MigrationStep()
