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

WORKSPACE_V3_SCHEMA = """
CREATE SCHEMA "workspace_v3";

CREATE TABLE "workspace_v3"."users" (
    "uuid" UUID PRIMARY KEY,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL,
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL,
    "username" VARCHAR(128) NOT NULL UNIQUE,
    "source" VARCHAR(16) NOT NULL,
    "status" VARCHAR(16) NOT NULL,
    "first_name" VARCHAR(128),
    "last_name" VARCHAR(128),
    "email" VARCHAR(256),
    "last_ping_at" TIMESTAMP WITH TIME ZONE NOT NULL
        DEFAULT CURRENT_TIMESTAMP,
    "status_emoji" VARCHAR(64),
    "status_text" VARCHAR(256),
    "avatar" VARCHAR(2048) NOT NULL,
    CONSTRAINT "users_source_check"
        CHECK ("source" IN ('iam', 'zulip')),
    CONSTRAINT "users_status_check"
        CHECK (
            "status" IN ('active', 'idle', 'offline', 'do_not_disturb')
        ),
    CONSTRAINT "users_avatar_urn_check"
        CHECK (
            (
                "avatar" LIKE 'urn:image:%'
                AND length("avatar") = 46
            )
            OR (
                "avatar" LIKE 'urn:gravatar:%'
                AND length("avatar") IN (45, 77)
            )
            OR "avatar" LIKE 'urn:url:http://%'
            OR "avatar" LIKE 'urn:url:https://%'
        )
);

CREATE UNIQUE INDEX "users_iam_email_unique_idx"
    ON "workspace_v3"."users" ("email")
    WHERE "source" = 'iam' AND "email" IS NOT NULL;

CREATE TABLE "workspace_v3"."streams" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "name" VARCHAR(255) NOT NULL,
    "description" VARCHAR(255),
    "owner_uuid" UUID NOT NULL,
    "source_name" VARCHAR(32) NOT NULL DEFAULT 'native',
    "invite_only" BOOLEAN NOT NULL DEFAULT FALSE,
    "announce" BOOLEAN NOT NULL DEFAULT FALSE,
    "direct_user_uuid" UUID,
    "private" BOOLEAN NOT NULL DEFAULT FALSE,
    "is_archived" BOOLEAN NOT NULL DEFAULT FALSE,
    "private_index" VARCHAR(73),
    "color" BIGINT NOT NULL
        DEFAULT floor(random() * 16777216)::bigint,
    "default_topic_uuid" UUID,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    CONSTRAINT "streams_owner_uuid_fkey"
        FOREIGN KEY ("owner_uuid")
        REFERENCES "workspace_v3"."users" ("uuid"),
    CONSTRAINT "streams_direct_user_uuid_fkey"
        FOREIGN KEY ("direct_user_uuid")
        REFERENCES "workspace_v3"."users" ("uuid"),
    CONSTRAINT "streams_source_name_check"
        CHECK ("source_name" IN ('native', 'zulip')),
    CONSTRAINT "streams_direct_private_check"
        CHECK ("direct_user_uuid" IS NULL OR "private"),
    CONSTRAINT "streams_color_check"
        CHECK ("color" BETWEEN 0 AND 16777215)
);

CREATE UNIQUE INDEX "streams_project_private_index_unique_idx"
    ON "workspace_v3"."streams" ("project_id", "private_index")
    WHERE "private_index" IS NOT NULL;

CREATE TABLE "workspace_v3"."topics" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "stream_uuid" UUID NOT NULL,
    "name" VARCHAR(128) NOT NULL,
    "color" BIGINT NOT NULL
        DEFAULT floor(random() * 16777216)::bigint,
    "source_name" VARCHAR(32) NOT NULL DEFAULT 'native',
    "summary" VARCHAR(4096),
    "summary_last_message_uuid" UUID,
    "summary_enabled" BOOLEAN NOT NULL DEFAULT TRUE,
    "summary_system_prompt" VARCHAR(16384),
    "summary_reasoning_effort" VARCHAR(16),
    "is_done" BOOLEAN NOT NULL DEFAULT FALSE,
    "version" INTEGER NOT NULL DEFAULT 0,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    UNIQUE ("project_id", "stream_uuid", "uuid"),
    CONSTRAINT "topics_stream_fkey"
        FOREIGN KEY ("project_id", "stream_uuid")
        REFERENCES "workspace_v3"."streams" ("project_id", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "topics_source_name_check"
        CHECK ("source_name" IN ('native', 'zulip')),
    CONSTRAINT "topics_color_check"
        CHECK ("color" BETWEEN 0 AND 16777215),
    CONSTRAINT "topics_version_check"
        CHECK ("version" >= 0)
);

CREATE TABLE "workspace_v3"."messages" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "stream_uuid" UUID NOT NULL,
    "topic_uuid" UUID NOT NULL,
    "author_uuid" UUID NOT NULL,
    "payload" JSONB NOT NULL,
    "source_name" VARCHAR(32) NOT NULL DEFAULT 'native',
    "reactions" JSONB NOT NULL DEFAULT '{}'::jsonb,
    "reaction_users" JSONB NOT NULL DEFAULT '{}'::jsonb,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    UNIQUE ("project_id", "stream_uuid", "uuid"),
    UNIQUE ("project_id", "topic_uuid", "uuid"),
    CONSTRAINT "messages_stream_fkey"
        FOREIGN KEY ("project_id", "stream_uuid")
        REFERENCES "workspace_v3"."streams" ("project_id", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "messages_topic_fkey"
        FOREIGN KEY ("project_id", "stream_uuid", "topic_uuid")
        REFERENCES "workspace_v3"."topics"
            ("project_id", "stream_uuid", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "messages_author_uuid_fkey"
        FOREIGN KEY ("author_uuid")
        REFERENCES "workspace_v3"."users" ("uuid"),
    CONSTRAINT "messages_source_name_check"
        CHECK ("source_name" IN ('native', 'zulip'))
);

ALTER TABLE "workspace_v3"."streams"
    ADD CONSTRAINT "streams_default_topic_fkey"
    FOREIGN KEY ("project_id", "uuid", "default_topic_uuid")
    REFERENCES "workspace_v3"."topics"
        ("project_id", "stream_uuid", "uuid")
    ON DELETE SET NULL ("default_topic_uuid")
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE "workspace_v3"."topics"
    ADD CONSTRAINT "topics_summary_last_message_fkey"
    FOREIGN KEY ("project_id", "summary_last_message_uuid")
    REFERENCES "workspace_v3"."messages" ("project_id", "uuid")
    ON DELETE SET NULL ("summary_last_message_uuid")
    DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX "messages_stream_timeline_idx"
    ON "workspace_v3"."messages" (
        "project_id", "stream_uuid", "created_at" DESC, "uuid" DESC
    );

CREATE INDEX "messages_topic_timeline_idx"
    ON "workspace_v3"."messages" (
        "project_id", "stream_uuid", "topic_uuid",
        "created_at" DESC, "uuid" DESC
    );

CREATE INDEX "messages_author_timeline_idx"
    ON "workspace_v3"."messages" (
        "author_uuid", "created_at" DESC
    );

CREATE INDEX "messages_project_timeline_idx"
    ON "workspace_v3"."messages" (
        "project_id", "created_at" DESC, "uuid" DESC
    );

CREATE INDEX "messages_project_updated_idx"
    ON "workspace_v3"."messages" (
        "project_id", "updated_at", "uuid"
    );

CREATE TABLE "workspace_v3"."stream_bindings" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "stream_uuid" UUID NOT NULL,
    "user_uuid" UUID NOT NULL,
    "who_uuid" UUID NOT NULL,
    "role" VARCHAR(32) NOT NULL DEFAULT 'member',
    "notification_mode" VARCHAR(32) NOT NULL DEFAULT 'all_messages',
    "notification_updated_at" TIMESTAMP WITH TIME ZONE NOT NULL
        DEFAULT NOW(),
    "unread_count" INTEGER NOT NULL DEFAULT 0,
    "active_unread_count" INTEGER NOT NULL DEFAULT 0,
    "passive_unread_count" INTEGER NOT NULL DEFAULT 0,
    "last_message_uuid" UUID,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    UNIQUE ("project_id", "stream_uuid", "user_uuid"),
    CONSTRAINT "stream_bindings_stream_fkey"
        FOREIGN KEY ("project_id", "stream_uuid")
        REFERENCES "workspace_v3"."streams" ("project_id", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "stream_bindings_user_fkey"
        FOREIGN KEY ("user_uuid")
        REFERENCES "workspace_v3"."users" ("uuid")
        ON DELETE CASCADE,
    CONSTRAINT "stream_bindings_who_fkey"
        FOREIGN KEY ("who_uuid")
        REFERENCES "workspace_v3"."users" ("uuid"),
    CONSTRAINT "stream_bindings_role_check"
        CHECK (
            "role" IN (
                'guest', 'member', 'moderator', 'administrator', 'owner'
            )
        ),
    CONSTRAINT "stream_bindings_notification_mode_check"
        CHECK (
            "notification_mode" IN (
                'all_messages', 'mentions_only', 'muted'
            )
        ),
    CONSTRAINT "stream_bindings_unread_count_check"
        CHECK (
            "unread_count" >= 0
            AND "active_unread_count" >= 0
            AND "passive_unread_count" >= 0
            AND "unread_count" =
                "active_unread_count" + "passive_unread_count"
        )
);

CREATE INDEX "stream_bindings_user_idx"
    ON "workspace_v3"."stream_bindings" (
        "project_id", "user_uuid", "stream_uuid"
    );

CREATE TABLE "workspace_v3"."topic_bindings" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "stream_uuid" UUID NOT NULL,
    "topic_uuid" UUID NOT NULL,
    "user_uuid" UUID NOT NULL,
    "notification_mode" VARCHAR(32) NOT NULL DEFAULT 'default',
    "notification_updated_at" TIMESTAMP WITH TIME ZONE NOT NULL
        DEFAULT NOW(),
    "unread_count" INTEGER NOT NULL DEFAULT 0,
    "active_unread_count" INTEGER NOT NULL DEFAULT 0,
    "passive_unread_count" INTEGER NOT NULL DEFAULT 0,
    "last_message_uuid" UUID,
    "summary_has_new_messages" BOOLEAN,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    UNIQUE ("project_id", "topic_uuid", "user_uuid"),
    CONSTRAINT "topic_bindings_topic_fkey"
        FOREIGN KEY ("project_id", "stream_uuid", "topic_uuid")
        REFERENCES "workspace_v3"."topics"
            ("project_id", "stream_uuid", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "topic_bindings_stream_binding_fkey"
        FOREIGN KEY ("project_id", "stream_uuid", "user_uuid")
        REFERENCES "workspace_v3"."stream_bindings"
            ("project_id", "stream_uuid", "user_uuid")
        ON DELETE CASCADE,
    CONSTRAINT "topic_bindings_notification_mode_check"
        CHECK (
            "notification_mode" IN ('default', 'mute', 'follow', 'unmute')
        ),
    CONSTRAINT "topic_bindings_unread_count_check"
        CHECK (
            "unread_count" >= 0
            AND "active_unread_count" >= 0
            AND "passive_unread_count" >= 0
            AND "unread_count" =
                "active_unread_count" + "passive_unread_count"
        )
);

CREATE INDEX "topic_bindings_user_idx"
    ON "workspace_v3"."topic_bindings" (
        "project_id", "user_uuid", "topic_uuid"
    );

CREATE TABLE "workspace_v3"."message_flags" (
    "uuid" UUID NOT NULL DEFAULT gen_random_uuid(),
    "project_id" UUID NOT NULL,
    "stream_uuid" UUID NOT NULL,
    "message_uuid" UUID NOT NULL,
    "user_uuid" UUID NOT NULL,
    "read" BOOLEAN NOT NULL DEFAULT FALSE,
    "pinned" BOOLEAN NOT NULL DEFAULT FALSE,
    "starred" BOOLEAN NOT NULL DEFAULT FALSE,
    "mentioned" BOOLEAN NOT NULL DEFAULT FALSE,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    UNIQUE ("project_id", "message_uuid", "user_uuid"),
    CONSTRAINT "message_flags_message_fkey"
        FOREIGN KEY ("project_id", "stream_uuid", "message_uuid")
        REFERENCES "workspace_v3"."messages"
            ("project_id", "stream_uuid", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "message_flags_stream_binding_fkey"
        FOREIGN KEY ("project_id", "stream_uuid", "user_uuid")
        REFERENCES "workspace_v3"."stream_bindings"
            ("project_id", "stream_uuid", "user_uuid")
        ON DELETE CASCADE
);

CREATE INDEX "message_flags_unread_user_idx"
    ON "workspace_v3"."message_flags" (
        "project_id", "user_uuid", "stream_uuid", "message_uuid"
    ) WHERE NOT "read";

CREATE TABLE "workspace_v3"."message_reactions" (
    "uuid" UUID NOT NULL DEFAULT gen_random_uuid(),
    "project_id" UUID NOT NULL,
    "message_uuid" UUID NOT NULL,
    "user_uuid" UUID NOT NULL,
    "emoji_name" VARCHAR(128) NOT NULL,
    "source_name" VARCHAR(32) NOT NULL DEFAULT 'native',
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    UNIQUE ("project_id", "message_uuid", "user_uuid", "emoji_name"),
    CONSTRAINT "message_reactions_message_fkey"
        FOREIGN KEY ("project_id", "message_uuid")
        REFERENCES "workspace_v3"."messages" ("project_id", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "message_reactions_user_fkey"
        FOREIGN KEY ("user_uuid")
        REFERENCES "workspace_v3"."users" ("uuid")
        ON DELETE CASCADE,
    CONSTRAINT "message_reactions_source_name_check"
        CHECK ("source_name" IN ('native', 'zulip'))
);

CREATE INDEX "message_reactions_snapshot_idx"
    ON "workspace_v3"."message_reactions" (
        "project_id", "message_uuid", "emoji_name", "created_at", "uuid"
    ) INCLUDE ("user_uuid");

CREATE TABLE "workspace_v3"."drafts" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "user_uuid" UUID NOT NULL,
    "stream_uuid" UUID NOT NULL,
    "topic_uuid" UUID NOT NULL,
    "payload" JSONB NOT NULL,
    "revision" INTEGER NOT NULL DEFAULT 1,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "user_uuid", "uuid"),
    CONSTRAINT "drafts_stream_binding_fkey"
        FOREIGN KEY ("project_id", "stream_uuid", "user_uuid")
        REFERENCES "workspace_v3"."stream_bindings"
            ("project_id", "stream_uuid", "user_uuid")
        ON DELETE CASCADE,
    CONSTRAINT "drafts_topic_fkey"
        FOREIGN KEY ("project_id", "stream_uuid", "topic_uuid")
        REFERENCES "workspace_v3"."topics"
            ("project_id", "stream_uuid", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "drafts_revision_check" CHECK ("revision" > 0)
);

CREATE INDEX "drafts_user_timeline_idx"
    ON "workspace_v3"."drafts" (
        "project_id", "user_uuid", "updated_at", "uuid"
    );

CREATE TABLE "workspace_v3"."files" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "user_uuid" UUID NOT NULL,
    "stream_uuid" UUID,
    "acl_mode" VARCHAR(16) NOT NULL DEFAULT 'stream',
    "name" VARCHAR(255) NOT NULL,
    "description" VARCHAR(255) NOT NULL DEFAULT '',
    "content_type" VARCHAR(255) NOT NULL,
    "size_bytes" BIGINT NOT NULL,
    "hash" VARCHAR(255) NOT NULL,
    "storage_type" VARCHAR(32) NOT NULL,
    "storage_id" VARCHAR(255) NOT NULL DEFAULT '',
    "storage_object_id" VARCHAR(255) NOT NULL,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    CONSTRAINT "files_user_fkey"
        FOREIGN KEY ("user_uuid")
        REFERENCES "workspace_v3"."users" ("uuid"),
    CONSTRAINT "files_stream_fkey"
        FOREIGN KEY ("project_id", "stream_uuid")
        REFERENCES "workspace_v3"."streams" ("project_id", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "files_acl_mode_check"
        CHECK ("acl_mode" IN ('owner', 'stream', 'public')),
    CONSTRAINT "files_acl_shape_check"
        CHECK (
            ("acl_mode" = 'public' AND "stream_uuid" IS NULL)
            OR ("acl_mode" = 'owner' AND "stream_uuid" IS NULL)
            OR ("acl_mode" = 'stream' AND "stream_uuid" IS NOT NULL)
        ),
    CONSTRAINT "files_size_bytes_check" CHECK ("size_bytes" >= 0)
);

CREATE INDEX "files_stream_idx"
    ON "workspace_v3"."files" (
        "project_id", "stream_uuid", "created_at", "uuid"
    ) WHERE "stream_uuid" IS NOT NULL;

CREATE INDEX "files_owner_idx"
    ON "workspace_v3"."files" (
        "project_id", "user_uuid", "created_at", "uuid"
    );

CREATE TABLE "workspace_v3"."folders" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "user_uuid" UUID NOT NULL,
    "kind" VARCHAR(32) NOT NULL,
    "title" VARCHAR(64) NOT NULL,
    "background_color_value" BIGINT,
    "unread_count" INTEGER NOT NULL DEFAULT 0,
    "active_unread_count" INTEGER NOT NULL DEFAULT 0,
    "passive_unread_count" INTEGER NOT NULL DEFAULT 0,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "user_uuid", "uuid"),
    CONSTRAINT "folders_user_fkey"
        FOREIGN KEY ("user_uuid")
        REFERENCES "workspace_v3"."users" ("uuid")
        ON DELETE CASCADE,
    CONSTRAINT "folders_kind_check"
        CHECK ("kind" IN ('all_chats', 'direct', 'streams', 'custom')),
    CONSTRAINT "folders_unread_count_check"
        CHECK (
            "unread_count" >= 0
            AND "active_unread_count" >= 0
            AND "passive_unread_count" >= 0
            AND "unread_count" =
                "active_unread_count" + "passive_unread_count"
        )
);

CREATE UNIQUE INDEX "folders_system_kind_unique_idx"
    ON "workspace_v3"."folders" ("project_id", "user_uuid", "kind")
    WHERE "kind" <> 'custom';

CREATE INDEX "folders_user_idx"
    ON "workspace_v3"."folders" ("project_id", "user_uuid", "created_at", "uuid");

CREATE TABLE "workspace_v3"."folder_items" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "user_uuid" UUID NOT NULL,
    "folder_uuid" UUID NOT NULL,
    "stream_uuid" UUID NOT NULL,
    "order_index" INTEGER,
    "pinned_at" TIMESTAMP WITH TIME ZONE,
    "chat_type" VARCHAR(32) NOT NULL,
    "automatic" BOOLEAN NOT NULL DEFAULT FALSE,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "user_uuid", "uuid"),
    UNIQUE ("project_id", "user_uuid", "folder_uuid", "stream_uuid"),
    CONSTRAINT "folder_items_folder_fkey"
        FOREIGN KEY ("project_id", "user_uuid", "folder_uuid")
        REFERENCES "workspace_v3"."folders"
            ("project_id", "user_uuid", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "folder_items_stream_binding_fkey"
        FOREIGN KEY ("project_id", "stream_uuid", "user_uuid")
        REFERENCES "workspace_v3"."stream_bindings"
            ("project_id", "stream_uuid", "user_uuid")
        ON DELETE CASCADE,
    CONSTRAINT "folder_items_chat_type_check"
        CHECK ("chat_type" IN ('private', 'group', 'stream')),
    CONSTRAINT "folder_items_automatic_shape_check"
        CHECK (
            NOT "automatic"
            OR ("order_index" IS NULL AND "pinned_at" IS NULL)
        )
);

CREATE INDEX "folder_items_folder_order_idx"
    ON "workspace_v3"."folder_items" (
        "project_id", "user_uuid", "folder_uuid",
        "order_index" NULLS LAST, "created_at", "uuid"
    );

CREATE INDEX "folder_items_stream_idx"
    ON "workspace_v3"."folder_items" (
        "project_id", "user_uuid", "stream_uuid", "folder_uuid"
    );

CREATE TABLE "workspace_v3"."event_audience_snapshots" (
    "uuid" UUID NOT NULL,
    "project_id" UUID NOT NULL,
    "membership_digest" VARCHAR(64) NOT NULL,
    "current_epoch_version" BIGINT NOT NULL DEFAULT 0,
    "pruned_through_epoch_version" BIGINT NOT NULL DEFAULT 0,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    UNIQUE ("project_id", "membership_digest"),
    CONSTRAINT "event_audience_snapshots_versions_check"
        CHECK (
            "current_epoch_version" >= 0
            AND "pruned_through_epoch_version" >= 0
            AND "pruned_through_epoch_version" <= "current_epoch_version"
        )
);

CREATE TABLE "workspace_v3"."event_audience_members" (
    "project_id" UUID NOT NULL,
    "audience_snapshot_uuid" UUID NOT NULL,
    "consumer_type" VARCHAR(16) NOT NULL,
    "consumer_uuid" UUID NOT NULL,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (
        "project_id", "audience_snapshot_uuid",
        "consumer_type", "consumer_uuid"
    ),
    CONSTRAINT "event_audience_members_snapshot_fkey"
        FOREIGN KEY ("project_id", "audience_snapshot_uuid")
        REFERENCES "workspace_v3"."event_audience_snapshots"
            ("project_id", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "event_audience_members_consumer_type_check"
        CHECK ("consumer_type" IN ('user', 'provider'))
);

CREATE INDEX "event_audience_members_consumer_idx"
    ON "workspace_v3"."event_audience_members" (
        "project_id", "consumer_type", "consumer_uuid",
        "audience_snapshot_uuid"
    );

CREATE TABLE "workspace_v3"."events" (
    "epoch_version" BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    "uuid" UUID NOT NULL UNIQUE,
    "project_id" UUID NOT NULL,
    "entity_uuid" UUID NOT NULL,
    "audience_snapshot_uuid" UUID NOT NULL,
    "schema_version" SMALLINT NOT NULL DEFAULT 1,
    "object_type" VARCHAR(32) NOT NULL,
    "action" VARCHAR(16) NOT NULL,
    "payload" JSONB NOT NULL,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE ("project_id", "uuid"),
    CONSTRAINT "events_audience_snapshot_fkey"
        FOREIGN KEY ("project_id", "audience_snapshot_uuid")
        REFERENCES "workspace_v3"."event_audience_snapshots"
            ("project_id", "uuid"),
    CONSTRAINT "events_schema_version_check"
        CHECK ("schema_version" > 0),
    CONSTRAINT "events_payload_check"
        CHECK (jsonb_typeof("payload") = 'object')
);

CREATE INDEX "events_project_epoch_idx"
    ON "workspace_v3"."events" ("project_id", "epoch_version");

CREATE INDEX "events_audience_epoch_idx"
    ON "workspace_v3"."events" (
        "project_id", "audience_snapshot_uuid", "epoch_version" DESC
    );

CREATE INDEX "events_entity_idx"
    ON "workspace_v3"."events" (
        "project_id", "entity_uuid", "epoch_version"
    );

CREATE INDEX "events_retention_idx"
    ON "workspace_v3"."events" (
        "created_at", "project_id", "epoch_version"
    );

CREATE FUNCTION "workspace_v3"."notify_event_created"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    PERFORM pg_notify('workspace_events', NEW.epoch_version::text);
    RETURN NEW;
END
$function$;

CREATE TRIGGER "events_notify_created"
    AFTER INSERT ON "workspace_v3"."events"
    FOR EACH ROW
    EXECUTE FUNCTION "workspace_v3"."notify_event_created"();

CREATE TABLE "workspace_v3"."event_recipient_payloads" (
    "project_id" UUID NOT NULL,
    "event_uuid" UUID NOT NULL,
    "consumer_type" VARCHAR(16) NOT NULL,
    "consumer_uuid" UUID NOT NULL,
    "payload" JSONB NOT NULL,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (
        "project_id", "event_uuid", "consumer_type", "consumer_uuid"
    ),
    CONSTRAINT "event_recipient_payloads_event_fkey"
        FOREIGN KEY ("project_id", "event_uuid")
        REFERENCES "workspace_v3"."events" ("project_id", "uuid")
        ON DELETE CASCADE,
    CONSTRAINT "event_recipient_payloads_consumer_type_check"
        CHECK ("consumer_type" IN ('user', 'provider')),
    CONSTRAINT "event_recipient_payloads_payload_check"
        CHECK (jsonb_typeof("payload") = 'object')
);

CREATE INDEX "event_recipient_payloads_consumer_idx"
    ON "workspace_v3"."event_recipient_payloads" (
        "project_id", "consumer_type", "consumer_uuid", "event_uuid"
    );

CREATE TABLE "workspace_v3"."event_cursors" (
    "project_id" UUID NOT NULL,
    "consumer_type" VARCHAR(16) NOT NULL,
    "consumer_uuid" UUID NOT NULL,
    "epoch_generation" UUID NOT NULL DEFAULT gen_random_uuid(),
    "current_epoch_version" BIGINT NOT NULL DEFAULT 0,
    "pruned_through_epoch_version" BIGINT NOT NULL DEFAULT 0,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "consumer_type", "consumer_uuid"),
    CONSTRAINT "event_cursors_consumer_type_check"
        CHECK ("consumer_type" IN ('user', 'provider')),
    CONSTRAINT "event_cursors_versions_check"
        CHECK (
            "current_epoch_version" >= 0
            AND "pruned_through_epoch_version" >= 0
            AND "pruned_through_epoch_version" <= "current_epoch_version"
        )
);

CREATE TABLE "workspace_v3"."projection_tasks" (
    "uuid" UUID NOT NULL DEFAULT gen_random_uuid(),
    "project_id" UUID NOT NULL,
    "task_type" VARCHAR(32) NOT NULL,
    "scope_type" VARCHAR(32) NOT NULL,
    "scope_uuid" UUID NOT NULL,
    "user_uuid" UUID,
    "payload" JSONB NOT NULL DEFAULT '{}'::jsonb,
    "status" VARCHAR(16) NOT NULL DEFAULT 'pending',
    "lease_owner" VARCHAR(255),
    "lease_expires_at" TIMESTAMP WITH TIME ZONE,
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "next_retry_at" TIMESTAMP WITH TIME ZONE,
    "last_error" VARCHAR(255),
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY ("project_id", "uuid"),
    UNIQUE ("uuid"),
    CONSTRAINT "projection_tasks_user_fkey"
        FOREIGN KEY ("user_uuid")
        REFERENCES "workspace_v3"."users" ("uuid")
        ON DELETE CASCADE,
    CONSTRAINT "projection_tasks_task_type_check"
        CHECK (
            "task_type" IN (
                'reaction_snapshot', 'read_counters', 'folder_membership',
                'folder_counters'
            )
        ),
    CONSTRAINT "projection_tasks_scope_type_check"
        CHECK (
            "scope_type" IN ('message', 'user_stream', 'user_topic', 'user_folder')
        ),
    CONSTRAINT "projection_tasks_status_check"
        CHECK (
            "status" IN (
                'pending', 'running', 'completed', 'failed', 'dead_letter'
            )
        ),
    CONSTRAINT "projection_tasks_attempts_check"
        CHECK ("attempts" >= 0),
    CONSTRAINT "projection_tasks_payload_check"
        CHECK (jsonb_typeof("payload") = 'object')
);

CREATE INDEX "projection_tasks_claim_idx"
    ON "workspace_v3"."projection_tasks" (
        "status", "next_retry_at", "created_at", "user_uuid",
        "scope_uuid", "uuid"
    ) WHERE "status" IN ('pending', 'running', 'failed');

CREATE INDEX "projection_tasks_scope_idx"
    ON "workspace_v3"."projection_tasks" (
        "project_id", "task_type", "scope_type", "scope_uuid",
        "user_uuid", "created_at", "uuid"
    );

CREATE UNIQUE INDEX "projection_tasks_pending_counter_idx"
    ON "workspace_v3"."projection_tasks" (
        "project_id", "task_type", "scope_type", "scope_uuid", "user_uuid"
    ) WHERE "status" = 'pending'
      AND "task_type" = 'read_counters'
      AND "payload" IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      );

CREATE FUNCTION "workspace_v3"."enforce_private_stream_member_limit"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
DECLARE
    stream_is_private BOOLEAN;
    member_count INTEGER;
BEGIN
    SELECT stream.private
    INTO stream_is_private
    FROM "workspace_v3"."streams" AS stream
    WHERE stream.project_id = NEW.project_id
      AND stream.uuid = NEW.stream_uuid
    FOR UPDATE;

    IF stream_is_private THEN
        SELECT count(*)
        INTO member_count
        FROM "workspace_v3"."stream_bindings" AS binding
        WHERE binding.project_id = NEW.project_id
          AND binding.stream_uuid = NEW.stream_uuid
          AND (TG_OP = 'INSERT' OR binding.uuid <> OLD.uuid);

        IF member_count >= 2 THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'stream_bindings_private_member_limit_check',
                MESSAGE = 'private stream cannot have more than two members';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER "stream_bindings_private_member_limit"
    BEFORE INSERT OR UPDATE OF project_id, stream_uuid
    ON "workspace_v3"."stream_bindings"
    FOR EACH ROW
    EXECUTE FUNCTION "workspace_v3"."enforce_private_stream_member_limit"();

CREATE FUNCTION "workspace_v3"."enforce_private_stream_shape"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.private AND NOT OLD.private AND (
        SELECT count(*)
        FROM "workspace_v3"."stream_bindings" AS binding
        WHERE binding.project_id = NEW.project_id
          AND binding.stream_uuid = NEW.uuid
    ) > 2 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'streams_private_member_limit_check',
            MESSAGE = 'stream with more than two members cannot become private';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER "streams_private_member_limit"
    BEFORE UPDATE OF private ON "workspace_v3"."streams"
    FOR EACH ROW
    EXECUTE FUNCTION "workspace_v3"."enforce_private_stream_shape"();

ALTER TABLE "workspace_v3"."stream_bindings"
    ADD CONSTRAINT "stream_bindings_last_message_fkey"
    FOREIGN KEY ("project_id", "stream_uuid", "last_message_uuid")
    REFERENCES "workspace_v3"."messages"
        ("project_id", "stream_uuid", "uuid")
    ON DELETE SET NULL ("last_message_uuid")
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE "workspace_v3"."topic_bindings"
    ADD CONSTRAINT "topic_bindings_last_message_fkey"
    FOREIGN KEY ("project_id", "topic_uuid", "last_message_uuid")
    REFERENCES "workspace_v3"."messages"
        ("project_id", "topic_uuid", "uuid")
    ON DELETE SET NULL ("last_message_uuid")
    DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION "workspace_v3"."enqueue_reaction_insert_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, payload
    )
    SELECT reaction.project_id, 'reaction_snapshot', 'message',
           reaction.message_uuid,
           jsonb_build_object(
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'action', 'created',
                       'reaction_uuid', reaction.uuid,
                       'message_uuid', reaction.message_uuid,
                       'user_uuid', reaction.user_uuid,
                       'emoji_name', reaction.emoji_name,
                       'source_name', reaction.source_name
                   ) ORDER BY reaction.uuid
               )
           )
    FROM new_rows AS reaction
    GROUP BY reaction.project_id, reaction.message_uuid;
    RETURN NULL;
END
$function$;

CREATE FUNCTION "workspace_v3"."enqueue_reaction_update_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM old_rows AS old_reaction
        JOIN new_rows AS new_reaction USING (uuid)
        WHERE (
                old_reaction.project_id,
                old_reaction.message_uuid,
                old_reaction.user_uuid
              ) IS DISTINCT FROM (
                new_reaction.project_id,
                new_reaction.message_uuid,
                new_reaction.user_uuid
              )
    ) THEN
        RAISE EXCEPTION 'reaction identity is immutable';
    END IF;
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, payload
    )
    SELECT reaction.project_id, 'reaction_snapshot', 'message',
           reaction.message_uuid,
           jsonb_build_object(
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'action', 'updated',
                       'reaction_uuid', reaction.uuid,
                       'message_uuid', reaction.message_uuid,
                       'user_uuid', reaction.user_uuid,
                       'emoji_name', reaction.emoji_name,
                       'source_name', reaction.source_name,
                       'old_emoji_name', old_reaction.emoji_name,
                       'old_source_name', old_reaction.source_name
                   ) ORDER BY reaction.uuid
               )
           )
    FROM new_rows AS reaction
    JOIN old_rows AS old_reaction USING (uuid)
    WHERE old_reaction IS DISTINCT FROM reaction
    GROUP BY reaction.project_id, reaction.message_uuid;
    RETURN NULL;
END
$function$;

CREATE FUNCTION "workspace_v3"."enqueue_reaction_delete_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, payload
    )
    SELECT reaction.project_id, 'reaction_snapshot', 'message',
           reaction.message_uuid,
           jsonb_build_object(
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'action', 'deleted',
                       'reaction_uuid', reaction.uuid,
                       'message_uuid', reaction.message_uuid,
                       'user_uuid', reaction.user_uuid,
                       'emoji_name', reaction.emoji_name,
                       'source_name', reaction.source_name
                   ) ORDER BY reaction.uuid
               )
           )
    FROM old_rows AS reaction
    GROUP BY reaction.project_id, reaction.message_uuid;
    RETURN NULL;
END
$function$;

CREATE TRIGGER "message_reactions_projection_insert"
    AFTER INSERT ON "workspace_v3"."message_reactions"
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_reaction_insert_projection"();

CREATE TRIGGER "message_reactions_projection_update"
    AFTER UPDATE ON "workspace_v3"."message_reactions"
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_reaction_update_projection"();

CREATE TRIGGER "message_reactions_projection_delete"
    AFTER DELETE ON "workspace_v3"."message_reactions"
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_reaction_delete_projection"();

CREATE FUNCTION "workspace_v3"."enqueue_message_flag_insert_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT flag.project_id, 'read_counters', 'user_stream',
           message.stream_uuid, flag.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM new_rows AS flag
    JOIN "workspace_v3"."messages" AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;

    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT flag.project_id, 'read_counters', 'user_topic',
           message.topic_uuid, flag.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM new_rows AS flag
    JOIN "workspace_v3"."messages" AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;
    RETURN NULL;
END
$function$;

CREATE FUNCTION "workspace_v3"."enqueue_message_flag_update_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT flag.project_id, 'read_counters', 'user_stream',
           message.stream_uuid, flag.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM new_rows AS flag
    JOIN old_rows AS old_flag USING (uuid)
    JOIN "workspace_v3"."messages" AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    WHERE old_flag.read IS DISTINCT FROM flag.read
       OR old_flag.mentioned IS DISTINCT FROM flag.mentioned
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;

    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT flag.project_id, 'read_counters', 'user_topic',
           message.topic_uuid, flag.user_uuid,
           jsonb_build_object(
               'emit_message_events', true,
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'message_uuid', flag.message_uuid,
                       'operation', 'update',
                       'read', flag.read,
                       'pinned', flag.pinned,
                       'starred', flag.starred,
                       'mentioned', flag.mentioned
                   ) ORDER BY flag.uuid
               )
           )
    FROM new_rows AS flag
    JOIN old_rows AS old_flag USING (uuid)
    JOIN "workspace_v3"."messages" AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    WHERE (
            old_flag.read,
            old_flag.pinned,
            old_flag.starred,
            old_flag.mentioned
          ) IS DISTINCT FROM (
            flag.read,
            flag.pinned,
            flag.starred,
            flag.mentioned
          )
    GROUP BY flag.project_id, message.topic_uuid, flag.user_uuid;
    RETURN NULL;
END
$function$;

CREATE FUNCTION "workspace_v3"."enqueue_message_flag_delete_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT flag.project_id, 'read_counters', 'user_stream',
           message.stream_uuid, flag.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM old_rows AS flag
    JOIN "workspace_v3"."messages" AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;

    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT flag.project_id, 'read_counters', 'user_topic',
           message.topic_uuid, flag.user_uuid,
           jsonb_build_object(
               'emit_message_events', true,
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'message_uuid', flag.message_uuid,
                       'operation', 'delete',
                       'read', flag.read,
                       'pinned', flag.pinned,
                       'starred', flag.starred,
                       'mentioned', flag.mentioned
                   ) ORDER BY flag.uuid
               )
           )
    FROM old_rows AS flag
    JOIN "workspace_v3"."messages" AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    GROUP BY flag.project_id, message.topic_uuid, flag.user_uuid;
    RETURN NULL;
END
$function$;

CREATE TRIGGER "message_flags_projection_insert"
    AFTER INSERT ON "workspace_v3"."message_flags"
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_message_flag_insert_projection"();

CREATE TRIGGER "message_flags_projection_update"
    AFTER UPDATE ON "workspace_v3"."message_flags"
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_message_flag_update_projection"();

CREATE TRIGGER "message_flags_projection_delete"
    AFTER DELETE ON "workspace_v3"."message_flags"
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_message_flag_delete_projection"();

CREATE FUNCTION "workspace_v3"."enqueue_binding_counter_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.notification_mode IS NOT DISTINCT FROM NEW.notification_mode THEN
        RETURN NEW;
    END IF;
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid,
        user_uuid, payload
    ) VALUES (
        NEW.project_id,
        'read_counters',
        CASE TG_TABLE_NAME
            WHEN 'stream_bindings' THEN 'user_stream'
            ELSE 'user_topic'
        END,
        CASE TG_TABLE_NAME
            WHEN 'stream_bindings'
                THEN (to_jsonb(NEW)->>'stream_uuid')::uuid
            ELSE (to_jsonb(NEW)->>'topic_uuid')::uuid
        END,
        NEW.user_uuid,
        '{"emit_message_event": false}'::jsonb
    )
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;
    RETURN NEW;
END
$function$;

CREATE TRIGGER "stream_bindings_counter_projection"
    AFTER INSERT OR UPDATE ON "workspace_v3"."stream_bindings"
    FOR EACH ROW
    EXECUTE FUNCTION "workspace_v3"."enqueue_binding_counter_projection"();

CREATE TRIGGER "topic_bindings_counter_projection"
    AFTER INSERT OR UPDATE ON "workspace_v3"."topic_bindings"
    FOR EACH ROW
    EXECUTE FUNCTION "workspace_v3"."enqueue_binding_counter_projection"();

CREATE FUNCTION "workspace_v3"."enqueue_folder_membership_insert_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    )
    SELECT DISTINCT binding.project_id, 'folder_membership', 'user_stream',
           binding.stream_uuid, binding.user_uuid
    FROM new_rows AS binding;
    RETURN NULL;
END
$function$;

CREATE FUNCTION "workspace_v3"."enqueue_folder_membership_update_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    )
    SELECT DISTINCT changed.project_id, 'folder_membership', 'user_stream',
           changed.stream_uuid, changed.user_uuid
    FROM (
        SELECT old_binding.project_id, old_binding.stream_uuid,
               old_binding.user_uuid
        FROM old_rows AS old_binding
        JOIN new_rows AS new_binding USING (uuid)
        WHERE (
                old_binding.project_id,
                old_binding.stream_uuid,
                old_binding.user_uuid
              ) IS DISTINCT FROM (
                new_binding.project_id,
                new_binding.stream_uuid,
                new_binding.user_uuid
              )
        UNION
        SELECT new_binding.project_id, new_binding.stream_uuid,
               new_binding.user_uuid
        FROM old_rows AS old_binding
        JOIN new_rows AS new_binding USING (uuid)
        WHERE (
                old_binding.project_id,
                old_binding.stream_uuid,
                old_binding.user_uuid
              ) IS DISTINCT FROM (
                new_binding.project_id,
                new_binding.stream_uuid,
                new_binding.user_uuid
              )
    ) AS changed;
    RETURN NULL;
END
$function$;

CREATE FUNCTION "workspace_v3"."enqueue_folder_membership_delete_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    )
    SELECT DISTINCT binding.project_id, 'folder_membership', 'user_stream',
           binding.stream_uuid, binding.user_uuid
    FROM old_rows AS binding;
    RETURN NULL;
END
$function$;

CREATE TRIGGER "stream_bindings_folder_membership_insert"
    AFTER INSERT ON "workspace_v3"."stream_bindings"
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_folder_membership_insert_projection"();

CREATE TRIGGER "stream_bindings_folder_membership_update"
    AFTER UPDATE ON "workspace_v3"."stream_bindings"
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_folder_membership_update_projection"();

CREATE TRIGGER "stream_bindings_folder_membership_delete"
    AFTER DELETE ON "workspace_v3"."stream_bindings"
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_folder_membership_delete_projection"();

CREATE FUNCTION "workspace_v3"."enqueue_stream_folder_membership_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    )
    SELECT DISTINCT stream.project_id, 'folder_membership', 'user_stream',
           stream.uuid, binding.user_uuid
    FROM new_rows AS stream
    JOIN old_rows AS old_stream USING (project_id, uuid)
    JOIN "workspace_v3"."stream_bindings" AS binding
      ON binding.project_id = stream.project_id
     AND binding.stream_uuid = stream.uuid
    WHERE (old_stream.private, old_stream.is_archived)
          IS DISTINCT FROM (stream.private, stream.is_archived);
    RETURN NULL;
END
$function$;

CREATE TRIGGER "streams_folder_membership_update"
    AFTER UPDATE ON "workspace_v3"."streams"
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_stream_folder_membership_projection"();

CREATE FUNCTION "workspace_v3"."enqueue_folder_item_insert_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF current_setting(
        'workspace_v3.suppress_folder_item_projection', true
    ) = 'on' THEN
        RETURN NULL;
    END IF;
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT item.project_id, 'folder_counters', 'user_folder',
           item.folder_uuid, item.user_uuid,
           jsonb_build_object(
               'operations',
               jsonb_agg(
                   jsonb_build_object(
                       'action', 'created',
                       'uuid', item.uuid,
                       'folder_uuid', item.folder_uuid,
                       'stream_uuid', item.stream_uuid
                   ) ORDER BY item.uuid
               )
           )
    FROM new_rows AS item
    GROUP BY item.project_id, item.user_uuid, item.folder_uuid;
    RETURN NULL;
END
$function$;

CREATE FUNCTION "workspace_v3"."enqueue_folder_item_update_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF current_setting(
        'workspace_v3.suppress_folder_item_projection', true
    ) = 'on' THEN
        RETURN NULL;
    END IF;
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT item.project_id, 'folder_counters', 'user_folder',
           item.folder_uuid, item.user_uuid,
           jsonb_build_object(
               'operations',
               jsonb_agg(
                   jsonb_build_object(
                       'action', 'updated',
                       'uuid', item.uuid,
                       'folder_uuid', item.folder_uuid,
                       'stream_uuid', item.stream_uuid
                   ) ORDER BY item.uuid
               )
           )
    FROM new_rows AS item
    JOIN old_rows AS old_item USING (project_id, user_uuid, uuid)
    WHERE old_item IS DISTINCT FROM item
    GROUP BY item.project_id, item.user_uuid, item.folder_uuid;

    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT item.project_id, 'folder_counters', 'user_folder',
           item.folder_uuid, item.user_uuid,
           jsonb_build_object(
               'operations',
               jsonb_agg(
                   jsonb_build_object(
                       'action', 'deleted',
                       'uuid', item.uuid,
                       'folder_uuid', item.folder_uuid,
                       'stream_uuid', item.stream_uuid
                   ) ORDER BY item.uuid
               )
           )
    FROM old_rows AS item
    JOIN new_rows AS new_item USING (project_id, user_uuid, uuid)
    WHERE (item.folder_uuid, item.stream_uuid)
          IS DISTINCT FROM (new_item.folder_uuid, new_item.stream_uuid)
    GROUP BY item.project_id, item.user_uuid, item.folder_uuid;
    RETURN NULL;
END
$function$;

CREATE FUNCTION "workspace_v3"."enqueue_folder_item_delete_projection"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF current_setting(
        'workspace_v3.suppress_folder_item_projection', true
    ) = 'on' THEN
        RETURN NULL;
    END IF;
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT item.project_id, 'folder_counters', 'user_folder',
           item.folder_uuid, item.user_uuid,
           jsonb_build_object(
               'operations',
               jsonb_agg(
                   jsonb_build_object(
                       'action', 'deleted',
                       'uuid', item.uuid,
                       'folder_uuid', item.folder_uuid,
                       'stream_uuid', item.stream_uuid
                   ) ORDER BY item.uuid
               )
           )
    FROM old_rows AS item
    GROUP BY item.project_id, item.user_uuid, item.folder_uuid;
    RETURN NULL;
END
$function$;

CREATE TRIGGER "folder_items_projection_insert"
    AFTER INSERT ON "workspace_v3"."folder_items"
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_folder_item_insert_projection"();

CREATE TRIGGER "folder_items_projection_update"
    AFTER UPDATE ON "workspace_v3"."folder_items"
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_folder_item_update_projection"();

CREATE TRIGGER "folder_items_projection_delete"
    AFTER DELETE ON "workspace_v3"."folder_items"
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_folder_item_delete_projection"();

CREATE FUNCTION "workspace_v3"."enqueue_folder_counter_from_binding_update"()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO "workspace_v3"."projection_tasks" (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    )
    SELECT DISTINCT binding.project_id, 'folder_counters', 'user_folder',
           item.folder_uuid, binding.user_uuid
    FROM new_rows AS binding
    JOIN old_rows AS old_binding USING (project_id, uuid)
    JOIN "workspace_v3"."folder_items" AS item
      ON item.project_id = binding.project_id
     AND item.user_uuid = binding.user_uuid
     AND item.stream_uuid = binding.stream_uuid
    WHERE (
            old_binding.unread_count,
            old_binding.active_unread_count,
            old_binding.passive_unread_count
          ) IS DISTINCT FROM (
            binding.unread_count,
            binding.active_unread_count,
            binding.passive_unread_count
          );
    RETURN NULL;
END
$function$;

CREATE TRIGGER "stream_bindings_folder_counter_update"
    AFTER UPDATE ON "workspace_v3"."stream_bindings"
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION "workspace_v3"."enqueue_folder_counter_from_binding_update"();
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0185-Repair-per-user-operation-delivery-projections-e1f5ca.py"
        ]

    @property
    def migration_id(self):
        return "209c0aac-e702-4645-847d-30c5754beb77"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(WORKSPACE_V3_SCHEMA)

    def downgrade(self, session):
        session.execute('DROP SCHEMA IF EXISTS "workspace_v3" CASCADE;')


migration_step = MigrationStep()
