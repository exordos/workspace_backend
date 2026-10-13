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


UPGRADE_SQL = """
CREATE TABLE "bridge_zulip_v5_message_contents" (
    "uuid" UUID PRIMARY KEY,
    "zulip_content" VARCHAR(10000) NOT NULL,
    "workspace_content" VARCHAR(64536) NOT NULL,
    "zulip_content_hash" VARCHAR(255) NOT NULL,
    "workspace_content_hash" VARCHAR(255) NOT NULL,
    "zulip_content_updated_at" TIMESTAMP WITH TIME ZONE NOT NULL,
    "workspace_content_updated_at" TIMESTAMP WITH TIME ZONE NOT NULL,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE "bridge_zulip_v5_channels" (
    "uuid" UUID PRIMARY KEY,
    "stream_id" BIGINT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT NOT NULL,
    "rendered_description" TEXT NOT NULL,
    "date_created" BIGINT NOT NULL,
    "creator_id" BIGINT,
    "invite_only" BOOLEAN NOT NULL,
    "is_web_public" BOOLEAN NOT NULL,
    "history_public_to_subscribers" BOOLEAN NOT NULL,
    "is_archived" BOOLEAN NOT NULL,
    "message_retention_days" BIGINT,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX "bridge_zulip_v5_channels_stream_id_idx"
    ON "bridge_zulip_v5_channels" ("stream_id");

CREATE TABLE "bridge_zulip_v5_topics" (
    "uuid" UUID PRIMARY KEY,
    "stream_id" BIGINT NOT NULL,
    "name" TEXT NOT NULL,
    "max_id" BIGINT NOT NULL,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX "bridge_zulip_v5_topics_stream_id_idx"
    ON "bridge_zulip_v5_topics" ("stream_id");

CREATE TABLE "bridge_zulip_v5_messages" (
    "uuid" UUID PRIMARY KEY,
    "external_account_uuid" UUID NOT NULL,
    "message_id" BIGINT NOT NULL,
    "sender_id" BIGINT NOT NULL,
    "timestamp" BIGINT NOT NULL,
    "content" UUID NOT NULL,
    "topic" UUID NOT NULL,
    "channel" UUID NOT NULL,
    "read" BOOLEAN NOT NULL DEFAULT FALSE,
    "starred" BOOLEAN NOT NULL DEFAULT FALSE,
    "collapsed" BOOLEAN NOT NULL DEFAULT FALSE,
    "hide_link_previews" BOOLEAN NOT NULL DEFAULT FALSE,
    "mentioned" BOOLEAN NOT NULL DEFAULT FALSE,
    "stream_wildcard_mentioned" BOOLEAN NOT NULL DEFAULT FALSE,
    "topic_wildcard_mentioned" BOOLEAN NOT NULL DEFAULT FALSE,
    "has_alert_word" BOOLEAN NOT NULL DEFAULT FALSE,
    "historical" BOOLEAN NOT NULL DEFAULT FALSE,
    "is_me_message" BOOLEAN NOT NULL DEFAULT FALSE,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT "bridge_zulip_v5_messages_external_account_uuid_fkey"
        FOREIGN KEY ("external_account_uuid")
        REFERENCES "m_external_accounts_v2" ("uuid") ON DELETE CASCADE,
    CONSTRAINT "bridge_zulip_v5_messages_content_fkey"
        FOREIGN KEY ("content")
        REFERENCES "bridge_zulip_v5_message_contents" ("uuid") ON DELETE CASCADE,
    CONSTRAINT "bridge_zulip_v5_messages_topic_fkey"
        FOREIGN KEY ("topic")
        REFERENCES "bridge_zulip_v5_topics" ("uuid") ON DELETE CASCADE,
    CONSTRAINT "bridge_zulip_v5_messages_channel_fkey"
        FOREIGN KEY ("channel")
        REFERENCES "bridge_zulip_v5_channels" ("uuid") ON DELETE CASCADE,
    CONSTRAINT "bridge_zulip_v5_messages_account_message_key"
        UNIQUE ("external_account_uuid", "message_id")
);

CREATE INDEX "bridge_zulip_v5_messages_content_idx"
    ON "bridge_zulip_v5_messages" ("content");
CREATE INDEX "bridge_zulip_v5_messages_topic_idx"
    ON "bridge_zulip_v5_messages" ("topic");
CREATE INDEX "bridge_zulip_v5_messages_channel_idx"
    ON "bridge_zulip_v5_messages" ("channel");

CREATE TABLE "bridge_zulip_v5_files" (
    "uuid" UUID PRIMARY KEY,
    "zulip_file_url" VARCHAR(2048) NOT NULL,
    "workspace_file_url" VARCHAR(2048) NOT NULL,
    "zulip_author_id" BIGINT NOT NULL,
    "workspace_author_uuid" UUID NOT NULL,
    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0189-remove-external-provider-processing-6e254a.py"]

    @property
    def migration_id(self):
        return "1f232b84-9d10-4d28-9a24-f4b3d9b6feca"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        tables = (
            "bridge_zulip_v5_messages",
            "bridge_zulip_v5_files",
            "bridge_zulip_v5_topics",
            "bridge_zulip_v5_channels",
            "bridge_zulip_v5_message_contents",
        )
        for table in tables:
            self._delete_table_if_exists(session, table)


migration_step = MigrationStep()
