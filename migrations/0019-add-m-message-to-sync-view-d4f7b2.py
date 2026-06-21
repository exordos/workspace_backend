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
        self._depends = ["0017-init-m-workspace-user-messages-b9d2f1.py"]

    @property
    def migration_id(self):
        return "d4f7b2c1-8e3a-4f9d-b6e2-5a1c7f0d3e84"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_message_to_sync" AS
            SELECT
                gen_random_uuid()   AS uuid,
                msg.uuid            AS message,
                us.uuid             AS user_stream,
                um.uuid             AS user_message
            FROM "m_workspace_messages" AS msg
            JOIN "m_workspace_user_streams" AS us
                ON us.source_stream_uuid = msg.stream_uuid
            LEFT JOIN "m_workspace_user_messages" AS um
                ON um.source_message_uuid = msg.uuid
                AND um.user_uuid = us.user_uuid
            WHERE um.last_synced_at IS NULL
               OR um.last_synced_at <> msg.updated_at;
            """
        )

    def downgrade(self, session):
        self._delete_view_if_exists(session, "m_message_to_sync")


migration_step = MigrationStep()
