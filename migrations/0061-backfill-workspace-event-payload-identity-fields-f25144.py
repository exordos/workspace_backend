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
        self._depends = ["0060-add-workspace-user-profile-status-fields-174dc8.py"]

    @property
    def migration_id(self):
        return "f2514438-8502-4e81-bcdc-7aa4a8dcd944"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            UPDATE "m_workspace_events"
            SET "payload" = "payload"
                || CASE
                    WHEN "payload"->>'project_id' IS NOT NULL
                        OR "payload"->>'kind' NOT IN (
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
                        ) THEN '{}'::jsonb
                    ELSE jsonb_build_object('project_id', "project_id")
                END
                || CASE
                    WHEN "payload"->>'user_uuid' IS NOT NULL
                        OR "payload"->>'kind' NOT IN (
                            'message.created',
                            'message.updated',
                            'stream.created',
                            'stream.updated',
                            'topic.created',
                            'topic.updated',
                            'folder.created',
                            'folder.updated'
                        ) THEN '{}'::jsonb
                    ELSE jsonb_build_object('user_uuid', "user_uuid")
                END
            WHERE (
                    "payload"->>'project_id' IS NULL
                    AND "payload"->>'kind' IN (
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
                    )
                )
                OR (
                    "payload"->>'user_uuid' IS NULL
                    AND "payload"->>'kind' IN (
                        'message.created',
                        'message.updated',
                        'stream.created',
                        'stream.updated',
                        'topic.created',
                        'topic.updated',
                        'folder.created',
                        'folder.updated'
                    )
                );
            """
        )

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
