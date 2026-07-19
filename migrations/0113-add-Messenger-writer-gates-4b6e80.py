# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = [
            "0112-deduplicate-Messenger-message-recipient-events-6f42ab.py"
        ]

    @property
    def migration_id(self):
        return "4b6e8031-28dd-4cb5-9bf6-37d75bb2da45"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE "m_messenger_writer_gates_v1" (
                "project_id" UUID PRIMARY KEY,
                "gate_uuid" UUID NOT NULL UNIQUE,
                "state" TEXT NOT NULL,
                "acquired_at" TIMESTAMPTZ NOT NULL,
                "lease_expires_at" TIMESTAMPTZ NOT NULL,
                "released_at" TIMESTAMPTZ,
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK ("state" IN ('closed', 'open')),
                CHECK ("lease_expires_at" > "acquired_at")
            );
            CREATE TABLE "m_messenger_writer_instances_v1" (
                "writer_class" TEXT NOT NULL,
                "instance_id" TEXT NOT NULL,
                "started_at" TIMESTAMPTZ NOT NULL,
                "heartbeat_at" TIMESTAMPTZ NOT NULL,
                "lease_expires_at" TIMESTAMPTZ NOT NULL,
                PRIMARY KEY ("writer_class", "instance_id"),
                CHECK ("writer_class" IN (
                    'api', 'worker', 'smtp_ingress', 'external_bridge'
                )),
                CHECK ("lease_expires_at" > "heartbeat_at")
            );
            CREATE INDEX "m_messenger_writer_instances_live_idx"
                ON "m_messenger_writer_instances_v1" (
                    "writer_class", "lease_expires_at"
                );
            CREATE TABLE "m_messenger_writer_gate_expected_v1" (
                "gate_uuid" UUID NOT NULL REFERENCES
                    "m_messenger_writer_gates_v1" ("gate_uuid")
                    ON DELETE CASCADE,
                "writer_class" TEXT NOT NULL,
                "instance_id" TEXT NOT NULL,
                PRIMARY KEY ("gate_uuid", "writer_class", "instance_id")
            );
            CREATE TABLE "m_messenger_writer_gate_acks_v1" (
                "gate_uuid" UUID NOT NULL REFERENCES
                    "m_messenger_writer_gates_v1" ("gate_uuid")
                    ON DELETE CASCADE,
                "writer_class" TEXT NOT NULL,
                "instance_id" TEXT NOT NULL,
                "acknowledged_at" TIMESTAMPTZ NOT NULL,
                "lease_expires_at" TIMESTAMPTZ NOT NULL,
                PRIMARY KEY ("gate_uuid", "writer_class", "instance_id"),
                FOREIGN KEY ("gate_uuid", "writer_class", "instance_id")
                    REFERENCES "m_messenger_writer_gate_expected_v1" (
                        "gate_uuid", "writer_class", "instance_id"
                    ) ON DELETE CASCADE,
                CHECK ("writer_class" IN (
                    'api', 'worker', 'smtp_ingress', 'external_bridge'
                )),
                CHECK ("lease_expires_at" > "acknowledged_at")
            );
            CREATE INDEX "m_messenger_writer_gate_acks_live_idx"
                ON "m_messenger_writer_gate_acks_v1" (
                    "gate_uuid", "writer_class", "lease_expires_at"
                );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP TABLE "m_messenger_writer_gate_acks_v1";
            DROP TABLE "m_messenger_writer_gate_expected_v1";
            DROP TABLE "m_messenger_writer_instances_v1";
            DROP TABLE "m_messenger_writer_gates_v1";
            """
        )


migration_step = MigrationStep()
