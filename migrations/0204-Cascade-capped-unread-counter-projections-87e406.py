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
CREATE OR REPLACE FUNCTION workspace_v3.enqueue_message_flag_insert_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT flag.project_id, 'read_counters', 'user_topic',
           message.topic_uuid, flag.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM new_rows AS flag
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
    RETURN NULL;
END
$function$;

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_message_flag_update_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    WITH flag_pairs AS MATERIALIZED (
        SELECT old_flag.project_id AS old_project_id,
               old_flag.message_uuid AS old_message_uuid,
               old_flag.user_uuid AS old_user_uuid,
               flag.project_id, flag.message_uuid, flag.user_uuid
        FROM new_rows AS flag
        JOIN old_rows AS old_flag USING (uuid)
        WHERE (old_flag.project_id, old_flag.message_uuid, old_flag.user_uuid)
              IS DISTINCT FROM
              (flag.project_id, flag.message_uuid, flag.user_uuid)
    ), identity_flags AS MATERIALIZED (
        SELECT old_project_id AS project_id,
               old_message_uuid AS message_uuid,
               old_user_uuid AS user_uuid
        FROM flag_pairs
        UNION
        SELECT project_id, message_uuid, user_uuid FROM flag_pairs
    )
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT flag.project_id, 'read_counters', 'user_topic',
           message.topic_uuid, flag.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM identity_flags AS flag
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
           CASE WHEN current_setting(
                        'workspace_v3.provider_backfill_flags', true
                    ) = 'on'
                THEN '{"emit_message_events": false}'::jsonb
                ELSE jsonb_build_object(
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
           END
    FROM changed_flags AS flag
    JOIN workspace_v3.messages AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    GROUP BY flag.project_id, message.topic_uuid, flag.user_uuid
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

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_message_flag_delete_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
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
                       'operation', 'delete',
                       'read', flag.read,
                       'pinned', flag.pinned,
                       'starred', flag.starred,
                       'mentioned', flag.mentioned
                   ) ORDER BY flag.uuid
               )
           )
    FROM old_rows AS flag
    JOIN workspace_v3.messages AS message
      ON message.project_id = flag.project_id
     AND message.uuid = flag.message_uuid
    GROUP BY flag.project_id, message.topic_uuid, flag.user_uuid;
    RETURN NULL;
END
$function$;

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_message_delete_counter_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT message.project_id, 'read_counters', 'user_topic',
           message.topic_uuid, binding.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM old_rows AS message
    JOIN workspace_v3.topic_bindings AS binding
      ON binding.project_id = message.project_id
     AND binding.topic_uuid = message.topic_uuid
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

CREATE OR REPLACE FUNCTION workspace_v3.reconcile_message_move()
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
    SELECT scope.project_id, 'read_counters', 'user_topic',
           scope.topic_uuid, scope.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM (
        SELECT old_message.project_id,
               old_message.topic_uuid, binding.user_uuid
        FROM old_rows AS old_message
        JOIN new_rows AS message USING (uuid)
        JOIN workspace_v3.topic_bindings AS binding
          ON binding.project_id = old_message.project_id
         AND binding.topic_uuid = old_message.topic_uuid
        WHERE old_message.topic_uuid IS DISTINCT FROM message.topic_uuid
        UNION
        SELECT message.project_id, message.topic_uuid, binding.user_uuid
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

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_binding_counter_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_TABLE_NAME = 'stream_bindings' THEN
        IF TG_OP = 'UPDATE'
           AND OLD.notification_mode IS DISTINCT FROM NEW.notification_mode THEN
            INSERT INTO workspace_v3.projection_tasks (
                project_id, task_type, scope_type, scope_uuid,
                user_uuid, payload
            )
            SELECT binding.project_id, 'read_counters', 'user_topic',
                   binding.topic_uuid, binding.user_uuid,
                   '{"emit_message_events": false}'::jsonb
            FROM workspace_v3.topic_bindings AS binding
            WHERE binding.project_id = NEW.project_id
              AND binding.stream_uuid =
                  (to_jsonb(NEW)->>'stream_uuid')::uuid
              AND binding.user_uuid = NEW.user_uuid
            ON CONFLICT (
                project_id, task_type, scope_type, scope_uuid, user_uuid
            ) WHERE status = 'pending'
              AND task_type = 'read_counters'
              AND payload IN (
                  '{"emit_message_events": false}'::jsonb,
                  '{"emit_message_event": false}'::jsonb
              ) DO NOTHING;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'INSERT'
       OR OLD.notification_mode IS DISTINCT FROM NEW.notification_mode THEN
        INSERT INTO workspace_v3.projection_tasks (
            project_id, task_type, scope_type, scope_uuid,
            user_uuid, payload
        ) VALUES (
            NEW.project_id, 'read_counters', 'user_topic',
            (to_jsonb(NEW)->>'topic_uuid')::uuid, NEW.user_uuid,
            '{"emit_message_events": false}'::jsonb
        )
        ON CONFLICT (
            project_id, task_type, scope_type, scope_uuid, user_uuid
        ) WHERE status = 'pending'
          AND task_type = 'read_counters'
          AND payload IN (
              '{"emit_message_events": false}'::jsonb,
              '{"emit_message_event": false}'::jsonb
          ) DO NOTHING;
    END IF;

    IF TG_OP = 'UPDATE'
       AND (
            OLD.unread_count,
            OLD.active_unread_count,
            OLD.passive_unread_count,
            OLD.last_message_uuid
       ) IS DISTINCT FROM (
            NEW.unread_count,
            NEW.active_unread_count,
            NEW.passive_unread_count,
            NEW.last_message_uuid
       ) THEN
        INSERT INTO workspace_v3.projection_tasks (
            project_id, task_type, scope_type, scope_uuid,
            user_uuid, payload
        ) VALUES (
            NEW.project_id, 'read_counters', 'user_stream',
            (to_jsonb(NEW)->>'stream_uuid')::uuid, NEW.user_uuid,
            '{"emit_message_events": false}'::jsonb
        )
        ON CONFLICT (
            project_id, task_type, scope_type, scope_uuid, user_uuid
        ) WHERE status = 'pending'
          AND task_type = 'read_counters'
          AND payload IN (
              '{"emit_message_events": false}'::jsonb,
              '{"emit_message_event": false}'::jsonb
          ) DO NOTHING;
    END IF;
    RETURN NEW;
END
$function$;

INSERT INTO workspace_v3.projection_tasks (
    project_id, task_type, scope_type, scope_uuid, user_uuid, payload
)
SELECT binding.project_id, 'read_counters', 'user_topic',
       binding.topic_uuid, binding.user_uuid,
       '{"emit_message_events": false}'::jsonb
FROM workspace_v3.topic_bindings AS binding
ON CONFLICT (
    project_id, task_type, scope_type, scope_uuid, user_uuid
) WHERE status = 'pending'
  AND task_type = 'read_counters'
  AND payload IN (
      '{"emit_message_events": false}'::jsonb,
      '{"emit_message_event": false}'::jsonb
  ) DO NOTHING;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0203-Retry-Workspace-v3-dead-counter-projections-fd807a.py"]

    @property
    def migration_id(self):
        return "87e40626-0c59-4e48-bbb0-43257dfcff2a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
