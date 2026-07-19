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
        self._depends = ["0030-add-m-workspace-user-topics-view-8a2f4e.py"]

    @property
    def migration_id(self):
        return "1a3f7c2e-4b8d-4e9a-c5f0-6d2b3e1a7f84"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_folders"
                DROP COLUMN IF EXISTS "unread_messages",
                ADD COLUMN IF NOT EXISTS "unread_count" INTEGER NOT NULL DEFAULT 0;
            """
        )
        session.execute(
            """
            ALTER TABLE "m_folder_items"
                ADD COLUMN IF NOT EXISTS "unread_count" INTEGER NOT NULL DEFAULT 0;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_folder_items"
                DROP COLUMN IF EXISTS "unread_count";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_folders"
                DROP COLUMN IF EXISTS "unread_count",
                ADD COLUMN IF NOT EXISTS "unread_messages" JSONB[] NOT NULL DEFAULT '{}';
            """
        )


migration_step = MigrationStep()
