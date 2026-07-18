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


SYSTEM_FOLDERS = (
    (
        "m_folder_all_view",
        "m_folder_all_items_view",
        "00000000-0000-0000-0000-000000000000",
        "All chats",
        "2000-01-01 00:00:00",
    ),
    (
        "m_folder_personal_view",
        "m_folder_private_items_view",
        "00000000-0000-0000-0000-000000000001",
        "Personal",
        "2000-01-01 00:00:01",
    ),
    (
        "m_folder_channels_view",
        "m_folder_channel_items_view",
        "00000000-0000-0000-0000-000000000002",
        "Channels",
        "2000-01-01 00:00:02",
    ),
)


def _system_folder_view_sql(
    view,
    item_view,
    folder_uuid,
    title,
    created_at,
    legacy_external_accounts=False,
):
    external_users = (
        """
            SELECT project_id, user_uuid
            FROM "m_external_accounts"
        """
        if legacy_external_accounts
        else """
            SELECT project_id, owner_user_uuid AS user_uuid
            FROM "m_external_chats_v2"
            WHERE selected AND project_id IS NOT NULL
        """
    )
    return f"""
        CREATE OR REPLACE VIEW "{view}" AS
        WITH project_users AS (
            SELECT project_id, user_uuid
            FROM "m_workspace_stream_bindings"
            UNION
            {external_users}
            UNION
            SELECT project_id, user_uuid
            FROM "m_folders"
            WHERE project_id != '00000000-0000-0000-0000-000000000000'::uuid
              AND user_uuid != '00000000-0000-0000-0000-000000000000'::uuid
        )
        SELECT
            '{folder_uuid}'::uuid AS uuid,
            pu.project_id,
            pu.user_uuid,
            '{title}'::varchar AS title,
            11184810::bigint AS background_color_value,
            'all'::varchar AS system_type,
            COALESCE(SUM(i.unread_count), 0)::integer AS unread_count,
            COALESCE(
                json_agg(
                    json_build_object(
                        'uuid', i.uuid,
                        'folder', i.folder,
                        'project_id', i.project_id,
                        'user_uuid', i.user_uuid,
                        'stream_uuid', i.stream_uuid,
                        'order_index', i.order_index,
                        'pinned_at', i.pinned_at,
                        'chat_type', i.chat_type,
                        'unread_count', i.unread_count,
                        'created_at', i.created_at,
                        'updated_at', i.updated_at
                    )
                    ORDER BY i.created_at, i.uuid
                ) FILTER (WHERE i.uuid IS NOT NULL),
                '[]'::json
            ) AS folder_items,
            '{created_at}'::timestamp AS created_at,
            '{created_at}'::timestamp AS updated_at
        FROM project_users AS pu
        LEFT JOIN "{item_view}" AS i
          ON i.project_id = pu.project_id
         AND i.user_uuid = pu.user_uuid
        GROUP BY pu.project_id, pu.user_uuid
    """


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0102-add-workspace-drafts-b447aa.py"]

    @property
    def migration_id(self):
        return "d23ce4d3-a31e-4813-b2f2-0682d3c7a0d2"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_streams"
                ADD COLUMN "provider_metadata" JSONB,
                ADD COLUMN "delivery_metadata" JSONB;
            ALTER TABLE "m_workspace_stream_topics"
                ADD COLUMN "provider_metadata" JSONB,
                ADD COLUMN "delivery_metadata" JSONB;
            ALTER TABLE "m_workspace_messages"
                ADD COLUMN "provider_metadata" JSONB,
                ADD COLUMN "delivery_metadata" JSONB;
            ALTER TABLE "m_workspace_message_reactions"
                ADD COLUMN "provider_metadata" JSONB,
                ADD COLUMN "delivery_metadata" JSONB;
            """,
            """
            ALTER TABLE "m_workspace_events"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_object_type_check",
                ADD CONSTRAINT "m_workspace_events_object_type_check"
                    CHECK ("object_type" IN (
                        'message', 'message_reaction', 'stream',
                        'stream_binding', 'topic', 'user', 'folder',
                        'folder_item', 'file', 'external_account',
                        'external_chat', 'external_operation'
                    ))
            """,
            """
            CREATE TABLE "m_external_accounts_v2" (
                "uuid" UUID PRIMARY KEY,
                "owner_user_uuid" UUID NOT NULL,
                "provider" TEXT NOT NULL,
                "settings" JSONB NOT NULL,
                "credential_present" BOOLEAN NOT NULL DEFAULT FALSE,
                "status" TEXT NOT NULL DEFAULT 'connecting',
                "live_ready" BOOLEAN NOT NULL DEFAULT FALSE,
                "capabilities" JSONB NOT NULL DEFAULT '{}',
                "safe_error" TEXT,
                "desired_generation" INTEGER NOT NULL DEFAULT 1,
                "applied_generation" INTEGER NOT NULL DEFAULT 0,
                "last_progress_at" TIMESTAMPTZ,
                "revision" INTEGER NOT NULL DEFAULT 1,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_accounts_v2_owner_provider_key"
                    UNIQUE ("owner_user_uuid", "provider"),
                CONSTRAINT "m_external_accounts_v2_provider_check"
                    CHECK ("provider" IN ('zulip')),
                CONSTRAINT "m_external_accounts_v2_status_check"
                    CHECK ("status" IN (
                        'connecting', 'backfill', 'live', 'degraded',
                        'auth_required', 'disconnected', 'suspended'
                    )),
                CONSTRAINT "m_external_accounts_v2_generation_check"
                    CHECK (
                        "desired_generation" >= 1 AND
                        "applied_generation" >= 0
                    ),
                CONSTRAINT "m_external_accounts_v2_revision_check"
                    CHECK ("revision" >= 1)
            )
            """,
            """
            CREATE TABLE "m_external_provider_policies_v1" (
                "uuid" UUID PRIMARY KEY,
                "provider" TEXT NOT NULL UNIQUE,
                "enabled" BOOLEAN NOT NULL DEFAULT FALSE,
                "emergency_suspended" BOOLEAN NOT NULL DEFAULT FALSE,
                "limits" JSONB NOT NULL DEFAULT '{}',
                "custom_ca_bundle" JSONB,
                "custom_ca_certificates" JSONB,
                "revision" INTEGER NOT NULL DEFAULT 1,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_provider_policies_v1_provider_check"
                    CHECK ("provider" IN ('zulip')),
                CONSTRAINT "m_external_provider_policies_v1_revision_check"
                    CHECK ("revision" >= 1),
                CONSTRAINT "m_external_provider_policies_v1_ca_pair_check"
                    CHECK (
                        ("custom_ca_bundle" IS NULL) =
                        ("custom_ca_certificates" IS NULL)
                    )
            )
            """,
            """
            CREATE TABLE "m_external_credentials_v2" (
                "uuid" UUID PRIMARY KEY,
                "external_account_uuid" UUID NOT NULL UNIQUE,
                "key_version" INTEGER NOT NULL,
                "envelope" JSONB NOT NULL,
                CONSTRAINT "m_external_credentials_v2_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_credentials_v2_key_version_check"
                    CHECK ("key_version" >= 1)
            )
            """,
            """
            CREATE TABLE "m_external_chats_v2" (
                "uuid" UUID PRIMARY KEY,
                "external_account_uuid" UUID NOT NULL,
                "owner_user_uuid" UUID NOT NULL,
                "provider" TEXT NOT NULL,
                "provider_chat_id" TEXT NOT NULL,
                "source" JSONB NOT NULL,
                "display_name" TEXT NOT NULL,
                "selected" BOOLEAN NOT NULL DEFAULT FALSE,
                "project_id" UUID,
                "history_depth" TEXT NOT NULL DEFAULT '30_days',
                "projection_stream_uuid" UUID,
                "status" TEXT NOT NULL DEFAULT 'available',
                "capabilities" JSONB NOT NULL DEFAULT '{}',
                "safe_error" TEXT,
                "revision" INTEGER NOT NULL DEFAULT 1,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_chats_v2_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_chats_v2_provider_check"
                    CHECK ("provider" IN ('zulip')),
                CONSTRAINT "m_external_chats_v2_status_check"
                    CHECK ("status" IN (
                        'available', 'syncing', 'live', 'degraded', 'deselected'
                    )),
                CONSTRAINT "m_external_chats_v2_history_depth_check"
                    CHECK ("history_depth" IN (
                        'new', '7_days', '30_days', '90_days', 'all'
                    )),
                CONSTRAINT "m_external_chats_v2_selection_check"
                    CHECK (
                        ("selected" AND "project_id" IS NOT NULL) OR
                        (NOT "selected")
                    ),
                CONSTRAINT "m_external_chats_v2_revision_check"
                    CHECK ("revision" >= 1)
            )
            """,
            """
            CREATE UNIQUE INDEX "m_external_chats_v2_provider_key"
                ON "m_external_chats_v2"
                ("external_account_uuid", "provider_chat_id")
            """,
            """
            CREATE TABLE "m_external_operations_v2" (
                "uuid" UUID PRIMARY KEY,
                "external_account_uuid" UUID NOT NULL,
                "owner_user_uuid" UUID NOT NULL,
                "action" TEXT NOT NULL,
                "target_type" TEXT NOT NULL,
                "target_uuid" UUID,
                "details" JSONB NOT NULL DEFAULT '{}',
                "attempt_history" JSONB[] NOT NULL DEFAULT ARRAY[]::JSONB[],
                "status" TEXT NOT NULL DEFAULT 'queued',
                "attempt" INTEGER NOT NULL DEFAULT 0,
                "safe_error" TEXT,
                "can_retry" BOOLEAN NOT NULL DEFAULT FALSE,
                "can_discard" BOOLEAN NOT NULL DEFAULT FALSE,
                "duplicate_risk" BOOLEAN NOT NULL DEFAULT FALSE,
                "retry_requires_confirmation" BOOLEAN NOT NULL DEFAULT FALSE,
                "original_url" TEXT,
                "reconciliation_state" TEXT NOT NULL DEFAULT 'not_required',
                "reconciliation_reason" TEXT,
                "reconciliation_evidence" JSONB NOT NULL DEFAULT '{}',
                "revision" INTEGER NOT NULL DEFAULT 1,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_operations_v2_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_operations_v2_status_check"
                    CHECK ("status" IN (
                        'queued', 'running', 'succeeded', 'failed',
                        'manual_reconciliation_required', 'discarded'
                    )),
                CONSTRAINT "m_external_operations_v2_reconciliation_check"
                    CHECK (
                        (
                            "status" = 'manual_reconciliation_required' AND
                            "reconciliation_state" = 'manual_required' AND
                            "reconciliation_reason" IN (
                                'provider_history_unavailable',
                                'no_match_after_auto_resend',
                                'unsafe_provider_state'
                            ) AND
                            "duplicate_risk" AND
                            "retry_requires_confirmation"
                        ) OR (
                            "status" <> 'manual_reconciliation_required' AND
                            "reconciliation_state" <> 'manual_required'
                        )
                    ),
                CONSTRAINT "m_external_operations_v2_reconciliation_state_check"
                    CHECK ("reconciliation_state" IN (
                        'not_required', 'delayed_check', 'committed_match',
                        'automatic_resend_queued', 'manual_required'
                    )),
                CONSTRAINT "m_external_operations_v2_reconciliation_reason_check"
                    CHECK ("reconciliation_reason" IS NULL OR
                        "reconciliation_reason" IN (
                            'provider_history_unavailable',
                            'no_match_after_auto_resend',
                            'unsafe_provider_state'
                        )),
                CONSTRAINT "m_external_operations_v2_attempt_check"
                    CHECK ("attempt" >= 0),
                CONSTRAINT "m_external_operations_v2_revision_check"
                    CHECK ("revision" >= 1)
            )
            """,
            """
            CREATE TABLE "m_external_bridge_instances_v2" (
                "uuid" UUID PRIMARY KEY,
                "provider" TEXT NOT NULL,
                "identity_generation" INTEGER NOT NULL DEFAULT 1,
                "status" TEXT NOT NULL DEFAULT 'enrolling',
                "capabilities" JSONB NOT NULL DEFAULT '{}',
                "last_heartbeat_at" TIMESTAMPTZ,
                "certificate_not_after" TIMESTAMPTZ,
                "safe_error" TEXT,
                "revision" INTEGER NOT NULL DEFAULT 1,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_bridge_instances_v2_provider_check"
                    CHECK ("provider" IN ('zulip')),
                CONSTRAINT "m_external_bridge_instances_v2_status_check"
                    CHECK ("status" IN (
                        'enrolling', 'active', 'degraded', 'incompatible',
                        'suspended', 'revoked'
                    )),
                CONSTRAINT "m_external_bridge_instances_v2_generation_check"
                    CHECK ("identity_generation" >= 1),
                CONSTRAINT "m_external_bridge_instances_v2_revision_check"
                    CHECK ("revision" >= 1)
            )
            """,
            """
            CREATE INDEX "m_external_chats_v2_owner_idx"
                ON "m_external_chats_v2" ("owner_user_uuid", "uuid")
            """,
            """
            CREATE INDEX "m_external_operations_v2_owner_idx"
                ON "m_external_operations_v2" ("owner_user_uuid", "created_at", "uuid")
            """,
            """
            CREATE INDEX "m_external_bridge_instances_v2_status_idx"
                ON "m_external_bridge_instances_v2" ("provider", "status", "uuid")
            """,
            """
            CREATE OR REPLACE VIEW "m_confirmed_external_account_access" AS
            SELECT DISTINCT
                chat.project_id,
                account.owner_user_uuid AS user_uuid,
                account.provider::varchar(32) AS account_type,
                (account.settings->>'server_url')::varchar(2048) AS source_scope
            FROM "m_external_accounts_v2" AS account
            JOIN "m_external_chats_v2" AS chat
              ON chat.external_account_uuid = account.uuid
            WHERE chat.selected
              AND chat.project_id IS NOT NULL
              AND account.credential_present
              AND account.status NOT IN ('disconnected', 'suspended')
              AND account.settings->>'server_url' IS NOT NULL
            """,
        ]
        expressions.extend(
            _system_folder_view_sql(*folder) for folder in SYSTEM_FOLDERS
        )
        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_confirmed_external_account_access" AS
            SELECT
                project_id,
                user_uuid,
                account_type,
                COALESCE(source_scope, server_url) AS source_scope
            FROM "m_external_accounts"
            WHERE access_status = 'confirmed'
              AND account_settings->'credentials' IS NOT NULL
              AND account_settings->'credentials' <> 'null'::jsonb
              AND COALESCE(source_scope, server_url) IS NOT NULL
            """
        )
        for folder in SYSTEM_FOLDERS:
            session.execute(
                _system_folder_view_sql(
                    *folder,
                    legacy_external_accounts=True,
                )
            )
        session.execute(
            """
            DELETE FROM "m_workspace_events"
            WHERE "object_type" IN (
                'external_account', 'external_chat', 'external_operation'
            );

            ALTER TABLE "m_workspace_events"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_object_type_check",
                ADD CONSTRAINT "m_workspace_events_object_type_check"
                    CHECK ("object_type" IN (
                        'message', 'message_reaction', 'stream',
                        'stream_binding', 'topic', 'user', 'folder',
                        'folder_item', 'file'
                    ));
            """
        )
        self._delete_table_if_exists(session, "m_external_provider_policies_v1")
        self._delete_table_if_exists(session, "m_external_bridge_instances_v2")
        self._delete_table_if_exists(session, "m_external_operations_v2")
        self._delete_table_if_exists(session, "m_external_chats_v2")
        self._delete_table_if_exists(session, "m_external_credentials_v2")
        self._delete_table_if_exists(session, "m_external_accounts_v2")
        session.execute(
            """
            ALTER TABLE "m_workspace_message_reactions"
                DROP COLUMN "delivery_metadata",
                DROP COLUMN "provider_metadata";
            ALTER TABLE "m_workspace_messages"
                DROP COLUMN "delivery_metadata",
                DROP COLUMN "provider_metadata";
            ALTER TABLE "m_workspace_stream_topics"
                DROP COLUMN "delivery_metadata",
                DROP COLUMN "provider_metadata";
            ALTER TABLE "m_workspace_streams"
                DROP COLUMN "delivery_metadata",
                DROP COLUMN "provider_metadata";
            """
        )


migration_step = MigrationStep()
