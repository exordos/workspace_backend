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
            "0177-Page-restored-history-file-access-and-cascade-job-hashes-e7563e.py"
        ]

    @property
    def migration_id(self):
        return "49c4f3ef-9212-4f2c-9f73-b023d65985ee"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE m_external_history_imports_v1
                ADD COLUMN active_file_uuids uuid[] NOT NULL DEFAULT '{}'::uuid[];
        """)

    def downgrade(self, session):
        session.execute("""
            ALTER TABLE m_external_history_imports_v1 DROP COLUMN active_file_uuids;
        """)


migration_step = MigrationStep()
