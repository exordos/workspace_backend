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
        self._depends = ["0020-add-stream-topics-and-topic-uuid-a3f9c2.py"]

    @property
    def migration_id(self):
        return "c5e2a1f3-9d4b-4e7c-b8f1-2a6d0e3c9b15"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_user_messages"
                DROP CONSTRAINT IF EXISTS "m_workspace_user_messages_user_stream_uuid_fkey";
            """,
            """
            DROP INDEX IF EXISTS "m_workspace_user_messages_user_stream_uuid_idx";
            """,
            """
            ALTER TABLE "m_workspace_user_messages"
                DROP COLUMN IF EXISTS "user_stream_uuid";
            """,
        ]
        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
