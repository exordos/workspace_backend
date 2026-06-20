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
        self._depends = ["0009-init-m-workspace-stream-bindings-5d91a4.py"]

    @property
    def migration_id(self):
        return "c3e9b8d1-1ae1-42e5-88a7-a9b41d94c8a5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_bindings"
                ADD COLUMN IF NOT EXISTS "status" VARCHAR(16) NOT NULL
                    DEFAULT 'new'
                    CHECK ("status" IN ('new', 'in_progress', 'active'));
            """
        )

    def downgrade(self, session):
        session.execute(
            (
                'ALTER TABLE "m_workspace_stream_bindings" '
                'DROP COLUMN IF EXISTS "status";'
            )
        )


migration_step = MigrationStep()
