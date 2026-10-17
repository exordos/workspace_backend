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
            "0199-Scope-external-accounts-and-Zulip-tasks-to-projects-4fb70b.py"
        ]

    @property
    def migration_id(self):
        return "e707eced-6cb7-4cb0-bbd4-46e46a73c7d6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_channels
                ADD COLUMN external_account_uuid UUID NOT NULL
                    REFERENCES m_external_accounts_v2 (uuid) ON DELETE CASCADE,
                ADD COLUMN stream_uuid UUID NOT NULL
                    REFERENCES messenger_streams (uuid) ON DELETE CASCADE;
            CREATE INDEX bridge_zulip_v5_channels_account_idx
                ON bridge_zulip_v5_channels (external_account_uuid);
            CREATE INDEX bridge_zulip_v5_channels_stream_uuid_idx
                ON bridge_zulip_v5_channels (stream_uuid);
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_channels
                DROP COLUMN stream_uuid,
                DROP COLUMN external_account_uuid;
            """
        )


migration_step = MigrationStep()
