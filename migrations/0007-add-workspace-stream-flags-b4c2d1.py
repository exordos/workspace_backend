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
        self._depends = ["0006-init-messenger-api-models-a2f110.py"]

    @property
    def migration_id(self):
        return "b4c2d1a7-4e21-4da2-98b5-0d682cc3c98c"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "workspace_streams"
                ADD COLUMN IF NOT EXISTS
                    "invite_only" BOOLEAN NOT NULL DEFAULT FALSE;
            """,
            """
            ALTER TABLE "workspace_streams"
                ADD COLUMN IF NOT EXISTS
                    "announce" BOOLEAN NOT NULL DEFAULT FALSE;
            """,
            """
            ALTER TABLE "m_workspace_streams"
                ADD COLUMN IF NOT EXISTS
                    "invite_only" BOOLEAN NOT NULL DEFAULT FALSE;
            """,
            """
            ALTER TABLE "m_workspace_streams"
                ADD COLUMN IF NOT EXISTS
                    "announce" BOOLEAN NOT NULL DEFAULT FALSE;
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            'ALTER TABLE "m_workspace_streams" DROP COLUMN IF EXISTS "announce";',
            (
                'ALTER TABLE "m_workspace_streams" '
                'DROP COLUMN IF EXISTS "invite_only";'
            ),
            'ALTER TABLE "workspace_streams" DROP COLUMN IF EXISTS "announce";',
            (
                'ALTER TABLE "workspace_streams" '
                'DROP COLUMN IF EXISTS "invite_only";'
            ),
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
