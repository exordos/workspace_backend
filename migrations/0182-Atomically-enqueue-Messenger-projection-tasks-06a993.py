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


TRIGGER_NAME = "messenger_projection_tasks_enqueue_v1"
FUNCTION_NAME = "messenger_enqueue_projection_tasks_v1"
NOTIFY_CHANNEL = "workspace_projection_tasks"

CREATE_FUNCTION = f"""
    CREATE FUNCTION {FUNCTION_NAME}()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $function$
    DECLARE
        inserted_count integer;
    BEGIN
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
                event.payload#>>'{{placement,uuid}}',
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
        FROM new_outbox_events AS event
        ON CONFLICT (project_id, outbox_event_uuid) DO NOTHING;

        GET DIAGNOSTICS inserted_count = ROW_COUNT;
        IF inserted_count > 0 THEN
            PERFORM pg_notify('{NOTIFY_CHANNEL}', inserted_count::text);
        END IF;
        RETURN NULL;
    END
    $function$
"""

CREATE_TRIGGER = f"""
    CREATE TRIGGER {TRIGGER_NAME}
    AFTER INSERT ON messenger_domain_outbox_events
    REFERENCING NEW TABLE AS new_outbox_events
    FOR EACH STATEMENT
    EXECUTE FUNCTION {FUNCTION_NAME}()
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0181-Index-Messenger-projection-partitions-81bfd9.py"]

    @property
    def migration_id(self):
        return "06a9931d-8dfa-415d-8ff0-057c3193ae7c"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(CREATE_FUNCTION, ())
        session.execute(CREATE_TRIGGER, ())

    def downgrade(self, session):
        session.execute(
            f"""
            DROP TRIGGER IF EXISTS {TRIGGER_NAME}
            ON messenger_domain_outbox_events
            """,
            (),
        )
        session.execute(f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}()", ())


migration_step = MigrationStep()
