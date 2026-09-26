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
WITH dead_scopes AS MATERIALIZED (
    SELECT DISTINCT task.project_id, task.scope_type, task.scope_uuid,
                    task.user_uuid
    FROM workspace_v3.projection_tasks AS task
    WHERE task.task_type = 'read_counters'
      AND task.status = 'dead_letter'
      AND task.user_uuid IS NOT NULL
),
closed AS (
    UPDATE workspace_v3.projection_tasks AS task
    SET status = 'completed', lease_owner = NULL, lease_expires_at = NULL,
        next_retry_at = NULL, last_error = NULL,
        updated_at = clock_timestamp()
    WHERE task.task_type = 'read_counters'
      AND task.status = 'dead_letter'
    RETURNING task.uuid
)
INSERT INTO workspace_v3.projection_tasks (
    project_id, task_type, scope_type, scope_uuid, user_uuid, payload
)
SELECT dead.project_id, 'read_counters', dead.scope_type,
       dead.scope_uuid, dead.user_uuid,
       '{"repair":"bounded_topic_counter_scan_v1"}'::jsonb
FROM dead_scopes AS dead
WHERE (
    (
        dead.scope_type = 'user_stream'
        AND EXISTS (
            SELECT 1
            FROM workspace_v3.stream_bindings AS binding
            WHERE binding.project_id = dead.project_id
              AND binding.stream_uuid = dead.scope_uuid
              AND binding.user_uuid = dead.user_uuid
        )
    )
    OR (
        dead.scope_type = 'user_topic'
        AND EXISTS (
            SELECT 1
            FROM workspace_v3.topic_bindings AS binding
            WHERE binding.project_id = dead.project_id
              AND binding.topic_uuid = dead.scope_uuid
              AND binding.user_uuid = dead.user_uuid
        )
    )
)
AND NOT EXISTS (
    SELECT 1
    FROM workspace_v3.projection_tasks AS active
    WHERE active.project_id = dead.project_id
      AND active.task_type = 'read_counters'
      AND active.scope_type = dead.scope_type
      AND active.scope_uuid = dead.scope_uuid
      AND active.user_uuid = dead.user_uuid
      AND active.status IN ('pending', 'running', 'failed')
)
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0202-Annotate-provider-reaction-projections-without-task-scans-d1b4a6.py"
        ]

    @property
    def migration_id(self):
        return "fd807a61-189a-449d-8f57-94e419ea335f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
