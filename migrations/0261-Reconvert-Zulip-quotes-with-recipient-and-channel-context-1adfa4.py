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
            "0260-Rebuild-bounded-reaction-users-for-existing-messages-be705e.py"
        ]

    @property
    def migration_id(self):
        return "1adfa4dc-28ab-4f8a-9010-8ce9a857a8eb"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Reuse the existing bounded worker and its source-edit guards. Only
        # contextual quote headers need another conversion, not all history.
        session.execute(r"""
            WITH pending AS (
                UPDATE bridge_zulip_v5_message_contents
                SET conversion_version = 0
                WHERE conversion_version > 0
                  AND zulip_content ~ '(^|\n)@_?\*\*[^\n]+\*\* \[[^\n]+\]\([^\n]+\) (to|in) '
                RETURNING uuid
            )
            INSERT INTO bridge_zulip_v5_tasks(uuid, queue, priority, content)
            SELECT gen_random_uuid(), 'background', 150000,
                '{"kind":"convert_message_content","batch_size":100}'::jsonb
            WHERE EXISTS (SELECT 1 FROM pending)
        """)

    def downgrade(self, session):
        # Restoring the previous parser does not require corrupting saved text.
        pass


migration_step = MigrationStep()
