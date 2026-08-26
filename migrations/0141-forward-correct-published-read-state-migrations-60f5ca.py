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
PROJECT_SEQUENCE_RANGE_SIZE = 4_294_967_296
PROJECT_SEQUENCE_LIVE_START = 2_147_483_648


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0140-add-resumable-provider-read-downgrade-68c9b8.py"]

    @property
    def migration_id(self):
        return "60f5cad2-fe10-4df3-bced-2a248497afd1"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute("LOCK TABLE m_workspace_messages IN ACCESS EXCLUSIVE MODE")
        session.execute(
            f"""
            ALTER TABLE m_workspace_project_ingest_ranges_v2
                DROP CONSTRAINT m_workspace_project_ingest_ranges_local_check;
            ALTER TABLE m_workspace_project_ingest_ranges_v2
                RENAME COLUMN next_local_sequence TO last_backfill_sequence;
            ALTER TABLE m_workspace_project_ingest_ranges_v2
                ALTER COLUMN last_backfill_sequence SET DEFAULT 0,
                ADD COLUMN last_live_sequence BIGINT NOT NULL
                    DEFAULT {PROJECT_SEQUENCE_LIVE_START - 1},
                ADD CONSTRAINT
                    m_workspace_project_ingest_ranges_backfill_check
                    CHECK (
                        last_backfill_sequence BETWEEN 0 AND
                            {PROJECT_SEQUENCE_LIVE_START - 1}
                    ),
                ADD CONSTRAINT m_workspace_project_ingest_ranges_live_check
                    CHECK (
                        last_live_sequence BETWEEN
                            {PROJECT_SEQUENCE_LIVE_START - 1} AND
                            {PROJECT_SEQUENCE_RANGE_SIZE - 1}
                    );

            UPDATE m_workspace_project_ingest_ranges_v2 AS project_range
            SET last_live_sequence = COALESCE(
                    (
                        SELECT MAX(
                            message.ingest_sequence -
                                project_range.range_number *
                                    {PROJECT_SEQUENCE_RANGE_SIZE}
                        )
                        FROM m_workspace_messages AS message
                        WHERE message.project_id = project_range.project_id
                          AND message.ingest_sequence >=
                                project_range.range_number *
                                    {PROJECT_SEQUENCE_RANGE_SIZE} +
                                    {PROJECT_SEQUENCE_LIVE_START}
                          AND message.ingest_sequence <
                                (project_range.range_number + 1) *
                                    {PROJECT_SEQUENCE_RANGE_SIZE}
                    ),
                    {PROJECT_SEQUENCE_LIVE_START - 1}
                );

            -- The published backend exposes the physical provider-operation
            -- UUID for every read delivery. Keep that client contract and
            -- remove an internal marker that an old worker would return as
            -- ordinary payload during a rolling deployment.
            UPDATE m_external_provider_operations_v1
            SET payload = payload - '_workspace_response_revision'
            WHERE operation_kind = 'read_state.set'
              AND payload ? '_workspace_response_revision';
            UPDATE m_external_provider_read_snapshots_v1
            SET payload = payload - '_workspace_response_revision'
            WHERE payload ? '_workspace_response_revision';

            CREATE OR REPLACE FUNCTION m_workspace_assign_ingest_sequence_v1()
            RETURNS TRIGGER AS $$
            DECLARE
                allocated_sequence BIGINT;
            BEGIN
                IF NEW.ingest_sequence IS NOT NULL THEN
                    RETURN NEW;
                END IF;
                LOOP
                    UPDATE m_workspace_project_ingest_ranges_v2
                    SET last_live_sequence = last_live_sequence + 1,
                        updated_at = NOW()
                    WHERE project_id = NEW.project_id
                      AND last_live_sequence <
                            {PROJECT_SEQUENCE_RANGE_SIZE - 1}
                    RETURNING
                        range_number * {PROJECT_SEQUENCE_RANGE_SIZE}
                            + last_live_sequence
                    INTO allocated_sequence;
                    IF FOUND THEN
                        NEW.ingest_sequence := allocated_sequence;
                        RETURN NEW;
                    END IF;
                    IF EXISTS (
                        SELECT 1
                        FROM m_workspace_project_ingest_ranges_v2
                        WHERE project_id = NEW.project_id
                    ) THEN
                        RAISE EXCEPTION
                            'Workspace project live message sequence is exhausted'
                            USING ERRCODE = '54000';
                    END IF;

                    INSERT INTO m_workspace_project_ingest_ranges_v2 (
                        project_id, range_number, last_live_sequence
                    ) VALUES (
                        NEW.project_id,
                        nextval('m_workspace_project_ingest_range_v2_seq'),
                        {PROJECT_SEQUENCE_LIVE_START}
                    )
                    ON CONFLICT (project_id) DO NOTHING
                    RETURNING
                        range_number * {PROJECT_SEQUENCE_RANGE_SIZE}
                            + last_live_sequence
                    INTO allocated_sequence;
                    IF FOUND THEN
                        NEW.ingest_sequence := allocated_sequence;
                        RETURN NEW;
                    END IF;
                END LOOP;
            END;
            $$ LANGUAGE plpgsql;

            CREATE OR REPLACE FUNCTION
                m_external_provider_read_lease_fence_v1()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.status <> 'queued' OR NEW.status <> 'leased' THEN
                    RETURN NEW;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM m_external_provider_read_snapshots_v1 AS snapshot
                        WHERE snapshot.external_operation_uuid =
                                OLD.external_operation_uuid
                   ) AND (
                        COALESCE(
                            current_setting(
                                'workspace.provider_read_snapshot_lease_v2',
                                TRUE
                            ),
                            ''
                        ) <> 'on'
                        OR NOT EXISTS (
                            SELECT 1
                            FROM m_external_bridge_instances_v2 AS bridge
                            WHERE bridge.uuid = OLD.bridge_instance_uuid
                              AND CASE
                                    WHEN jsonb_typeof(
                                        bridge.capabilities
                                            ->'messenger.message.read'
                                            ->'revision'
                                    ) = 'number'
                                    THEN (
                                        bridge.capabilities
                                            ->'messenger.message.read'
                                            ->>'revision'
                                    )::integer >= 2
                                    ELSE FALSE
                                  END
                        )
                   ) THEN
                    RETURN NULL;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM m_external_provider_read_snapshots_v1 AS snapshot
                        WHERE snapshot.bridge_instance_uuid =
                                OLD.bridge_instance_uuid
                          AND snapshot.external_account_uuid =
                                OLD.external_account_uuid
                          AND snapshot.queue_sequence < OLD.sequence
                          AND (
                                OLD.causal_lane IS NULL
                                OR snapshot.causal_lane = OLD.causal_lane
                          )
                          AND snapshot.external_operation_uuid <>
                                OLD.external_operation_uuid
                   ) THEN
                    RETURN NULL;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )

    def downgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute("LOCK TABLE m_workspace_messages IN ACCESS EXCLUSIVE MODE")
        session.execute(
            f"""
            ALTER TABLE m_workspace_project_ingest_ranges_v2
                DROP CONSTRAINT
                    m_workspace_project_ingest_ranges_backfill_check,
                DROP CONSTRAINT m_workspace_project_ingest_ranges_live_check;

            ALTER TABLE m_workspace_project_ingest_ranges_v2
                DROP COLUMN last_live_sequence;
            ALTER TABLE m_workspace_project_ingest_ranges_v2
                RENAME COLUMN last_backfill_sequence TO next_local_sequence;
            ALTER TABLE m_workspace_project_ingest_ranges_v2
                ALTER COLUMN next_local_sequence DROP DEFAULT,
                ADD CONSTRAINT m_workspace_project_ingest_ranges_local_check
                    CHECK (
                        next_local_sequence BETWEEN 0 AND
                            {PROJECT_SEQUENCE_RANGE_SIZE - 1}
                    );

            CREATE OR REPLACE FUNCTION m_workspace_assign_ingest_sequence_v1()
            RETURNS TRIGGER AS $$
            DECLARE
                allocated_sequence BIGINT;
            BEGIN
                IF NEW.ingest_sequence IS NOT NULL THEN
                    RETURN NEW;
                END IF;
                LOOP
                    UPDATE m_workspace_project_ingest_ranges_v2
                    SET next_local_sequence = next_local_sequence + 1,
                        updated_at = NOW()
                    WHERE project_id = NEW.project_id
                      AND next_local_sequence <
                            {PROJECT_SEQUENCE_RANGE_SIZE - 1}
                    RETURNING
                        range_number * {PROJECT_SEQUENCE_RANGE_SIZE}
                            + next_local_sequence
                    INTO allocated_sequence;
                    IF FOUND THEN
                        NEW.ingest_sequence := allocated_sequence;
                        RETURN NEW;
                    END IF;
                    IF EXISTS (
                        SELECT 1
                        FROM m_workspace_project_ingest_ranges_v2
                        WHERE project_id = NEW.project_id
                    ) THEN
                        RAISE EXCEPTION
                            'Workspace project message sequence is exhausted'
                            USING ERRCODE = '54000';
                    END IF;
                    BEGIN
                        INSERT INTO m_workspace_project_ingest_ranges_v2 (
                            project_id, range_number, next_local_sequence
                        ) VALUES (
                            NEW.project_id,
                            nextval(
                                'm_workspace_project_ingest_range_v2_seq'
                            ),
                            1
                        )
                        RETURNING
                            range_number * {PROJECT_SEQUENCE_RANGE_SIZE} + 1
                        INTO allocated_sequence;
                        NEW.ingest_sequence := allocated_sequence;
                        RETURN NEW;
                    EXCEPTION WHEN unique_violation THEN
                    END;
                END LOOP;
            END;
            $$ LANGUAGE plpgsql;

            CREATE OR REPLACE FUNCTION
                m_external_provider_read_lease_fence_v1()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.status <> 'queued' OR NEW.status <> 'leased' THEN
                    RETURN NEW;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM m_external_provider_read_snapshots_v1 AS snapshot
                        WHERE snapshot.external_operation_uuid =
                                OLD.external_operation_uuid
                   ) AND COALESCE(
                        current_setting(
                            'workspace.provider_read_snapshot_lease_v2',
                            TRUE
                        ),
                        ''
                   ) <> 'on' THEN
                    RETURN NULL;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM m_external_provider_read_snapshots_v1 AS snapshot
                        WHERE snapshot.bridge_instance_uuid =
                                OLD.bridge_instance_uuid
                          AND snapshot.external_account_uuid =
                                OLD.external_account_uuid
                          AND snapshot.queue_sequence < OLD.sequence
                          AND (
                                OLD.causal_lane IS NULL
                                OR snapshot.causal_lane = OLD.causal_lane
                          )
                          AND snapshot.external_operation_uuid <>
                                OLD.external_operation_uuid
                   ) THEN
                    RETURN NULL;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )


migration_step = MigrationStep()
