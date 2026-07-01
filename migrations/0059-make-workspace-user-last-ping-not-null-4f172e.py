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
        self._depends = ["0058-add-message-reactions-to-user-message-view-fa37f9.py"]

    @property
    def migration_id(self):
        return "4f172e5a-9f69-4c3d-92e9-8b0a34f9f819"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            UPDATE "m_workspace_users"
            SET "last_ping_at" = "created_at"
            WHERE "last_ping_at" IS NULL;
            """
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                ALTER COLUMN "last_ping_at" SET DEFAULT CURRENT_TIMESTAMP,
                ALTER COLUMN "last_ping_at" SET NOT NULL;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                ALTER COLUMN "last_ping_at" DROP DEFAULT,
                ALTER COLUMN "last_ping_at" DROP NOT NULL;
            """
        )


migration_step = MigrationStep()
