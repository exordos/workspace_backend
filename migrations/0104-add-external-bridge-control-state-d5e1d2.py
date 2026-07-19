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
        self._depends = ["0103-add-external-bridge-foundation-d23ce4.py"]

    @property
    def migration_id(self):
        return "d5e1d246-7b83-4a16-afd0-b2353db2418a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE "m_external_bridge_control_instances_v1" (
                "bridge_instance_uuid" UUID PRIMARY KEY,
                "provider_kind" TEXT NOT NULL,
                "identity_generation" BIGINT,
                "encryption_key_uuid" UUID,
                "encryption_public_key" TEXT,
                "snapshot_generation" BIGINT NOT NULL DEFAULT 1,
                "pruned_through_sequence" JSONB NOT NULL DEFAULT '{}',
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_bridge_control_instances_v1_instance_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_bridge_control_instances_v1_provider_check"
                    CHECK ("provider_kind" IN ('zulip')),
                CONSTRAINT "m_external_bridge_control_instances_v1_generation_check"
                    CHECK (
                        "snapshot_generation" >= 1 AND
                        ("identity_generation" IS NULL OR
                         "identity_generation" >= 1)
                    ),
                CONSTRAINT "m_external_bridge_control_instances_v1_key_check"
                    CHECK (
                        ("identity_generation" IS NULL AND
                         "encryption_key_uuid" IS NULL AND
                         "encryption_public_key" IS NULL) OR
                        ("identity_generation" IS NOT NULL AND
                         "encryption_key_uuid" IS NOT NULL AND
                         "encryption_public_key" IS NOT NULL)
                    )
            )
            """,
            """
            CREATE TABLE "m_external_bridge_desired_resources_v1" (
                "bridge_instance_uuid" UUID NOT NULL,
                "provider_kind" TEXT NOT NULL,
                "resource_type" TEXT NOT NULL,
                "resource_uuid" UUID NOT NULL,
                "operation" TEXT NOT NULL,
                "generation" BIGINT NOT NULL,
                "required_capabilities" JSONB NOT NULL DEFAULT '{}',
                "resource" JSONB,
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (
                    "bridge_instance_uuid", "provider_kind",
                    "resource_type", "resource_uuid"
                ),
                CONSTRAINT "m_external_bridge_desired_resources_v1_instance_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_bridge_desired_resources_v1_type_check"
                    CHECK ("resource_type" IN (
                        'external_account', 'external_chat_assignment',
                        'external_provider_policy', 'custom_ca_bundle'
                    )),
                CONSTRAINT "m_external_bridge_desired_resources_v1_operation_check"
                    CHECK (
                        ("operation" = 'upsert' AND "resource" IS NOT NULL) OR
                        ("operation" = 'delete' AND "resource" IS NULL)
                    ),
                CONSTRAINT "m_external_bridge_desired_resources_v1_generation_check"
                    CHECK ("generation" >= 1)
            )
            """,
            """
            CREATE TABLE "m_external_bridge_desired_changes_v1" (
                "sequence" BIGSERIAL PRIMARY KEY,
                "change_uuid" UUID NOT NULL UNIQUE,
                "bridge_instance_uuid" UUID NOT NULL,
                "provider_kind" TEXT NOT NULL,
                "resource_type" TEXT NOT NULL,
                "resource_uuid" UUID NOT NULL,
                "operation" TEXT NOT NULL,
                "generation" BIGINT NOT NULL,
                "required_capabilities" JSONB NOT NULL DEFAULT '{}',
                "resource" JSONB,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_bridge_desired_changes_v1_instance_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_bridge_desired_changes_v1_type_check"
                    CHECK ("resource_type" IN (
                        'external_account', 'external_chat_assignment',
                        'external_provider_policy', 'custom_ca_bundle'
                    )),
                CONSTRAINT "m_external_bridge_desired_changes_v1_operation_check"
                    CHECK (
                        ("operation" = 'upsert' AND "resource" IS NOT NULL) OR
                        ("operation" = 'delete' AND "resource" IS NULL)
                    ),
                CONSTRAINT "m_external_bridge_desired_changes_v1_generation_check"
                    CHECK ("generation" >= 1)
            )
            """,
            """
            CREATE INDEX "m_external_bridge_desired_changes_v1_feed_idx"
                ON "m_external_bridge_desired_changes_v1"
                ("bridge_instance_uuid", "provider_kind", "sequence")
            """,
            """
            CREATE INDEX "m_external_bridge_desired_changes_v1_retention_idx"
                ON "m_external_bridge_desired_changes_v1" ("created_at")
            """,
            """
            CREATE TABLE "m_external_bridge_snapshots_v1" (
                "snapshot_token" TEXT PRIMARY KEY,
                "request_uuid" UUID NOT NULL,
                "bridge_instance_uuid" UUID NOT NULL,
                "provider_kind" TEXT NOT NULL,
                "resource_types" TEXT[] NOT NULL,
                "snapshot_generation" BIGINT NOT NULL,
                "anchor_sequence" BIGINT NOT NULL,
                "anchor_cursor" TEXT NOT NULL,
                "resources" JSONB NOT NULL,
                "expires_at" TIMESTAMPTZ NOT NULL,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_bridge_snapshots_v1_instance_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_bridge_snapshots_v1_request_key"
                    UNIQUE (
                        "request_uuid", "bridge_instance_uuid",
                        "provider_kind", "resource_types"
                    )
            )
            """,
            """
            CREATE INDEX "m_external_bridge_snapshots_v1_expiry_idx"
                ON "m_external_bridge_snapshots_v1" ("expires_at")
            """,
            """
            CREATE TABLE "m_external_bridge_heartbeats_v1" (
                "heartbeat_uuid" UUID PRIMARY KEY,
                "bridge_instance_uuid" UUID NOT NULL,
                "canonical_sha256" TEXT NOT NULL,
                "response" JSONB NOT NULL,
                "received_at" TIMESTAMPTZ NOT NULL,
                CONSTRAINT "m_external_bridge_heartbeats_v1_instance_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid")
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE "m_external_bridge_observed_reports_v1" (
                "report_uuid" UUID PRIMARY KEY,
                "bridge_instance_uuid" UUID NOT NULL,
                "canonical_sha256" TEXT NOT NULL,
                "resource_type" TEXT NOT NULL,
                "resource_uuid" UUID NOT NULL,
                "observed_generation" BIGINT NOT NULL,
                "payload" JSONB NOT NULL,
                "observed_at" TIMESTAMPTZ NOT NULL,
                CONSTRAINT "m_external_bridge_observed_reports_v1_instance_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid")
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX "m_external_bridge_observed_reports_v1_resource_idx"
                ON "m_external_bridge_observed_reports_v1"
                ("bridge_instance_uuid", "resource_type", "resource_uuid",
                 "observed_generation" DESC)
            """,
            """
            CREATE TABLE "m_external_bridge_file_transfers_v1" (
                "transfer_key" TEXT PRIMARY KEY,
                "bridge_instance_uuid" UUID NOT NULL,
                "value" JSONB NOT NULL,
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_bridge_file_transfers_v1_instance_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid")
                    ON DELETE CASCADE
            )
            """,
        ]
        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "m_external_bridge_file_transfers_v1")
        self._delete_table_if_exists(session, "m_external_bridge_observed_reports_v1")
        self._delete_table_if_exists(session, "m_external_bridge_heartbeats_v1")
        self._delete_table_if_exists(session, "m_external_bridge_snapshots_v1")
        self._delete_table_if_exists(session, "m_external_bridge_desired_changes_v1")
        self._delete_table_if_exists(session, "m_external_bridge_desired_resources_v1")
        self._delete_table_if_exists(session, "m_external_bridge_control_instances_v1")


migration_step = MigrationStep()
