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
        self._depends = ["0002-init-services-catalog-7c1a2b.py"]

    @property
    def migration_id(self):
        return "1b624427-a8ad-4e06-a2f8-2576557d105a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "folder_items"
                ADD COLUMN IF NOT EXISTS "chat_type" VARCHAR(16) NULL
                    CHECK (chat_type IN ('stream', 'group', 'private'));
            """,
            """
            UPDATE "folder_items" SET "chat_type" = 'private' WHERE "chat_type" IS NULL;
            """,
            """
            ALTER TABLE "folder_items"
                ALTER COLUMN "chat_type" SET NOT NULL;
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            ALTER TABLE "folder_items"
                DROP COLUMN IF EXISTS "chat_type";
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
