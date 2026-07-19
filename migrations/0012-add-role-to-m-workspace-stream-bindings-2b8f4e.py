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
        self._depends = ["0011-add-project-id-to-m-workspace-streams-7f3d2a.py"]

    @property
    def migration_id(self):
        return "2b8f4e3a-7d1c-4f9e-a2b5-c6d8e1f4a7b3"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_bindings"
                ADD COLUMN IF NOT EXISTS "role" VARCHAR(16) NOT NULL
                    DEFAULT 'member'
                    CHECK ("role" IN ('guest', 'member', 'moderator', 'administrator', 'owner'));
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_bindings"
                DROP COLUMN IF EXISTS "role";
            """
        )


migration_step = MigrationStep()
