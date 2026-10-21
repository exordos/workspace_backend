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
            "0244-Index-pending-Zulip-tasks-by-kind-for-recurring-work-8a2649.py"
        ]

    @property
    def migration_id(self):
        return "cc62d6ae-6e7a-437f-964a-8a179b042689"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            UPDATE bridge_zulip_v5_tasks
            SET status='retry',next_retry_at=now() AT TIME ZONE 'UTC',
                lease_owner=NULL,lease_expires_at=NULL,updated_at=now() AT TIME ZONE 'UTC'
            WHERE status='error' AND content->>'kind'='import_history'
              AND split_part(error_description,chr(10),1) IN (
                  'deadlock detected',
                  'Deadlock found when trying to get lock. Original message: deadlock detected'
              );
        """)

    def downgrade(self, session):
        # Retried or completed work must not return to a terminal error.
        pass


migration_step = MigrationStep()
