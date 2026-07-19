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
        self._depends = ["0109-add-scalable-Messenger-visibility-views-0ae35f.py"]

    @property
    def migration_id(self):
        return "e8e1b2c3-3739-4238-97cf-fa7613109917"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE "m_messenger_import_runs_v1" (
                "run_uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "phase" TEXT NOT NULL,
                "source_uid_validity" BIGINT,
                "source_checkpoint_uid" BIGINT NOT NULL DEFAULT 0,
                "source_inventory" JSONB,
                "destination_inventory" JSONB,
                "source_digest" TEXT,
                "destination_digest" TEXT,
                "s3_urn_inventory" JSONB NOT NULL DEFAULT '{}',
                "source_event_watermarks" JSONB NOT NULL DEFAULT '{}',
                "freeze_confirmed_at" TIMESTAMPTZ,
                "last_error" TEXT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_messenger_import_runs_phase_check"
                    CHECK ("phase" IN (
                        'inventory', 'staged', 'applying', 'frozen',
                        'final_delta', 'parity_verified', 'failed'
                    ))
            );
            CREATE INDEX "m_messenger_import_runs_project_idx"
                ON "m_messenger_import_runs_v1" (
                    "project_id", "created_at" DESC
                );

            CREATE TABLE "m_messenger_import_items_v1" (
                "run_uuid" UUID NOT NULL REFERENCES
                    "m_messenger_import_runs_v1" ("run_uuid")
                    ON DELETE CASCADE,
                "collection" TEXT NOT NULL,
                "entity_key" TEXT NOT NULL,
                "operation" TEXT NOT NULL,
                "payload" JSONB,
                "payload_sha256" TEXT NOT NULL,
                "status" TEXT NOT NULL DEFAULT 'staged',
                "attempts" INTEGER NOT NULL DEFAULT 0,
                "applied_at" TIMESTAMPTZ,
                "last_error" TEXT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("run_uuid", "collection", "entity_key"),
                CONSTRAINT "m_messenger_import_items_operation_check"
                    CHECK ("operation" IN ('upsert', 'delete')),
                CONSTRAINT "m_messenger_import_items_status_check"
                    CHECK ("status" IN ('staged', 'applying', 'applied', 'error')),
                CONSTRAINT "m_messenger_import_items_payload_check"
                    CHECK (
                        ("operation" = 'upsert' AND "payload" IS NOT NULL)
                        OR ("operation" = 'delete' AND "payload" IS NULL)
                    )
            );
            CREATE INDEX "m_messenger_import_items_pending_idx"
                ON "m_messenger_import_items_v1" (
                    "run_uuid", "status", "collection", "entity_key"
                );

            CREATE TABLE "m_messenger_import_checkpoints_v1" (
                "sequence" BIGSERIAL PRIMARY KEY,
                "run_uuid" UUID NOT NULL REFERENCES
                    "m_messenger_import_runs_v1" ("run_uuid")
                    ON DELETE CASCADE,
                "phase" TEXT NOT NULL,
                "source_uid_validity" BIGINT,
                "source_checkpoint_uid" BIGINT NOT NULL,
                "snapshot_digest" TEXT NOT NULL,
                "details" JSONB NOT NULL DEFAULT '{}',
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX "m_messenger_import_checkpoints_run_idx"
                ON "m_messenger_import_checkpoints_v1" (
                    "run_uuid", "sequence" DESC
                );

            CREATE TABLE "m_messenger_import_quarantine_v1" (
                "sequence" BIGSERIAL PRIMARY KEY,
                "run_uuid" UUID NOT NULL REFERENCES
                    "m_messenger_import_runs_v1" ("run_uuid")
                    ON DELETE CASCADE,
                "source_kind" TEXT NOT NULL,
                "source_position" TEXT NOT NULL,
                "error_code" TEXT NOT NULL,
                "error_summary" TEXT NOT NULL,
                "record_sha256" TEXT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE ("run_uuid", "source_kind", "source_position")
            );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP TABLE "m_messenger_import_quarantine_v1";
            DROP TABLE "m_messenger_import_checkpoints_v1";
            DROP TABLE "m_messenger_import_items_v1";
            DROP TABLE "m_messenger_import_runs_v1";
            """
        )


migration_step = MigrationStep()
