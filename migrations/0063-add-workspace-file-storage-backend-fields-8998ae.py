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
        self._depends = ["0062-clean-invalid-workspace-event-payload-project-ids-82eab5.py"]

    @property
    def migration_id(self):
        return "8998ae2f-240a-4585-b6a6-72fd68ad042c"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_files"
                ADD COLUMN "storage_type" VARCHAR(16),
                ADD COLUMN "storage_id" VARCHAR(255),
                ADD COLUMN "storage_object_id" VARCHAR(255);

            UPDATE "m_workspace_files"
            SET "storage_type" = 'file',
                "storage_id" = '',
                "storage_object_id" = SUBSTRING("uuid"::text, 1, 2)
                    || '/' || "uuid"::text;

            ALTER TABLE "m_workspace_files"
                ALTER COLUMN "storage_type" SET DEFAULT 'file',
                ALTER COLUMN "storage_type" SET NOT NULL,
                ALTER COLUMN "storage_id" SET DEFAULT '',
                ALTER COLUMN "storage_id" SET NOT NULL,
                ALTER COLUMN "storage_object_id" SET NOT NULL,
                ADD CONSTRAINT "m_workspace_files_storage_type_check"
                    CHECK ("storage_type" IN ('file', 's3'));
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_files"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_files_storage_type_check",
                DROP COLUMN IF EXISTS "storage_object_id",
                DROP COLUMN IF EXISTS "storage_id",
                DROP COLUMN IF EXISTS "storage_type";
            """
        )


migration_step = MigrationStep()
