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
        self._depends = ["0100-add-mentioned-to-user-message-view-7a0cf1.py"]

    @property
    def migration_id(self):
        return "0a9df414-e94e-4761-a011-885b13d9e46b"

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
                        'folder_item',
                        'file'
                    ));
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DELETE FROM "m_workspace_events"
            WHERE "object_type" = 'file';

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


migration_step = MigrationStep()
