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
            "0201-Allow-unmapped-Zulip-channels-and-enforce-account-stream-identity-42e7cb.py"
        ]

    @property
    def migration_id(self):
        return "e4f31c4e-d7bc-4f0b-a653-3a227ec38fc6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_channels
                DROP CONSTRAINT bridge_zulip_v5_channels_stream_uuid_fkey;
            """
        )
        channels = session.execute(
            """
            SELECT channel.uuid, channel.stream_id,
                account.settings->>'server_url' AS server_url
            FROM bridge_zulip_v5_channels AS channel
            JOIN m_external_accounts_v2 AS account
                ON account.uuid = channel.external_account_uuid
            """
        ).fetchall()
        for channel in channels:
            hostname = urllib.parse.urlsplit(channel["server_url"]).hostname
            namespace = uuid.uuid5(uuid.NAMESPACE_DNS, hostname)
            stream_uuid = uuid.uuid5(namespace, str(channel["stream_id"]))
            session.execute(
                "UPDATE bridge_zulip_v5_channels SET stream_uuid=%s WHERE uuid=%s",
                (stream_uuid, channel["uuid"]),
            )
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_channels
                ALTER COLUMN stream_uuid SET NOT NULL;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_channels
                ALTER COLUMN stream_uuid DROP NOT NULL,
                ADD CONSTRAINT bridge_zulip_v5_channels_stream_uuid_fkey
                    FOREIGN KEY (stream_uuid)
                    REFERENCES messenger_streams (uuid) ON DELETE CASCADE;
            """
        )


migration_step = MigrationStep()
