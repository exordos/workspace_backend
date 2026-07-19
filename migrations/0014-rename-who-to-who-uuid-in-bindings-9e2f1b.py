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
        self._depends = ["0013-rename-stream-to-stream-uuid-in-bindings-4c1d9f.py"]

    @property
    def migration_id(self):
        return "9e2f1b3c-4a7d-4e8f-b2d6-c1e4f7a9b3d2"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_stream_bindings"
                RENAME COLUMN "who" TO "who_uuid";
            """,
            """
            ALTER INDEX IF EXISTS "m_workspace_stream_bindings_who_idx"
                RENAME TO "m_workspace_stream_bindings_who_uuid_idx";
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            ALTER INDEX IF EXISTS "m_workspace_stream_bindings_who_uuid_idx"
                RENAME TO "m_workspace_stream_bindings_who_idx";
            """,
            """
            ALTER TABLE "m_workspace_stream_bindings"
                RENAME COLUMN "who_uuid" TO "who";
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
