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


NOTIFY_CHANNEL = "workspace_projection_tasks"

BACKFILL_MISSING_TASKS = """
    INSERT INTO messenger_projection_tasks (
        uuid, project_id, outbox_event_uuid, task_kind,
        scope_kind, scope_key, ordering_key, ordering_created_at, payload
    )
    SELECT
        messenger_uuid_v5(
            event.uuid,
            'projection-task:' || event.event_kind
        ),
        event.project_id,
        event.uuid,
        event.event_kind,
        event.scope_kind,
        event.scope_key,
        COALESCE(
            event.payload->>'placement_uuid',
            event.payload#>>'{placement,uuid}',
            event.payload->>'resource_uuid',
            event.payload->>'canonical_message_uuid',
            event.payload->>'topic_uuid',
            event.payload->>'stream_uuid',
            event.payload->>'folder_uuid',
            event.payload->>'user_uuid',
            event.uuid::text
        ),
        COALESCE(
            (event.payload->>'message_created_at')::timestamptz,
            (event.payload->>'audience_created_before')::timestamptz,
            (event.payload->>'membership_started_at')::timestamptz,
            event.created_at
        ),
        event.payload
    FROM messenger_domain_outbox_events AS event
    WHERE NOT EXISTS (
        SELECT 1
        FROM messenger_projection_tasks AS task
        WHERE task.project_id = event.project_id
          AND task.outbox_event_uuid = event.uuid
    )
    ON CONFLICT (project_id, outbox_event_uuid) DO NOTHING
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0182-Atomically-enqueue-Messenger-projection-tasks-06a993.py"]

    @property
    def migration_id(self):
        return "bf0cd62a-eb16-4e68-a97d-b9fa227080d0"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(BACKFILL_MISSING_TASKS, ())
        session.execute("SELECT pg_notify(%s, 'backfill')", (NOTIFY_CHANNEL,))

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
