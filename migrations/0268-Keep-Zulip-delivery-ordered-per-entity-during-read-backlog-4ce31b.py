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
            "0267-Keep-native-entities-local-and-inherit-Zulip-provenance-06fad5.py"
        ]

    @property
    def migration_id(self):
        return "4ce31b6a-1c87-4941-9835-0e53e8ff63c9"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            UPDATE messenger_domain_outbox_events event
            SET scope_key = event.project_id::text || ':incoming:' ||
                    (event.payload->>'entity_uuid'),
                payload = event.payload || jsonb_build_object(
                    'resource_uuid', event.payload->>'entity_uuid')
            WHERE event.event_kind='delivery_snapshot_event'
              AND event.payload->>'source_kind'='zulip.incoming'
              AND NOT EXISTS (
                  SELECT 1 FROM messenger_projection_tasks task
                  WHERE task.project_id=event.project_id
                    AND task.outbox_event_uuid=event.uuid
                    AND task.status IN ('completed','dead_letter')
              );
            UPDATE messenger_projection_tasks task
            SET scope_key=event.scope_key,
                ordering_key=event.payload->>'entity_uuid',
                payload=event.payload
            FROM messenger_domain_outbox_events event
            WHERE event.project_id=task.project_id
              AND event.uuid=task.outbox_event_uuid
              AND task.task_kind='delivery_snapshot_event'
              AND task.payload->>'source_kind'='zulip.incoming'
              AND task.status NOT IN ('completed','dead_letter');
        """)

    def downgrade(self, session):
        raise RuntimeError("Zulip delivery ordering cutover is irreversible")


migration_step = MigrationStep()
