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


UPGRADE = r"""
INSERT INTO workspace_v3.projection_tasks (
    project_id, task_type, scope_type, scope_uuid, user_uuid, payload
)
SELECT binding.project_id, 'read_counters', 'user_stream',
       binding.stream_uuid, binding.user_uuid,
       '{"emit_message_events": false}'::jsonb
FROM workspace_v3.stream_bindings AS binding
ON CONFLICT (
    project_id, task_type, scope_type, scope_uuid, user_uuid
) WHERE status = 'pending'
  AND task_type = 'read_counters'
  AND payload IN (
      '{"emit_message_events": false}'::jsonb,
      '{"emit_message_event": false}'::jsonb
  ) DO NOTHING;

INSERT INTO workspace_v3.projection_tasks (
    project_id, task_type, scope_type, scope_uuid, user_uuid, payload
)
SELECT folder.project_id, 'folder_counters', 'user_folder',
       folder.uuid, folder.user_uuid,
       '{"repair":"hierarchical_unread_rollup_v1"}'::jsonb
FROM workspace_v3.folders AS folder
WHERE NOT EXISTS (
    SELECT 1
    FROM workspace_v3.projection_tasks AS active
    WHERE active.project_id = folder.project_id
      AND active.task_type = 'folder_counters'
      AND active.scope_type = 'user_folder'
      AND active.scope_uuid = folder.uuid
      AND active.user_uuid = folder.user_uuid
      AND active.status IN ('pending', 'running', 'failed')
);
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = [
            "0204-Cascade-capped-unread-counter-projections-87e406.py"
        ]

    @property
    def migration_id(self):
        return "bf1934be-c3e8-48fe-9f5e-ec37e430da24"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
