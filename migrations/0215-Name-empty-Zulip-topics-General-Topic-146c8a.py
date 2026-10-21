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
        self._depends = ["0214-Expand-descriptions-to-10000-characters-fa3fa9.py"]

    @property
    def migration_id(self):
        return "146c8a61-9563-45aa-948e-008aa219b688"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            UPDATE bridge_zulip_v5_topics
            SET name = 'General Topic', updated_at = NOW()
            WHERE name = '';
            """
        )

    def downgrade(self, session):
        # Keep corrected names; originally named topics cannot be distinguished.
        pass


migration_step = MigrationStep()
