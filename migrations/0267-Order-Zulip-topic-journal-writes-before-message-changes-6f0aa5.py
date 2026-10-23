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
            "0266-Preserve-native-topic-titles-and-stream-ownership-during-provider-reconciliation-11e12e.py"
        ]

    @property
    def migration_id(self):
        return "6f0aa554-e085-4de2-9b38-f399b3140b22"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Reconciliation changes the topic journal before acknowledging the
        # message journal. Ingress must acquire the same rows in that order.
        # PostgreSQL runs triggers for the same event in name order.
        session.execute("""
            ALTER TRIGGER bridge_zulip_v5_track_message_topic_changes
                ON bridge_zulip_v5_messages
                RENAME TO bridge_zulip_v5_track_00_message_topic_changes;
        """)

    def downgrade(self, session):
        session.execute("""
            ALTER TRIGGER bridge_zulip_v5_track_00_message_topic_changes
                ON bridge_zulip_v5_messages
                RENAME TO bridge_zulip_v5_track_message_topic_changes;
        """)


migration_step = MigrationStep()
