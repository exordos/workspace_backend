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
        self._depends = ["0084-add-zulip-event-queue-subscription-version-c4c0ac.py"]

    @property
    def migration_id(self):
        return "e371e20a-2515-4cd1-a237-13830042cad8"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_events"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_object_type_check",
                ADD CONSTRAINT "m_workspace_events_object_type_check"
                    CHECK ("object_type" IN (
                        'message',
                        'message_reaction',
                        'stream',
                        'stream_binding',
                        'topic',
                        'user',
                        'folder',
                        'folder_item'
                    ));
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_events"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_object_type_check",
                ADD CONSTRAINT "m_workspace_events_object_type_check"
                    CHECK ("object_type" IN (
                        'message',
                        'stream',
                        'stream_binding',
                        'topic',
                        'user',
                        'folder',
                        'folder_item'
                    ));
            """
        )


migration_step = MigrationStep()
