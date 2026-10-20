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
            "0243-Separate-realtime-and-background-Zulip-task-queues-cc1987.py"
        ]

    @property
    def migration_id(self):
        return "8a264908-d661-4699-8fac-a59e610b6011"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE INDEX bridge_zulip_v5_tasks_recurring_idx
                ON bridge_zulip_v5_tasks ((content->>'kind'))
                WHERE status IN ('new','waiting','retry')
                  AND content->>'kind' IN ('reconciliation','refresh_message_counters');
        """)

    def downgrade(self, session):
        session.execute("DROP INDEX bridge_zulip_v5_tasks_recurring_idx")


migration_step = MigrationStep()
