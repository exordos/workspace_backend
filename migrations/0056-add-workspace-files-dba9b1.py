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
        self._depends = ["0055-add-stream-topic-last-message-f89f6f.py"]

    @property
    def migration_id(self):
        return "dba9b149-50f9-41b5-ae83-747ecfa2f855"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_files" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "name" VARCHAR(255) NOT NULL,
                "description" VARCHAR(255) NOT NULL DEFAULT '',
                "user_uuid" UUID NOT NULL,
                "stream_uuid" UUID NOT NULL,
                "content_type" VARCHAR(255) NOT NULL,
                "size_bytes" BIGINT NOT NULL,
                "hash" VARCHAR(255) NOT NULL,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_files_stream_uuid_fkey"
                    FOREIGN KEY ("stream_uuid")
                    REFERENCES "m_workspace_streams" ("uuid")
                    ON DELETE CASCADE
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_files_project_id_idx"
                ON "m_workspace_files" ("project_id");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_files_user_uuid_idx"
                ON "m_workspace_files" ("user_uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_files_stream_uuid_idx"
                ON "m_workspace_files" ("stream_uuid");
            """,
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_file_accesses" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "file_uuid" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_file_accesses_file_uuid_fkey"
                    FOREIGN KEY ("file_uuid")
                    REFERENCES "m_workspace_files" ("uuid")
                    ON DELETE CASCADE
            );
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_workspace_file_accesses_file_user_project_idx"
                ON "m_workspace_file_accesses"
                    ("project_id", "file_uuid", "user_uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_file_accesses_project_id_idx"
                ON "m_workspace_file_accesses" ("project_id");
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_file_accesses_user_uuid_idx"
                ON "m_workspace_file_accesses" ("user_uuid");
            """,
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_file_accesses_file_uuid_idx"
                ON "m_workspace_file_accesses" ("file_uuid");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        tables = [
            "m_workspace_file_accesses",
            "m_workspace_files",
        ]

        for table in tables:
            self._delete_table_if_exists(session, table)

migration_step = MigrationStep()
