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


# Capture still updates exact state and parents during baseline. The wrapper
# suppresses only derived snapshot tasks; the original bounded page function
# retains its explicit final snapshot sweep and planner settings. Use ordinary
# transaction-local set_config: ALTER FUNCTION SET for a custom placeholder
# requires privileges that the runtime database owner does not have.
SNAPSHOT_FUNCTION = r"""
CREATE OR REPLACE FUNCTION workspace_v3.enqueue_exact_counter_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $f$
DECLARE scope text; identity_field text;
BEGIN
    IF TG_TABLE_NAME = 'topic_bindings' THEN
        scope := 'user_topic'; identity_field := 'topic_uuid';
    ELSE
        scope := 'user_stream'; identity_field := 'stream_uuid';
    END IF;
    INSERT INTO workspace_v3.projection_tasks
        (project_id, task_type, scope_type, scope_uuid, user_uuid, payload)
    SELECT binding.project_id, 'read_counters', scope,
           (to_jsonb(binding)->>identity_field)::uuid, binding.user_uuid,
           '{"emit_message_events":false}'::jsonb
    FROM new_rows AS binding JOIN old_rows AS previous USING (project_id, uuid)
    WHERE binding.counter_version IS DISTINCT FROM previous.counter_version
       OR (binding.project_id,binding.user_uuid,to_jsonb(binding)->'topic_uuid')
          IS DISTINCT FROM (previous.project_id,previous.user_uuid,to_jsonb(previous)->'topic_uuid')
    ON CONFLICT (project_id, task_type, scope_type, scope_uuid, user_uuid)
    WHERE status = 'pending' AND task_type = 'read_counters'
      AND payload IN ('{"emit_message_events":false}'::jsonb,
                      '{"emit_message_event":false}'::jsonb) DO NOTHING;
    RETURN NULL;
END $f$;
"""

BASELINE_GUARD = """BEGIN
    IF current_setting('workspace_v3.suppress_counter_snapshots', true) = 'on' THEN
        RETURN NULL;
    END IF;
"""

UPGRADE = (
    SNAPSHOT_FUNCTION.replace("BEGIN\n", BASELINE_GUARD, 1)
    + """
ALTER FUNCTION workspace_v3.advance_unread_baseline(integer)
    RENAME TO advance_unread_baseline_page;

CREATE FUNCTION workspace_v3.advance_unread_baseline(batch_size integer)
RETURNS integer LANGUAGE plpgsql SET enable_sort = off SET jit = off AS $f$
DECLARE
    previous_counter_setting text := current_setting(
        'workspace_v3.suppress_counter_snapshots', true);
    previous_folder_setting text := current_setting(
        'workspace_v3.suppress_folder_item_projection', true);
    advanced integer;
BEGIN
    PERFORM set_config('workspace_v3.suppress_counter_snapshots', 'on', true);
    PERFORM set_config('workspace_v3.suppress_folder_item_projection', 'on', true);
    advanced := workspace_v3.advance_unread_baseline_page(batch_size);
    PERFORM set_config('workspace_v3.suppress_counter_snapshots',
                       previous_counter_setting, true);
    PERFORM set_config('workspace_v3.suppress_folder_item_projection',
                       previous_folder_setting, true);
    RETURN advanced;
END $f$;
"""
)

DOWNGRADE = (
    """
DROP FUNCTION workspace_v3.advance_unread_baseline(integer);
ALTER FUNCTION workspace_v3.advance_unread_baseline_page(integer)
    RENAME TO advance_unread_baseline;
"""
    + SNAPSHOT_FUNCTION
)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0207-Maintain-exact-hierarchical-unread-counters-77e2f5.py"]

    @property
    def migration_id(self):
        return "ec0e37f1-0707-4035-9182-91d18055fcf6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
