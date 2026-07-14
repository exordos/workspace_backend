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
        self._depends = [
            "0096-move-default-topic-reference-to-workspace-streams-675f18.py"
        ]

    @property
    def migration_id(self):
        return "0f613bcf-f691-40c5-b57d-081db8e1e555"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_external_accounts"
                DROP CONSTRAINT IF EXISTS
                    "m_external_accounts_account_type_check",
                ADD CONSTRAINT "m_external_accounts_account_type_check"
                    CHECK ("account_type" IN (
                        'zulip', 'iam', 'mail', 'calendar'
                    ));
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
                        'calendar', 'calendar_event'
                    ));
            """,
            """
            ALTER TABLE "m_external_accounts"
                DROP CONSTRAINT IF EXISTS
                    "m_external_accounts_access_status_check",
                ADD CONSTRAINT "m_external_accounts_access_status_check"
                    CHECK ("access_status" IN (
                        'pending', 'missing_credentials', 'confirmed',
                        'invalid_credentials', 'unavailable'
                    ));
            """,
            """
            CREATE TABLE "m_mail_folders" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "external_user_uuid" UUID,
                "path" VARCHAR(1024) NOT NULL,
                "name" VARCHAR(256) NOT NULL,
                "delimiter" VARCHAR(8) NOT NULL DEFAULT '/',
                "special_use" VARCHAR(64),
                "unread_count" INTEGER NOT NULL DEFAULT 0,
                "total_count" INTEGER NOT NULL DEFAULT 0,
                "source_name" VARCHAR(32) NOT NULL DEFAULT 'native',
                "source" JSONB NOT NULL DEFAULT '{}'::jsonb,
                "sync_cursor" VARCHAR(512),
                "sync_status" VARCHAR(32) NOT NULL DEFAULT 'synced',
                "sync_error" TEXT,
                "deleted" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_mail_folders_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_mail_folders_external_user_uuid_fkey"
                    FOREIGN KEY ("external_user_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                CONSTRAINT "m_mail_folders_source_name_check"
                    CHECK ("source_name" IN ('native', 'imap')),
                CONSTRAINT "m_mail_folders_sync_status_check"
                    CHECK ("sync_status" IN (
                        'pending', 'processing', 'synced', 'failed'
                    )),
                CONSTRAINT "m_mail_folders_counts_check"
                    CHECK ("unread_count" >= 0 AND "total_count" >= 0),
                UNIQUE (
                    "project_id", "user_uuid", "external_user_uuid", "path"
                )
            );
            """,
            """
            CREATE TABLE "m_mail_messages" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "folder_uuid" UUID NOT NULL,
                "external_user_uuid" UUID,
                "external_uid" BIGINT,
                "from_address" VARCHAR(2048) NOT NULL DEFAULT '',
                "to_addresses" JSONB NOT NULL DEFAULT '[]'::jsonb,
                "cc_addresses" JSONB NOT NULL DEFAULT '[]'::jsonb,
                "bcc_addresses" JSONB NOT NULL DEFAULT '[]'::jsonb,
                "reply_to" VARCHAR(2048),
                "subject" VARCHAR(2048) NOT NULL DEFAULT '',
                "snippet" VARCHAR(4096) NOT NULL DEFAULT '',
                "body_html" TEXT,
                "body_text" TEXT,
                "message_id" VARCHAR(2048),
                "references" TEXT,
                "sent_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "seen" BOOLEAN NOT NULL DEFAULT FALSE,
                "flagged" BOOLEAN NOT NULL DEFAULT FALSE,
                "draft" BOOLEAN NOT NULL DEFAULT FALSE,
                "deleted" BOOLEAN NOT NULL DEFAULT FALSE,
                "source_name" VARCHAR(32) NOT NULL DEFAULT 'native',
                "source" JSONB NOT NULL DEFAULT '{}'::jsonb,
                "sync_status" VARCHAR(32) NOT NULL DEFAULT 'synced',
                "sync_error" TEXT,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_mail_messages_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_mail_messages_folder_uuid_fkey"
                    FOREIGN KEY ("folder_uuid")
                    REFERENCES "m_mail_folders" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_mail_messages_external_user_uuid_fkey"
                    FOREIGN KEY ("external_user_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                CONSTRAINT "m_mail_messages_source_name_check"
                    CHECK ("source_name" IN ('native', 'imap')),
                CONSTRAINT "m_mail_messages_sync_status_check"
                    CHECK ("sync_status" IN (
                        'pending', 'processing', 'synced', 'failed'
                    )),
                UNIQUE ("external_user_uuid", "folder_uuid", "external_uid")
            );
            """,
            """
            CREATE TABLE "m_mail_attachments" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "message_uuid" UUID NOT NULL,
                "content_id" VARCHAR(2048),
                "name" VARCHAR(1024) NOT NULL,
                "content_type" VARCHAR(255) NOT NULL,
                "size_bytes" BIGINT NOT NULL,
                "hash" VARCHAR(255) NOT NULL,
                "storage_type" VARCHAR(32) NOT NULL DEFAULT 'file',
                "storage_id" VARCHAR(255) NOT NULL DEFAULT '',
                "storage_object_id" VARCHAR(255) NOT NULL,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_mail_attachments_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_mail_attachments_message_uuid_fkey"
                    FOREIGN KEY ("message_uuid")
                    REFERENCES "m_mail_messages" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_mail_attachments_size_check"
                    CHECK ("size_bytes" >= 0)
            );
            """,
            """
            CREATE TABLE "m_calendars" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "external_user_uuid" UUID,
                "name" VARCHAR(256) NOT NULL,
                "color" VARCHAR(32),
                "ctag" VARCHAR(512),
                "source_name" VARCHAR(32) NOT NULL DEFAULT 'native',
                "source" JSONB NOT NULL DEFAULT '{}'::jsonb,
                "sync_token" TEXT,
                "sync_status" VARCHAR(32) NOT NULL DEFAULT 'synced',
                "sync_error" TEXT,
                "deleted" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_calendars_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_calendars_external_user_uuid_fkey"
                    FOREIGN KEY ("external_user_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                CONSTRAINT "m_calendars_source_name_check"
                    CHECK ("source_name" IN ('native', 'caldav')),
                CONSTRAINT "m_calendars_sync_status_check"
                    CHECK ("sync_status" IN (
                        'pending', 'processing', 'synced', 'failed'
                    )),
                UNIQUE (
                    "project_id", "user_uuid", "external_user_uuid", "name"
                )
            );
            """,
            """
            CREATE TABLE "m_calendar_events" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "calendar_uuid" UUID NOT NULL,
                "external_user_uuid" UUID,
                "uid" VARCHAR(1024) NOT NULL,
                "summary" VARCHAR(2048) NOT NULL DEFAULT '',
                "description" TEXT,
                "location" VARCHAR(2048),
                "starts_at" TIMESTAMPTZ NOT NULL,
                "ends_at" TIMESTAMPTZ NOT NULL,
                "all_day" BOOLEAN NOT NULL DEFAULT FALSE,
                "recurrence" JSONB,
                "attendees" JSONB NOT NULL DEFAULT '[]'::jsonb,
                "alarms" JSONB NOT NULL DEFAULT '[]'::jsonb,
                "recurrence_id" VARCHAR(1024),
                "ics" TEXT,
                "etag" VARCHAR(512),
                "source_name" VARCHAR(32) NOT NULL DEFAULT 'native',
                "source" JSONB NOT NULL DEFAULT '{}'::jsonb,
                "sync_status" VARCHAR(32) NOT NULL DEFAULT 'synced',
                "sync_error" TEXT,
                "deleted" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_calendar_events_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_calendar_events_calendar_uuid_fkey"
                    FOREIGN KEY ("calendar_uuid")
                    REFERENCES "m_calendars" ("uuid")
                    ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT "m_calendar_events_external_user_uuid_fkey"
                    FOREIGN KEY ("external_user_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON UPDATE CASCADE ON DELETE SET NULL,
                CONSTRAINT "m_calendar_events_source_name_check"
                    CHECK ("source_name" IN ('native', 'caldav')),
                CONSTRAINT "m_calendar_events_sync_status_check"
                    CHECK ("sync_status" IN (
                        'pending', 'processing', 'synced', 'failed'
                    )),
                CONSTRAINT "m_calendar_events_time_check"
                    CHECK ("ends_at" >= "starts_at")
            );
            """,
            """
            CREATE INDEX "m_mail_folders_owner_idx"
                ON "m_mail_folders" ("project_id", "user_uuid");
            CREATE INDEX "m_mail_messages_owner_folder_date_idx"
                ON "m_mail_messages" (
                    "project_id", "user_uuid", "folder_uuid", "sent_at" DESC
                );
            CREATE INDEX "m_mail_messages_pending_idx"
                ON "m_mail_messages" ("sync_status", "updated_at")
                WHERE "sync_status" IN ('pending', 'failed');
            CREATE INDEX "m_mail_folders_pending_idx"
                ON "m_mail_folders" ("sync_status", "updated_at")
                WHERE "sync_status" IN ('pending', 'failed');
            CREATE INDEX "m_mail_attachments_message_idx"
                ON "m_mail_attachments" ("message_uuid");
            CREATE INDEX "m_calendars_owner_idx"
                ON "m_calendars" ("project_id", "user_uuid");
            CREATE INDEX "m_calendar_events_owner_time_idx"
                ON "m_calendar_events" (
                    "project_id", "user_uuid", "starts_at", "ends_at"
                );
            CREATE INDEX "m_calendar_events_pending_idx"
                ON "m_calendar_events" ("sync_status", "updated_at")
                WHERE "sync_status" IN ('pending', 'failed');
            CREATE UNIQUE INDEX "m_calendar_events_identity_idx"
                ON "m_calendar_events" (
                    "calendar_uuid", "uid", COALESCE("recurrence_id", '')
                );
            CREATE INDEX "m_calendars_pending_idx"
                ON "m_calendars" ("sync_status", "updated_at")
                WHERE "sync_status" IN ('pending', 'failed');
            """,
        ]
        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            'DROP TABLE IF EXISTS "m_calendar_events";',
            'DROP TABLE IF EXISTS "m_calendars";',
            'DROP TABLE IF EXISTS "m_mail_attachments";',
            'DROP TABLE IF EXISTS "m_mail_messages";',
            'DROP TABLE IF EXISTS "m_mail_folders";',
            """
            DELETE FROM "m_workspace_events"
                WHERE "object_type" IN (
                    'mail_folder', 'mail_message',
                    'calendar', 'calendar_event'
                );
            ALTER TABLE "m_workspace_events"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_object_type_check",
                ADD CONSTRAINT "m_workspace_events_object_type_check"
                    CHECK ("object_type" IN (
                        'message', 'message_reaction', 'stream',
                        'stream_binding', 'topic', 'user', 'folder',
                        'folder_item'
                    ));
            """,
            """
            DELETE FROM "m_external_accounts"
                WHERE "account_type" IN ('mail', 'calendar');
            """,
            """
            ALTER TABLE "m_external_accounts"
                DROP CONSTRAINT IF EXISTS
                    "m_external_accounts_account_type_check",
                ADD CONSTRAINT "m_external_accounts_account_type_check"
                    CHECK ("account_type" IN ('zulip', 'iam'));
            """,
            """
            UPDATE "m_external_accounts"
                SET "access_status" = 'unavailable'
                WHERE "access_status" = 'pending';
            ALTER TABLE "m_external_accounts"
                DROP CONSTRAINT IF EXISTS
                    "m_external_accounts_access_status_check",
                ADD CONSTRAINT "m_external_accounts_access_status_check"
                    CHECK ("access_status" IN (
                        'missing_credentials', 'confirmed',
                        'invalid_credentials', 'unavailable'
                    ));
            """,
        ]
        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
