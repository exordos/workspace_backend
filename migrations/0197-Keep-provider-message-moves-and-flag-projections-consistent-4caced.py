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


UPGRADE = """
ALTER TABLE workspace_v3.message_flags
    ALTER CONSTRAINT message_flags_message_fkey
    DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_message_flag_update_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    WITH changed_flags AS MATERIALIZED (
        SELECT flag.project_id, flag.message_uuid, flag.user_uuid
        FROM new_rows AS flag
        JOIN old_rows AS old_flag USING (uuid)
        WHERE old_flag.read IS DISTINCT FROM flag.read
           OR old_flag.mentioned IS DISTINCT FROM flag.mentioned
        UNION ALL
        SELECT flag.project_id, flag.message_uuid, flag.user_uuid
        FROM new_rows AS flag
        JOIN old_rows AS old_flag
          ON old_flag.project_id = flag.project_id
         AND old_flag.message_uuid = flag.message_uuid
         AND old_flag.user_uuid = flag.user_uuid
        WHERE old_flag.uuid IS DISTINCT FROM flag.uuid
          AND (
              old_flag.read IS DISTINCT FROM flag.read
              OR old_flag.mentioned IS DISTINCT FROM flag.mentioned
          )
    )
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT flag.project_id, 'read_counters', 'user_stream',
           message.stream_uuid, flag.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM changed_flags AS flag
    JOIN workspace_v3.messages AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;

    WITH changed_flags AS MATERIALIZED (
        SELECT flag.project_id, flag.message_uuid, flag.user_uuid, flag.uuid,
               flag.read, flag.pinned, flag.starred, flag.mentioned
        FROM new_rows AS flag
        JOIN old_rows AS old_flag USING (uuid)
        WHERE (
                old_flag.read,
                old_flag.pinned,
                old_flag.starred,
                old_flag.mentioned
              ) IS DISTINCT FROM (
                flag.read,
                flag.pinned,
                flag.starred,
                flag.mentioned
              )
        UNION ALL
        SELECT flag.project_id, flag.message_uuid, flag.user_uuid, flag.uuid,
               flag.read, flag.pinned, flag.starred, flag.mentioned
        FROM new_rows AS flag
        JOIN old_rows AS old_flag
          ON old_flag.project_id = flag.project_id
         AND old_flag.message_uuid = flag.message_uuid
         AND old_flag.user_uuid = flag.user_uuid
        WHERE old_flag.uuid IS DISTINCT FROM flag.uuid
          AND (
                old_flag.read,
                old_flag.pinned,
                old_flag.starred,
                old_flag.mentioned
              ) IS DISTINCT FROM (
                flag.read,
                flag.pinned,
                flag.starred,
                flag.mentioned
              )
    )
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT flag.project_id, 'read_counters', 'user_topic',
           message.topic_uuid, flag.user_uuid,
           jsonb_build_object(
               'emit_message_events', true,
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'message_uuid', flag.message_uuid,
                       'operation', 'update',
                       'read', flag.read,
                       'pinned', flag.pinned,
                       'starred', flag.starred,
                       'mentioned', flag.mentioned
                   ) ORDER BY flag.uuid
               )
           )
    FROM changed_flags AS flag
    JOIN workspace_v3.messages AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    GROUP BY flag.project_id, message.topic_uuid, flag.user_uuid;
    RETURN NULL;
END
$function$;

CREATE FUNCTION workspace_v3.reconcile_message_move()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    DELETE FROM workspace_v3.message_flags AS flag
    USING new_rows AS message, old_rows AS old_message
    WHERE old_message.uuid = message.uuid
      AND (old_message.stream_uuid, old_message.topic_uuid)
          IS DISTINCT FROM (message.stream_uuid, message.topic_uuid)
      AND flag.project_id = message.project_id
      AND flag.message_uuid = message.uuid
      AND NOT EXISTS (
          SELECT 1
          FROM workspace_v3.stream_bindings AS binding
          WHERE binding.project_id = message.project_id
            AND binding.stream_uuid = message.stream_uuid
            AND binding.user_uuid = flag.user_uuid
      );

    UPDATE workspace_v3.message_flags AS flag
    SET stream_uuid = message.stream_uuid,
        updated_at = clock_timestamp()
    FROM new_rows AS message
    JOIN old_rows AS old_message USING (uuid)
    WHERE (old_message.stream_uuid, old_message.topic_uuid)
          IS DISTINCT FROM (message.stream_uuid, message.topic_uuid)
      AND flag.project_id = message.project_id
      AND flag.message_uuid = message.uuid
      AND flag.stream_uuid IS DISTINCT FROM message.stream_uuid;

    UPDATE workspace_v3.stream_bindings AS binding
    SET last_message_uuid = NULL,
        updated_at = clock_timestamp()
    FROM old_rows AS old_message
    JOIN new_rows AS message USING (uuid)
    WHERE old_message.stream_uuid IS DISTINCT FROM message.stream_uuid
      AND binding.project_id = old_message.project_id
      AND binding.stream_uuid = old_message.stream_uuid
      AND binding.last_message_uuid = old_message.uuid;

    UPDATE workspace_v3.topic_bindings AS binding
    SET last_message_uuid = NULL,
        updated_at = clock_timestamp()
    FROM old_rows AS old_message
    JOIN new_rows AS message USING (uuid)
    WHERE old_message.topic_uuid IS DISTINCT FROM message.topic_uuid
      AND binding.project_id = old_message.project_id
      AND binding.topic_uuid = old_message.topic_uuid
      AND binding.last_message_uuid = old_message.uuid;

    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT scope.project_id, 'read_counters', scope.scope_type,
           scope.scope_uuid, scope.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM (
        SELECT old_message.project_id, 'user_stream'::varchar AS scope_type,
               old_message.stream_uuid AS scope_uuid, binding.user_uuid
        FROM old_rows AS old_message
        JOIN new_rows AS message USING (uuid)
        JOIN workspace_v3.stream_bindings AS binding
          ON binding.project_id = old_message.project_id
         AND binding.stream_uuid = old_message.stream_uuid
        WHERE old_message.stream_uuid IS DISTINCT FROM message.stream_uuid
        UNION
        SELECT message.project_id, 'user_stream'::varchar,
               message.stream_uuid, binding.user_uuid
        FROM old_rows AS old_message
        JOIN new_rows AS message USING (uuid)
        JOIN workspace_v3.stream_bindings AS binding
          ON binding.project_id = message.project_id
         AND binding.stream_uuid = message.stream_uuid
        WHERE old_message.stream_uuid IS DISTINCT FROM message.stream_uuid
        UNION
        SELECT old_message.project_id, 'user_topic'::varchar,
               old_message.topic_uuid, binding.user_uuid
        FROM old_rows AS old_message
        JOIN new_rows AS message USING (uuid)
        JOIN workspace_v3.topic_bindings AS binding
          ON binding.project_id = old_message.project_id
         AND binding.topic_uuid = old_message.topic_uuid
        WHERE old_message.topic_uuid IS DISTINCT FROM message.topic_uuid
        UNION
        SELECT message.project_id, 'user_topic'::varchar,
               message.topic_uuid, binding.user_uuid
        FROM old_rows AS old_message
        JOIN new_rows AS message USING (uuid)
        JOIN workspace_v3.topic_bindings AS binding
          ON binding.project_id = message.project_id
         AND binding.topic_uuid = message.topic_uuid
        WHERE old_message.topic_uuid IS DISTINCT FROM message.topic_uuid
    ) AS scope
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;
    RETURN NULL;
END
$function$;

CREATE TRIGGER messages_reconcile_move
    AFTER UPDATE ON workspace_v3.messages
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION workspace_v3.reconcile_message_move();
"""


