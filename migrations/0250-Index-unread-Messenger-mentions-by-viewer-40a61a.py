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
            "0249-Restore-active-legacy-direct-conversations-after-archive-merging-079b28.py"
        ]

    @property
    def migration_id(self):
        return "40a61ab8-f394-434c-819e-32c6f270d62c"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE INDEX messenger_user_message_states_unread_mentions_idx
                ON messenger_user_message_states
                    (project_id, user_uuid, placement_uuid, membership_generation)
                WHERE mentioned AND read_at IS NULL;
        """)

    def downgrade(self, session):
        session.execute("DROP INDEX messenger_user_message_states_unread_mentions_idx")


migration_step = MigrationStep()
