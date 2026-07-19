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
        self._depends = ["0107-add-PostgreSQL-Messenger-event-cursors-d06fed.py"]

    @property
    def migration_id(self):
        return "f7457384-87e7-4a9e-bc60-a94ae8c71df6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE "m_external_provider_operations_v1" (
                "sequence" BIGSERIAL UNIQUE NOT NULL,
                "uuid" UUID PRIMARY KEY,
                "external_operation_uuid" UUID NOT NULL UNIQUE,
                "bridge_instance_uuid" UUID NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "operation_kind" TEXT NOT NULL,
                "payload" JSONB NOT NULL,
                "status" TEXT NOT NULL DEFAULT 'queued',
                "attempt" INTEGER NOT NULL DEFAULT 0,
                "available_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "lease_uuid" UUID,
                "lease_expires_at" TIMESTAMPTZ,
                "safe_error" TEXT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "completed_at" TIMESTAMPTZ,
                CONSTRAINT "m_external_provider_operations_v1_operation_fkey"
                    FOREIGN KEY ("external_operation_uuid")
                    REFERENCES "m_external_operations_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_provider_operations_v1_bridge_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid"),
                CONSTRAINT "m_external_provider_operations_v1_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_provider_operations_v1_status_check"
                    CHECK ("status" IN (
                        'queued', 'leased', 'succeeded', 'failed', 'discarded'
                    )),
                CONSTRAINT "m_external_provider_operations_v1_attempt_check"
                    CHECK ("attempt" >= 0),
                CONSTRAINT "m_external_provider_operations_v1_lease_check"
                    CHECK (
                        ("status" = 'leased' AND "lease_uuid" IS NOT NULL
                            AND "lease_expires_at" IS NOT NULL)
                        OR
                        ("status" <> 'leased' AND "lease_uuid" IS NULL
                            AND "lease_expires_at" IS NULL)
                    )
            );
            CREATE INDEX "m_external_provider_operations_v1_feed_idx"
                ON "m_external_provider_operations_v1" (
                    "bridge_instance_uuid", "status", "available_at", "sequence"
                );

            CREATE TABLE "m_external_provider_operation_results_v1" (
                "result_uuid" UUID PRIMARY KEY,
                "operation_uuid" UUID NOT NULL,
                "payload_sha256" TEXT NOT NULL,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_provider_operation_results_v1_operation_fkey"
                    FOREIGN KEY ("operation_uuid")
                    REFERENCES "m_external_provider_operations_v1" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_provider_operation_results_v1_hash_check"
                    CHECK ("payload_sha256" ~ '^[0-9a-f]{64}$')
            );

            CREATE TABLE "m_external_provider_events_v1" (
                "bridge_instance_uuid" UUID NOT NULL,
                "provider_event_uuid" UUID NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "provider_sequence" TEXT,
                "event_kind" TEXT NOT NULL,
                "payload_sha256" TEXT NOT NULL,
                "status" TEXT NOT NULL DEFAULT 'applied',
                "target_uuid" UUID,
                "safe_error" TEXT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("bridge_instance_uuid", "provider_event_uuid"),
                CONSTRAINT "m_external_provider_events_v1_bridge_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid"),
                CONSTRAINT "m_external_provider_events_v1_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_provider_events_v1_hash_check"
                    CHECK ("payload_sha256" ~ '^[0-9a-f]{64}$'),
                CONSTRAINT "m_external_provider_events_v1_status_check"
                    CHECK ("status" IN ('processing', 'applied', 'rejected'))
            );
            CREATE INDEX "m_external_provider_events_v1_account_sequence_idx"
                ON "m_external_provider_events_v1" (
                    "external_account_uuid", "provider_sequence"
                );
            """
        )

    def downgrade(self, session):
        self._delete_table_if_exists(session, "m_external_provider_events_v1")
        self._delete_table_if_exists(
            session, "m_external_provider_operation_results_v1"
        )
        self._delete_table_if_exists(session, "m_external_provider_operations_v1")


migration_step = MigrationStep()
