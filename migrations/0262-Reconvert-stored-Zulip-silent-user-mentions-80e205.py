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
            "0261-Reconvert-Zulip-quotes-with-recipient-and-channel-context-1adfa4.py"
        ]

    @property
    def migration_id(self):
        return "80e205d3-569d-41e5-be87-ff34d0d08aef"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Revisit all mention-bearing messages: silence can come from quote
        # context, and earlier conversions can hide references inside snapshots.
        session.execute(r"""
            WITH pending AS (
                UPDATE bridge_zulip_v5_message_contents
                SET conversion_version = 0
                WHERE conversion_version > 0
                  AND zulip_content ~ '@_?\*'
                RETURNING uuid
            )
            INSERT INTO bridge_zulip_v5_tasks(uuid, queue, priority, content)
            SELECT gen_random_uuid(), 'background', 150000,
                '{"kind":"convert_message_content","batch_size":100}'::jsonb
            WHERE EXISTS (SELECT 1 FROM pending)
        """)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
