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
        self._depends = ["0043-require-workspace-message-topic-8e4d6a.py"]

    @property
    def migration_id(self):
        return "4d8b2a6e-9c1f-4b73-a25d-8e6f1c3b7a90"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            DROP INDEX IF EXISTS "m_workspace_stream_bindings_unique_idx";
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_workspace_stream_bindings_unique_idx"
                ON "m_workspace_stream_bindings"
                ("project_id", "stream_uuid", "user_uuid");
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            DROP INDEX IF EXISTS "m_workspace_stream_bindings_unique_idx";
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_workspace_stream_bindings_unique_idx"
                ON "m_workspace_stream_bindings"
                ("project_id", "stream_uuid", "user_uuid", "who_uuid");
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
