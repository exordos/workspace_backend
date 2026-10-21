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
        self._depends = [
            "0208-Reserve-descending-priorities-for-history-import-tasks-bc5850.py"
        ]

    @property
    def migration_id(self):
        return "6bdbf2e3-7c4b-4efe-b58b-50ad5c11374a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_channels
                ALTER COLUMN stream_id DROP NOT NULL,
                ALTER COLUMN date_created DROP NOT NULL,
                ADD COLUMN channel_type VARCHAR(16) NOT NULL DEFAULT 'channel'
                    CHECK (channel_type IN ('private', 'group', 'channel')),
                ADD COLUMN direct_user_uuid UUID
                    REFERENCES m_external_accounts_v2 (uuid) ON DELETE CASCADE,
                ADD CONSTRAINT bridge_zulip_v5_channels_direct_user_check
                    CHECK ((channel_type = 'private') = (direct_user_uuid IS NOT NULL)),
                ADD CONSTRAINT bridge_zulip_v5_channels_stream_id_check
                    CHECK ((channel_type = 'channel') = (stream_id IS NOT NULL)),
                ADD CONSTRAINT bridge_zulip_v5_channels_account_stream_uuid_key
                    UNIQUE (external_account_uuid, stream_uuid);
            ALTER TABLE bridge_zulip_v5_topics ALTER COLUMN stream_id DROP NOT NULL;
            CREATE INDEX bridge_zulip_v5_channels_direct_user_uuid_idx
                ON bridge_zulip_v5_channels (direct_user_uuid);
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_channels
                ALTER COLUMN stream_id SET NOT NULL,
                ALTER COLUMN date_created SET NOT NULL,
                DROP CONSTRAINT bridge_zulip_v5_channels_account_stream_uuid_key,
                DROP CONSTRAINT bridge_zulip_v5_channels_stream_id_check,
                DROP CONSTRAINT bridge_zulip_v5_channels_direct_user_check,
                DROP COLUMN direct_user_uuid,
                DROP COLUMN channel_type;
            ALTER TABLE bridge_zulip_v5_topics ALTER COLUMN stream_id SET NOT NULL;
            """
        )


migration_step = MigrationStep()
