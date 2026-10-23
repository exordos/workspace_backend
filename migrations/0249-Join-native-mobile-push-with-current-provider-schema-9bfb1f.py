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
        self._depends = ["0189-Add-encrypted-mobile-push-outbox-140dd5.py", "0248-Snapshot-outgoing-Zulip-deletion-tasks-before-tombstone-cleanup-e378c7.py"]

    @property
    def migration_id(self):
        return "9bfb1f25-d487-4238-89d5-8d29b736047f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        pass

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
