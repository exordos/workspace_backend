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


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0137-fence-lazy-provider-read-leases-dfc779.py"]

    @property
    def migration_id(self):
        return "1b0b0164-4d20-4d6a-9991-26a13b1a4d60"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE "m_workspace_user_read_revisions_v1" (
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "revision" BIGINT NOT NULL DEFAULT 0,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("project_id", "user_uuid"),
                CONSTRAINT "m_workspace_user_read_revisions_project_fkey"
                    FOREIGN KEY ("project_id")
                    REFERENCES "m_workspace_read_state_projects_v1" (
                        "project_id"
                    ) ON DELETE CASCADE,
                CONSTRAINT "m_workspace_user_read_revisions_number_check"
                    CHECK ("revision" >= 0)
            );

            CREATE TABLE "m_external_provider_read_candidate_chunks_v1" (
                "external_operation_uuid" UUID NOT NULL,
                "chunk_number" BIGINT NOT NULL,
                "candidate_bits" BIT(4096) NOT NULL,
                PRIMARY KEY ("external_operation_uuid", "chunk_number"),
                CONSTRAINT "m_external_provider_read_candidate_chunks_fkey"
                    FOREIGN KEY ("external_operation_uuid")
                    REFERENCES "m_external_provider_read_snapshots_v1" (
                        "external_operation_uuid"
                    ) ON DELETE CASCADE,
                CONSTRAINT
                    "m_external_provider_read_candidate_chunks_number_check"
                    CHECK ("chunk_number" >= 0),
                CONSTRAINT
                    "m_external_provider_read_candidate_chunks_bits_check"
                    CHECK (bit_count("candidate_bits") > 0)
            );

            CREATE OR REPLACE FUNCTION
                "m_external_provider_read_lease_fence_v1"()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD."status" <> 'queued' OR NEW."status" <> 'leased' THEN
                    RETURN NEW;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM "m_external_provider_read_snapshots_v1" AS snapshot
                        WHERE snapshot."external_operation_uuid" =
                                OLD."external_operation_uuid"
                   ) AND COALESCE(
                        current_setting(
                            'workspace.provider_read_snapshot_lease_v2',
                            TRUE
                        ),
                        ''
                   ) <> 'on' THEN
                    -- The transaction-local capability distinguishes the
                    -- snapshot-aware backend from an old worker sharing the
                    -- same bridge during a rolling deployment.
                    RETURN NULL;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM "m_external_provider_read_snapshots_v1" AS snapshot
                        WHERE snapshot."bridge_instance_uuid" =
                                OLD."bridge_instance_uuid"
                          AND snapshot."external_account_uuid" =
                                OLD."external_account_uuid"
                          AND snapshot."queue_sequence" < OLD."sequence"
                          AND (
                                OLD."causal_lane" IS NULL
                                OR snapshot."causal_lane" = OLD."causal_lane"
                          )
                          AND snapshot."external_operation_uuid" <>
                                OLD."external_operation_uuid"
                   ) THEN
                    -- Preserve the lane barrier for old and new workers.
                    RETURN NULL;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE OR REPLACE FUNCTION
                "m_external_provider_read_completion_fence_v1"()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW."status" = 'succeeded'
                   AND OLD."status" IS DISTINCT FROM NEW."status"
                   AND EXISTS (
                        SELECT 1
                        FROM "m_external_provider_read_snapshots_v1" AS snapshot
                        WHERE snapshot."external_operation_uuid" = NEW."uuid"
                          AND (
                                snapshot."exhausted" = FALSE
                                OR EXISTS (
                                    SELECT 1
                                    FROM "m_external_provider_operations_v1"
                                        AS provider_operation
                                    WHERE provider_operation
                                            ."external_operation_uuid" =
                                                NEW."uuid"
                                      AND provider_operation."status" NOT IN (
                                            'succeeded', 'discarded'
                                      )
                                )
                          )
                   ) THEN
                    -- An old worker completes one provider page at a time.
                    -- Keep the aggregate running until every sibling is
                    -- terminal, even after materialization is exhausted.
                    RETURN NULL;
                END IF;
                IF NEW."status" = 'succeeded'
                   AND OLD."status" IS DISTINCT FROM NEW."status" THEN
                    DELETE FROM "m_external_provider_read_snapshots_v1"
                    WHERE "external_operation_uuid" = NEW."uuid"
                      AND "exhausted" = TRUE
                      AND NOT EXISTS (
                            SELECT 1
                            FROM "m_external_provider_operations_v1"
                                AS provider_operation
                            WHERE provider_operation
                                    ."external_operation_uuid" = NEW."uuid"
                              AND provider_operation."status" NOT IN (
                                    'succeeded', 'discarded'
                              )
                      );
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DO $$
            DECLARE
                attempt INTEGER := 0;
                previous_lock_timeout TEXT := current_setting('lock_timeout');
            BEGIN
                LOOP
                    attempt := attempt + 1;
                    BEGIN
                        PERFORM set_config('lock_timeout', '250ms', TRUE);
                        PERFORM pg_advisory_xact_lock(
                            hashtextextended(
                                'workspace-read-state-schema-v1', 0
                            )
                        );
                        LOCK TABLE m_external_provider_read_snapshots_v1
                            IN ACCESS EXCLUSIVE MODE;
                        IF EXISTS (
                            SELECT 1
                            FROM m_external_provider_read_snapshots_v1
                        ) THEN
                            RAISE EXCEPTION
                                'Provider read rolling fence downgrade requires active snapshots to be completed or discarded first'
                                USING ERRCODE = '55000';
                        END IF;
                        EXIT;
                    EXCEPTION
                        WHEN lock_not_available
                            OR query_canceled
                            OR deadlock_detected THEN
                            IF attempt >= 120 THEN
                                RAISE;
                            END IF;
                            PERFORM pg_sleep(0.05);
                    END;
                END LOOP;
                PERFORM set_config(
                    'lock_timeout', previous_lock_timeout, TRUE
                );
            END;
            $$;

            DROP TABLE "m_external_provider_read_candidate_chunks_v1";
            DROP TABLE "m_workspace_user_read_revisions_v1";

            CREATE OR REPLACE FUNCTION
                "m_external_provider_read_lease_fence_v1"()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD."status" <> 'queued' OR NEW."status" <> 'leased' THEN
                    RETURN NEW;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM "m_external_provider_read_snapshots_v1" AS snapshot
                        WHERE snapshot."external_operation_uuid" =
                                OLD."external_operation_uuid"
                   ) AND NOT EXISTS (
                        SELECT 1
                        FROM "m_external_bridge_instances_v2" AS bridge
                        WHERE bridge."uuid" = OLD."bridge_instance_uuid"
                          AND CASE
                                WHEN jsonb_typeof(
                                    bridge."capabilities"
                                        ->'messenger.message.read'->'revision'
                                ) = 'number'
                                THEN (
                                    bridge."capabilities"
                                        ->'messenger.message.read'->>'revision'
                                )::integer >= 2
                                ELSE FALSE
                              END
                   ) THEN
                    RETURN NULL;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM "m_external_provider_read_snapshots_v1" AS snapshot
                        WHERE snapshot."bridge_instance_uuid" =
                                OLD."bridge_instance_uuid"
                          AND snapshot."external_account_uuid" =
                                OLD."external_account_uuid"
                          AND snapshot."queue_sequence" < OLD."sequence"
                          AND (
                                OLD."causal_lane" IS NULL
                                OR snapshot."causal_lane" = OLD."causal_lane"
                          )
                          AND snapshot."external_operation_uuid" <>
                                OLD."external_operation_uuid"
                   ) THEN
                    RETURN NULL;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE OR REPLACE FUNCTION
                "m_external_provider_read_completion_fence_v1"()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW."status" = 'succeeded'
                   AND OLD."status" IS DISTINCT FROM NEW."status"
                   AND EXISTS (
                        SELECT 1
                        FROM "m_external_provider_read_snapshots_v1" AS snapshot
                        WHERE snapshot."external_operation_uuid" = NEW."uuid"
                          AND snapshot."exhausted" = FALSE
                   ) THEN
                    RETURN NULL;
                END IF;
                IF NEW."status" = 'succeeded'
                   AND OLD."status" IS DISTINCT FROM NEW."status" THEN
                    DELETE FROM "m_external_provider_read_snapshots_v1"
                    WHERE "external_operation_uuid" = NEW."uuid"
                      AND "exhausted" = TRUE;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )


migration_step = MigrationStep()
