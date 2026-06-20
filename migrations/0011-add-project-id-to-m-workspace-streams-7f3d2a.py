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
        self._depends = ["0010-add-m-workspace-stream-binding-status-c3e9b8.py"]

    @property
    def migration_id(self):
        return "7f3d2a1e-5c4b-4e8f-9d6a-b2e7f1c3d4a5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_streams"
                ADD COLUMN IF NOT EXISTS "project_id" UUID NOT NULL
                    DEFAULT '00000000-0000-0000-0000-000000000000';
            """,
            """
            ALTER TABLE "m_workspace_streams"
                ALTER COLUMN "project_id" DROP DEFAULT;
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_streams_project_id_idx"
                ON "m_workspace_streams" ("project_id");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            DROP INDEX IF EXISTS "m_workspace_streams_project_id_idx";
            """,
            """
            ALTER TABLE "m_workspace_streams"
                DROP COLUMN IF EXISTS "project_id";
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
