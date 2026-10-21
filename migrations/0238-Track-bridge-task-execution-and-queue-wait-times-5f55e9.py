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
            "0237-Split-pending-Zulip-history-imports-into-100-ID-batches-342a1c.py"
        ]

    @property
    def migration_id(self):
        return "5f55e92a-1d9e-4380-8464-2f50e162eab1"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE bridge_zulip_v5_tasks
                ADD COLUMN IF NOT EXISTS execution_time DOUBLE PRECISION
                    CHECK (execution_time >= 0),
                ADD COLUMN IF NOT EXISTS queue_wait_time DOUBLE PRECISION
                    CHECK (queue_wait_time >= 0);
        """)

    def downgrade(self, session):
        session.execute("""
            ALTER TABLE bridge_zulip_v5_tasks
                DROP COLUMN queue_wait_time,
                DROP COLUMN execution_time;
        """)


migration_step = MigrationStep()
