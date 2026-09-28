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
CREATE OR REPLACE FUNCTION workspace_v3.enqueue_message_flag_update_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    WITH flag_pairs AS MATERIALIZED (
        SELECT old_flag.project_id AS old_project_id,
               old_flag.message_uuid AS old_message_uuid,
               old_flag.user_uuid AS old_user_uuid,
               flag.project_id, flag.message_uuid, flag.user_uuid,
               flag.uuid, flag.read, flag.pinned, flag.starred, flag.mentioned,
               (old_flag.project_id, old_flag.message_uuid, old_flag.user_uuid)
                   IS DISTINCT FROM
               (flag.project_id, flag.message_uuid, flag.user_uuid)
                   AS identity_changed,
               (old_flag.read, old_flag.mentioned)
                   IS DISTINCT FROM (flag.read, flag.mentioned)
                   AS counter_changed
        FROM new_rows AS flag
        JOIN old_rows AS old_flag USING (uuid)
        UNION ALL
        SELECT old_flag.project_id AS old_project_id,
               old_flag.message_uuid AS old_message_uuid,
               old_flag.user_uuid AS old_user_uuid,
               flag.project_id, flag.message_uuid, flag.user_uuid,
               flag.uuid, flag.read, flag.pinned, flag.starred, flag.mentioned,
               false AS identity_changed,
               (old_flag.read, old_flag.mentioned)
                   IS DISTINCT FROM (flag.read, flag.mentioned)
                   AS counter_changed
        FROM new_rows AS flag
        JOIN old_rows AS old_flag
          ON old_flag.project_id = flag.project_id
         AND old_flag.message_uuid = flag.message_uuid
         AND old_flag.user_uuid = flag.user_uuid
        WHERE old_flag.uuid IS DISTINCT FROM flag.uuid
    ), counter_flags AS MATERIALIZED (
        SELECT old_project_id AS project_id,
               old_message_uuid AS message_uuid,
               old_user_uuid AS user_uuid,
               identity_changed
        FROM flag_pairs WHERE identity_changed
        UNION
        SELECT project_id, message_uuid, user_uuid, identity_changed
        FROM flag_pairs WHERE identity_changed OR counter_changed
    ), counter_scopes AS MATERIALIZED (
        SELECT flag.project_id, flag.user_uuid, message.stream_uuid,
               message.topic_uuid, flag.identity_changed
        FROM counter_flags AS flag
        JOIN workspace_v3.messages AS message
          ON message.project_id = flag.project_id
         AND message.uuid = flag.message_uuid
    )
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT scope.project_id, 'read_counters', 'user_stream',
           scope.stream_uuid, scope.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM counter_scopes AS scope
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;

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
"""


DOWNGRADE = r"""
CREATE OR REPLACE FUNCTION workspace_v3.enqueue_message_flag_update_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    WITH flag_pairs AS MATERIALIZED (
        SELECT old_flag.project_id AS old_project_id,
               old_flag.message_uuid AS old_message_uuid,
               old_flag.user_uuid AS old_user_uuid,
               flag.project_id, flag.message_uuid, flag.user_uuid,
               flag.uuid, flag.read, flag.pinned, flag.starred, flag.mentioned,
               (old_flag.project_id, old_flag.message_uuid, old_flag.user_uuid)
                   IS DISTINCT FROM
               (flag.project_id, flag.message_uuid, flag.user_uuid)
                   AS identity_changed,
               (old_flag.read, old_flag.mentioned)
                   IS DISTINCT FROM (flag.read, flag.mentioned)
                   AS counter_changed
        FROM new_rows AS flag
        JOIN old_rows AS old_flag USING (uuid)
        UNION ALL
        SELECT old_flag.project_id AS old_project_id,
               old_flag.message_uuid AS old_message_uuid,
               old_flag.user_uuid AS old_user_uuid,
               flag.project_id, flag.message_uuid, flag.user_uuid,
               flag.uuid, flag.read, flag.pinned, flag.starred, flag.mentioned,
               false AS identity_changed,
               (old_flag.read, old_flag.mentioned)
                   IS DISTINCT FROM (flag.read, flag.mentioned)
                   AS counter_changed
        FROM new_rows AS flag
        JOIN old_rows AS old_flag
          ON old_flag.project_id = flag.project_id
         AND old_flag.message_uuid = flag.message_uuid
         AND old_flag.user_uuid = flag.user_uuid
        WHERE old_flag.uuid IS DISTINCT FROM flag.uuid
    ), counter_flags AS MATERIALIZED (
        SELECT old_project_id AS project_id,
               old_message_uuid AS message_uuid,
               old_user_uuid AS user_uuid,
               identity_changed
        FROM flag_pairs WHERE identity_changed
        UNION
        SELECT project_id, message_uuid, user_uuid, identity_changed
        FROM flag_pairs WHERE identity_changed OR counter_changed
    ), counter_scopes AS MATERIALIZED (
        SELECT flag.project_id, flag.user_uuid, message.stream_uuid,
               message.topic_uuid, flag.identity_changed
        FROM counter_flags AS flag
        JOIN workspace_v3.messages AS message
          ON message.project_id = flag.project_id
         AND message.uuid = flag.message_uuid
    )
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT scope.project_id, 'read_counters', 'user_stream',
           scope.stream_uuid, scope.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM counter_scopes AS scope
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;

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
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0200-Allow-ten-thousand-character-Workspace-descriptions-912ea7.py"
        ]

    @property
    def migration_id(self):
        return "ad387574-760e-42f6-a0fb-a499b6e1a07d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
