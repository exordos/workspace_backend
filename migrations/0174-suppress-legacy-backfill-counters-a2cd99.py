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


def _legacy_flag_trigger_sql(suppressible):
    delete_counter_start = ""
    update_counter_start = ""
    counter_end = ""
    insert_counter_guard = ""
    if suppressible:
        delete_counter_start = """
        IF current_setting(
            'workspace.messenger_v2_suppress_legacy_flag_counters', TRUE
        ) IS DISTINCT FROM OLD.project_id::text THEN
        """
        update_counter_start = """
    IF current_setting(
        'workspace.messenger_v2_suppress_legacy_flag_counters', TRUE
    ) IS DISTINCT FROM NEW.project_id::text THEN
    """
        counter_end = """
    END IF;
        """
        insert_counter_guard = """
      AND current_setting(
              'workspace.messenger_v2_suppress_legacy_flag_counters', TRUE
          ) IS DISTINCT FROM flag.project_id::text
        """
    return f"""
CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_message_flags()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
    target_placement uuid;
    generation integer;
    target_stream_uuid uuid;
    target_topic_uuid uuid;
    source_event_uuid uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT placement.uuid, placement.stream_uuid, placement.topic_uuid
          INTO target_placement, target_stream_uuid, target_topic_uuid
        FROM messenger_message_placements AS placement
        WHERE placement.project_id = OLD.project_id
          AND (
              placement.message_uuid = OLD.uuid
              OR placement.uuid = OLD.uuid
              OR placement.legacy_public_uuid = OLD.uuid
          )
        ORDER BY placement.uuid LIMIT 1;
        IF target_placement IS NULL THEN
            RETURN OLD;
        END IF;
        DELETE FROM messenger_user_message_states
        WHERE project_id = OLD.project_id AND user_uuid = OLD.user_uuid
          AND placement_uuid = target_placement;
        {delete_counter_start}
        source_event_uuid := gen_random_uuid();
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload
        ) VALUES
        (
            messenger_uuid_v5(source_event_uuid, 'legacy-flag-delete-stream'),
            OLD.project_id, 'read_counters', 'user-stream',
            OLD.project_id::text || ':' || OLD.user_uuid::text || ':' ||
                target_stream_uuid::text,
            jsonb_build_object(
                'source_kind', 'legacy_message_state.deleted',
                'user_uuid', OLD.user_uuid,
                'stream_uuid', target_stream_uuid,
                'topic_uuid', target_topic_uuid,
                'placement_uuid', target_placement
            )
        ),
        (
            messenger_uuid_v5(source_event_uuid, 'legacy-flag-delete-topic'),
            OLD.project_id, 'read_counters', 'user-topic',
            OLD.project_id::text || ':' || OLD.user_uuid::text || ':' ||
                target_topic_uuid::text,
            jsonb_build_object(
                'source_kind', 'legacy_message_state.deleted',
                'user_uuid', OLD.user_uuid,
                'stream_uuid', target_stream_uuid,
                'topic_uuid', target_topic_uuid,
                'placement_uuid', target_placement
            )
        );
        {counter_end}
        RETURN OLD;
    END IF;
    SELECT placement.uuid, binding.membership_generation,
           placement.stream_uuid, placement.topic_uuid
      INTO target_placement, generation, target_stream_uuid, target_topic_uuid
    FROM messenger_message_placements AS placement
    JOIN messenger_stream_bindings AS binding
      ON binding.project_id = placement.project_id
     AND binding.stream_uuid = placement.stream_uuid
     AND binding.user_uuid = NEW.user_uuid AND binding.active
    WHERE placement.project_id = NEW.project_id
      AND (
          placement.message_uuid = NEW.uuid
          OR placement.uuid = NEW.uuid
          OR placement.legacy_public_uuid = NEW.uuid
      )
    ORDER BY placement.uuid LIMIT 1;
    IF target_placement IS NULL THEN
        RETURN NEW;
    END IF;
    INSERT INTO messenger_user_message_states (
        uuid, project_id, placement_uuid, user_uuid, membership_generation,
        read_at, starred, pinned, created_at, updated_at
    ) VALUES (
        messenger_uuid_v5(target_placement, NEW.user_uuid::text),
        NEW.project_id, target_placement, NEW.user_uuid, generation,
        CASE WHEN NEW.read THEN NEW.updated_at END,
        NEW.starred, NEW.pinned, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE SET
        read_at = CASE WHEN NEW.read THEN NEW.updated_at END,
        starred = NEW.starred,
        pinned = NEW.pinned,
        updated_at = NEW.updated_at;
    {update_counter_start}
    source_event_uuid := gen_random_uuid();
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    ) VALUES
    (
        messenger_uuid_v5(source_event_uuid, 'legacy-flag-stream'),
        NEW.project_id, 'read_counters', 'user-stream',
        NEW.project_id::text || ':' || NEW.user_uuid::text || ':' ||
            target_stream_uuid::text,
        jsonb_build_object(
            'source_kind', 'legacy_message_state.updated',
            'user_uuid', NEW.user_uuid,
            'stream_uuid', target_stream_uuid,
            'topic_uuid', target_topic_uuid,
            'placement_uuid', target_placement
        )
    ),
    (
        messenger_uuid_v5(source_event_uuid, 'legacy-flag-topic'),
        NEW.project_id, 'read_counters', 'user-topic',
        NEW.project_id::text || ':' || NEW.user_uuid::text || ':' ||
            target_topic_uuid::text,
        jsonb_build_object(
            'source_kind', 'legacy_message_state.updated',
            'user_uuid', NEW.user_uuid,
            'stream_uuid', target_stream_uuid,
            'topic_uuid', target_topic_uuid,
            'placement_uuid', target_placement
        )
    );
    {counter_end}
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_message_flag_inserts()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;
    INSERT INTO messenger_user_message_states (
        uuid, project_id, placement_uuid, user_uuid, membership_generation,
        read_at, starred, pinned, created_at, updated_at
    )
    SELECT messenger_uuid_v5(placement.uuid, flag.user_uuid::text),
           flag.project_id, placement.uuid, flag.user_uuid,
           binding.membership_generation,
           CASE WHEN flag.read THEN flag.updated_at END,
           flag.starred, flag.pinned, flag.created_at, flag.updated_at
    FROM inserted_legacy_message_flags AS flag
    JOIN messenger_message_placements AS placement
      ON placement.project_id = flag.project_id
     AND placement.legacy_public_uuid = flag.uuid
    JOIN messenger_stream_bindings AS binding
      ON binding.project_id = placement.project_id
     AND binding.stream_uuid = placement.stream_uuid
     AND binding.user_uuid = flag.user_uuid AND binding.active
    ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE SET
        read_at = EXCLUDED.read_at,
        starred = EXCLUDED.starred,
        pinned = EXCLUDED.pinned,
        updated_at = EXCLUDED.updated_at;
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    )
    SELECT gen_random_uuid(), flag.project_id, 'read_counters', lane.scope_kind,
           flag.project_id::text || ':' || flag.user_uuid::text || ':' ||
               CASE WHEN lane.scope_kind = 'user-stream'
                    THEN placement.stream_uuid::text
                    ELSE placement.topic_uuid::text END,
           jsonb_build_object(
               'source_kind', 'legacy_message_state.updated',
               'user_uuid', flag.user_uuid,
               'stream_uuid', placement.stream_uuid,
               'topic_uuid', placement.topic_uuid,
               'placement_uuid', placement.uuid
           )
    FROM inserted_legacy_message_flags AS flag
    JOIN messenger_message_placements AS placement
      ON placement.project_id = flag.project_id
     AND placement.legacy_public_uuid = flag.uuid
    CROSS JOIN (
        VALUES ('user-stream'::varchar), ('user-topic'::varchar)
    ) AS lane(scope_kind)
    WHERE EXISTS (
        SELECT 1 FROM messenger_stream_bindings AS binding
        WHERE binding.project_id = placement.project_id
          AND binding.stream_uuid = placement.stream_uuid
          AND binding.user_uuid = flag.user_uuid AND binding.active
    )
    {insert_counter_guard};
    RETURN NULL;
END;
$function$;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0173-accelerate-Messenger-v2-projections-8cda92.py"]

    @property
    def migration_id(self):
        return "a2cd99ae-7165-4885-9889-f7729d74e45c"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(_legacy_flag_trigger_sql(suppressible=True))

    def downgrade(self, session):
        session.execute(_legacy_flag_trigger_sql(suppressible=False))


migration_step = MigrationStep()
