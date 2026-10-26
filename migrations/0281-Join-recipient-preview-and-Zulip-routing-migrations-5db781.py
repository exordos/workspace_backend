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
            "0280-Index-recipient-scoped-canonical-message-previews-6025ad.py",
            "0280-Restore-direct-Zulip-message-recipients-d1cf4e.py",
        ]

    @property
    def migration_id(self):
        return "5db7815b-7d7d-40e0-b447-16e16b365c52"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        return None

    def downgrade(self, session):
        return None


migration_step = MigrationStep()
