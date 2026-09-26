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
CREATE OR REPLACE FUNCTION workspace_v3.enqueue_reaction_insert_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
DECLARE
    provider_emit_events TEXT := current_setting(
        'workspace_v3.provider_reaction_emit_events', true
    );
    provider_origin_uuid TEXT := current_setting(
        'workspace_v3.provider_reaction_origin_uuid', true
    );
BEGIN
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, payload
    )
    SELECT reaction.project_id, 'reaction_snapshot', 'message',
           reaction.message_uuid,
           jsonb_build_object(
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'action', 'created',
                       'reaction_uuid', reaction.uuid,
                       'message_uuid', reaction.message_uuid,
                       'user_uuid', reaction.user_uuid,
                       'emoji_name', reaction.emoji_name,
                       'source_name', reaction.source_name
                   ) ORDER BY reaction.uuid
               )
           ) || CASE
                    WHEN provider_emit_events IN ('on', 'off')
                    THEN jsonb_build_object(
                        'emit_events', provider_emit_events = 'on',
                        'origin_provider_uuid', provider_origin_uuid
                    )
                    ELSE '{}'::jsonb
                END
    FROM new_rows AS reaction
    GROUP BY reaction.project_id, reaction.message_uuid;
    RETURN NULL;
END
$function$;

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_reaction_update_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
DECLARE
    provider_emit_events TEXT := current_setting(
        'workspace_v3.provider_reaction_emit_events', true
    );
    provider_origin_uuid TEXT := current_setting(
        'workspace_v3.provider_reaction_origin_uuid', true
    );
BEGIN
    IF EXISTS (
        SELECT 1
        FROM old_rows AS old_reaction
        JOIN new_rows AS new_reaction USING (uuid)
        WHERE (
                old_reaction.project_id,
                old_reaction.message_uuid,
                old_reaction.user_uuid
              ) IS DISTINCT FROM (
                new_reaction.project_id,
                new_reaction.message_uuid,
                new_reaction.user_uuid
              )
    ) THEN
        RAISE EXCEPTION 'reaction identity is immutable';
    END IF;
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, payload
    )
    SELECT reaction.project_id, 'reaction_snapshot', 'message',
           reaction.message_uuid,
           jsonb_build_object(
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'action', 'updated',
                       'reaction_uuid', reaction.uuid,
                       'message_uuid', reaction.message_uuid,
                       'user_uuid', reaction.user_uuid,
                       'emoji_name', reaction.emoji_name,
                       'source_name', reaction.source_name,
                       'old_emoji_name', old_reaction.emoji_name,
                       'old_source_name', old_reaction.source_name
                   ) ORDER BY reaction.uuid
               )
           ) || CASE
                    WHEN provider_emit_events IN ('on', 'off')
                    THEN jsonb_build_object(
                        'emit_events', provider_emit_events = 'on',
                        'origin_provider_uuid', provider_origin_uuid
                    )
                    ELSE '{}'::jsonb
                END
    FROM new_rows AS reaction
    JOIN old_rows AS old_reaction USING (uuid)
    WHERE old_reaction IS DISTINCT FROM reaction
    GROUP BY reaction.project_id, reaction.message_uuid;
    RETURN NULL;
END
$function$;

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_reaction_delete_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
DECLARE
    provider_emit_events TEXT := current_setting(
        'workspace_v3.provider_reaction_emit_events', true
    );
    provider_origin_uuid TEXT := current_setting(
        'workspace_v3.provider_reaction_origin_uuid', true
    );
BEGIN
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, payload
    )
    SELECT reaction.project_id, 'reaction_snapshot', 'message',
           reaction.message_uuid,
           jsonb_build_object(
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'action', 'deleted',
                       'reaction_uuid', reaction.uuid,
                       'message_uuid', reaction.message_uuid,
                       'user_uuid', reaction.user_uuid,
                       'emoji_name', reaction.emoji_name,
                       'source_name', reaction.source_name
                   ) ORDER BY reaction.uuid
               )
           ) || CASE
                    WHEN provider_emit_events IN ('on', 'off')
                    THEN jsonb_build_object(
                        'emit_events', provider_emit_events = 'on',
                        'origin_provider_uuid', provider_origin_uuid
                    )
                    ELSE '{}'::jsonb
                END
    FROM old_rows AS reaction
    GROUP BY reaction.project_id, reaction.message_uuid;
    RETURN NULL;
END
$function$;
"""


DOWNGRADE = r"""
CREATE OR REPLACE FUNCTION workspace_v3.enqueue_reaction_insert_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, payload
    )
    SELECT reaction.project_id, 'reaction_snapshot', 'message',
           reaction.message_uuid,
           jsonb_build_object(
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'action', 'created',
                       'reaction_uuid', reaction.uuid,
                       'message_uuid', reaction.message_uuid,
                       'user_uuid', reaction.user_uuid,
                       'emoji_name', reaction.emoji_name,
                       'source_name', reaction.source_name
                   ) ORDER BY reaction.uuid
               )
           )
    FROM new_rows AS reaction
    GROUP BY reaction.project_id, reaction.message_uuid;
    RETURN NULL;
END
$function$;

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_reaction_update_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM old_rows AS old_reaction
        JOIN new_rows AS new_reaction USING (uuid)
        WHERE (
                old_reaction.project_id,
                old_reaction.message_uuid,
                old_reaction.user_uuid
              ) IS DISTINCT FROM (
                new_reaction.project_id,
                new_reaction.message_uuid,
                new_reaction.user_uuid
              )
    ) THEN
        RAISE EXCEPTION 'reaction identity is immutable';
    END IF;
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, payload
    )
    SELECT reaction.project_id, 'reaction_snapshot', 'message',
           reaction.message_uuid,
           jsonb_build_object(
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'action', 'updated',
                       'reaction_uuid', reaction.uuid,
                       'message_uuid', reaction.message_uuid,
                       'user_uuid', reaction.user_uuid,
                       'emoji_name', reaction.emoji_name,
                       'source_name', reaction.source_name,
                       'old_emoji_name', old_reaction.emoji_name,
                       'old_source_name', old_reaction.source_name
                   ) ORDER BY reaction.uuid
               )
           )
    FROM new_rows AS reaction
    JOIN old_rows AS old_reaction USING (uuid)
    WHERE old_reaction IS DISTINCT FROM reaction
    GROUP BY reaction.project_id, reaction.message_uuid;
    RETURN NULL;
END
$function$;

CREATE OR REPLACE FUNCTION workspace_v3.enqueue_reaction_delete_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, payload
    )
    SELECT reaction.project_id, 'reaction_snapshot', 'message',
           reaction.message_uuid,
           jsonb_build_object(
               'operations', jsonb_agg(
                   jsonb_build_object(
                       'action', 'deleted',
                       'reaction_uuid', reaction.uuid,
                       'message_uuid', reaction.message_uuid,
                       'user_uuid', reaction.user_uuid,
                       'emoji_name', reaction.emoji_name,
                       'source_name', reaction.source_name
                   ) ORDER BY reaction.uuid
               )
           )
    FROM old_rows AS reaction
    GROUP BY reaction.project_id, reaction.message_uuid;
    RETURN NULL;
END
$function$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0201-Keep-provider-backfill-flag-events-quiet-without-task-scans-ad3875.py"
        ]

    @property
    def migration_id(self):
        return "d1b4a6aa-dc6f-4191-b715-4d846ef2537b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
