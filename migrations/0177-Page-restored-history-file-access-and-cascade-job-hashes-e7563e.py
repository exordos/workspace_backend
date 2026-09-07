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
            "0176-Guard-history-tombstones-for-incomplete-provider-identities-e42162.py"
        ]

    @property
    def migration_id(self):
        return "e7563e1d-4fdd-46ee-9c5a-ab7afa38a606"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE m_external_history_imports_v1
                ADD COLUMN file_access_progress jsonb;
            ALTER TABLE m_external_history_message_hashes_v1
                DROP CONSTRAINT m_external_history_message_hashes_v1_job_uuid_fkey,
                ADD CONSTRAINT m_external_history_message_hashes_v1_job_uuid_fkey
                    FOREIGN KEY (job_uuid) REFERENCES m_external_history_imports_v1(uuid)
                    ON DELETE CASCADE;
        """)

    def downgrade(self, session):
        session.execute("""
            ALTER TABLE m_external_history_message_hashes_v1
                DROP CONSTRAINT m_external_history_message_hashes_v1_job_uuid_fkey,
                ADD CONSTRAINT m_external_history_message_hashes_v1_job_uuid_fkey
                    FOREIGN KEY (job_uuid) REFERENCES m_external_history_imports_v1(uuid);
            ALTER TABLE m_external_history_imports_v1
                DROP COLUMN file_access_progress;
        """)


migration_step = MigrationStep()
