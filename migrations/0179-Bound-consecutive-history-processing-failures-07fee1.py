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
        self._depends = ["0178-Track-active-history-attachment-requests-49c4f3.py"]

    @property
    def migration_id(self):
        return "07fee1d8-160c-4190-b2e7-5f5819f40016"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE m_external_history_imports_v1
                ADD COLUMN consecutive_errors integer NOT NULL DEFAULT 0
                CHECK (consecutive_errors >= 0);
        """)

    def downgrade(self, session):
        session.execute("""
            ALTER TABLE m_external_history_imports_v1 DROP COLUMN consecutive_errors;
        """)


migration_step = MigrationStep()
