# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0104-add-external-bridge-control-state-d5e1d2.py"]

    @property
    def migration_id(self):
        return "a81c7e09-a9fd-49a7-924d-4b7a040539f5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        for expression in (
            """
            ALTER TABLE "m_external_chats_v2"
                ADD COLUMN "transition_pending" BOOLEAN NOT NULL DEFAULT FALSE
            """,
            """
            CREATE TABLE "m_external_projection_transitions_v1" (
                "uuid" UUID PRIMARY KEY,
                "external_chat_uuid" UUID NOT NULL,
                "owner_user_uuid" UUID NOT NULL,
                "action" TEXT NOT NULL,
                "revision" INTEGER NOT NULL,
                "stream_uuid" UUID NOT NULL,
                "old_project_uuid" UUID NOT NULL,
                "new_project_uuid" UUID,
                "phase" TEXT NOT NULL DEFAULT 'planned',
                "cleanup_files" JSONB NOT NULL DEFAULT '[]',
                "repair_attempts" INTEGER NOT NULL DEFAULT 0,
                "next_repair_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "safe_error" TEXT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_projection_transitions_v1_chat_fkey"
                    FOREIGN KEY ("external_chat_uuid")
                    REFERENCES "m_external_chats_v2" ("uuid") ON DELETE CASCADE,
                CONSTRAINT "m_external_projection_transitions_v1_key"
                    UNIQUE ("external_chat_uuid", "revision", "action"),
                CONSTRAINT "m_external_projection_transitions_v1_action_check"
                    CHECK ("action" IN ('move', 'deselect')),
                CONSTRAINT "m_external_projection_transitions_v1_phase_check"
                    CHECK ("phase" IN (
                        'planned', 'canonical_new', 'canonical_old',
                        'sql_applied', 'files_purged', 'completed', 'failed'
                    )),
                CONSTRAINT "m_external_projection_transitions_v1_target_check"
                    CHECK (
                        ("action" = 'move' AND "new_project_uuid" IS NOT NULL) OR
                        ("action" = 'deselect' AND "new_project_uuid" IS NULL)
                    ),
                CONSTRAINT "m_external_projection_transitions_v1_attempts_check"
                    CHECK ("repair_attempts" >= 0)
            )
            """,
            """
            CREATE INDEX "m_external_projection_transitions_v1_pending_idx"
                ON "m_external_projection_transitions_v1"
                    ("phase", "next_repair_at", "created_at")
            """,
            """
            CREATE TABLE "m_external_bridge_mail_lanes_v1" (
                "external_account_uuid" UUID NOT NULL,
                "origin" TEXT NOT NULL,
                "causal_lane" TEXT NOT NULL,
                "last_sequence" BIGINT NOT NULL DEFAULT 0,
                "last_operation_uuid" UUID,
                PRIMARY KEY ("external_account_uuid", "origin", "causal_lane"),
                CONSTRAINT "m_external_bridge_mail_lanes_v1_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_bridge_mail_lanes_v1_origin_check"
                    CHECK ("origin" IN ('workspace', 'zulip')),
                CONSTRAINT "m_external_bridge_mail_lanes_v1_sequence_check"
                    CHECK ("last_sequence" >= 0)
            )
            """,
            """
            CREATE TABLE "m_external_bridge_mail_outbox_v1" (
                "record_uuid" UUID PRIMARY KEY,
                "operation_uuid" UUID NOT NULL,
                "record_kind" TEXT NOT NULL,
                "attempt" INTEGER NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "project_uuid" UUID NOT NULL,
                "operation_sha256" TEXT NOT NULL,
                "raw_message" BYTEA NOT NULL,
                "status" TEXT NOT NULL DEFAULT 'queued',
                "send_attempts" INTEGER NOT NULL DEFAULT 0,
                "safe_error" TEXT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "sent_at" TIMESTAMPTZ,
                CONSTRAINT "m_external_bridge_mail_outbox_v1_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_bridge_mail_outbox_v1_status_check"
                    CHECK ("status" IN ('queued', 'sent')),
                CONSTRAINT "m_external_bridge_mail_outbox_v1_kind_check"
                    CHECK ("record_kind" IN ('operation', 'result')),
                CONSTRAINT "m_external_bridge_mail_outbox_v1_operation_attempt_key"
                    UNIQUE ("operation_uuid", "record_kind", "attempt"),
                CONSTRAINT "m_external_bridge_mail_outbox_v1_hash_check"
                    CHECK ("operation_sha256" ~ '^[0-9a-f]{64}$'),
                CONSTRAINT "m_external_bridge_mail_outbox_v1_attempt_check"
                    CHECK ("attempt" > 0 AND "send_attempts" >= 0)
            )
            """,
            """
            CREATE INDEX "m_external_bridge_mail_outbox_v1_queue_idx"
                ON "m_external_bridge_mail_outbox_v1" ("status", "created_at")
            """,
            """
            CREATE TABLE "m_external_bridge_mail_cursors_v1" (
                "bridge_instance_uuid" UUID NOT NULL,
                "mailbox" TEXT NOT NULL,
                "uid_validity" BIGINT NOT NULL,
                "last_uid" BIGINT NOT NULL DEFAULT 0,
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("bridge_instance_uuid", "mailbox"),
                CONSTRAINT "m_external_bridge_mail_cursors_v1_uid_check"
                    CHECK ("uid_validity" > 0 AND "last_uid" >= 0)
            )
            """,
            """
            CREATE TABLE "m_external_bridge_mail_quarantine_v1" (
                "bridge_instance_uuid" UUID NOT NULL,
                "mailbox" TEXT NOT NULL,
                "uid_validity" BIGINT NOT NULL,
                "uid" BIGINT NOT NULL,
                "raw_sha256" TEXT NOT NULL,
                "reason" TEXT NOT NULL,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (
                    "bridge_instance_uuid", "mailbox", "uid_validity", "uid"
                ),
                CONSTRAINT "m_external_bridge_mail_quarantine_v1_hash_check"
                    CHECK ("raw_sha256" ~ '^[0-9a-f]{64}$'),
                CONSTRAINT "m_external_bridge_mail_quarantine_v1_uid_check"
                    CHECK ("uid_validity" > 0 AND "uid" > 0)
            )
            """,
            """
            CREATE TABLE "m_external_bridge_mail_pending_v1" (
                "bridge_instance_uuid" UUID NOT NULL,
                "mailbox" TEXT NOT NULL,
                "uid_validity" BIGINT NOT NULL,
                "uid" BIGINT NOT NULL,
                "record_uuid" UUID NOT NULL,
                "operation_uuid" UUID NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "project_uuid" UUID NOT NULL,
                "record_kind" TEXT NOT NULL,
                "origin" TEXT NOT NULL,
                "causal_lane" TEXT NOT NULL,
                "sequence" BIGINT NOT NULL,
                "predecessor_operation_uuid" UUID,
                "operation_sha256" TEXT NOT NULL,
                "record" JSONB NOT NULL,
                "raw_message" BYTEA NOT NULL,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (
                    "bridge_instance_uuid", "mailbox", "uid_validity", "uid"
                ),
                CONSTRAINT "m_external_bridge_mail_pending_v1_record_unique"
                    UNIQUE ("bridge_instance_uuid", "record_uuid"),
                CONSTRAINT "m_external_bridge_mail_pending_v1_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_bridge_mail_pending_v1_kind_check"
                    CHECK ("record_kind" IN ('operation', 'result')),
                CONSTRAINT "m_external_bridge_mail_pending_v1_origin_check"
                    CHECK ("origin" IN ('workspace', 'zulip')),
                CONSTRAINT "m_external_bridge_mail_pending_v1_sequence_check"
                    CHECK ("sequence" > 0),
                CONSTRAINT "m_external_bridge_mail_pending_v1_hash_check"
                    CHECK ("operation_sha256" ~ '^[0-9a-f]{64}$'),
                CONSTRAINT "m_external_bridge_mail_pending_v1_uid_check"
                    CHECK ("uid_validity" > 0 AND "uid" > 0)
            )
            """,
            """
            CREATE INDEX "m_external_bridge_mail_pending_v1_lane_idx"
                ON "m_external_bridge_mail_pending_v1" (
                    "record_kind", "external_account_uuid", "origin",
                    "causal_lane", "sequence", "uid"
                )
            """,
            """
            CREATE TABLE "m_external_bridge_mail_records_v1" (
                "record_uuid" UUID PRIMARY KEY,
                "operation_uuid" UUID NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "direction" TEXT NOT NULL,
                "operation_sha256" TEXT NOT NULL,
                "processed_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_bridge_mail_records_v1_direction_check"
                    CHECK ("direction" IN ('workspace-to-zulip', 'zulip-to-workspace')),
                CONSTRAINT "m_external_bridge_mail_records_v1_hash_check"
                    CHECK ("operation_sha256" ~ '^[0-9a-f]{64}$')
            )
            """,
        ):
            session.execute(expression)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "m_external_bridge_mail_records_v1")
        self._delete_table_if_exists(session, "m_external_bridge_mail_pending_v1")
        self._delete_table_if_exists(session, "m_external_bridge_mail_quarantine_v1")
        self._delete_table_if_exists(session, "m_external_bridge_mail_cursors_v1")
        self._delete_table_if_exists(session, "m_external_bridge_mail_outbox_v1")
        self._delete_table_if_exists(session, "m_external_bridge_mail_lanes_v1")
        self._delete_table_if_exists(session, "m_external_projection_transitions_v1")
        session.execute(
            'ALTER TABLE "m_external_chats_v2" DROP COLUMN IF EXISTS "transition_pending"'
        )


migration_step = MigrationStep()
