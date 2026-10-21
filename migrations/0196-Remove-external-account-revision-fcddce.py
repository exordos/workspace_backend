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
        self._depends = ["0195-Add-Zulip-task-not-before-timestamp-444eb8.py"]

    @property
    def migration_id(self):
        return "fcddce7c-b81a-446b-9411-1585a5731110"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute('ALTER TABLE "m_external_accounts_v2" DROP COLUMN "revision";')

    def downgrade(self, session):
        session.execute(
            'ALTER TABLE "m_external_accounts_v2" '
            'ADD COLUMN "revision" INTEGER NOT NULL DEFAULT 1 CHECK ("revision" >= 1);'
        )


migration_step = MigrationStep()