DOWNGRADE = """
DROP TRIGGER messages_reconcile_move ON workspace_v3.messages;
DROP FUNCTION workspace_v3.reconcile_message_move();

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_message_flag_update_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT flag.project_id, 'read_counters', 'user_stream',
           message.stream_uuid, flag.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM new_rows AS flag
    JOIN old_rows AS old_flag USING (uuid)
    JOIN workspace_v3.messages AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    WHERE old_flag.read IS DISTINCT FROM flag.read
       OR old_flag.mentioned IS DISTINCT FROM flag.mentioned
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
    SELECT flag.project_id, 'read_counters', 'user_topic',
           message.topic_uuid, flag.user_uuid,
           jsonb_build_object(
               'emit_message_events', true,
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'message_uuid', flag.message_uuid,
                       'operation', 'update',
                       'read', flag.read,
                       'pinned', flag.pinned,
                       'starred', flag.starred,
                       'mentioned', flag.mentioned
                   ) ORDER BY flag.uuid
               )
           )
    FROM new_rows AS flag
    JOIN old_rows AS old_flag USING (uuid)
    JOIN workspace_v3.messages AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    WHERE (
            old_flag.read,
            old_flag.pinned,
            old_flag.starred,
            old_flag.mentioned
          ) IS DISTINCT FROM (
            flag.read,
            flag.pinned,
            flag.starred,
            flag.mentioned
          )
    GROUP BY flag.project_id, message.topic_uuid, flag.user_uuid;
    RETURN NULL;
END
$function$;

ALTER TABLE workspace_v3.message_flags
    ALTER CONSTRAINT message_flags_message_fkey
    NOT DEFERRABLE;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0196-Keep-v3-delete-projections-and-rebinds-consistent-d38bd2.py"
        ]

    @property
    def migration_id(self):
        return "4caced80-7483-4e16-a812-6bb36f15c24a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
