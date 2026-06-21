#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
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
        self._depends = ["0014-rename-who-to-who-uuid-in-bindings-9e2f1b.py"]

    @property
    def migration_id(self):
        return "6f4f3b22-2ae4-4f83-8182-2c0ab2ff7da6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_user_streams" (
                "uuid" UUID PRIMARY KEY,
                "source_stream_uuid" UUID NOT NULL,
                "name" VARCHAR(255) NOT NULL,
                "description" VARCHAR(255) NULL,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "last_synced_at" TIMESTAMP(6) NOT NULL,
                "source_name" VARCHAR(64) NOT NULL,
                "source" JSONB NOT NULL,
                "invite_only" BOOLEAN NOT NULL DEFAULT FALSE,
                "announce" BOOLEAN NOT NULL DEFAULT FALSE,
                "private" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_user_streams_source_stream_uuid_fkey"
                    FOREIGN KEY ("source_stream_uuid") REFERENCES "m_workspace_streams" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_user_streams_stream_user_unique" UNIQUE ("source_stream_uuid", "user_uuid")
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_streams_project_id_idx"
                ON "m_workspace_user_streams" ("project_id");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_streams_user_uuid_idx"
                ON "m_workspace_user_streams" ("user_uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_streams_source_stream_uuid_idx"
                ON "m_workspace_user_streams" ("source_stream_uuid");
            """,
            """
            CREATE OR REPLACE VIEW "m_stream_binding_to_sync" AS
            SELECT
                b.uuid AS uuid,
                s.uuid AS stream,
                us.uuid AS user_stream,
                b.uuid AS binding
            FROM "m_workspace_stream_bindings" AS b
            LEFT JOIN "m_workspace_streams" AS s
                ON s.uuid = b.stream_uuid
            LEFT JOIN "m_workspace_user_streams" AS us
                ON us.source_stream_uuid = b.stream_uuid
                AND us.user_uuid = b.user_uuid
            WHERE us.last_synced_at IS NULL OR us.last_synced_at <> s.updated_at;
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        self._delete_view_if_exists(session, "m_stream_binding_to_sync")
        self._delete_table_if_exists(session, "m_workspace_user_streams")


migration_step = MigrationStep()
