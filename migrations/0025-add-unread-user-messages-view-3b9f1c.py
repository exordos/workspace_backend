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
        self._depends = ["0024-init-m-workspace-users-90c43d.py"]

    @property
    def migration_id(self):
        return "3b9f1c47-a2e5-4d8b-b1f3-6c0e9d2a7f84"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_unread_user_messages" AS
            SELECT
                um.stream_uuid AS uuid,
                um.user_uuid,
                um.project_id,
                COUNT(*) AS unread_count
            FROM "m_workspace_user_messages" AS um
            WHERE um.read = false
            GROUP BY um.user_uuid, um.stream_uuid, um.project_id;
            """
        )

    def downgrade(self, session):
        self._delete_view_if_exists(session, "m_unread_user_messages")


migration_step = MigrationStep()
