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
        self._depends = ["0203-Add-Workspace-message-UUID-to-Zulip-messages-4118dc.py"]

    @property
    def migration_id(self):
        return "c0781408-5d2f-44ca-b15e-841ae6730d7a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_messages ADD COLUMN project_id UUID;
            UPDATE bridge_zulip_v5_messages AS message
            SET project_id = account.project_id
            FROM m_external_accounts_v2 AS account
            WHERE message.external_account_uuid = account.uuid;
            ALTER TABLE bridge_zulip_v5_messages
                ALTER COLUMN project_id SET NOT NULL,
                DROP CONSTRAINT bridge_zulip_v5_messages_external_account_uuid_fkey,
                ADD CONSTRAINT bridge_zulip_v5_messages_account_project_fkey
                    FOREIGN KEY (external_account_uuid, project_id)
                    REFERENCES m_external_accounts_v2 (uuid, project_id)
                    ON DELETE CASCADE;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_messages
                DROP CONSTRAINT bridge_zulip_v5_messages_account_project_fkey,
                DROP COLUMN project_id,
                ADD CONSTRAINT bridge_zulip_v5_messages_external_account_uuid_fkey
                    FOREIGN KEY (external_account_uuid)
                    REFERENCES m_external_accounts_v2 (uuid) ON DELETE CASCADE;
            """
        )


migration_step = MigrationStep()
