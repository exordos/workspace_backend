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

import urllib.parse
import uuid

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0202-Generate-required-Zulip-channel-stream-UUIDs-from-server-identity-e4f31c.py"
        ]

    @property
    def migration_id(self):
        return "4118dc2f-41ad-4e61-9e19-e1f8fc9afd48"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_messages ADD COLUMN message_uuid UUID;
            """
        )
        messages = session.execute(
            """
            SELECT message.uuid, message.message_id,
                account.settings->>'server_url' AS server_url
            FROM bridge_zulip_v5_messages AS message
            JOIN m_external_accounts_v2 AS account
                ON account.uuid = message.external_account_uuid
            """
        ).fetchall()
        for message in messages:
            hostname = urllib.parse.urlsplit(message["server_url"]).hostname
            namespace = uuid.uuid5(uuid.NAMESPACE_DNS, hostname)
            message_uuid = uuid.uuid5(namespace, str(message["message_id"]))
            session.execute(
                "UPDATE bridge_zulip_v5_messages SET message_uuid=%s WHERE uuid=%s",
                (message_uuid, message["uuid"]),
            )
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_messages
                ALTER COLUMN message_uuid SET NOT NULL;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_messages DROP COLUMN message_uuid;
            """
        )


migration_step = MigrationStep()
