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
        self._depends = ["0028-add-m-workspace-user-messages-view-5f3a9b.py"]

    @property
    def migration_id(self):
        return "3c7d1e4a-2f8b-4a9c-b5d6-1e0f7a3c9b42"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_unread_user_messages" AS
            SELECT
                m.stream_uuid                   AS uuid,
                f.user_uuid,
                f.project_id,
                COUNT(*)                        AS unread_count
            FROM "m_workspace_user_message_flags" AS f
            JOIN "m_workspace_messages" AS m
                ON  m.uuid       = f.uuid
                AND m.project_id = f.project_id
            WHERE f.read = false
            GROUP BY m.stream_uuid, f.user_uuid, f.project_id;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_unread_user_messages" AS
            SELECT
                f.uuid AS uuid,
                f.user_uuid,
                f.project_id,
                COUNT(*) AS unread_count
            FROM "m_workspace_user_message_flags" AS f
            WHERE f.read = false
            GROUP BY f.uuid, f.user_uuid, f.project_id;
            """
        )


migration_step = MigrationStep()
