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

import time

from restalchemy.storage.sql import migrations


PROVIDER_HISTORY_DRAIN_BATCH_SIZE = 1_000


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0139-densify-project-read-sequences-1bca8f.py"]

    @property
    def migration_id(self):
        return "68c9b8f1-d900-46db-b395-b514499698df"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE FUNCTION
                m_external_prepare_provider_history_downgrade_v1(
                    batch_size INTEGER DEFAULT 1000
                )
            RETURNS INTEGER AS $$
            DECLARE
                processed INTEGER;
                affected_external_operation_uuids UUID[];
            BEGIN
                IF batch_size < 1 OR batch_size > 10000 THEN
                    RAISE EXCEPTION
                        'Provider history downgrade batch size must be between 1 and 10000'
                        USING ERRCODE = '22023';
                END IF;

                PERFORM pg_advisory_xact_lock(
                    hashtextextended('workspace-read-state-schema-v1', 0)
                );
                IF EXISTS (
                    SELECT 1
                    FROM m_external_provider_read_snapshots_v1
                ) THEN
                    RAISE EXCEPTION
                        'Provider history downgrade requires active read snapshots to complete first'
                        USING ERRCODE = '55000';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM m_external_provider_operations_v1 AS candidate
                    WHERE EXISTS (
                        SELECT 1
                        FROM m_external_provider_operations_v1 AS sibling
                        WHERE sibling.external_operation_uuid =
                                candidate.external_operation_uuid
                          AND sibling.uuid <> candidate.uuid
                    )
                      AND candidate.status NOT IN (
                            'succeeded', 'failed', 'discarded'
                      )
                ) THEN
                    RAISE EXCEPTION
                        'Provider history downgrade requires aggregate pages to be terminal'
                        USING ERRCODE = '55000';
                END IF;

                WITH candidates AS MATERIALIZED (
                    SELECT
                        provider_operation.uuid AS provider_operation_uuid,
                        gen_random_uuid() AS new_external_operation_uuid,
                        provider_operation.external_operation_uuid
                            AS original_external_operation_uuid,
                        provider_operation.status AS provider_status,
                        provider_operation.attempt AS provider_attempt,
                        provider_operation.safe_error AS provider_safe_error,
                        provider_operation.public_result_status,
                        provider_operation.terminal_result,
                        provider_operation.updated_at AS provider_updated_at,
                        CASE
                            WHEN provider_operation.status = 'discarded'
                                THEN 'discarded'
                            WHEN provider_operation.public_result_status
                                    IS NOT NULL
                                THEN provider_operation.public_result_status
                            ELSE provider_operation.status
                        END AS split_public_status,
                        public_operation.*
                    FROM m_external_provider_operations_v1
                        AS provider_operation
                    JOIN m_external_operations_v2 AS public_operation
                      ON public_operation.uuid =
                            provider_operation.external_operation_uuid
                    WHERE EXISTS (
                        SELECT 1
                        FROM m_external_provider_operations_v1 AS earlier
                        WHERE earlier.external_operation_uuid =
                                provider_operation.external_operation_uuid
                          AND earlier.sequence < provider_operation.sequence
                    )
                    ORDER BY
                        provider_operation.external_operation_uuid,
                        provider_operation.sequence
                    LIMIT batch_size
                    FOR UPDATE OF provider_operation SKIP LOCKED
                ), cloned AS (
                    INSERT INTO m_external_operations_v2 (
                        uuid, external_account_uuid, owner_user_uuid,
                        action, target_type, target_uuid, details,
                        attempt_history, status, attempt, safe_error,
                        can_retry, can_discard, duplicate_risk,
                        retry_requires_confirmation, original_url,
                        reconciliation_state, reconciliation_reason,
                        reconciliation_evidence, revision,
                        created_at, updated_at
                    )
                    SELECT
                        candidate.new_external_operation_uuid,
                        candidate.external_account_uuid,
                        candidate.owner_user_uuid,
                        candidate.action,
                        candidate.target_type,
                        candidate.target_uuid,
                        jsonb_set(
                            candidate.details,
                            '{provider_result}',
                            COALESCE(
                                candidate.terminal_result,
                                jsonb_build_object(
                                    'status',
                                    CASE
                                        WHEN candidate.provider_status =
                                                'discarded'
                                            THEN 'discarded'
                                        ELSE candidate.provider_status
                                    END,
                                    'provider_operation_uuid',
                                    candidate.provider_operation_uuid::text
                                )
                            ),
                            TRUE
                        ),
                        candidate.attempt_history,
                        candidate.split_public_status,
                        candidate.provider_attempt,
                        candidate.provider_safe_error,
                        candidate.split_public_status IN (
                            'failed', 'manual_reconciliation_required'
                        ),
                        candidate.split_public_status = 'failed',
                        CASE
                            WHEN candidate.split_public_status =
                                    'manual_reconciliation_required'
                                THEN TRUE
                            ELSE FALSE
                        END,
                        CASE
                            WHEN candidate.split_public_status =
                                    'manual_reconciliation_required'
                                THEN TRUE
                            ELSE FALSE
                        END,
                        CASE
                            WHEN candidate.split_public_status =
                                    'manual_reconciliation_required'
                                THEN COALESCE(
                                    candidate.terminal_result->>'original_url',
                                    candidate.original_url
                                )
                            ELSE candidate.terminal_result->>'original_url'
                        END,
                        CASE
                            WHEN candidate.split_public_status =
                                    'manual_reconciliation_required'
                                THEN 'manual_required'
                            ELSE 'not_required'
                        END,
                        CASE
                            WHEN candidate.split_public_status =
                                    'manual_reconciliation_required'
                                THEN COALESCE(
                                    candidate.terminal_result
                                        ->'reconciliation'->>'reason',
                                    candidate.reconciliation_reason,
                                    'unsafe_provider_state'
                                )
                            ELSE NULL
                        END,
                        CASE
                            WHEN candidate.split_public_status =
                                    'manual_reconciliation_required'
                                THEN COALESCE(
                                    candidate.terminal_result
                                        ->'reconciliation'->'evidence',
                                    candidate.reconciliation_evidence
                                )
                            ELSE '{}'::jsonb
                        END,
                        candidate.revision,
                        candidate.created_at,
                        candidate.provider_updated_at
                    FROM candidates AS candidate
                    RETURNING uuid
                ), rebound AS (
                    UPDATE m_external_provider_operations_v1
                        AS provider_operation
                    SET external_operation_uuid =
                            candidate.new_external_operation_uuid
                    FROM candidates AS candidate
                    JOIN cloned
                      ON cloned.uuid =
                            candidate.new_external_operation_uuid
                    WHERE provider_operation.uuid =
                            candidate.provider_operation_uuid
                    RETURNING provider_operation.uuid
                )
                SELECT COUNT(*)::integer,
                       array_agg(
                           DISTINCT candidate.original_external_operation_uuid
                       )
                INTO processed, affected_external_operation_uuids
                FROM rebound
                JOIN candidates AS candidate
                  ON candidate.provider_operation_uuid = rebound.uuid;

                IF processed > 0 THEN
                    UPDATE m_external_operations_v2 AS public_operation
                    SET details = jsonb_set(
                            public_operation.details,
                            '{provider_result}',
                            COALESCE(
                                remaining.terminal_result,
                                jsonb_build_object(
                                    'status',
                                    CASE
                                        WHEN remaining.status = 'discarded'
                                            THEN 'discarded'
                                        ELSE remaining.status
                                    END,
                                    'provider_operation_uuid',
                                    remaining.uuid::text
                                )
                            ),
                            TRUE
                        ),
                        status = CASE
                            WHEN remaining.status = 'discarded'
                                THEN 'discarded'
                            WHEN remaining.public_result_status IS NOT NULL
                                THEN remaining.public_result_status
                            ELSE remaining.status
                        END,
                        attempt = remaining.attempt,
                        safe_error = remaining.safe_error,
                        can_retry = CASE
                            WHEN remaining.status = 'discarded' THEN FALSE
                            WHEN remaining.public_result_status IN (
                                    'failed',
                                    'manual_reconciliation_required'
                                )
                                THEN TRUE
                            ELSE remaining.status = 'failed'
                        END,
                        can_discard = COALESCE(
                            remaining.public_result_status,
                            remaining.status
                        ) = 'failed',
                        duplicate_risk = COALESCE(
                            remaining.public_result_status =
                                'manual_reconciliation_required',
                            FALSE
                        ),
                        retry_requires_confirmation = COALESCE(
                            remaining.public_result_status =
                                'manual_reconciliation_required',
                            FALSE
                        ),
                        original_url = CASE
                            WHEN remaining.public_result_status =
                                    'manual_reconciliation_required'
                                THEN COALESCE(
                                    remaining.terminal_result->>'original_url',
                                    public_operation.original_url
                                )
                            ELSE remaining.terminal_result->>'original_url'
                        END,
                        reconciliation_state = CASE
                            WHEN remaining.public_result_status =
                                    'manual_reconciliation_required'
                                THEN 'manual_required'
                            ELSE 'not_required'
                        END,
                        reconciliation_reason = CASE
                            WHEN remaining.public_result_status =
                                    'manual_reconciliation_required'
                                THEN COALESCE(
                                    remaining.terminal_result
                                        ->'reconciliation'->>'reason',
                                    public_operation.reconciliation_reason,
                                    'unsafe_provider_state'
                                )
                            ELSE NULL
                        END,
                        reconciliation_evidence = CASE
                            WHEN remaining.public_result_status =
                                    'manual_reconciliation_required'
                                THEN COALESCE(
                                    remaining.terminal_result
                                        ->'reconciliation'->'evidence',
                                    public_operation.reconciliation_evidence
                                )
                            ELSE '{}'::jsonb
                        END,
                        updated_at = remaining.updated_at
                    FROM m_external_provider_operations_v1 AS remaining
                    WHERE public_operation.uuid = ANY(
                            affected_external_operation_uuids
                        )
                      AND remaining.external_operation_uuid =
                            public_operation.uuid
                      AND NOT EXISTS (
                            SELECT 1
                            FROM m_external_provider_operations_v1 AS sibling
                            WHERE sibling.external_operation_uuid =
                                    public_operation.uuid
                              AND sibling.uuid <> remaining.uuid
                        )
                    ;
                END IF;

                RETURN processed;
            END;
            $$ LANGUAGE plpgsql;
            """
        )

    def downgrade(self, session):
        function_exists = session.execute(
            """
            SELECT to_regprocedure(
                'm_external_prepare_provider_history_downgrade_v1(integer)'
            ) AS function
            """
        ).fetchone()["function"]
        if function_exists is None:
            return

        # Each batch commits independently, so a large retained result journal
        # can be drained without one unbounded transaction and a failed retry
        # resumes from the remaining aggregate parents.
        while True:
            processed = session.execute(
                """
                SELECT m_external_prepare_provider_history_downgrade_v1(%s)
                    AS processed
                """,
                (PROVIDER_HISTORY_DRAIN_BATCH_SIZE,),
            ).fetchone()["processed"]
            if processed == 0:
                remaining = session.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM m_external_provider_operations_v1 AS candidate
                        WHERE EXISTS (
                            SELECT 1
                            FROM m_external_provider_operations_v1 AS sibling
                            WHERE sibling.external_operation_uuid =
                                    candidate.external_operation_uuid
                              AND sibling.uuid <> candidate.uuid
                        )
                    ) AS remaining
                    """
                ).fetchone()["remaining"]
                if remaining:
                    session.commit()
                    time.sleep(0.05)
                    continue
                session.execute(
                    """
                    DROP FUNCTION
                        m_external_prepare_provider_history_downgrade_v1(INTEGER)
                    """
                )
                session.commit()
                return
            session.commit()


migration_step = MigrationStep()
