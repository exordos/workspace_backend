#    Copyright 2025 Genesis Corporation.
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
        self._depends = ["0003-add-chat-type-to-folder-items-a1b2c3.py"]

    @property
    def migration_id(self):
        return "9d8e7f64-4a5b-4fd2-b1d8-a0e4d8f63490"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "workspace_streams" (
                "uuid" UUID PRIMARY KEY,
                "name" VARCHAR(255) NOT NULL,
                "description" VARCHAR(255) NULL,
                "source_name" VARCHAR(64) NOT NULL,
                "source" JSONB NOT NULL,
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW()
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "workspace_streams_source_name_idx"
                ON "workspace_streams" ("source_name");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        tables = ["workspace_streams"]

        for table in tables:
            self._delete_table_if_exists(session, table)


migration_step = MigrationStep()
