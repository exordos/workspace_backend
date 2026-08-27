# Copyright 2026 Genesis Corporation.
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
            "0147-repair-read-state-maintenance-indexes-804f77.py",
            "0134-allow-disabled-topic-summary-reasoning-b9d394.py",
        ]

    @property
    def migration_id(self):
        return "4588d689-bb04-4599-8ab4-ade40e386548"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        return None

    def downgrade(self, session):
        return None


migration_step = MigrationStep()
