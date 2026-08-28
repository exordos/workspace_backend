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


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0149-split-messenger-unread-read-state-branches-c84ae9.py"
        ]

    @property
    def migration_id(self):
        return "8e694871-17e9-4510-941d-c576aee5c2b4"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_read_state_compaction_v1"
                ADD COLUMN legacy_gap_repair_kind VARCHAR(16),
                ADD CONSTRAINT
                    m_workspace_read_state_legacy_gap_kind_check
                    CHECK (
                        legacy_gap_repair_kind IS NULL
                        OR legacy_gap_repair_kind IN (
                            'full_pending', 'full_done'
                        )
                    ),
                DROP CONSTRAINT
                    "m_workspace_read_state_compaction_phase_check",
                ADD CONSTRAINT
                    "m_workspace_read_state_compaction_phase_check"
                    CHECK (
                        "phase" IN (
                            'sequences', 'memberships', 'flags',
                            'legacy_gaps', 'stats', 'mentions', 'verify',
                            'verify_chunks', 'verify_stats',
                            'verify_read_stats', 'verify_mentions'
                        )
                    );

            CREATE OR REPLACE FUNCTION
                m_workspace_fence_legacy_gap_cutover_v1()
            RETURNS TRIGGER AS $$
            DECLARE
                repair_kind VARCHAR(16);
                repair_phase VARCHAR(32);
            BEGIN
                PERFORM pg_advisory_xact_lock_shared(
                    hashtextextended('workspace-read-state-schema-v1', 0)
                );
                IF OLD.mode = 'dual' AND NEW.mode = 'compact' THEN
                    SELECT progress.legacy_gap_repair_kind, progress.phase
                    INTO repair_kind, repair_phase
                    FROM m_workspace_read_state_compaction_v1 AS progress
                    WHERE progress.project_id = OLD.project_id;
                    IF repair_kind IS DISTINCT FROM 'full_done'
                       OR repair_phase IS DISTINCT FROM 'verify_mentions'
                       OR current_setting(
                            'workspace.legacy_gap_cutover_v1', TRUE
                          ) IS DISTINCT FROM OLD.project_id::text
                       OR NOT EXISTS (
                            SELECT 1
                            FROM pg_index AS target_index
                            WHERE target_index.indexrelid = to_regclass(
                                'm_workspace_read_memberships_stream_user_idx'
                            )
                              AND target_index.indisready
                              AND target_index.indisvalid
                          ) THEN
                        NEW.mode := OLD.mode;
                        UPDATE m_workspace_read_state_compaction_v1
                        SET phase = 'legacy_gaps',
                            legacy_gap_repair_kind = 'full_pending',
                            last_message_uuid = NULL,
                            last_user_uuid = NULL,
                            last_ingest_sequence = 0,
                            completed_at = NULL,
                            updated_at = NOW()
                        WHERE project_id = OLD.project_id;
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS
                m_workspace_fence_legacy_gap_cutover_v1
                ON m_workspace_read_state_projects_v1;
            CREATE TRIGGER m_workspace_fence_legacy_gap_cutover_v1
                BEFORE UPDATE OF mode
                ON m_workspace_read_state_projects_v1
                FOR EACH ROW
                EXECUTE FUNCTION m_workspace_fence_legacy_gap_cutover_v1();

            CREATE OR REPLACE FUNCTION
                m_workspace_hold_legacy_gap_progress_v1()
            RETURNS TRIGGER AS $$
            BEGIN
                PERFORM pg_advisory_xact_lock_shared(
                    hashtextextended('workspace-read-state-schema-v1', 0)
                );
                IF OLD.phase = 'legacy_gaps'
                   AND OLD.legacy_gap_repair_kind = 'full_pending'
                   AND EXISTS (
                        SELECT 1
                        FROM m_workspace_read_state_projects_v1 AS state
                        WHERE state.project_id = NEW.project_id
                          AND state.mode = 'dual'
                   )
                   AND current_setting(
                        'workspace.legacy_gap_scan_v1', TRUE
                       ) IS DISTINCT FROM OLD.project_id::text THEN
                    IF NEW.phase = OLD.phase
                       AND NEW.legacy_gap_repair_kind =
                            OLD.legacy_gap_repair_kind
                       AND NEW.last_message_uuid IS NULL
                       AND NEW.last_user_uuid IS NULL
                       AND NEW.last_ingest_sequence = 0
                       AND NEW.completed_at IS NULL THEN
                        RETURN NEW;
                    END IF;
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS
                m_workspace_hold_legacy_gap_progress_v1
                ON m_workspace_read_state_compaction_v1;
            CREATE TRIGGER m_workspace_hold_legacy_gap_progress_v1
                BEFORE UPDATE
                ON m_workspace_read_state_compaction_v1
                FOR EACH ROW
                EXECUTE FUNCTION m_workspace_hold_legacy_gap_progress_v1();

            """
        )

    def downgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM m_workspace_read_state_compaction_v1
                    WHERE legacy_gap_repair_kind IS NOT NULL
                       OR phase = 'legacy_gaps'
                ) THEN
                    RAISE EXCEPTION
                        'legacy gap repair must finish before downgrade';
                END IF;
            END;
            $$;

            DROP TRIGGER IF EXISTS
                m_workspace_fence_legacy_gap_cutover_v1
                ON m_workspace_read_state_projects_v1;
            DROP FUNCTION IF EXISTS
                m_workspace_fence_legacy_gap_cutover_v1();
            DROP TRIGGER IF EXISTS
                m_workspace_hold_legacy_gap_progress_v1
                ON m_workspace_read_state_compaction_v1;
            DROP FUNCTION IF EXISTS
                m_workspace_hold_legacy_gap_progress_v1();

            DROP INDEX IF EXISTS
                m_workspace_read_memberships_stream_user_idx;

            ALTER TABLE "m_workspace_read_state_compaction_v1"
                DROP CONSTRAINT
                    m_workspace_read_state_legacy_gap_kind_check,
                DROP COLUMN legacy_gap_repair_kind,
                DROP CONSTRAINT
                    "m_workspace_read_state_compaction_phase_check",
                ADD CONSTRAINT
                    "m_workspace_read_state_compaction_phase_check"
                    CHECK (
                        "phase" IN (
                            'sequences', 'memberships', 'flags', 'stats',
                            'mentions', 'verify', 'verify_chunks',
                            'verify_stats', 'verify_read_stats',
                            'verify_mentions'
                        )
                    );
            """
        )


migration_step = MigrationStep()
