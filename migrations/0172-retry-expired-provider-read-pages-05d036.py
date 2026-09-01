# Copyright 2016 Eugene Frolov <eugene@frolov.net.ru>
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from restalchemy.storage.sql import migrations


READ_STATE_SCHEMA_LOCK_KEY = "workspace-read-state-schema-v1"


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0171-discard-cancelled-provider-read-snapshots-87ed2e.py"]

    @property
    def migration_id(self):
        return "05d036bd-5dd8-49e1-8e37-f8b3b93939c6"

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
            WITH retryable AS MATERIALIZED (
                SELECT public_operation.uuid AS external_operation_uuid,
                       public_operation.attempt + 1 AS next_attempt
                FROM m_external_operations_v2 AS public_operation
                JOIN m_external_provider_read_snapshots_v1 AS snapshot
                  ON snapshot.external_operation_uuid = public_operation.uuid
                WHERE public_operation.action = 'read_state.set'
                  AND public_operation.status = 'failed'
                  AND public_operation.safe_error = 'expired'
                  AND EXISTS (
                        SELECT 1
                        FROM m_external_provider_operations_v1 AS failed_page
                        WHERE failed_page.external_operation_uuid =
                                public_operation.uuid
                          AND failed_page.operation_kind = 'read_state.set'
                          AND failed_page.status = 'failed'
                  )
                  AND NOT EXISTS (
                        SELECT 1
                        FROM m_external_provider_operations_v1 AS active_page
                        WHERE active_page.external_operation_uuid =
                                public_operation.uuid
                          AND active_page.status IN ('queued', 'leased')
                  )
                FOR UPDATE OF public_operation
            ), retry_source AS MATERIALIZED (
                SELECT failed_page.uuid, failed_page.external_operation_uuid,
                       failed_page.bridge_instance_uuid,
                       failed_page.external_account_uuid,
                       failed_page.project_id, failed_page.operation_kind,
                       failed_page.causal_lane, failed_page.payload,
                       retryable.next_attempt
                FROM m_external_provider_operations_v1 AS failed_page
                JOIN retryable
                  ON retryable.external_operation_uuid =
                        failed_page.external_operation_uuid
                WHERE failed_page.operation_kind = 'read_state.set'
                  AND failed_page.status = 'failed'
                FOR UPDATE OF failed_page
            ), neutralized AS (
                UPDATE m_external_provider_operations_v1 AS failed_page
                SET status = 'discarded', public_result_status = NULL,
                    payload = jsonb_set(
                        failed_page.payload, '{message_uuids}', '[]'::jsonb
                    ),
                    updated_at = NOW()
                FROM retry_source
                WHERE failed_page.uuid = retry_source.uuid
                RETURNING failed_page.uuid
            ), queued AS (
                INSERT INTO m_external_provider_operations_v1 (
                    uuid, external_operation_uuid, bridge_instance_uuid,
                    external_account_uuid, project_id, operation_kind,
                    causal_lane, payload, status, attempt, available_at,
                    created_at, updated_at
                )
                SELECT
                    gen_random_uuid(), retry_source.external_operation_uuid,
                    retry_source.bridge_instance_uuid,
                    retry_source.external_account_uuid,
                    retry_source.project_id, retry_source.operation_kind,
                    retry_source.causal_lane, retry_source.payload,
                    'queued', retry_source.next_attempt - 1,
                    NOW(), NOW(), NOW()
                FROM retry_source
                CROSS JOIN (SELECT count(*) FROM neutralized) AS completed_update
                RETURNING uuid, external_operation_uuid
            ), queued_operation AS (
                SELECT external_operation_uuid,
                       min(uuid::text)::uuid AS record_uuid
                FROM queued
                GROUP BY external_operation_uuid
            )
            UPDATE m_external_operations_v2 AS public_operation
            SET status = 'queued',
                attempt = retryable.next_attempt,
                attempt_history = array_append(
                    COALESCE(
                        public_operation.attempt_history,
                        ARRAY[]::jsonb[]
                    ),
                    jsonb_build_object(
                        'attempt', public_operation.attempt,
                        'status', public_operation.status,
                        'safe_error', public_operation.safe_error,
                        'duplicate_risk', public_operation.duplicate_risk,
                        'original_url', public_operation.original_url,
                        'reconciliation_state',
                            public_operation.reconciliation_state,
                        'reconciliation_reason',
                            public_operation.reconciliation_reason
                    )
                ),
                details = public_operation.details || jsonb_build_object(
                    'record_uuid', queued_operation.record_uuid::text
                ),
                safe_error = NULL,
                can_retry = FALSE,
                can_discard = TRUE,
                duplicate_risk = FALSE,
                retry_requires_confirmation = FALSE,
                reconciliation_state = 'not_required',
                reconciliation_reason = NULL,
                reconciliation_evidence = '{}'::jsonb,
                revision = public_operation.revision + 1,
                updated_at = NOW()
            FROM retryable
            JOIN queued_operation
              ON queued_operation.external_operation_uuid =
                    retryable.external_operation_uuid
            WHERE public_operation.uuid = retryable.external_operation_uuid
            """
        )

    def downgrade(self, session):
        return None


migration_step = MigrationStep()
