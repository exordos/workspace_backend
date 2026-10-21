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
        self._depends = ["0218-Add-Zulip-project-user-reconciliation-view-e80455.py"]

    @property
    def migration_id(self):
        return "004ba41d-d02d-4495-9232-3cee1d809112"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE messenger_messages
                DROP CONSTRAINT messenger_messages_source_name_check;
            ALTER TABLE messenger_messages
                ADD CONSTRAINT messenger_messages_source_name_check
                CHECK (source_name IN ('native', 'zulip'));
            CREATE INDEX bridge_zulip_v5_messages_identity_idx
                ON bridge_zulip_v5_messages
                    (project_id, message_uuid, updated_at DESC, uuid);

            CREATE VIEW bridge_zulip_v5_message_states AS
            WITH zulip_messages AS (
                SELECT DISTINCT ON (message.project_id, message.message_uuid)
                       message.project_id,
                       messenger_uuid_v5(
                           message.project_id, message.message_uuid::text
                       ) AS message_uuid,
                       message.uuid AS source_uuid
                FROM bridge_zulip_v5_messages AS message
                JOIN m_external_accounts_v2 AS account
                  ON account.uuid = message.external_account_uuid
                 AND account.project_id = message.project_id
                WHERE account.provider = 'zulip'
                ORDER BY message.project_id, message.message_uuid,
                         message.updated_at DESC, message.uuid
            )
            SELECT
                COALESCE(zulip.project_id, workspace.project_id) AS project_id,
                COALESCE(zulip.message_uuid, workspace.uuid) AS message_uuid,
                zulip.source_uuid,
                CASE
                    WHEN zulip.source_uuid IS NULL THEN 'delete'
                    ELSE 'create'
                END AS action
            FROM zulip_messages AS zulip
            FULL JOIN messenger_messages AS workspace
              ON workspace.project_id = zulip.project_id
             AND workspace.uuid = zulip.message_uuid
            WHERE workspace.uuid IS NULL
               OR (
                   workspace.source_name = 'zulip'
                   AND (
                       zulip.source_uuid IS NULL
                       OR workspace.deleted_at IS NOT NULL
                   )
               );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP VIEW bridge_zulip_v5_message_states;
            DROP INDEX bridge_zulip_v5_messages_identity_idx;
            UPDATE messenger_messages
                SET source_name = 'native', source = '{"kind":"native"}'::jsonb
                WHERE source_name = 'zulip';
            ALTER TABLE messenger_messages
                DROP CONSTRAINT messenger_messages_source_name_check;
            ALTER TABLE messenger_messages
                ADD CONSTRAINT messenger_messages_source_name_check
                CHECK (source_name = 'native');
            """
        )


migration_step = MigrationStep()
