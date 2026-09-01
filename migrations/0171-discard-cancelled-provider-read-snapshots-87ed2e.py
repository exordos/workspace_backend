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
        self._depends = ["0170-accept-provider-chat-owner-labels-90d43c.py"]

    @property
    def migration_id(self):
        return "87ed2ec4-bcb3-4c5a-8ca4-904c3a998014"

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
            UPDATE m_external_provider_operations_v1 AS page
            SET status = 'discarded', updated_at = NOW()
            WHERE page.status = 'failed'
              AND EXISTS (
                    SELECT 1
                    FROM m_external_provider_read_snapshots_v1 AS snapshot
                    JOIN m_external_operations_v2 AS public_operation
                      ON public_operation.uuid =
                            snapshot.external_operation_uuid
                    WHERE snapshot.external_operation_uuid =
                            page.external_operation_uuid
                      AND public_operation.status = 'failed'
                      AND public_operation.safe_error = 'cancelled'
                      AND NOT EXISTS (
                            SELECT 1
                            FROM m_external_provider_operations_v1 AS active_page
                            WHERE active_page.external_operation_uuid =
                                    snapshot.external_operation_uuid
                              AND active_page.status IN ('queued', 'leased')
                      )
              )
            """
        )
        session.execute(
            """
            WITH deleted_snapshots AS (
                DELETE FROM m_external_provider_read_snapshots_v1 AS snapshot
                USING m_external_operations_v2 AS public_operation
                WHERE public_operation.uuid = snapshot.external_operation_uuid
                  AND public_operation.status = 'failed'
                  AND public_operation.safe_error = 'cancelled'
                  AND NOT EXISTS (
                        SELECT 1
                        FROM m_external_provider_operations_v1 AS active_page
                        WHERE active_page.external_operation_uuid =
                                snapshot.external_operation_uuid
                          AND active_page.status IN ('queued', 'leased')
                  )
                RETURNING snapshot.external_operation_uuid
            )
            UPDATE m_external_operations_v2 AS public_operation
            SET status = 'discarded', can_retry = FALSE, can_discard = FALSE,
                revision = revision + 1, updated_at = NOW()
            FROM deleted_snapshots
            WHERE public_operation.uuid =
                    deleted_snapshots.external_operation_uuid
            """
        )

    def downgrade(self, session):
        return None


migration_step = MigrationStep()
