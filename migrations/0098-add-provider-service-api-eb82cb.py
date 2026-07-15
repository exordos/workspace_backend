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
        self._depends = ["0097-add-mail-calendar-domain-models-0f613b.py"]

    @property
    def migration_id(self):
        return "eb82cbb6-149b-4d9b-bf82-e35ec7fe1798"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE "m_workspace_providers" (
                "uuid" UUID PRIMARY KEY,
                "name" VARCHAR(256) NOT NULL,
                "supported_kinds" JSONB NOT NULL DEFAULT '[]'::jsonb,
                "version" VARCHAR(128),
                "enabled" BOOLEAN NOT NULL DEFAULT TRUE,
                "registered_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "last_seen_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_providers_supported_kinds_check"
                    CHECK (
                        "supported_kinds" <@ '["zulip", "mail", "calendar"]'::jsonb
                    )
            );
            """,
            """
            ALTER TABLE "m_external_accounts"
                ADD COLUMN "provider_uuid" UUID,
                ADD CONSTRAINT "m_external_accounts_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid")
                    REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT;

            ALTER TABLE "m_external_accounts"
                ADD CONSTRAINT "m_external_accounts_provider_owner_unique"
                    UNIQUE ("provider_uuid", "uuid");

            CREATE INDEX "m_external_accounts_provider_idx"
                ON "m_external_accounts" ("provider_uuid", "account_type");

            ALTER TABLE "m_external_accounts"
                DROP COLUMN "access_next_check_at";
            """,
            """
            ALTER TABLE "m_workspace_files"
                ALTER COLUMN "stream_uuid" DROP NOT NULL,
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "external_account_uuid" UUID,
                ADD CONSTRAINT "m_workspace_files_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid")
                    REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_workspace_files_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_account_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                ADD CONSTRAINT "m_workspace_files_origin_check"
                    CHECK (
                        "stream_uuid" IS NOT NULL OR
                        (
                            "provider_uuid" IS NOT NULL AND
                            "external_account_uuid" IS NOT NULL
                        )
                    );

            CREATE INDEX "m_workspace_files_provider_idx"
                ON "m_workspace_files" (
                    "provider_uuid", "external_account_uuid", "created_at"
                )
                WHERE "provider_uuid" IS NOT NULL;
            """,
            """
            ALTER TABLE "m_mail_folders"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048),
                ADD COLUMN "delivery_status" VARCHAR(32),
                ADD COLUMN "delivery_error" TEXT,
                ADD COLUMN "delivery_updated_at" TIMESTAMPTZ,
                ADD CONSTRAINT "m_mail_folders_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid")
                    REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_mail_folders_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_user_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                ADD CONSTRAINT "m_mail_folders_delivery_status_check"
                    CHECK (
                        "delivery_status" IS NULL OR
                        "delivery_status" IN ('pending', 'delivered', 'failed')
                    );

            CREATE UNIQUE INDEX "m_mail_folders_provider_identity_idx"
                ON "m_mail_folders" (
                    "provider_uuid", "external_user_uuid", "provider_external_id"
                )
                WHERE "provider_uuid" IS NOT NULL
                  AND "external_user_uuid" IS NOT NULL
                  AND "provider_external_id" IS NOT NULL;
            """,
            """
            ALTER TABLE "m_mail_messages"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048),
                ADD COLUMN "delivery_status" VARCHAR(32),
                ADD COLUMN "delivery_error" TEXT,
                ADD COLUMN "delivery_updated_at" TIMESTAMPTZ,
                ADD CONSTRAINT "m_mail_messages_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid")
                    REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_mail_messages_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_user_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                ADD CONSTRAINT "m_mail_messages_delivery_status_check"
                    CHECK (
                        "delivery_status" IS NULL OR
                        "delivery_status" IN ('pending', 'delivered', 'failed')
                    );

            CREATE UNIQUE INDEX "m_mail_messages_provider_identity_idx"
                ON "m_mail_messages" (
                    "provider_uuid", "external_user_uuid", "provider_external_id"
                )
                WHERE "provider_uuid" IS NOT NULL
                  AND "external_user_uuid" IS NOT NULL
                  AND "provider_external_id" IS NOT NULL;
            """,
            """
            ALTER TABLE "m_calendars"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048),
                ADD COLUMN "delivery_status" VARCHAR(32),
                ADD COLUMN "delivery_error" TEXT,
                ADD COLUMN "delivery_updated_at" TIMESTAMPTZ,
                ADD CONSTRAINT "m_calendars_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid")
                    REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_calendars_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_user_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                ADD CONSTRAINT "m_calendars_delivery_status_check"
                    CHECK (
                        "delivery_status" IS NULL OR
                        "delivery_status" IN ('pending', 'delivered', 'failed')
                    );

            CREATE UNIQUE INDEX "m_calendars_provider_identity_idx"
                ON "m_calendars" (
                    "provider_uuid", "external_user_uuid", "provider_external_id"
                )
                WHERE "provider_uuid" IS NOT NULL
                  AND "external_user_uuid" IS NOT NULL
                  AND "provider_external_id" IS NOT NULL;
            """,
            """
            ALTER TABLE "m_calendar_events"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048),
                ADD COLUMN "delivery_status" VARCHAR(32),
                ADD COLUMN "delivery_error" TEXT,
                ADD COLUMN "delivery_updated_at" TIMESTAMPTZ,
                ADD CONSTRAINT "m_calendar_events_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid")
                    REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_calendar_events_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_user_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                ADD CONSTRAINT "m_calendar_events_delivery_status_check"
                    CHECK (
                        "delivery_status" IS NULL OR
                        "delivery_status" IN ('pending', 'delivered', 'failed')
                    );

            CREATE UNIQUE INDEX "m_calendar_events_provider_identity_idx"
                ON "m_calendar_events" (
                    "provider_uuid", "external_user_uuid", "provider_external_id"
                )
                WHERE "provider_uuid" IS NOT NULL
                  AND "external_user_uuid" IS NOT NULL
                  AND "provider_external_id" IS NOT NULL;
            """,
            """
            ALTER TABLE "m_workspace_users"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "external_account_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048),
                ADD CONSTRAINT "m_workspace_users_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid")
                    REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_workspace_users_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_account_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL;

            CREATE UNIQUE INDEX "m_workspace_users_provider_identity_idx"
                ON "m_workspace_users" (
                    "provider_uuid", "external_account_uuid", "provider_external_id"
                )
                WHERE "provider_uuid" IS NOT NULL
                  AND "external_account_uuid" IS NOT NULL
                  AND "provider_external_id" IS NOT NULL;
            """,
            """
            ALTER TABLE "m_workspace_streams"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "external_account_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048),
                ADD COLUMN "delivery_status" VARCHAR(32),
                ADD COLUMN "delivery_error" TEXT,
                ADD COLUMN "delivery_updated_at" TIMESTAMPTZ,
                ADD CONSTRAINT "m_workspace_streams_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid") REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_workspace_streams_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_account_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                ADD CONSTRAINT "m_workspace_streams_delivery_status_check"
                    CHECK ("delivery_status" IS NULL OR "delivery_status" IN ('pending', 'delivered', 'failed'));
            CREATE UNIQUE INDEX "m_workspace_streams_provider_identity_idx"
                ON "m_workspace_streams" ("provider_uuid", "external_account_uuid", "provider_external_id")
                WHERE "provider_uuid" IS NOT NULL AND "external_account_uuid" IS NOT NULL
                  AND "provider_external_id" IS NOT NULL;
            """,
            """
            ALTER TABLE "m_workspace_stream_topics"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "external_account_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048),
                ADD COLUMN "delivery_status" VARCHAR(32),
                ADD COLUMN "delivery_error" TEXT,
                ADD COLUMN "delivery_updated_at" TIMESTAMPTZ,
                ADD CONSTRAINT "m_workspace_stream_topics_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid") REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_workspace_stream_topics_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_account_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                ADD CONSTRAINT "m_workspace_stream_topics_delivery_status_check"
                    CHECK ("delivery_status" IS NULL OR "delivery_status" IN ('pending', 'delivered', 'failed'));
            CREATE UNIQUE INDEX "m_workspace_stream_topics_provider_identity_idx"
                ON "m_workspace_stream_topics" ("provider_uuid", "external_account_uuid", "provider_external_id")
                WHERE "provider_uuid" IS NOT NULL AND "external_account_uuid" IS NOT NULL
                  AND "provider_external_id" IS NOT NULL;
            """,
            """
            ALTER TABLE "m_workspace_messages"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "external_account_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048),
                ADD COLUMN "delivery_status" VARCHAR(32),
                ADD COLUMN "delivery_error" TEXT,
                ADD COLUMN "delivery_updated_at" TIMESTAMPTZ,
                ADD CONSTRAINT "m_workspace_messages_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid") REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_workspace_messages_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_account_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                ADD CONSTRAINT "m_workspace_messages_delivery_status_check"
                    CHECK ("delivery_status" IS NULL OR "delivery_status" IN ('pending', 'delivered', 'failed'));
            CREATE UNIQUE INDEX "m_workspace_messages_provider_identity_idx"
                ON "m_workspace_messages" ("provider_uuid", "external_account_uuid", "provider_external_id")
                WHERE "provider_uuid" IS NOT NULL AND "external_account_uuid" IS NOT NULL
                  AND "provider_external_id" IS NOT NULL;
            """,
            """
            ALTER TABLE "m_workspace_message_reactions"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "external_account_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048),
                ADD COLUMN "delivery_status" VARCHAR(32),
                ADD COLUMN "delivery_error" TEXT,
                ADD COLUMN "delivery_updated_at" TIMESTAMPTZ,
                ADD CONSTRAINT "m_workspace_message_reactions_provider_uuid_fkey"
                    FOREIGN KEY ("provider_uuid") REFERENCES "m_workspace_providers" ("uuid")
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                ADD CONSTRAINT "m_workspace_message_reactions_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_account_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                ADD CONSTRAINT "m_workspace_message_reactions_delivery_status_check"
                    CHECK ("delivery_status" IS NULL OR "delivery_status" IN ('pending', 'delivered', 'failed'));
            CREATE UNIQUE INDEX "m_workspace_message_reactions_provider_identity_idx"
                ON "m_workspace_message_reactions" ("provider_uuid", "external_account_uuid", "provider_external_id")
                WHERE "provider_uuid" IS NOT NULL AND "external_account_uuid" IS NOT NULL
                  AND "provider_external_id" IS NOT NULL;
            """,
            """
            CREATE TABLE "m_provider_commands" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "provider_uuid" UUID NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "domain" VARCHAR(32) NOT NULL,
                "operation" VARCHAR(64) NOT NULL,
                "entity_uuid" UUID NOT NULL,
                "entity_urn" VARCHAR(2300) NOT NULL,
                "payload" JSONB NOT NULL,
                "status" VARCHAR(32) NOT NULL DEFAULT 'pending',
                "safe_error" TEXT,
                "completed_at" TIMESTAMPTZ,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_provider_commands_provider_account_fkey"
                    FOREIGN KEY ("provider_uuid", "external_account_uuid")
                    REFERENCES "m_external_accounts" ("provider_uuid", "uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_provider_commands_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_provider_commands_domain_check"
                    CHECK ("domain" IN ('messenger', 'mail', 'calendar')),
                CONSTRAINT "m_provider_commands_status_check"
                    CHECK ("status" IN ('pending', 'delivered', 'failed'))
            );

            CREATE INDEX "m_provider_commands_poll_idx"
                ON "m_provider_commands" (
                    "provider_uuid", "domain", "status", "created_at", "uuid"
                );
            CREATE INDEX "m_provider_commands_entity_idx"
                ON "m_provider_commands" ("entity_uuid", "created_at" DESC);
            """,
            """
            ALTER TABLE "m_workspace_events"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_object_type_check",
                ADD CONSTRAINT "m_workspace_events_object_type_check"
                    CHECK ("object_type" IN (
                        'message', 'message_reaction', 'stream',
                        'stream_binding', 'topic', 'user', 'folder',
                        'folder_item', 'mail_folder', 'mail_message',
                        'calendar', 'calendar_event', 'external_account'
                    ));
            """,
            """
            DROP TABLE IF EXISTS "m_zulip_outbound_event_states";
            DROP TABLE IF EXISTS "m_zulip_history_sync_tasks";
            DROP TABLE IF EXISTS "m_zulip_event_queue_states";
            DROP TABLE IF EXISTS "m_zulip_processed_entities";
            DROP TABLE IF EXISTS "m_integration_bridge_event_cursors";
            DROP TABLE IF EXISTS "m_external_account_user_syncs";
            """,
        ]
        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            'DROP TABLE IF EXISTS "m_provider_commands";',
            """
            DELETE FROM "m_workspace_events"
                WHERE "object_type" = 'external_account';
            ALTER TABLE "m_workspace_events"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_object_type_check",
                ADD CONSTRAINT "m_workspace_events_object_type_check"
                    CHECK ("object_type" IN (
                        'message', 'message_reaction', 'stream',
                        'stream_binding', 'topic', 'user', 'folder',
                        'folder_item', 'mail_folder', 'mail_message',
                        'calendar', 'calendar_event'
                    ));
            """,
            """
            DELETE FROM "m_workspace_files"
                WHERE "provider_uuid" IS NOT NULL;
            DROP INDEX IF EXISTS "m_workspace_files_provider_idx";
            ALTER TABLE "m_workspace_files"
                DROP CONSTRAINT IF EXISTS "m_workspace_files_origin_check",
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_files_provider_account_fkey",
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_files_provider_uuid_fkey",
                DROP COLUMN IF EXISTS "external_account_uuid",
                DROP COLUMN IF EXISTS "provider_uuid",
                ALTER COLUMN "stream_uuid" SET NOT NULL;
            """,
            """
            DROP INDEX IF EXISTS "m_workspace_message_reactions_provider_identity_idx";
            ALTER TABLE "m_workspace_message_reactions"
                DROP COLUMN IF EXISTS "delivery_updated_at",
                DROP COLUMN IF EXISTS "delivery_error",
                DROP COLUMN IF EXISTS "delivery_status",
                DROP COLUMN IF EXISTS "provider_external_id",
                DROP COLUMN IF EXISTS "external_account_uuid",
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            """
            DROP INDEX IF EXISTS "m_workspace_messages_provider_identity_idx";
            ALTER TABLE "m_workspace_messages"
                DROP COLUMN IF EXISTS "delivery_updated_at",
                DROP COLUMN IF EXISTS "delivery_error",
                DROP COLUMN IF EXISTS "delivery_status",
                DROP COLUMN IF EXISTS "provider_external_id",
                DROP COLUMN IF EXISTS "external_account_uuid",
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            """
            DROP INDEX IF EXISTS "m_workspace_stream_topics_provider_identity_idx";
            ALTER TABLE "m_workspace_stream_topics"
                DROP COLUMN IF EXISTS "delivery_updated_at",
                DROP COLUMN IF EXISTS "delivery_error",
                DROP COLUMN IF EXISTS "delivery_status",
                DROP COLUMN IF EXISTS "provider_external_id",
                DROP COLUMN IF EXISTS "external_account_uuid",
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            """
            DROP INDEX IF EXISTS "m_workspace_streams_provider_identity_idx";
            ALTER TABLE "m_workspace_streams"
                DROP COLUMN IF EXISTS "delivery_updated_at",
                DROP COLUMN IF EXISTS "delivery_error",
                DROP COLUMN IF EXISTS "delivery_status",
                DROP COLUMN IF EXISTS "provider_external_id",
                DROP COLUMN IF EXISTS "external_account_uuid",
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            """
            DROP INDEX IF EXISTS "m_workspace_users_provider_identity_idx";
            ALTER TABLE "m_workspace_users"
                DROP COLUMN IF EXISTS "provider_external_id",
                DROP COLUMN IF EXISTS "external_account_uuid",
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            """
            DROP INDEX IF EXISTS "m_calendar_events_provider_identity_idx";
            ALTER TABLE "m_calendar_events"
                DROP COLUMN IF EXISTS "delivery_updated_at",
                DROP COLUMN IF EXISTS "delivery_error",
                DROP COLUMN IF EXISTS "delivery_status",
                DROP COLUMN IF EXISTS "provider_external_id",
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            """
            DROP INDEX IF EXISTS "m_calendars_provider_identity_idx";
            ALTER TABLE "m_calendars"
                DROP COLUMN IF EXISTS "delivery_updated_at",
                DROP COLUMN IF EXISTS "delivery_error",
                DROP COLUMN IF EXISTS "delivery_status",
                DROP COLUMN IF EXISTS "provider_external_id",
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            """
            DROP INDEX IF EXISTS "m_mail_messages_provider_identity_idx";
            ALTER TABLE "m_mail_messages"
                DROP COLUMN IF EXISTS "delivery_updated_at",
                DROP COLUMN IF EXISTS "delivery_error",
                DROP COLUMN IF EXISTS "delivery_status",
                DROP COLUMN IF EXISTS "provider_external_id",
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            """
            DROP INDEX IF EXISTS "m_mail_folders_provider_identity_idx";
            ALTER TABLE "m_mail_folders"
                DROP COLUMN IF EXISTS "delivery_updated_at",
                DROP COLUMN IF EXISTS "delivery_error",
                DROP COLUMN IF EXISTS "delivery_status",
                DROP COLUMN IF EXISTS "provider_external_id",
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            """
            DROP INDEX IF EXISTS "m_external_accounts_provider_idx";
            ALTER TABLE "m_external_accounts"
                ADD COLUMN IF NOT EXISTS "access_next_check_at" TIMESTAMPTZ
                    NOT NULL DEFAULT NOW(),
                DROP COLUMN IF EXISTS "provider_uuid";
            """,
            'DROP TABLE IF EXISTS "m_workspace_providers";',
        ]
        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
