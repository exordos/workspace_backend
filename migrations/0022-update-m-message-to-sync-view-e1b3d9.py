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
        self._depends = ["0021-drop-user-stream-uuid-from-user-messages-c5e2a1.py"]

    @property
    def migration_id(self):
        return "e1b3d9f2-7a4c-4e8b-a3d1-9c5f2b0e6a47"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("DROP VIEW IF EXISTS \"m_message_to_sync\";")
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_message_to_sync" AS
            SELECT
                gen_random_uuid()   AS uuid,
                msg.uuid            AS message,
                msg.stream_uuid     AS stream,
                us.user_uuid        AS user_uuid,
                um.uuid             AS user_message_uuid
            FROM "m_workspace_messages" AS msg
            JOIN "m_workspace_user_streams" AS us
                ON us.uuid = msg.stream_uuid
            LEFT JOIN "m_workspace_user_messages" AS um
                ON um.uuid = msg.uuid
                AND um.user_uuid = us.user_uuid
            WHERE um.last_synced_at IS NULL
               OR um.last_synced_at <> msg.updated_at;
            """
        )

    def downgrade(self, session):
        session.execute("DROP VIEW IF EXISTS \"m_message_to_sync\";")
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
                ON us.uuid = msg.stream_uuid
            LEFT JOIN "m_workspace_user_messages" AS um
                ON um.uuid = msg.uuid
                AND um.user_uuid = us.user_uuid
            WHERE um.last_synced_at IS NULL
               OR um.last_synced_at <> msg.updated_at;
            """
        )


migration_step = MigrationStep()
