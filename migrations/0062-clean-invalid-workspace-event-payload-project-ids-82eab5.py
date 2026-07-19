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
        self._depends = ["0061-backfill-workspace-event-payload-identity-fields-f25144.py"]

    @property
    def migration_id(self):
        return "82eab538-4c16-4294-8fb9-723b13a4faca"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            UPDATE "m_workspace_events"
            SET "payload" = "payload" - 'project_id'
            WHERE "payload" ? 'project_id'
                AND "payload"->>'kind' NOT IN (
                    'message.created',
                    'message.updated',
                    'messages.read',
                    'stream_bindings.created',
                    'stream.created',
                    'stream.updated',
                    'topic.created',
                    'topic.updated',
                    'folder.created',
                    'folder.updated'
                );
            """
        )

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
