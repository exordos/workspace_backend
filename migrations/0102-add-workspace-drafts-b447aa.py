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
        self._depends = ["0101-add-file-workspace-events-0a9df4.py"]

    @property
    def migration_id(self):
        return "b447aa02-8eb0-4bee-87a1-c865757d2ccd"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_drafts" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "stream_uuid" UUID NOT NULL,
                "topic_uuid" UUID NOT NULL,
                "payload" JSONB NOT NULL,
                "revision" INTEGER NOT NULL DEFAULT 1,
                "created_at" TIMESTAMP(6) WITH TIME ZONE
                    NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) WITH TIME ZONE
                    NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_drafts_revision_check"
                    CHECK ("revision" >= 1),
                CONSTRAINT "m_workspace_drafts_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_drafts_stream_uuid_fkey"
                    FOREIGN KEY ("stream_uuid")
                    REFERENCES "m_workspace_streams" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_drafts_topic_uuid_fkey"
                    FOREIGN KEY ("topic_uuid")
                    REFERENCES "m_workspace_stream_topics" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_drafts_binding_fkey"
                    FOREIGN KEY ("project_id", "stream_uuid", "user_uuid")
                    REFERENCES "m_workspace_stream_bindings"
                        ("project_id", "stream_uuid", "user_uuid")
                    ON DELETE CASCADE
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_drafts_owner_updated_uuid_idx"
            ON "m_workspace_drafts"
                ("project_id", "user_uuid", "updated_at", "uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_drafts_owner_stream_updated_uuid_idx"
            ON "m_workspace_drafts"
                ("project_id", "user_uuid", "stream_uuid", "updated_at", "uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_drafts_owner_topic_updated_uuid_idx"
            ON "m_workspace_drafts"
                ("project_id", "user_uuid", "topic_uuid", "updated_at", "uuid");
            """,
        ]
        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "m_workspace_drafts")


migration_step = MigrationStep()
