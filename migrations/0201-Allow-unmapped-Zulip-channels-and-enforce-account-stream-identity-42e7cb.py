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
            "0200-Add-account-and-workspace-stream-references-to-Zulip-channels-e707ec.py"
        ]

    @property
    def migration_id(self):
        return "42e7cbdc-e9f7-4d88-a33e-d97e62a5d574"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_channels
                ALTER COLUMN stream_uuid DROP NOT NULL,
                ADD CONSTRAINT bridge_zulip_v5_channels_account_stream_key
                    UNIQUE (external_account_uuid, stream_id);
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_channels
                ALTER COLUMN stream_uuid SET NOT NULL,
                DROP CONSTRAINT bridge_zulip_v5_channels_account_stream_key;
            """
        )


migration_step = MigrationStep()
