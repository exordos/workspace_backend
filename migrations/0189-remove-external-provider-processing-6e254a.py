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


# Preserve canonical content; remove only integration metadata and processing state.
CLEANUP_SQL = """
SELECT pg_advisory_xact_lock(hashtextextended('workspace-read-state-schema-v1', 0));

DROP VIEW IF EXISTS "m_confirmed_external_account_access" CASCADE;
DROP VIEW IF EXISTS "m_confirmed_external_stream_access" CASCADE;
DROP VIEW IF EXISTS "m_folder_all_items_view" CASCADE;
DROP VIEW IF EXISTS "m_folder_all_view" CASCADE;
DROP VIEW IF EXISTS "m_folder_channel_items_view" CASCADE;
DROP VIEW IF EXISTS "m_folder_channels_view" CASCADE;
DROP VIEW IF EXISTS "m_folder_created_view" CASCADE;
DROP VIEW IF EXISTS "m_folder_items_created_view" CASCADE;
DROP VIEW IF EXISTS "m_folder_personal_view" CASCADE;
DROP VIEW IF EXISTS "m_folder_private_items_view" CASCADE;
DROP VIEW IF EXISTS "m_folders_view" CASCADE;
DROP VIEW IF EXISTS "m_unread_user_messages" CASCADE;
DROP VIEW IF EXISTS "m_workspace_directory_users_v1" CASCADE;
DROP VIEW IF EXISTS "m_workspace_projects_view" CASCADE;
DROP VIEW IF EXISTS "m_workspace_topic_summary_settings_view" CASCADE;
DROP VIEW IF EXISTS "m_workspace_user_messages_view" CASCADE;
DROP VIEW IF EXISTS "m_workspace_user_streams" CASCADE;
DROP VIEW IF EXISTS "m_workspace_user_streams_base_v1" CASCADE;
DROP VIEW IF EXISTS "m_workspace_user_topic_unread_counts_v1" CASCADE;
DROP VIEW IF EXISTS "m_workspace_user_topics_view" CASCADE;
DROP VIEW IF EXISTS "m_workspace_user_unread_messages_base_v1" CASCADE;
DROP VIEW IF EXISTS "m_workspace_user_unread_messages_view" CASCADE;
DROP VIEW IF EXISTS "m_workspace_visible_events" CASCADE;
DROP VIEW IF EXISTS "m_workspace_visible_events_pre_messenger_v2" CASCADE;
DROP VIEW IF EXISTS "m_workspace_visible_files_v1" CASCADE;
DROP VIEW IF EXISTS "m_workspace_visible_message_reactions_v1" CASCADE;
DROP VIEW IF EXISTS "messenger_api_message_reactions_v1" CASCADE;
DROP VIEW IF EXISTS "messenger_api_stream_bindings_v1" CASCADE;
DROP VIEW IF EXISTS "messenger_api_user_folder_items_v1" CASCADE;
DROP VIEW IF EXISTS "messenger_api_user_folders_v1" CASCADE;
DROP VIEW IF EXISTS "messenger_api_user_messages_v1" CASCADE;
DROP VIEW IF EXISTS "messenger_api_user_streams_v1" CASCADE;
DROP VIEW IF EXISTS "messenger_api_user_topics_v1" CASCADE;

DROP FUNCTION IF EXISTS public."m_external_provider_read_completion_fence_v1"() CASCADE;

DROP FUNCTION IF EXISTS public."m_external_prepare_provider_history_downgrade_v1"(batch_size integer) CASCADE;

DROP FUNCTION IF EXISTS public."messenger_v2_provider_message_uuid"(provider_metadata jsonb, provider_external_id text, fallback_uuid uuid) CASCADE;

DROP FUNCTION IF EXISTS public."m_external_provider_operation_lane_v1"() CASCADE;

DROP FUNCTION IF EXISTS public."m_external_provider_read_payload_scrub_v1"() CASCADE;

DROP FUNCTION IF EXISTS public."messenger_v2_apply_legacy_provider_identity"(legacy_project_uuid uuid, legacy_message_uuid uuid, account_uuid uuid, external_message_id text, legacy_source_name text, legacy_source jsonb, legacy_provider_metadata jsonb) CASCADE;

DROP FUNCTION IF EXISTS public."messenger_v2_repair_legacy_message_provider_identity"() CASCADE;

DROP FUNCTION IF EXISTS public."messenger_v2_repair_legacy_message_provider_identity_inserts"() CASCADE;

DROP FUNCTION IF EXISTS public."m_external_provider_read_lease_fence_v1"() CASCADE;

DROP FUNCTION IF EXISTS public."workspace_history_remember_message_delete_v1"() CASCADE;

DROP FUNCTION IF EXISTS public."workspace_reject_per_user_target_delivery_v1"() CASCADE;

CREATE OR REPLACE FUNCTION public.messenger_v2_import_legacy_message()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_canonical_uuid uuid;
    target_placement_uuid uuid;
    old_placement_uuid uuid;
    target_placement record;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT placement.message_uuid INTO target_canonical_uuid
        FROM messenger_message_placements AS placement
        WHERE placement.project_id = OLD.project_id
          AND (
              placement.message_uuid = OLD.uuid
              OR placement.uuid = OLD.uuid
              OR placement.legacy_public_uuid = OLD.uuid
          )
        ORDER BY (placement.message_uuid = OLD.uuid) DESC, placement.uuid
        LIMIT 1;
        IF target_canonical_uuid IS NULL THEN
            RETURN OLD;
        END IF;
        FOR target_placement IN
            SELECT placement.uuid, placement.stream_uuid, placement.topic_uuid
            FROM messenger_message_placements AS placement
            WHERE placement.project_id = OLD.project_id
              AND placement.message_uuid = target_canonical_uuid
            ORDER BY placement.uuid
        LOOP
            INSERT INTO messenger_domain_outbox_events (uuid, project_id, event_kind, scope_kind, scope_key, payload) VALUES (gen_random_uuid(), old.project_id, 'delivery_snapshot_event', 'message', CAST(old.project_id AS text) || ':' || CAST(target_canonical_uuid AS text), jsonb_build_object('source_kind', 'message.deleted', 'placement', jsonb_build_object('uuid', target_placement.uuid, 'stream_uuid', target_placement.stream_uuid, 'topic_uuid', target_placement.topic_uuid), 'canonical_message_uuid', target_canonical_uuid, 'message_created_at', old.created_at, 'author_uuid', old.user_uuid, 'source_name', old.source_name, 'source', old.source, 'emit_public_event', FALSE));
        END LOOP;
        UPDATE messenger_messages
        SET deleted_at = NOW()
        WHERE project_id = OLD.project_id AND uuid = target_canonical_uuid;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.project_id IS DISTINCT FROM NEW.project_id THEN
        PERFORM messenger_v2_move_canonical_message_project(
            NEW.uuid,
            OLD.project_id,
            NEW.project_id,
            NEW.stream_uuid,
            NEW.topic_uuid
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM messenger_project_users AS project_user
        WHERE project_user.project_id = NEW.project_id
          AND project_user.user_uuid = NEW.user_uuid
    ) THEN
        PERFORM messenger_v2_register_project_user(
            NEW.project_id, NEW.user_uuid
        );
    END IF;
    SELECT placement.uuid, placement.message_uuid
      INTO target_placement_uuid, target_canonical_uuid
    FROM messenger_message_placements AS placement
    WHERE placement.project_id = NEW.project_id
      AND (
          placement.message_uuid = NEW.uuid
          OR placement.uuid = NEW.uuid
          OR placement.legacy_public_uuid = NEW.uuid
      )
    ORDER BY (placement.message_uuid = NEW.uuid) DESC, placement.uuid
    LIMIT 1;
    IF target_canonical_uuid IS NULL THEN
        target_canonical_uuid := NEW.uuid;
        target_placement_uuid := messenger_uuid_v5(
            NEW.topic_uuid, lower(target_canonical_uuid::text)
        );
    END IF;
    IF TG_OP = 'UPDATE' AND (
        OLD.topic_uuid IS DISTINCT FROM NEW.topic_uuid
        OR OLD.stream_uuid IS DISTINCT FROM NEW.stream_uuid
    ) THEN
        old_placement_uuid := messenger_uuid_v5(
            OLD.topic_uuid, lower(OLD.uuid::text)
        );
        DELETE FROM messenger_message_placements
        WHERE project_id = NEW.project_id AND uuid = old_placement_uuid;
    END IF;
    INSERT INTO messenger_messages (uuid, project_id, legacy_public_uuid, author_uuid, payload, source_name, source, reaction_users, ingest_sequence, created_at, updated_at) VALUES (target_canonical_uuid, new.project_id, new.uuid, new.user_uuid, new.payload, new.source_name, new.source, new.reaction_users, COALESCE(new.ingest_sequence, nextval('messenger_messages_ingest_sequence_seq')), new.created_at, new.updated_at) ON CONFLICT (project_id, uuid) DO UPDATE SET legacy_public_uuid = COALESCE(messenger_messages.legacy_public_uuid, excluded.legacy_public_uuid), author_uuid = excluded.author_uuid, payload = excluded.payload, source_name = excluded.source_name, source = excluded.source, reaction_users = excluded.reaction_users, deleted_at = NULL, updated_at = excluded.updated_at;
    INSERT INTO messenger_message_placements (
        uuid, project_id, legacy_public_uuid, message_uuid, stream_uuid,
        topic_uuid,
        created_at, updated_at
    ) VALUES (
        target_placement_uuid, NEW.project_id, NEW.uuid,
        target_canonical_uuid,
        NEW.stream_uuid, NEW.topic_uuid, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        legacy_public_uuid = EXCLUDED.legacy_public_uuid,
        stream_uuid = EXCLUDED.stream_uuid,
        topic_uuid = EXCLUDED.topic_uuid,
        updated_at = EXCLUDED.updated_at;
    INSERT INTO messenger_user_message_bindings (
        uuid, project_id, placement_uuid, user_uuid, membership_generation,
        relation_role, visibility, permissions, created_at, updated_at
    )
    SELECT messenger_uuid_v5(target_placement_uuid, binding.user_uuid::text),
           NEW.project_id, target_placement_uuid, binding.user_uuid,
           binding.membership_generation,
           CASE WHEN binding.user_uuid = NEW.user_uuid
                THEN 'author' ELSE 'member' END,
           'visible',
           '{"read":true,"react":true,"star":true,"pin":true}'::jsonb,
           NEW.created_at, NEW.updated_at
    FROM messenger_stream_bindings AS binding
    WHERE binding.project_id = NEW.project_id
      AND binding.stream_uuid = NEW.stream_uuid AND binding.active
      AND binding.user_uuid = NEW.user_uuid
    ON CONFLICT (project_id, placement_uuid, user_uuid) DO NOTHING;
    INSERT INTO messenger_user_message_states (
        uuid, project_id, placement_uuid, user_uuid, membership_generation,
        read_at, mentioned, created_at, updated_at
    )
    SELECT messenger_uuid_v5(target_placement_uuid, binding.user_uuid::text),
           NEW.project_id, target_placement_uuid, binding.user_uuid,
           binding.membership_generation,
           CASE WHEN binding.user_uuid = NEW.user_uuid THEN NEW.updated_at END,
           POSITION(
               '](urn:user:' || lower(binding.user_uuid::text) || ')'
               IN lower(COALESCE(NEW.payload->>'content', ''))
           ) > 0,
           NEW.created_at, NEW.updated_at
    FROM messenger_stream_bindings AS binding
    WHERE binding.project_id = NEW.project_id
      AND binding.stream_uuid = NEW.stream_uuid AND binding.active
      AND binding.user_uuid = NEW.user_uuid
    ON CONFLICT (project_id, user_uuid, placement_uuid) DO NOTHING;
    IF TG_OP = 'INSERT' THEN
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload,
            created_at, updated_at
        ) VALUES (
            messenger_uuid_v5(target_placement_uuid, 'legacy-message-fanout'),
            NEW.project_id, 'fanout', 'topic',
            NEW.project_id::text || ':' || NEW.topic_uuid::text,
            jsonb_build_object(
                'source_kind', 'message.created',
                'placement_uuid', target_placement_uuid,
                'canonical_message_uuid', target_canonical_uuid,
                'audience_created_before', NEW.created_at,
                'message_created_at', NEW.created_at,
                'emit_public_event', false
            ),
            NEW.created_at AT TIME ZONE current_setting('TIMEZONE'),
            NEW.updated_at AT TIME ZONE current_setting('TIMEZONE')
        )
        ON CONFLICT (project_id, uuid) DO NOTHING;
    ELSE
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload,
            created_at, updated_at
        ) VALUES (
            gen_random_uuid(), NEW.project_id, 'content_mentions', 'topic',
            NEW.project_id::text || ':' || NEW.topic_uuid::text,
            jsonb_build_object(
                'source_kind', 'message.updated',
                'placement_uuid', target_placement_uuid,
                'canonical_message_uuid', target_canonical_uuid,
                'message_created_at', NEW.created_at,
                'emit_message_updated', false
            ),
            NOW(), NOW()
        );
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.messenger_v2_import_legacy_message_inserts()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    project_user record;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;
    FOR project_user IN
        SELECT DISTINCT message.project_id, message.user_uuid
        FROM inserted_legacy_messages AS message
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM messenger_project_users AS registered_user
            WHERE registered_user.project_id = project_user.project_id
              AND registered_user.user_uuid = project_user.user_uuid
        ) THEN
            PERFORM messenger_v2_register_project_user(
                project_user.project_id, project_user.user_uuid
            );
        END IF;
    END LOOP;
    INSERT INTO messenger_messages (uuid, project_id, legacy_public_uuid, author_uuid, payload, source_name, source, reaction_users, ingest_sequence, created_at, updated_at) SELECT message.uuid, message.project_id, message.uuid, message.user_uuid, message.payload, message.source_name, message.source, message.reaction_users, COALESCE(message.ingest_sequence, nextval('messenger_messages_ingest_sequence_seq')), message.created_at, message.updated_at FROM inserted_legacy_messages AS message ON CONFLICT (project_id, uuid) DO UPDATE SET author_uuid = excluded.author_uuid, payload = excluded.payload, source_name = excluded.source_name, source = excluded.source, reaction_users = excluded.reaction_users, deleted_at = NULL, updated_at = excluded.updated_at;
    INSERT INTO messenger_message_placements (uuid, project_id, legacy_public_uuid, message_uuid, stream_uuid, topic_uuid, created_at, updated_at) SELECT messenger_uuid_v5(message.topic_uuid, lower(CAST(message.uuid AS text))), message.project_id, message.uuid, message.uuid, message.stream_uuid, message.topic_uuid, message.created_at, message.updated_at FROM inserted_legacy_messages AS message ON CONFLICT (project_id, uuid) DO UPDATE SET legacy_public_uuid = excluded.legacy_public_uuid, stream_uuid = excluded.stream_uuid, topic_uuid = excluded.topic_uuid, updated_at = excluded.updated_at;
    INSERT INTO messenger_user_message_bindings (
        uuid, project_id, placement_uuid, user_uuid,
        membership_generation, relation_role, visibility, permissions,
        created_at, updated_at
    )
    SELECT messenger_uuid_v5(placement.uuid, message.user_uuid::text),
           message.project_id, placement.uuid, message.user_uuid,
           binding.membership_generation, 'author', 'visible',
           '{"read":true,"react":true,"star":true,"pin":true}'::jsonb,
           message.created_at, message.updated_at
    FROM inserted_legacy_messages AS message
    JOIN messenger_message_placements AS placement
      ON placement.project_id = message.project_id
     AND placement.legacy_public_uuid = message.uuid
    JOIN messenger_stream_bindings AS binding
      ON binding.project_id = message.project_id
     AND binding.stream_uuid = message.stream_uuid
     AND binding.user_uuid = message.user_uuid AND binding.active
    ON CONFLICT (project_id, placement_uuid, user_uuid) DO NOTHING;
    INSERT INTO messenger_user_message_states (
        uuid, project_id, placement_uuid, user_uuid,
        membership_generation, read_at, mentioned, created_at, updated_at
    )
    SELECT messenger_uuid_v5(placement.uuid, message.user_uuid::text),
           message.project_id, placement.uuid, message.user_uuid,
           binding.membership_generation, message.updated_at,
           POSITION(
               '](urn:user:' || lower(message.user_uuid::text) || ')'
               IN lower(COALESCE(message.payload->>'content', ''))
           ) > 0,
           message.created_at, message.updated_at
    FROM inserted_legacy_messages AS message
    JOIN messenger_message_placements AS placement
      ON placement.project_id = message.project_id
     AND placement.legacy_public_uuid = message.uuid
    JOIN messenger_stream_bindings AS binding
      ON binding.project_id = message.project_id
     AND binding.stream_uuid = message.stream_uuid
     AND binding.user_uuid = message.user_uuid AND binding.active
    ON CONFLICT (project_id, user_uuid, placement_uuid) DO NOTHING;
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload,
        created_at, updated_at
    )
    SELECT messenger_uuid_v5(placement.uuid, 'legacy-message-fanout'),
           message.project_id, 'fanout', 'topic',
           message.project_id::text || ':' || message.topic_uuid::text,
           jsonb_build_object(
               'source_kind', 'message.created',
               'placement_uuid', placement.uuid,
               'canonical_message_uuid', placement.message_uuid,
               'audience_created_before', message.created_at,
               'message_created_at', message.created_at,
               'emit_public_event', false
           ),
           message.created_at AT TIME ZONE current_setting('TIMEZONE'),
           message.updated_at AT TIME ZONE current_setting('TIMEZONE')
    FROM inserted_legacy_messages AS message
    JOIN messenger_message_placements AS placement
      ON placement.project_id = message.project_id
     AND placement.legacy_public_uuid = message.uuid
    ON CONFLICT (project_id, uuid) DO NOTHING;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION public.messenger_v2_import_legacy_stream()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        UPDATE messenger_streams
        SET deleted_at = NOW()
        WHERE project_id = OLD.project_id AND uuid = OLD.uuid;
        INSERT INTO messenger_domain_outbox_events (uuid, project_id, event_kind, scope_kind, scope_key, payload) VALUES (gen_random_uuid(), old.project_id, 'delivery_snapshot_event', 'resource', CAST(old.project_id AS text) || ':stream:' || CAST(old.uuid AS text), jsonb_build_object('source_kind', 'stream.deleted', 'stream_uuid', old.uuid, 'source_name', old.source_name, 'source', old.source, 'all_recipients', TRUE, 'private', old.private, 'emit_public_event', FALSE));
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.project_id IS DISTINCT FROM NEW.project_id THEN
        PERFORM messenger_v2_move_canonical_stream_project(
            NEW.uuid, OLD.project_id, NEW.project_id
        );
    END IF;
    PERFORM messenger_v2_register_project_user(NEW.project_id, NEW.user_uuid);
    PERFORM messenger_v2_register_project_user(
        NEW.project_id, NEW.direct_user_uuid
    );
    INSERT INTO messenger_streams (uuid, project_id, name, description, owner_uuid, source_name, source, invite_only, announce, direct_user_uuid, private, is_archived, private_index, color, default_topic_uuid, created_at, updated_at) VALUES (new.uuid, new.project_id, new.name, new.description, new.user_uuid, new.source_name, new.source, new.invite_only, new.announce, new.direct_user_uuid, new.private, new.is_archived, new.private_index, new.color, CASE WHEN EXISTS (SELECT 1 FROM messenger_topics WHERE project_id = new.project_id AND uuid = new.default_topic_uuid) THEN new.default_topic_uuid ELSE NULL END, new.created_at, new.updated_at) ON CONFLICT (project_id, uuid) DO UPDATE SET name = excluded.name, description = excluded.description, owner_uuid = excluded.owner_uuid, source_name = excluded.source_name, source = excluded.source, invite_only = excluded.invite_only, announce = excluded.announce, direct_user_uuid = excluded.direct_user_uuid, private = excluded.private, is_archived = excluded.is_archived, private_index = excluded.private_index, color = excluded.color, default_topic_uuid = excluded.default_topic_uuid, deleted_at = NULL, updated_at = excluded.updated_at;
    INSERT INTO messenger_domain_outbox_events (uuid, project_id, event_kind, scope_kind, scope_key, payload) VALUES (gen_random_uuid(), new.project_id, 'delivery_snapshot_event', 'resource', CAST(new.project_id AS text) || ':stream:' || CAST(new.uuid AS text), jsonb_build_object('source_kind', CASE WHEN tg_op = 'INSERT' THEN 'stream.created' ELSE 'stream.updated' END, 'resource_kind', 'stream', 'resource_uuid', new.uuid, 'stream_uuid', new.uuid, 'emit_public_event', FALSE));
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.messenger_v2_import_legacy_topic()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    done_value boolean;
    summary_placement uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        UPDATE messenger_streams
        SET default_topic_uuid = NULL, updated_at = NOW()
        WHERE project_id = OLD.project_id AND uuid = OLD.stream_uuid
          AND default_topic_uuid = OLD.uuid;
        UPDATE messenger_topics
        SET deleted_at = NOW()
        WHERE project_id = OLD.project_id AND uuid = OLD.uuid;
        INSERT INTO messenger_domain_outbox_events (uuid, project_id, event_kind, scope_kind, scope_key, payload) VALUES (gen_random_uuid(), old.project_id, 'delivery_snapshot_event', 'topic', CAST(old.project_id AS text) || ':' || CAST(old.uuid AS text), jsonb_build_object('source_kind', 'topic.deleted', 'topic_uuid', old.uuid, 'stream_uuid', old.stream_uuid, 'source_name', old.source_name, 'source', old.source, 'emit_public_event', FALSE));
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.project_id IS DISTINCT FROM NEW.project_id THEN
        RETURN NEW;
    END IF;
    SELECT COALESCE(bool_or(is_done), false) INTO done_value
    FROM m_workspace_user_topic_flags
    WHERE project_id = NEW.project_id AND uuid = NEW.uuid;
    SELECT placement.uuid INTO summary_placement
    FROM messenger_message_placements AS placement
    WHERE placement.project_id = NEW.project_id
      AND placement.message_uuid = NEW.summary_last_message_uuid
    ORDER BY placement.uuid
    LIMIT 1;
    INSERT INTO messenger_topics (uuid, project_id, stream_uuid, name, color, source_name, source, summary, summary_last_message_uuid, summary_enabled, summary_system_prompt, summary_reasoning_effort, is_done, created_at, updated_at) VALUES (new.uuid, new.project_id, new.stream_uuid, new.name, new.color, new.source_name, new.source, new.summary, summary_placement, new.summary_enabled, new.summary_system_prompt, new.summary_reasoning_effort, done_value, new.created_at, new.updated_at) ON CONFLICT (project_id, uuid) DO UPDATE SET stream_uuid = excluded.stream_uuid, name = excluded.name, color = excluded.color, source_name = excluded.source_name, source = excluded.source, summary = excluded.summary, summary_last_message_uuid = excluded.summary_last_message_uuid, summary_enabled = excluded.summary_enabled, summary_system_prompt = excluded.summary_system_prompt, summary_reasoning_effort = excluded.summary_reasoning_effort, is_done = excluded.is_done, deleted_at = NULL, updated_at = excluded.updated_at;
    UPDATE messenger_streams AS canonical
    SET default_topic_uuid = legacy.default_topic_uuid,
        updated_at = GREATEST(canonical.updated_at, NEW.updated_at)
    FROM m_workspace_streams AS legacy
    WHERE canonical.project_id = NEW.project_id
      AND canonical.uuid = NEW.stream_uuid
      AND legacy.project_id = NEW.project_id
      AND legacy.uuid = NEW.stream_uuid
      AND legacy.default_topic_uuid = NEW.uuid;
    INSERT INTO messenger_user_topic_bindings (
        uuid, project_id, user_uuid, topic_uuid, notification_mode,
        created_at, updated_at
    )
    SELECT messenger_uuid_v5(NEW.uuid, binding.user_uuid::text),
           NEW.project_id, binding.user_uuid, NEW.uuid, 'default',
           NEW.created_at, NEW.updated_at
    FROM messenger_stream_bindings AS binding
    WHERE binding.project_id = NEW.project_id
      AND binding.stream_uuid = NEW.stream_uuid AND binding.active
    ON CONFLICT (project_id, user_uuid, topic_uuid) DO NOTHING;
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    ) VALUES (
        gen_random_uuid(), NEW.project_id, 'topic_state_projection',
        'topic', NEW.project_id::text || ':' || NEW.uuid::text,
        jsonb_build_object(
            'source_kind', CASE WHEN TG_OP = 'INSERT'
                                THEN 'topic.created'
                                ELSE 'topic.updated' END,
            'topic_uuid', NEW.uuid,
            'emit_public_event', false
        )
    );
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.messenger_v2_mirror_message_update_to_legacy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        DELETE FROM m_workspace_messages AS legacy
        USING messenger_message_placements AS placement
        WHERE placement.project_id = OLD.project_id
          AND placement.message_uuid = OLD.uuid
          AND legacy.uuid = COALESCE(
              placement.legacy_public_uuid, placement.uuid
        );
        RETURN OLD;
    END IF;
    IF NEW.deleted_at IS NOT NULL THEN
        DELETE FROM m_workspace_messages AS legacy
        USING messenger_message_placements AS placement
        WHERE placement.project_id = NEW.project_id
          AND placement.message_uuid = NEW.uuid
          AND legacy.uuid = COALESCE(
              placement.legacy_public_uuid, placement.uuid
          );
        RETURN NEW;
    END IF;
    UPDATE m_workspace_messages AS legacy SET user_uuid = new.author_uuid, payload = new.payload, source_name = new.source_name, source = new.source, reaction_users = new.reaction_users, updated_at = new.updated_at FROM messenger_message_placements AS placement WHERE placement.project_id = new.project_id AND placement.message_uuid = new.uuid AND legacy.uuid = COALESCE(placement.legacy_public_uuid, placement.uuid);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.messenger_v2_mirror_placement_to_legacy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_row messenger_messages%ROWTYPE;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT * INTO source_row
        FROM messenger_messages
        WHERE project_id = OLD.project_id AND uuid = OLD.message_uuid;
        DELETE FROM m_workspace_messages
        WHERE uuid = COALESCE(OLD.legacy_public_uuid, OLD.uuid);
        RETURN OLD;
    END IF;
    SELECT * INTO source_row
    FROM messenger_messages
    WHERE project_id = NEW.project_id AND uuid = NEW.message_uuid;
    INSERT INTO m_workspace_messages (uuid, project_id, stream_uuid, user_uuid, payload, created_at, updated_at, topic_uuid, source_name, source, reaction_users) VALUES (COALESCE(new.legacy_public_uuid, new.uuid), new.project_id, new.stream_uuid, source_row.author_uuid, source_row.payload, source_row.created_at, source_row.updated_at, new.topic_uuid, source_row.source_name, source_row.source, source_row.reaction_users) ON CONFLICT (uuid) DO UPDATE SET project_id = excluded.project_id, stream_uuid = excluded.stream_uuid, user_uuid = excluded.user_uuid, payload = excluded.payload, updated_at = excluded.updated_at, topic_uuid = excluded.topic_uuid, source_name = excluded.source_name, source = excluded.source, reaction_users = excluded.reaction_users;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.messenger_v2_mirror_stream_to_legacy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        DELETE FROM m_workspace_streams WHERE uuid = OLD.uuid;
        RETURN OLD;
    END IF;
    IF NEW.deleted_at IS NOT NULL THEN
        DELETE FROM m_workspace_streams WHERE uuid = NEW.uuid;
        RETURN NEW;
    END IF;
    INSERT INTO m_workspace_streams (uuid, project_id, name, description, source_name, source, user_uuid, created_at, updated_at, invite_only, announce, private, direct_user_uuid, private_index, is_archived, color, default_topic_uuid) VALUES (new.uuid, new.project_id, new.name, new.description, new.source_name, new.source, new.owner_uuid, new.created_at, new.updated_at, new.invite_only, new.announce, new.private, new.direct_user_uuid, new.private_index, new.is_archived, new.color, new.default_topic_uuid) ON CONFLICT (uuid) DO UPDATE SET project_id = excluded.project_id, name = excluded.name, description = excluded.description, source_name = excluded.source_name, source = excluded.source, user_uuid = excluded.user_uuid, updated_at = excluded.updated_at, invite_only = excluded.invite_only, announce = excluded.announce, private = excluded.private, direct_user_uuid = excluded.direct_user_uuid, private_index = excluded.private_index, is_archived = excluded.is_archived, color = excluded.color, default_topic_uuid = excluded.default_topic_uuid;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.messenger_v2_mirror_topic_to_legacy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    legacy_summary_uuid uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        DELETE FROM m_workspace_stream_topics WHERE uuid = OLD.uuid;
        RETURN OLD;
    END IF;
    IF NEW.deleted_at IS NOT NULL OR EXISTS (
        SELECT 1 FROM messenger_streams AS stream
        WHERE stream.project_id = NEW.project_id
          AND stream.uuid = NEW.stream_uuid
          AND stream.deleted_at IS NOT NULL
    ) THEN
        DELETE FROM m_workspace_stream_topics WHERE uuid = NEW.uuid;
        RETURN NEW;
    END IF;
    SELECT COALESCE(message.legacy_public_uuid, placement.uuid)
    INTO legacy_summary_uuid
    FROM messenger_message_placements AS placement
    JOIN messenger_messages AS message
      ON message.project_id = placement.project_id
     AND message.uuid = placement.message_uuid
    WHERE placement.project_id = NEW.project_id
      AND placement.uuid = NEW.summary_last_message_uuid;
    INSERT INTO m_workspace_stream_topics (uuid, project_id, stream_uuid, name, color, source_name, source, summary, summary_last_message_uuid, summary_enabled, summary_system_prompt, summary_reasoning_effort, created_at, updated_at) VALUES (new.uuid, new.project_id, new.stream_uuid, new.name, new.color, new.source_name, new.source, new.summary, legacy_summary_uuid, new.summary_enabled, new.summary_system_prompt, new.summary_reasoning_effort, new.created_at, new.updated_at) ON CONFLICT (uuid) DO UPDATE SET project_id = excluded.project_id, stream_uuid = excluded.stream_uuid, name = excluded.name, color = excluded.color, source_name = excluded.source_name, source = excluded.source, summary = excluded.summary, summary_last_message_uuid = excluded.summary_last_message_uuid, summary_enabled = excluded.summary_enabled, summary_system_prompt = excluded.summary_system_prompt, summary_reasoning_effort = excluded.summary_reasoning_effort, updated_at = excluded.updated_at;
    UPDATE m_workspace_user_topic_flags
    SET is_done = NEW.is_done, updated_at = NEW.updated_at
    WHERE project_id = NEW.project_id AND uuid = NEW.uuid;
    RETURN NEW;
END;
$$;


UPDATE m_workspace_streams SET source_name = 'native', source = '{"kind":"native"}'::jsonb WHERE source_name <> 'native' OR source IS DISTINCT FROM '{"kind":"native"}'::jsonb;

UPDATE m_workspace_stream_topics SET source_name = 'native', source = '{"kind":"native"}'::jsonb WHERE source_name <> 'native' OR source IS DISTINCT FROM '{"kind":"native"}'::jsonb;

UPDATE m_workspace_messages SET source_name = 'native', source = '{"kind":"native"}'::jsonb WHERE source_name <> 'native' OR source IS DISTINCT FROM '{"kind":"native"}'::jsonb;

UPDATE messenger_streams SET source_name = 'native', source = '{"kind":"native"}'::jsonb WHERE source_name <> 'native' OR source IS DISTINCT FROM '{"kind":"native"}'::jsonb;

UPDATE messenger_topics SET source_name = 'native', source = '{"kind":"native"}'::jsonb WHERE source_name <> 'native' OR source IS DISTINCT FROM '{"kind":"native"}'::jsonb;

UPDATE messenger_messages SET source_name = 'native', source = '{"kind":"native"}'::jsonb WHERE source_name <> 'native' OR source IS DISTINCT FROM '{"kind":"native"}'::jsonb;

UPDATE workspace_streams SET source_name = 'native', source = '{"kind":"native"}'::jsonb WHERE source_name <> 'native' OR source IS DISTINCT FROM '{"kind":"native"}'::jsonb;

-- Preserve imported user UUIDs and contact data without changing IAM email
-- uniqueness or claiming that those records are IAM identities.
UPDATE m_workspace_users SET source = 'native' WHERE source <> 'iam';

DROP TABLE IF EXISTS "m_external_account_user_syncs" CASCADE;

DROP TABLE IF EXISTS "m_external_accounts" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_control_instances_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_desired_changes_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_desired_resources_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_file_transfers_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_heartbeats_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_instances_v2" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_mail_cursors_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_mail_lanes_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_mail_outbox_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_mail_pending_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_mail_quarantine_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_mail_records_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_observed_reports_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_snapshot_resources_v2" CASCADE;

DROP TABLE IF EXISTS "m_external_bridge_snapshots_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_chats_v2" CASCADE;

DROP TABLE IF EXISTS "m_external_credentials_v2" CASCADE;

DROP TABLE IF EXISTS "m_external_history_files_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_history_imports_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_history_message_hashes_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_history_notifications_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_history_scopes_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_history_tombstones_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_operations_v2" CASCADE;

DROP TABLE IF EXISTS "m_external_projection_transitions_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_provider_events_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_provider_identity_links_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_provider_operation_results_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_provider_operations_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_provider_policies_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_provider_read_candidate_chunks_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_provider_read_candidate_packs_v1" CASCADE;

DROP TABLE IF EXISTS "m_external_provider_read_snapshots_v1" CASCADE;

DROP TABLE IF EXISTS "m_workspace_external_chat_membership_revocations" CASCADE;

DROP TABLE IF EXISTS "m_zulip_event_queue_states" CASCADE;

DROP TABLE IF EXISTS "m_zulip_history_sync_tasks" CASCADE;

DROP TABLE IF EXISTS "m_zulip_outbound_event_states" CASCADE;

DROP TABLE IF EXISTS "m_zulip_processed_entities" CASCADE;

DROP TABLE IF EXISTS "messenger_provider_file_cleanup_tasks" CASCADE;

ALTER TABLE "m_workspace_files" DROP COLUMN "provider_uuid" CASCADE;

ALTER TABLE "m_workspace_files" DROP COLUMN "external_account_uuid" CASCADE;

ALTER TABLE "m_workspace_message_reactions" DROP COLUMN "provider_uuid" CASCADE;

ALTER TABLE "m_workspace_message_reactions" DROP COLUMN "external_account_uuid" CASCADE;

ALTER TABLE "m_workspace_message_reactions" DROP COLUMN "provider_external_id" CASCADE;

ALTER TABLE "m_workspace_message_reactions" DROP COLUMN "delivery_status" CASCADE;

ALTER TABLE "m_workspace_message_reactions" DROP COLUMN "delivery_error" CASCADE;

ALTER TABLE "m_workspace_message_reactions" DROP COLUMN "delivery_updated_at" CASCADE;

ALTER TABLE "m_workspace_message_reactions" DROP COLUMN "provider_metadata" CASCADE;

ALTER TABLE "m_workspace_message_reactions" DROP COLUMN "delivery_metadata" CASCADE;

ALTER TABLE "m_workspace_messages" DROP COLUMN "provider_uuid" CASCADE;

ALTER TABLE "m_workspace_messages" DROP COLUMN "external_account_uuid" CASCADE;

ALTER TABLE "m_workspace_messages" DROP COLUMN "provider_external_id" CASCADE;

ALTER TABLE "m_workspace_messages" DROP COLUMN "delivery_status" CASCADE;

ALTER TABLE "m_workspace_messages" DROP COLUMN "delivery_error" CASCADE;

ALTER TABLE "m_workspace_messages" DROP COLUMN "delivery_updated_at" CASCADE;

ALTER TABLE "m_workspace_messages" DROP COLUMN "provider_metadata" CASCADE;

ALTER TABLE "m_workspace_messages" DROP COLUMN "delivery_metadata" CASCADE;

ALTER TABLE "m_workspace_stream_topics" DROP COLUMN "provider_uuid" CASCADE;

ALTER TABLE "m_workspace_stream_topics" DROP COLUMN "external_account_uuid" CASCADE;

ALTER TABLE "m_workspace_stream_topics" DROP COLUMN "provider_external_id" CASCADE;

ALTER TABLE "m_workspace_stream_topics" DROP COLUMN "delivery_status" CASCADE;

ALTER TABLE "m_workspace_stream_topics" DROP COLUMN "delivery_error" CASCADE;

ALTER TABLE "m_workspace_stream_topics" DROP COLUMN "delivery_updated_at" CASCADE;

ALTER TABLE "m_workspace_stream_topics" DROP COLUMN "provider_metadata" CASCADE;

ALTER TABLE "m_workspace_stream_topics" DROP COLUMN "delivery_metadata" CASCADE;

ALTER TABLE "m_workspace_streams" DROP COLUMN "provider_uuid" CASCADE;

ALTER TABLE "m_workspace_streams" DROP COLUMN "external_account_uuid" CASCADE;

ALTER TABLE "m_workspace_streams" DROP COLUMN "provider_external_id" CASCADE;

ALTER TABLE "m_workspace_streams" DROP COLUMN "delivery_status" CASCADE;

ALTER TABLE "m_workspace_streams" DROP COLUMN "delivery_error" CASCADE;

ALTER TABLE "m_workspace_streams" DROP COLUMN "delivery_updated_at" CASCADE;

ALTER TABLE "m_workspace_streams" DROP COLUMN "provider_metadata" CASCADE;

ALTER TABLE "m_workspace_streams" DROP COLUMN "delivery_metadata" CASCADE;

ALTER TABLE "m_workspace_users" DROP COLUMN "provider_uuid" CASCADE;

ALTER TABLE "m_workspace_users" DROP COLUMN "external_account_uuid" CASCADE;

ALTER TABLE "m_workspace_users" DROP COLUMN "provider_external_id" CASCADE;

ALTER TABLE "messenger_messages" DROP COLUMN "provider_uuid" CASCADE;

ALTER TABLE "messenger_messages" DROP COLUMN "external_account_uuid" CASCADE;

ALTER TABLE "messenger_messages" DROP COLUMN "provider_external_id" CASCADE;

ALTER TABLE "messenger_messages" DROP COLUMN "provider_realm_uuid" CASCADE;

ALTER TABLE "messenger_messages" DROP COLUMN "provider_message_id" CASCADE;

ALTER TABLE "messenger_messages" DROP COLUMN "provider" CASCADE;

ALTER TABLE "messenger_messages" DROP COLUMN "delivery" CASCADE;

ALTER TABLE "messenger_streams" DROP COLUMN "provider" CASCADE;

ALTER TABLE "messenger_streams" DROP COLUMN "delivery" CASCADE;

ALTER TABLE "messenger_topics" DROP COLUMN "provider" CASCADE;

ALTER TABLE "messenger_topics" DROP COLUMN "delivery" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "credential_present" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "status" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "live_ready" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "capabilities" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "safe_error" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "desired_generation" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "applied_generation" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "last_progress_at" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "provider_realm_uuid" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "provider_owner_user_id" CASCADE;

ALTER TABLE m_external_accounts_v2 DROP COLUMN "projection_reset_generation" CASCADE;

ALTER TABLE m_external_accounts_v2 ALTER COLUMN provider DROP DEFAULT;

ALTER TABLE m_external_accounts_v2 DROP CONSTRAINT m_external_accounts_v2_provider_check;
ALTER TABLE m_external_accounts_v2 ALTER COLUMN provider TYPE varchar(64);
ALTER TABLE m_external_accounts_v2 ADD CONSTRAINT m_external_accounts_v2_provider_check CHECK (length(provider) >= 1);

ALTER TABLE messenger_streams DROP CONSTRAINT messenger_streams_source_name_check;
ALTER TABLE messenger_streams ADD CONSTRAINT messenger_streams_source_name_check CHECK (source_name = 'native');
ALTER TABLE messenger_topics DROP CONSTRAINT messenger_topics_source_name_check;
ALTER TABLE messenger_topics ADD CONSTRAINT messenger_topics_source_name_check CHECK (source_name = 'native');
ALTER TABLE messenger_messages DROP CONSTRAINT messenger_messages_source_name_check;
ALTER TABLE messenger_messages ADD CONSTRAINT messenger_messages_source_name_check CHECK (source_name = 'native');
ALTER TABLE workspace_streams ADD CONSTRAINT workspace_streams_source_name_check CHECK (source_name = 'native');

UPDATE m_external_accounts_v2 SET settings = settings - ARRAY['kind','selection_mode','history_depth','default_project_id'];

CREATE FUNCTION workspace_remove_provider_metadata(value jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $body$
DECLARE result jsonb; item record;
BEGIN
    IF jsonb_typeof(value) = 'array' THEN
        SELECT COALESCE(jsonb_agg(workspace_remove_provider_metadata(element)), '[]'::jsonb)
        INTO result FROM jsonb_array_elements(value) AS element;
        RETURN result;
    ELSIF jsonb_typeof(value) <> 'object' THEN
        RETURN value;
    END IF;
    result := '{}'::jsonb;
    FOR item IN SELECT key, val FROM jsonb_each(value) AS pairs(key, val) LOOP
        IF item.key = ANY(ARRAY['provider','delivery','provider_uuid',
            'external_account_uuid','provider_external_id','provider_metadata',
            'delivery_metadata','delivery_status','delivery_error',
            'delivery_updated_at','provider_realm_uuid','provider_message_id']) THEN
            CONTINUE;
        ELSIF item.key IN ('source_name', 'old_source_name') THEN
            result := result || jsonb_build_object(item.key, 'native');
        ELSIF item.key IN ('source', 'old_source') AND jsonb_typeof(item.val) = 'object' THEN
            result := result || jsonb_build_object(item.key, jsonb_build_object('kind','native'));
        ELSE
            result := result || jsonb_build_object(item.key, workspace_remove_provider_metadata(item.val));
        END IF;
    END LOOP;
    RETURN result;
END;
$body$;

DELETE FROM m_workspace_events WHERE object_type IN ('external_chat', 'external_operation');

UPDATE m_workspace_events SET payload = jsonb_set(payload, '{snapshot}',
    (payload->'snapshot') - ARRAY['credential_present','status','live_ready',
        'capabilities','safe_error','desired_generation','applied_generation',
        'last_progress_at','provider_realm_uuid','provider_owner_user_id',
        'projection_reset_generation'], FALSE)
WHERE object_type = 'external_account' AND payload ? 'snapshot';

UPDATE m_workspace_events SET payload = jsonb_set(payload, '{snapshot,settings}',
    (payload #> '{snapshot,settings}') - ARRAY['kind','selection_mode','history_depth','default_project_id'], FALSE)
WHERE object_type = 'external_account' AND (payload->'snapshot') ? 'settings';

UPDATE m_workspace_events SET payload = workspace_remove_provider_metadata(payload) WHERE object_type <> 'external_account' AND payload IS DISTINCT FROM workspace_remove_provider_metadata(payload);

UPDATE m_workspace_broadcast_message_events_v1 SET payload = workspace_remove_provider_metadata(payload) WHERE payload IS DISTINCT FROM workspace_remove_provider_metadata(payload);

UPDATE m_workspace_event_recipient_payloads_v1 SET payload = workspace_remove_provider_metadata(payload) WHERE payload IS DISTINCT FROM workspace_remove_provider_metadata(payload);

UPDATE messenger_domain_outbox_events SET payload = workspace_remove_provider_metadata(payload) WHERE payload IS DISTINCT FROM workspace_remove_provider_metadata(payload);

UPDATE messenger_projection_tasks SET payload = workspace_remove_provider_metadata(payload) WHERE payload IS DISTINCT FROM workspace_remove_provider_metadata(payload);

DROP FUNCTION workspace_remove_provider_metadata(jsonb);

UPDATE m_workspace_event_cursors SET epoch_generation = gen_random_uuid();

CREATE VIEW m_workspace_directory_users_v1
  AS SELECT uuid
          , created_at
          , updated_at
          , username
          , source
          , status
          , first_name
          , last_name
          , email
          , last_ping_at
          , status_emoji
          , status_text
          , avatar
     FROM m_workspace_users AS "user"
     WHERE TRUE;

CREATE VIEW m_workspace_projects_view
  AS SELECT m_workspace_stream_bindings.project_id
     FROM m_workspace_stream_bindings

     UNION

     SELECT m_workspace_events.project_id
     FROM m_workspace_events;

CREATE VIEW m_workspace_topic_summary_settings_view
  AS SELECT project_settings.project_id
          , global_settings.enabled AS global_enabled
          , project_settings.enabled AS project_enabled
     FROM m_workspace_topic_summary_project_settings AS project_settings
          CROSS JOIN m_workspace_topic_summary_global_settings AS global_settings
     WHERE global_settings.singleton = TRUE;

CREATE VIEW m_workspace_user_messages_view
  AS SELECT message.uuid
          , message.stream_uuid
          , message.user_uuid AS author_uuid
          , message.topic_uuid
          , message.payload
          , message.created_at
          , message.updated_at
          , binding.user_uuid
          , message.project_id
          , CASE
              WHEN CAST(COALESCE(project.mode
                               , CAST('legacy' AS varchar)) AS text) = ANY(CAST(ARRAY[CAST('compact' AS varchar)
                                                                                    , CAST('rollback' AS varchar)] AS text[]))
                THEN COALESCE(get_bit(chunk.read_bits
                                    , CAST(message.ingest_sequence % CAST(4096 AS bigint) AS integer))
                            , 0) = 1
              ELSE COALESCE(flags.read, FALSE)
            END AS read
          , COALESCE(flags.pinned, FALSE) AS pinned
          , COALESCE(flags.starred, FALSE) AS starred
          , message.user_uuid = binding.user_uuid AS is_own
          , COALESCE((SELECT jsonb_object_agg(reaction_counts.emoji_name, reaction_counts.reaction_count) AS jsonb_object_agg
                      FROM (SELECT reaction.emoji_name
                                 , count(*) AS reaction_count
                            FROM m_workspace_message_reactions AS reaction
                            WHERE reaction.project_id = message.project_id
                              AND reaction.message_uuid = message.uuid
                            GROUP BY reaction.emoji_name) AS reaction_counts)
                   , CAST('{}' AS jsonb)) AS reactions
          , message.source_name
          , message.source
          , CASE
              WHEN CAST(COALESCE(project.mode
                               , CAST('legacy' AS varchar)) AS text) = ANY(CAST(ARRAY[CAST('compact' AS varchar)
                                                                                    , CAST('rollback' AS varchar)] AS text[]))
                THEN mention.message_uuid IS NOT NULL
              ELSE position(CAST('](' AS text) || CAST('urn:user:' AS text) || lower(CAST(binding.user_uuid AS text)) || CAST(')' AS text) IN lower(COALESCE(message.payload ->> CAST('content' AS text)
                                                                                                                                                           , CAST('' AS text)))) > 0
            END AS mentioned
          , message.reaction_users
     FROM m_workspace_messages AS message
          INNER JOIN m_workspace_stream_bindings AS binding ON binding.stream_uuid = message.stream_uuid
                                                           AND binding.project_id = message.project_id
          INNER JOIN m_workspace_streams AS stream ON stream.uuid = message.stream_uuid
                                                  AND stream.project_id = message.project_id
          LEFT JOIN m_workspace_read_state_projects_v1 AS project ON project.project_id = message.project_id
          LEFT JOIN m_workspace_user_message_flags AS flags ON flags.uuid = message.uuid
                                                           AND flags.user_uuid = binding.user_uuid
                                                           AND flags.project_id = message.project_id
          LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk ON chunk.user_uuid = binding.user_uuid
                                                            AND chunk.chunk_number = (message.ingest_sequence / 4096)
          LEFT JOIN m_workspace_message_mentions_v1 AS mention ON mention.message_uuid = message.uuid
                                                              AND mention.user_uuid = binding.user_uuid
     WHERE TRUE;

CREATE VIEW m_workspace_user_topic_unread_counts_v1
  AS WITH legacy_counts AS (
            SELECT unread.stream_uuid
                 , unread.topic_uuid
                 , unread.user_uuid
                 , unread.project_id
                 , count(*) AS unread_count
                 , count(*) FILTER (WHERE unread.active) AS active_unread_count
            FROM (SELECT message.uuid AS message_uuid
                       , message.stream_uuid
                       , message.topic_uuid
                       , message_flags.user_uuid
                       , message_flags.project_id
                       , position(CAST('](' AS text) || CAST('urn:user:' AS text) || lower(CAST(binding.user_uuid AS text)) || CAST(')' AS text) IN lower(COALESCE(message.payload ->> CAST('content' AS text)
                                                                                                                                                                 , CAST('' AS text)))) > 0 AS mentioned
                       , CASE COALESCE(topic_flags.notification_mode
                                     , CAST('default' AS varchar))
                           WHEN CAST('mute' AS text)
                             THEN FALSE
                           WHEN CAST('follow' AS text)
                             THEN TRUE
                           WHEN CAST('unmute' AS text)
                             THEN position(CAST('](' AS text) || CAST('urn:user:' AS text) || lower(CAST(binding.user_uuid AS text)) || CAST(')' AS text) IN lower(COALESCE(message.payload ->> CAST('content' AS text)
                                                                                                                                                                          , CAST('' AS text)))) > 0
                           ELSE CASE binding.notification_mode
                                  WHEN CAST('all_messages' AS text)
                                    THEN TRUE
                                  WHEN CAST('mentions_only' AS text)
                                    THEN position(CAST('](' AS text) || CAST('urn:user:' AS text) || lower(CAST(binding.user_uuid AS text)) || CAST(')' AS text) IN lower(COALESCE(message.payload ->> CAST('content' AS text)
                                                                                                                                                                                 , CAST('' AS text)))) > 0
                                  ELSE FALSE
                                END
                         END AS active
                  FROM m_workspace_user_message_flags AS message_flags
                       INNER JOIN LATERAL (SELECT TRUE AS allowed
                                           WHERE NOT EXISTS (SELECT 1
                                                             FROM m_workspace_read_state_projects_v1 AS project
                                                             WHERE project.project_id = message_flags.project_id
                                                               AND CAST(project.mode AS text) = ANY(CAST(ARRAY[CAST('compact' AS varchar)
                                                                                                             , CAST('rollback' AS varchar)] AS text[])))
                                           OFFSET 0) AS legacy_mode ON TRUE
                       INNER JOIN LATERAL (SELECT stored_message.uuid
                                                , stored_message.stream_uuid
                                                , stored_message.topic_uuid
                                                , stored_message.project_id
                                                , stored_message.payload
                                           FROM m_workspace_messages AS stored_message
                                           WHERE stored_message.uuid = message_flags.uuid
                                             AND stored_message.project_id = message_flags.project_id
                                             AND legacy_mode.allowed
                                           OFFSET 0) AS message ON TRUE
                       INNER JOIN m_workspace_stream_bindings AS binding ON binding.stream_uuid = message.stream_uuid
                                                                        AND binding.project_id = message.project_id
                                                                        AND binding.user_uuid = message_flags.user_uuid
                       LEFT JOIN m_workspace_user_topic_flags AS topic_flags ON topic_flags.uuid = message.topic_uuid
                                                                            AND topic_flags.project_id = message.project_id
                                                                            AND topic_flags.user_uuid = binding.user_uuid
                  WHERE message_flags.read = FALSE) AS unread
            GROUP BY unread.stream_uuid, unread.topic_uuid, unread.user_uuid, unread.project_id
          )
        , compact_counts AS (
            SELECT stats.stream_uuid
                 , stats.topic_uuid
                 , binding.user_uuid
                 , stats.project_id
                 , GREATEST(stats.message_count - COALESCE(reads.read_count
                                                         , CAST(0 AS bigint))
                          , CAST(0 AS bigint)) AS unread_count
                 , CASE COALESCE(topic_flags.notification_mode
                               , CAST('default' AS varchar))
                     WHEN CAST('mute' AS text)
                       THEN CAST(0 AS bigint)
                     WHEN CAST('follow' AS text)
                       THEN GREATEST(stats.message_count - COALESCE(reads.read_count
                                                                  , CAST(0 AS bigint))
                                   , CAST(0 AS bigint))
                     WHEN CAST('unmute' AS text)
                       THEN COALESCE(mentions.unread_count
                                   , CAST(0 AS bigint))
                     ELSE CASE binding.notification_mode
                            WHEN CAST('all_messages' AS text)
                              THEN GREATEST(stats.message_count - COALESCE(reads.read_count
                                                                         , CAST(0 AS bigint))
                                          , CAST(0 AS bigint))
                            WHEN CAST('mentions_only' AS text)
                              THEN COALESCE(mentions.unread_count
                                          , CAST(0 AS bigint))
                            ELSE CAST(0 AS bigint)
                          END
                   END AS active_unread_count
            FROM m_workspace_topic_message_stats_v1 AS stats
                 INNER JOIN m_workspace_read_state_projects_v1 AS project ON project.project_id = stats.project_id
                                                                         AND CAST(project.mode AS text) = ANY(CAST(ARRAY[CAST('compact' AS varchar)
                                                                                                                       , CAST('rollback' AS varchar)] AS text[]))
                 INNER JOIN m_workspace_stream_bindings AS binding ON binding.project_id = stats.project_id
                                                                  AND binding.stream_uuid = stats.stream_uuid
                 LEFT JOIN m_workspace_user_topic_flags AS topic_flags ON topic_flags.project_id = stats.project_id
                                                                      AND topic_flags.uuid = stats.topic_uuid
                                                                      AND topic_flags.user_uuid = binding.user_uuid
                 LEFT JOIN LATERAL (SELECT topic_reads.read_count
                                    FROM m_workspace_user_topic_read_stats_v1 AS topic_reads
                                    WHERE topic_reads.project_id = stats.project_id
                                      AND topic_reads.topic_uuid = stats.topic_uuid
                                      AND topic_reads.user_uuid = binding.user_uuid) AS reads ON TRUE
                 LEFT JOIN LATERAL (SELECT count(*) AS unread_count
                                    FROM m_workspace_message_mentions_v1 AS mention
                                         LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk ON chunk.user_uuid = mention.user_uuid
                                                                                           AND chunk.chunk_number = (mention.ingest_sequence / 4096)
                                    WHERE mention.project_id = stats.project_id
                                      AND mention.topic_uuid = stats.topic_uuid
                                      AND mention.user_uuid = binding.user_uuid
                                      AND COALESCE(get_bit(chunk.read_bits
                                                         , CAST(mention.ingest_sequence % CAST(4096 AS bigint) AS integer))
                                                 , 0) = 0) AS mentions ON TRUE
          )
       SELECT legacy_counts.stream_uuid
            , legacy_counts.topic_uuid
            , legacy_counts.user_uuid
            , legacy_counts.project_id
            , legacy_counts.unread_count
            , CAST(legacy_counts.active_unread_count AS integer) AS active_unread_count
            , CAST(legacy_counts.unread_count - legacy_counts.active_unread_count AS integer) AS passive_unread_count
       FROM legacy_counts

       UNION ALL

       SELECT compact_counts.stream_uuid
            , compact_counts.topic_uuid
            , compact_counts.user_uuid
            , compact_counts.project_id
            , compact_counts.unread_count
            , CAST(compact_counts.active_unread_count AS integer) AS active_unread_count
            , CAST(compact_counts.unread_count - compact_counts.active_unread_count AS integer) AS passive_unread_count
       FROM compact_counts
       WHERE compact_counts.unread_count > 0;

CREATE VIEW m_workspace_user_unread_messages_base_v1
  AS SELECT message.uuid AS message_uuid
          , message.stream_uuid
          , message.topic_uuid
          , message_flags.user_uuid
          , message_flags.project_id
          , position(CAST('](' AS text) || CAST('urn:user:' AS text) || lower(CAST(binding.user_uuid AS text)) || CAST(')' AS text) IN lower(COALESCE(message.payload ->> CAST('content' AS text)
                                                                                                                                                    , CAST('' AS text)))) > 0 AS mentioned
          , CASE COALESCE(topic_flags.notification_mode
                        , CAST('default' AS varchar))
              WHEN CAST('mute' AS text)
                THEN FALSE
              WHEN CAST('follow' AS text)
                THEN TRUE
              WHEN CAST('unmute' AS text)
                THEN position(CAST('](' AS text) || CAST('urn:user:' AS text) || lower(CAST(binding.user_uuid AS text)) || CAST(')' AS text) IN lower(COALESCE(message.payload ->> CAST('content' AS text)
                                                                                                                                                             , CAST('' AS text)))) > 0
              ELSE CASE binding.notification_mode
                     WHEN CAST('all_messages' AS text)
                       THEN TRUE
                     WHEN CAST('mentions_only' AS text)
                       THEN position(CAST('](' AS text) || CAST('urn:user:' AS text) || lower(CAST(binding.user_uuid AS text)) || CAST(')' AS text) IN lower(COALESCE(message.payload ->> CAST('content' AS text)
                                                                                                                                                                    , CAST('' AS text)))) > 0
                     ELSE FALSE
                   END
            END AS active
     FROM m_workspace_user_message_flags AS message_flags
          INNER JOIN LATERAL (SELECT TRUE AS allowed
                              WHERE NOT EXISTS (SELECT 1
                                                FROM m_workspace_read_state_projects_v1 AS project
                                                WHERE project.project_id = message_flags.project_id
                                                  AND CAST(project.mode AS text) = ANY(CAST(ARRAY[CAST('compact' AS varchar)
                                                                                                , CAST('rollback' AS varchar)] AS text[])))
                              OFFSET 0) AS legacy_mode ON TRUE
          INNER JOIN LATERAL (SELECT stored_message.uuid
                                   , stored_message.stream_uuid
                                   , stored_message.topic_uuid
                                   , stored_message.project_id
                                   , stored_message.payload
                              FROM m_workspace_messages AS stored_message
                              WHERE stored_message.uuid = message_flags.uuid
                                AND stored_message.project_id = message_flags.project_id
                                AND legacy_mode.allowed
                              OFFSET 0) AS message ON TRUE
          INNER JOIN m_workspace_stream_bindings AS binding ON binding.stream_uuid = message.stream_uuid
                                                           AND binding.project_id = message.project_id
                                                           AND binding.user_uuid = message_flags.user_uuid
          LEFT JOIN m_workspace_user_topic_flags AS topic_flags ON topic_flags.uuid = message.topic_uuid
                                                               AND topic_flags.project_id = message.project_id
                                                               AND topic_flags.user_uuid = binding.user_uuid
     WHERE message_flags.read = FALSE

     UNION ALL

     SELECT message.uuid AS message_uuid
          , message.stream_uuid
          , message.topic_uuid
          , binding.user_uuid
          , message.project_id
          , mention.message_uuid IS NOT NULL AS mentioned
          , CASE COALESCE(topic_flags.notification_mode
                        , CAST('default' AS varchar))
              WHEN CAST('mute' AS text)
                THEN FALSE
              WHEN CAST('follow' AS text)
                THEN TRUE
              WHEN CAST('unmute' AS text)
                THEN mention.message_uuid IS NOT NULL
              ELSE CASE binding.notification_mode
                     WHEN CAST('all_messages' AS text)
                       THEN TRUE
                     WHEN CAST('mentions_only' AS text)
                       THEN mention.message_uuid IS NOT NULL
                     ELSE FALSE
                   END
            END AS active
     FROM m_workspace_read_state_projects_v1 AS project
          INNER JOIN m_workspace_messages AS message ON message.project_id = project.project_id
          INNER JOIN m_workspace_stream_bindings AS binding ON binding.stream_uuid = message.stream_uuid
                                                           AND binding.project_id = message.project_id
          LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk ON chunk.user_uuid = binding.user_uuid
                                                            AND chunk.chunk_number = (message.ingest_sequence / 4096)
          LEFT JOIN m_workspace_message_mentions_v1 AS mention ON mention.message_uuid = message.uuid
                                                              AND mention.user_uuid = binding.user_uuid
          LEFT JOIN m_workspace_user_topic_flags AS topic_flags ON topic_flags.uuid = message.topic_uuid
                                                               AND topic_flags.project_id = message.project_id
                                                               AND topic_flags.user_uuid = binding.user_uuid
     WHERE CAST(project.mode AS text) = ANY(CAST(ARRAY[CAST('compact' AS varchar)
                                                     , CAST('rollback' AS varchar)] AS text[]))
       AND COALESCE(get_bit(chunk.read_bits
                          , CAST(message.ingest_sequence % CAST(4096 AS bigint) AS integer))
                  , 0) = 0;

CREATE VIEW m_workspace_visible_events_pre_messenger_v2
  AS WITH event_rows AS (
            SELECT e_1.epoch_version
                 , e_1.uuid
                 , e_1.project_id
                 , e_1.user_uuid
                 , e_1.payload
                 , e_1.created_at
                 , e_1.updated_at
                 , e_1.schema_version
                 , e_1.object_type
                 , e_1.action
            FROM m_workspace_events AS e_1

            UNION ALL

            SELECT b.epoch_version
                 , b.uuid
                 , b.project_id
                 , recipient.user_uuid
                 , b.payload || COALESCE(override.payload
                                       , CAST('{}' AS jsonb)) || CASE
                                                                   WHEN b.object_type = CAST('user' AS text)
                                                                     THEN CAST('{}' AS jsonb)
                                                                   ELSE jsonb_build_object('user_uuid', recipient.user_uuid)
                                                                 END AS payload
                 , b.created_at
                 , b.updated_at
                 , b.schema_version
                 , b.object_type
                 , b.action
            FROM m_workspace_broadcast_message_events_v1 AS b
                 INNER JOIN m_workspace_event_audience_members_v1 AS recipient ON recipient.audience_snapshot_uuid = b.audience_snapshot_uuid
                 LEFT JOIN m_workspace_event_recipient_payloads_v1 AS override ON override.event_uuid = b.uuid
                                                                              AND override.user_uuid = recipient.user_uuid
          )
       SELECT e.epoch_version
            , e.uuid
            , e.project_id
            , e.user_uuid
            , e.payload
            , e.created_at
            , e.updated_at
            , e.schema_version
            , e.object_type
            , e.action
       FROM event_rows AS e
            LEFT JOIN LATERAL (SELECT COALESCE(CAST(NULLIF(e.payload ->> CAST('stream_uuid' AS text)
                                                         , CAST('' AS text)) AS uuid)
                                             , CASE
                                                 WHEN CAST(e.object_type AS text) = CAST('stream' AS text)
                                                   THEN CAST(NULLIF(e.payload ->> CAST('uuid' AS text)
                                                                  , CAST('' AS text)) AS uuid)
                                                 ELSE CAST(NULL AS uuid)
                                               END
                                             , (SELECT message.stream_uuid
                                                FROM m_workspace_messages AS message
                                                WHERE message.project_id = e.project_id
                                                  AND message.uuid = CAST(NULLIF(e.payload ->> CAST('message_uuid' AS text)
                                                                               , CAST('' AS text)) AS uuid))
                                             , (SELECT placement.stream_uuid
                                                FROM messenger_message_placements AS placement
                                                WHERE placement.project_id = e.project_id
                                                  AND placement.uuid = CAST(NULLIF(e.payload ->> CAST('message_uuid' AS text)
                                                                                 , CAST('' AS text)) AS uuid))) AS stream_uuid
                                    , COALESCE((SELECT message.stream_uuid
                                                FROM m_workspace_messages AS message
                                                WHERE message.project_id = e.project_id
                                                  AND message.uuid = CAST(NULLIF(e.payload ->> CAST('old_message_uuid' AS text)
                                                                               , CAST('' AS text)) AS uuid))
                                             , (SELECT placement.stream_uuid
                                                FROM messenger_message_placements AS placement
                                                WHERE placement.project_id = e.project_id
                                                  AND placement.uuid = CAST(NULLIF(e.payload ->> CAST('old_message_uuid' AS text)
                                                                                 , CAST('' AS text)) AS uuid))) AS old_stream_uuid) AS event_stream ON TRUE
       WHERE TRUE
         AND TRUE
         AND (CAST(e.object_type AS text) <> CAST('message' AS text)
           OR e.payload ->> CAST('stream_uuid' AS text) IS NULL
           OR EXISTS (SELECT 1
                      FROM m_workspace_stream_bindings AS binding
                      WHERE binding.project_id = e.project_id
                        AND binding.stream_uuid = CAST(e.payload ->> CAST('stream_uuid' AS text) AS uuid)
                        AND binding.user_uuid = e.user_uuid))
         AND TRUE;

CREATE VIEW m_workspace_visible_files_v1
  AS (SELECT files.uuid
           , files.project_id
           , files.name
           , files.description
           , files.user_uuid
           , files.stream_uuid
           , files.content_type
           , files.size_bytes
           , files.hash
           , files.created_at
           , files.updated_at
           , files.storage_type
           , files.storage_id
           , files.storage_object_id
           , files.acl_mode
           , bindings.user_uuid AS viewer_user_uuid
      FROM m_workspace_files AS files
           INNER JOIN m_workspace_stream_bindings AS bindings ON bindings.project_id = files.project_id
                                                             AND bindings.stream_uuid = files.stream_uuid
      WHERE files.acl_mode = CAST('stream' AS text)

      UNION

      SELECT files.uuid
           , files.project_id
           , files.name
           , files.description
           , files.user_uuid
           , files.stream_uuid
           , files.content_type
           , files.size_bytes
           , files.hash
           , files.created_at
           , files.updated_at
           , files.storage_type
           , files.storage_id
           , files.storage_object_id
           , files.acl_mode
           , files.user_uuid AS viewer_user_uuid
      FROM m_workspace_files AS files
      WHERE files.acl_mode = CAST('owner' AS text))

     UNION

     SELECT files.uuid
          , files.project_id
          , files.name
          , files.description
          , files.user_uuid
          , files.stream_uuid
          , files.content_type
          , files.size_bytes
          , files.hash
          , files.created_at
          , files.updated_at
          , files.storage_type
          , files.storage_id
          , files.storage_object_id
          , files.acl_mode
          , CAST(NULL AS uuid) AS viewer_user_uuid
     FROM m_workspace_files AS files
     WHERE files.acl_mode = CAST('public' AS text);

CREATE VIEW messenger_api_message_reactions_v1
  AS SELECT fact.uuid
          , fact.project_id
          , binding.user_uuid AS viewer_user_uuid
          , fact.placement_uuid AS message_uuid
          , fact.user_uuid
          , fact.emoji_name
          , fact.created_at
          , fact.updated_at
          , COALESCE(message.deleted_at, stream.deleted_at) AS deleted_at
          , COALESCE(message.deleted_at, stream.deleted_at) IS NULL AS visible
     FROM messenger_message_reaction_facts AS fact
          INNER JOIN messenger_messages AS message ON message.project_id = fact.project_id
                                                  AND message.uuid = fact.canonical_message_uuid
          INNER JOIN messenger_user_message_bindings AS binding ON binding.project_id = fact.project_id
                                                               AND binding.placement_uuid = fact.placement_uuid
          INNER JOIN messenger_message_placements AS placement ON placement.project_id = fact.project_id
                                                              AND placement.uuid = fact.placement_uuid
          INNER JOIN messenger_stream_bindings AS membership ON membership.project_id = fact.project_id
                                                            AND membership.user_uuid = binding.user_uuid
                                                            AND membership.stream_uuid = placement.stream_uuid
                                                            AND membership.active
                                                            AND membership.membership_generation = binding.membership_generation
          INNER JOIN messenger_streams AS stream ON stream.project_id = placement.project_id
                                                AND stream.uuid = placement.stream_uuid
     WHERE CAST(binding.visibility AS text) = CAST('visible' AS text);

CREATE VIEW messenger_api_stream_bindings_v1
  AS SELECT target.uuid
          , viewer.project_id
          , viewer.user_uuid AS viewer_user_uuid
          , target.stream_uuid
          , target.user_uuid
          , target.who_uuid
          , target.role
          , target.notification_mode
          , target.notification_updated_at
          , target.created_at
          , target.updated_at
          , stream.deleted_at
          , stream.deleted_at IS NULL AS visible
     FROM messenger_stream_bindings AS viewer
          INNER JOIN messenger_stream_bindings AS target ON target.project_id = viewer.project_id
                                                        AND target.stream_uuid = viewer.stream_uuid
                                                        AND target.active
          INNER JOIN messenger_streams AS stream ON stream.project_id = viewer.project_id
                                                AND stream.uuid = viewer.stream_uuid
     WHERE viewer.active;

CREATE VIEW messenger_api_user_folder_items_v1
  AS SELECT item.uuid
          , item.project_id
          , item.user_uuid
          , item.folder_uuid
          , item.stream_uuid
          , item.order_index
          , item.pinned_at
          , item.chat_type
          , COALESCE(binding.unread_count, 0) AS unread_count
          , COALESCE(binding.active_unread_count, 0) AS active_unread_count
          , COALESCE(binding.passive_unread_count, 0) AS passive_unread_count
          , item.created_at
          , item.updated_at
     FROM messenger_folder_items AS item
          INNER JOIN messenger_stream_bindings AS binding ON binding.project_id = item.project_id
                                                         AND binding.user_uuid = item.user_uuid
                                                         AND binding.stream_uuid = item.stream_uuid
                                                         AND binding.active
          INNER JOIN messenger_streams AS stream ON stream.project_id = item.project_id
                                                AND stream.uuid = item.stream_uuid
                                                AND stream.deleted_at IS NULL
                                                AND NOT stream.is_archived;

CREATE VIEW messenger_api_user_folders_v1
  AS SELECT folder.uuid
          , folder.project_id
          , binding.user_uuid
          , folder.title
          , folder.background_color_value
          , folder.system_type
          , binding.unread_count
          , binding.folder_items_snapshot AS folder_items
          , folder.created_at
          , folder.updated_at
     FROM messenger_user_folder_bindings AS binding
          INNER JOIN messenger_folders AS folder ON folder.project_id = binding.project_id
                                                AND folder.uuid = binding.folder_uuid;

CREATE VIEW messenger_api_user_messages_v1
  AS SELECT binding.uuid AS binding_uuid
          , placement.uuid
          , message.uuid AS canonical_message_uuid
          , binding.user_uuid
          , placement.project_id
          , message.created_at
          , message.updated_at
          , placement.stream_uuid
          , placement.topic_uuid
          , message.payload
          , message.author_uuid
          , state.read_at IS NOT NULL AS read
          , state.pinned
          , state.starred
          , message.author_uuid = binding.user_uuid AS is_own
          , state.mentioned
          , message.reactions
          , message.reaction_users
          , message.source_name
          , message.source
          , COALESCE(message.deleted_at, stream.deleted_at, topic.deleted_at) AS deleted_at
          , COALESCE(message.deleted_at, stream.deleted_at, topic.deleted_at) IS NULL AS visible
     FROM messenger_user_message_bindings AS binding
          INNER JOIN messenger_message_placements AS placement ON placement.project_id = binding.project_id
                                                              AND placement.uuid = binding.placement_uuid
          INNER JOIN messenger_messages AS message ON message.project_id = placement.project_id
                                                  AND message.uuid = placement.message_uuid
          INNER JOIN messenger_user_message_states AS state ON state.project_id = binding.project_id
                                                           AND state.user_uuid = binding.user_uuid
                                                           AND state.placement_uuid = binding.placement_uuid
                                                           AND state.membership_generation = binding.membership_generation
          INNER JOIN messenger_stream_bindings AS membership ON membership.project_id = binding.project_id
                                                            AND membership.user_uuid = binding.user_uuid
                                                            AND membership.stream_uuid = placement.stream_uuid
                                                            AND membership.active
                                                            AND membership.membership_generation = binding.membership_generation
          INNER JOIN messenger_streams AS stream ON stream.project_id = placement.project_id
                                                AND stream.uuid = placement.stream_uuid
          INNER JOIN messenger_topics AS topic ON topic.project_id = placement.project_id
                                              AND topic.uuid = placement.topic_uuid
     WHERE CAST(binding.visibility AS text) = CAST('visible' AS text);

CREATE VIEW messenger_api_user_streams_v1
  AS SELECT stream.uuid
          , binding.user_uuid
          , CAST(CASE
                   WHEN stream.private
                     THEN CAST(COALESCE(NULLIF(pg_catalog.btrim(CAST(COALESCE(peer.first_name
                                                                            , CAST('' AS varchar)) AS text) || CAST(' ' AS text) || CAST(COALESCE(peer.last_name
                                                                                                                                                , CAST('' AS varchar)) AS text))
                                             , CAST('' AS text))
                                      , CAST(peer.username AS text)
                                      , CAST(stream.name AS text)) AS varchar)
                   ELSE stream.name
                 END AS varchar(255)) AS name
          , stream.description
          , stream.project_id
          , stream.created_at
          , stream.updated_at
          , stream.owner_uuid AS owner
          , binding.role
          , binding.notification_mode
          , binding.unread_count
          , binding.active_unread_count
          , binding.passive_unread_count
          , stream.source_name
          , stream.source
          , stream.invite_only
          , stream.announce
          , stream.private
          , stream.is_archived
          , CASE
              WHEN stream.direct_user_uuid IS NULL
                THEN CAST(NULL AS uuid)
              WHEN binding.user_uuid = stream.owner_uuid
                THEN stream.direct_user_uuid
              ELSE stream.owner_uuid
            END AS direct_user_uuid
          , stream.private_index
          , stream.color
          , binding.last_message_uuid
          , stream.default_topic_uuid
          , stream.deleted_at
          , stream.deleted_at IS NULL AS visible
          , stream.encryption
          , stream.current_encryption_key_uuid
     FROM messenger_stream_bindings AS binding
          INNER JOIN messenger_streams AS stream ON stream.project_id = binding.project_id
                                                AND stream.uuid = binding.stream_uuid
          LEFT JOIN m_workspace_users AS peer ON peer.uuid = CASE
                                                               WHEN stream.private
                                                                AND stream.direct_user_uuid IS NOT NULL
                                                                AND stream.owner_uuid = binding.user_uuid
                                                                 THEN stream.direct_user_uuid
                                                               WHEN stream.private
                                                                AND stream.direct_user_uuid IS NOT NULL
                                                                 THEN stream.owner_uuid
                                                               ELSE CAST(NULL AS uuid)
                                                             END
     WHERE binding.active;

CREATE VIEW messenger_api_user_topics_v1
  AS SELECT topic.uuid
          , binding.user_uuid
          , topic.project_id
          , topic.created_at
          , topic.updated_at
          , topic.name
          , topic.stream_uuid
          , topic.color
          , binding.last_message_uuid
          , binding.unread_count
          , binding.active_unread_count
          , binding.passive_unread_count
          , stream.default_topic_uuid = topic.uuid AS is_default
          , topic.is_done
          , binding.notification_mode
          , topic.summary
          , topic.summary_last_message_uuid
          , binding.summary_has_new_messages
          , topic.summary_enabled
          , topic.summary_system_prompt
          , topic.summary_reasoning_effort
          , topic.source_name
          , topic.source
          , COALESCE(topic.deleted_at, stream.deleted_at) AS deleted_at
          , COALESCE(topic.deleted_at, stream.deleted_at) IS NULL AS visible
     FROM messenger_user_topic_bindings AS binding
          INNER JOIN messenger_topics AS topic ON topic.project_id = binding.project_id
                                              AND topic.uuid = binding.topic_uuid
          INNER JOIN messenger_streams AS stream ON stream.project_id = topic.project_id
                                                AND stream.uuid = topic.stream_uuid
          INNER JOIN messenger_stream_bindings AS membership ON membership.project_id = topic.project_id
                                                            AND membership.user_uuid = binding.user_uuid
                                                            AND membership.stream_uuid = topic.stream_uuid
                                                            AND membership.active;

CREATE VIEW m_unread_user_messages
  AS SELECT counts.stream_uuid AS uuid
          , counts.user_uuid
          , counts.project_id
          , CAST(sum(counts.unread_count) AS bigint) AS unread_count
          , CAST(sum(counts.active_unread_count) AS integer) AS active_unread_count
          , CAST(sum(counts.passive_unread_count) AS integer) AS passive_unread_count
     FROM m_workspace_user_topic_unread_counts_v1 AS counts
          INNER JOIN m_workspace_streams AS stream ON stream.uuid = counts.stream_uuid
                                                  AND stream.project_id = counts.project_id
     WHERE TRUE
     GROUP BY counts.stream_uuid, counts.user_uuid, counts.project_id;

CREATE VIEW m_workspace_user_topics_view
  AS SELECT topic.uuid
          , topic.name
          , topic.stream_uuid
          , topic.project_id
          , topic.created_at
          , topic.updated_at
          , COALESCE(topic.uuid = stream.default_topic_uuid
                   , FALSE) AS is_default
          , binding.user_uuid
          , COALESCE(unread.unread_count
                   , CAST(0 AS bigint)) AS unread_count
          , COALESCE(flags.is_done, FALSE) AS is_done
          , COALESCE(flags.notification_mode
                   , CAST('default' AS varchar)) AS notification_mode
          , topic.color
          , last_message.uuid AS last_message_uuid
          , topic.source_name
          , topic.source
          , topic.summary
          , topic.summary_last_message_uuid
          , CASE
              WHEN topic.summary IS NULL
                THEN CAST(NULL AS boolean)
              ELSE topic.summary_last_message_uuid IS DISTINCT FROM last_message.uuid
            END AS summary_has_new_messages
          , topic.summary_system_prompt
          , topic.summary_reasoning_effort
          , topic.summary_enabled
          , COALESCE(unread.active_unread_count, 0) AS active_unread_count
          , COALESCE(unread.passive_unread_count, 0) AS passive_unread_count
     FROM m_workspace_stream_topics AS topic
          INNER JOIN m_workspace_streams AS stream ON stream.uuid = topic.stream_uuid
                                                  AND stream.project_id = topic.project_id
          INNER JOIN m_workspace_stream_bindings AS binding ON binding.stream_uuid = topic.stream_uuid
                                                           AND binding.project_id = topic.project_id
          LEFT JOIN m_workspace_user_topic_unread_counts_v1 AS unread ON unread.topic_uuid = topic.uuid
                                                                     AND unread.stream_uuid = topic.stream_uuid
                                                                     AND unread.user_uuid = binding.user_uuid
                                                                     AND unread.project_id = topic.project_id
          LEFT JOIN LATERAL (SELECT message.uuid
                             FROM m_workspace_messages AS message
                             WHERE message.project_id = topic.project_id
                               AND message.topic_uuid = topic.uuid
                             ORDER BY message.created_at DESC
                                    , message.uuid DESC
                             LIMIT 1) AS last_message ON TRUE
          LEFT JOIN m_workspace_user_topic_flags AS flags ON flags.uuid = topic.uuid
                                                         AND flags.user_uuid = binding.user_uuid
                                                         AND flags.project_id = topic.project_id
     WHERE TRUE;

CREATE VIEW m_workspace_user_unread_messages_view
  AS SELECT unread.message_uuid
          , unread.stream_uuid
          , unread.topic_uuid
          , unread.user_uuid
          , unread.project_id
          , unread.mentioned
          , unread.active
     FROM m_workspace_user_unread_messages_base_v1 AS unread
          INNER JOIN m_workspace_streams AS stream ON stream.uuid = unread.stream_uuid
                                                  AND stream.project_id = unread.project_id
     WHERE TRUE;

CREATE VIEW m_workspace_visible_events
  AS SELECT epoch_version
          , uuid
          , project_id
          , user_uuid
          , payload
          , created_at
          , updated_at
          , schema_version
          , object_type
          , action
     FROM m_workspace_visible_events_pre_messenger_v2 AS event
     WHERE NOT EXISTS (SELECT 1
                       FROM messenger_event_membership_guards AS guard
                       WHERE guard.event_uuid = event.uuid
                         AND guard.user_uuid = event.user_uuid)
        OR EXISTS (SELECT 1
                   FROM messenger_event_membership_guards AS guard
                        LEFT JOIN messenger_stream_bindings AS binding ON binding.project_id = guard.project_id
                                                                      AND binding.stream_uuid = guard.stream_uuid
                                                                      AND binding.user_uuid = guard.user_uuid
                                                                      AND binding.active
                                                                      AND binding.membership_generation = guard.membership_generation
                   WHERE guard.event_uuid = event.uuid
                     AND guard.user_uuid = event.user_uuid
                     AND (guard.control_effect
                       OR binding.uuid IS NOT NULL));

CREATE VIEW m_workspace_visible_message_reactions_v1
  AS SELECT reactions.uuid
          , reactions.project_id
          , reactions.created_at
          , reactions.updated_at
          , reactions.message_uuid
          , reactions.user_uuid
          , reactions.emoji_name
          , messages.user_uuid AS viewer_user_uuid
     FROM m_workspace_message_reactions AS reactions
          INNER JOIN m_workspace_user_messages_view AS messages ON messages.project_id = reactions.project_id
                                                               AND messages.uuid = reactions.message_uuid;

CREATE VIEW m_workspace_user_streams
  AS SELECT stream.uuid
          , CASE
              WHEN stream.private
                THEN CAST(COALESCE(NULLIF(pg_catalog.btrim(CAST(COALESCE(peer.first_name
                                                                       , CAST('' AS varchar)) AS text) || CAST(' ' AS text) || CAST(COALESCE(peer.last_name
                                                                                                                                           , CAST('' AS varchar)) AS text))
                                        , CAST('' AS text))
                                 , CAST(peer.username AS text)
                                 , CAST(stream.name AS text)) AS varchar)
              ELSE stream.name
            END AS name
          , stream.description
          , stream.project_id
          , stream.source_name
          , stream.source
          , stream.user_uuid AS owner
          , binding.user_uuid
          , binding.role
          , COALESCE(unread.unread_count
                   , CAST(0 AS bigint)) AS unread_count
          , stream.invite_only
          , stream.announce
          , stream.private
          , stream.created_at
          , stream.updated_at
          , CASE
              WHEN stream.private
               AND stream.direct_user_uuid IS NOT NULL
               AND stream.user_uuid = binding.user_uuid
                THEN stream.direct_user_uuid
              WHEN stream.private
               AND stream.direct_user_uuid IS NOT NULL
                THEN stream.user_uuid
              ELSE CAST(NULL AS uuid)
            END AS direct_user_uuid
          , stream.private_index
          , stream.is_archived
          , binding.notification_mode
          , stream.color
          , last_message.uuid AS last_message_uuid
          , stream.default_topic_uuid
          , COALESCE(unread.active_unread_count, 0) AS active_unread_count
          , COALESCE(unread.passive_unread_count, 0) AS passive_unread_count
          , canonical.encryption
          , canonical.current_encryption_key_uuid
     FROM m_workspace_streams AS stream
          INNER JOIN messenger_streams AS canonical ON canonical.project_id = stream.project_id
                                                  AND canonical.uuid = stream.uuid
          INNER JOIN m_workspace_stream_bindings AS binding ON binding.stream_uuid = stream.uuid
                                                           AND binding.project_id = stream.project_id
          LEFT JOIN m_unread_user_messages AS unread ON unread.uuid = stream.uuid
                                                    AND unread.user_uuid = binding.user_uuid
                                                    AND unread.project_id = stream.project_id
          LEFT JOIN LATERAL (SELECT message.uuid
                             FROM m_workspace_messages AS message
                             WHERE message.project_id = stream.project_id
                               AND message.stream_uuid = stream.uuid
                             ORDER BY message.created_at DESC
                                    , message.uuid DESC
                             LIMIT 1) AS last_message ON TRUE
          LEFT JOIN m_workspace_users AS peer ON peer.uuid = CASE
                                                               WHEN stream.private
                                                                AND stream.direct_user_uuid IS NOT NULL
                                                                AND stream.user_uuid = binding.user_uuid
                                                                 THEN stream.direct_user_uuid
                                                               WHEN stream.private
                                                                AND stream.direct_user_uuid IS NOT NULL
                                                                 THEN stream.user_uuid
                                                               ELSE CAST(NULL AS uuid)
                                                             END
     WHERE TRUE;

CREATE VIEW m_folder_all_items_view
  AS SELECT CAST(CAST('00' AS text) || substr(CAST(s.uuid AS text)
                                            , 3) AS uuid) AS uuid
          , CAST('00000000-0000-0000-0000-000000000000' AS uuid) AS folder
          , s.project_id
          , s.user_uuid
          , s.uuid AS stream_uuid
          , fi.order_index
          , CAST(fi.pinned_at AS timestamp) AS pinned_at
          , CASE
              WHEN s.private
                THEN CAST('private' AS varchar)
              ELSE CAST('stream' AS varchar)
            END AS chat_type
          , COALESCE(s.unread_count
                   , CAST(0 AS bigint)) AS unread_count
          , COALESCE(CAST(fi.created_at AS timestamp)
                   , CAST('2000-01-01 00:00:00' AS timestamp)) AS created_at
          , COALESCE(CAST(fi.updated_at AS timestamp)
                   , CAST('2000-01-01 00:00:00' AS timestamp)) AS updated_at
          , COALESCE(s.active_unread_count, 0) AS active_unread_count
          , COALESCE(s.passive_unread_count, 0) AS passive_unread_count
     FROM m_workspace_user_streams AS s
          LEFT JOIN m_folder_items AS fi ON fi.folder_uuid = CAST('00000000-0000-0000-0000-000000000000' AS uuid)
                                        AND fi.stream_uuid = s.uuid
                                        AND fi.project_id = s.project_id
                                        AND fi.user_uuid = s.user_uuid
     WHERE s.is_archived = FALSE;

CREATE VIEW m_folder_channel_items_view
  AS SELECT CAST(CAST('22' AS text) || substr(CAST(s.uuid AS text)
                                            , 3) AS uuid) AS uuid
          , CAST('00000000-0000-0000-0000-000000000002' AS uuid) AS folder
          , s.project_id
          , s.user_uuid
          , s.uuid AS stream_uuid
          , fi.order_index
          , CAST(fi.pinned_at AS timestamp) AS pinned_at
          , CAST('stream' AS varchar) AS chat_type
          , COALESCE(s.unread_count
                   , CAST(0 AS bigint)) AS unread_count
          , COALESCE(CAST(fi.created_at AS timestamp)
                   , CAST('2000-01-01 00:00:02' AS timestamp)) AS created_at
          , COALESCE(CAST(fi.updated_at AS timestamp)
                   , CAST('2000-01-01 00:00:02' AS timestamp)) AS updated_at
          , COALESCE(s.active_unread_count, 0) AS active_unread_count
          , COALESCE(s.passive_unread_count, 0) AS passive_unread_count
     FROM m_workspace_user_streams AS s
          LEFT JOIN m_folder_items AS fi ON fi.folder_uuid = CAST('00000000-0000-0000-0000-000000000002' AS uuid)
                                        AND fi.stream_uuid = s.uuid
                                        AND fi.project_id = s.project_id
                                        AND fi.user_uuid = s.user_uuid
     WHERE s.is_archived = FALSE
       AND s.private = FALSE;

CREATE VIEW m_folder_items_created_view
  AS SELECT fi.uuid
          , fi.folder_uuid
          , fi.project_id
          , fi.user_uuid
          , fi.stream_uuid
          , fi.order_index
          , fi.pinned_at
          , fi.chat_type
          , COALESCE(s.unread_count
                   , CAST(0 AS bigint)) AS unread_count
          , fi.created_at
          , fi.updated_at
          , COALESCE(s.active_unread_count, 0) AS active_unread_count
          , COALESCE(s.passive_unread_count, 0) AS passive_unread_count
     FROM m_folder_items AS fi
          INNER JOIN m_workspace_user_streams AS s ON s.uuid = fi.stream_uuid
                                                  AND s.project_id = fi.project_id
                                                  AND s.user_uuid = fi.user_uuid
                                                  AND s.is_archived = FALSE;

CREATE VIEW m_folder_private_items_view
  AS SELECT CAST(CAST('11' AS text) || substr(CAST(s.uuid AS text)
                                            , 3) AS uuid) AS uuid
          , CAST('00000000-0000-0000-0000-000000000001' AS uuid) AS folder
          , s.project_id
          , s.user_uuid
          , s.uuid AS stream_uuid
          , fi.order_index
          , CAST(fi.pinned_at AS timestamp) AS pinned_at
          , CAST('private' AS varchar) AS chat_type
          , COALESCE(s.unread_count
                   , CAST(0 AS bigint)) AS unread_count
          , COALESCE(CAST(fi.created_at AS timestamp)
                   , CAST('2000-01-01 00:00:01' AS timestamp)) AS created_at
          , COALESCE(CAST(fi.updated_at AS timestamp)
                   , CAST('2000-01-01 00:00:01' AS timestamp)) AS updated_at
          , COALESCE(s.active_unread_count, 0) AS active_unread_count
          , COALESCE(s.passive_unread_count, 0) AS passive_unread_count
     FROM m_workspace_user_streams AS s
          LEFT JOIN m_folder_items AS fi ON fi.folder_uuid = CAST('00000000-0000-0000-0000-000000000001' AS uuid)
                                        AND fi.stream_uuid = s.uuid
                                        AND fi.project_id = s.project_id
                                        AND fi.user_uuid = s.user_uuid
     WHERE s.is_archived = FALSE
       AND s.private = TRUE;

CREATE VIEW m_folders_view
  AS WITH project_users AS (
            SELECT m_workspace_stream_bindings.project_id
                 , m_workspace_stream_bindings.user_uuid
            FROM m_workspace_stream_bindings

            UNION

            SELECT m_folders.project_id
                 , m_folders.user_uuid
            FROM m_folders
            WHERE m_folders.project_id <> CAST('00000000-0000-0000-0000-000000000000' AS uuid)
              AND m_folders.user_uuid <> CAST('00000000-0000-0000-0000-000000000000' AS uuid)
          )
        , system_folder_templates AS (
            SELECT template.uuid
                 , template.item_uuid_prefix
                 , template.title
                 , template.created_at
                 , template.private_filter
                 , template.chat_type
            FROM ((VALUES (CAST('00000000-0000-0000-0000-000000000000' AS uuid)
                         , CAST('00' AS text)
                         , CAST('All chats' AS varchar)
                         , CAST('2000-01-01 00:00:00' AS timestamp)
                         , CAST(NULL AS boolean)
                         , CAST(NULL AS varchar))
                        , (CAST('00000000-0000-0000-0000-000000000001' AS uuid)
                         , CAST('11' AS text)
                         , CAST('Personal' AS varchar)
                         , CAST('2000-01-01 00:00:01' AS timestamp)
                         , TRUE
                         , CAST('private' AS varchar))
                        , (CAST('00000000-0000-0000-0000-000000000002' AS uuid)
                         , CAST('22' AS text)
                         , CAST('Channels' AS varchar)
                         , CAST('2000-01-01 00:00:02' AS timestamp)
                         , FALSE
                         , CAST('stream' AS varchar)))) AS template (uuid, item_uuid_prefix, title, created_at, private_filter, chat_type)
          )
        , folder_definitions AS (
            SELECT template.uuid
                 , users.project_id
                 , users.user_uuid
                 , template.title
                 , CAST(11184810 AS bigint) AS background_color_value
                 , CAST('all' AS varchar) AS system_type
                 , template.created_at
                 , template.created_at AS updated_at
                 , TRUE AS system_folder
                 , template.item_uuid_prefix
                 , template.private_filter
                 , template.chat_type
            FROM project_users AS users
                 CROSS JOIN system_folder_templates AS template

            UNION ALL

            SELECT folder.uuid
                 , folder.project_id
                 , folder.user_uuid
                 , folder.title
                 , folder.background_color_value
                 , folder.system_type
                 , folder.created_at
                 , folder.updated_at
                 , FALSE AS system_folder
                 , CAST(NULL AS text) AS item_uuid_prefix
                 , CAST(NULL AS boolean) AS private_filter
                 , CAST(NULL AS varchar) AS chat_type
            FROM m_folders AS folder
          )
        , visible_streams AS (
            SELECT m_workspace_user_streams.uuid
                 , m_workspace_user_streams.name
                 , m_workspace_user_streams.description
                 , m_workspace_user_streams.project_id
                 , m_workspace_user_streams.source_name
                 , m_workspace_user_streams.source
                 , m_workspace_user_streams.owner
                 , m_workspace_user_streams.user_uuid
                 , m_workspace_user_streams.role
                 , m_workspace_user_streams.unread_count
                 , m_workspace_user_streams.invite_only
                 , m_workspace_user_streams.announce
                 , m_workspace_user_streams.private
                 , m_workspace_user_streams.created_at
                 , m_workspace_user_streams.updated_at
                 , m_workspace_user_streams.direct_user_uuid
                 , m_workspace_user_streams.private_index
                 , m_workspace_user_streams.is_archived
                 , m_workspace_user_streams.notification_mode
                 , m_workspace_user_streams.color
                 , m_workspace_user_streams.last_message_uuid
                 , m_workspace_user_streams.default_topic_uuid
                 , m_workspace_user_streams.active_unread_count
                 , m_workspace_user_streams.passive_unread_count
            FROM m_workspace_user_streams
            WHERE m_workspace_user_streams.is_archived = FALSE
          )
        , folder_items AS (
            SELECT definition_1.uuid AS folder_uuid
                 , definition_1.project_id
                 , definition_1.user_uuid
                 , definition_1.system_folder
                 , CASE
                     WHEN definition_1.system_folder
                       THEN CAST(definition_1.item_uuid_prefix || substr(CAST(stream.uuid AS text)
                                                                       , 3) AS uuid)
                     ELSE item_1.uuid
                   END AS uuid
                 , stream.uuid AS stream_uuid
                 , item_1.order_index
                 , CAST(item_1.pinned_at AS timestamp) AS pinned_at
                 , CASE
                     WHEN definition_1.system_folder
                       THEN COALESCE(definition_1.chat_type
                                   , CASE
                                       WHEN stream.private
                                         THEN CAST('private' AS varchar)
                                       ELSE CAST('stream' AS varchar)
                                     END)
                     ELSE item_1.chat_type
                   END AS chat_type
                 , COALESCE(stream.unread_count
                          , CAST(0 AS bigint)) AS unread_count
                 , COALESCE(stream.active_unread_count, 0) AS active_unread_count
                 , COALESCE(stream.passive_unread_count, 0) AS passive_unread_count
                 , CASE
                     WHEN definition_1.system_folder
                       THEN COALESCE(CAST(item_1.created_at AS timestamp)
                                   , definition_1.created_at)
                     ELSE CAST(item_1.created_at AS timestamp)
                   END AS created_at
                 , CASE
                     WHEN definition_1.system_folder
                       THEN COALESCE(CAST(item_1.updated_at AS timestamp)
                                   , definition_1.created_at)
                     ELSE CAST(item_1.updated_at AS timestamp)
                   END AS updated_at
            FROM folder_definitions AS definition_1
                 INNER JOIN visible_streams AS stream ON stream.project_id = definition_1.project_id
                                                     AND stream.user_uuid = definition_1.user_uuid
                 LEFT JOIN m_folder_items AS item_1 ON item_1.folder_uuid = definition_1.uuid
                                                   AND item_1.stream_uuid = stream.uuid
                                                   AND item_1.project_id = definition_1.project_id
                                                   AND item_1.user_uuid = definition_1.user_uuid
            WHERE (definition_1.system_folder
               AND (definition_1.private_filter IS NULL
                 OR stream.private = definition_1.private_filter))
               OR (NOT definition_1.system_folder
               AND item_1.uuid IS NOT NULL)
          )
       SELECT definition.uuid
            , definition.project_id
            , definition.user_uuid
            , definition.title
            , definition.background_color_value
            , definition.system_type
            , CAST(COALESCE(sum(item.active_unread_count)
                          , CAST(0 AS bigint)) AS integer) AS unread_count
            , COALESCE(json_agg(CASE
                                  WHEN item.system_folder
                                    THEN json_build_object('uuid', item.uuid, 'folder', item.folder_uuid, 'project_id', item.project_id, 'user_uuid', item.user_uuid, 'stream_uuid', item.stream_uuid, 'order_index', item.order_index, 'pinned_at', item.pinned_at, 'chat_type', item.chat_type, 'unread_count', item.unread_count, 'active_unread_count', item.active_unread_count, 'passive_unread_count', item.passive_unread_count, 'created_at', item.created_at, 'updated_at', item.updated_at)
                                  ELSE json_build_object('uuid', item.uuid, 'folder_uuid', item.folder_uuid, 'project_id', item.project_id, 'user_uuid', item.user_uuid, 'stream_uuid', item.stream_uuid, 'order_index', item.order_index, 'pinned_at', item.pinned_at, 'chat_type', item.chat_type, 'unread_count', item.unread_count, 'active_unread_count', item.active_unread_count, 'passive_unread_count', item.passive_unread_count, 'created_at', item.created_at, 'updated_at', item.updated_at)
                                END ORDER BY item.created_at
                                           , item.uuid) FILTER (WHERE item.uuid IS NOT NULL)
                     , CAST('[]' AS pg_catalog.json)) AS folder_items
            , definition.created_at
            , definition.updated_at
       FROM folder_definitions AS definition
            LEFT JOIN folder_items AS item ON item.folder_uuid = definition.uuid
                                          AND item.project_id = definition.project_id
                                          AND item.user_uuid = definition.user_uuid
       GROUP BY definition.uuid, definition.project_id, definition.user_uuid, definition.title, definition.background_color_value, definition.system_type, definition.created_at, definition.updated_at;

CREATE VIEW m_folder_all_view
  AS WITH project_users AS (
            SELECT m_workspace_stream_bindings.project_id
                 , m_workspace_stream_bindings.user_uuid
            FROM m_workspace_stream_bindings

            UNION

            SELECT m_folders.project_id
                 , m_folders.user_uuid
            FROM m_folders
            WHERE m_folders.project_id <> CAST('00000000-0000-0000-0000-000000000000' AS uuid)
              AND m_folders.user_uuid <> CAST('00000000-0000-0000-0000-000000000000' AS uuid)
          )
       SELECT CAST('00000000-0000-0000-0000-000000000000' AS uuid) AS uuid
            , pu.project_id
            , pu.user_uuid
            , CAST('All chats' AS varchar) AS title
            , CAST(11184810 AS bigint) AS background_color_value
            , CAST('all' AS varchar) AS system_type
            , CAST(COALESCE(sum(i.active_unread_count)
                          , CAST(0 AS bigint)) AS integer) AS unread_count
            , COALESCE(json_agg(json_build_object('uuid', i.uuid, 'folder', i.folder, 'project_id', i.project_id, 'user_uuid', i.user_uuid, 'stream_uuid', i.stream_uuid, 'order_index', i.order_index, 'pinned_at', i.pinned_at, 'chat_type', i.chat_type, 'unread_count', i.unread_count, 'active_unread_count', i.active_unread_count, 'passive_unread_count', i.passive_unread_count, 'created_at', i.created_at, 'updated_at', i.updated_at) ORDER BY i.created_at
                                                                                                                                                                                                                                                                                                                                                                                                                                                         , i.uuid) FILTER (WHERE i.uuid IS NOT NULL)
                     , CAST('[]' AS pg_catalog.json)) AS folder_items
            , CAST('2000-01-01 00:00:00' AS timestamp) AS created_at
            , CAST('2000-01-01 00:00:00' AS timestamp) AS updated_at
       FROM project_users AS pu
            LEFT JOIN m_folder_all_items_view AS i ON i.project_id = pu.project_id
                                                  AND i.user_uuid = pu.user_uuid
       GROUP BY pu.project_id, pu.user_uuid;

CREATE VIEW m_folder_channels_view
  AS WITH project_users AS (
            SELECT m_workspace_stream_bindings.project_id
                 , m_workspace_stream_bindings.user_uuid
            FROM m_workspace_stream_bindings

            UNION

            SELECT m_folders.project_id
                 , m_folders.user_uuid
            FROM m_folders
            WHERE m_folders.project_id <> CAST('00000000-0000-0000-0000-000000000000' AS uuid)
              AND m_folders.user_uuid <> CAST('00000000-0000-0000-0000-000000000000' AS uuid)
          )
       SELECT CAST('00000000-0000-0000-0000-000000000002' AS uuid) AS uuid
            , pu.project_id
            , pu.user_uuid
            , CAST('Channels' AS varchar) AS title
            , CAST(11184810 AS bigint) AS background_color_value
            , CAST('all' AS varchar) AS system_type
            , CAST(COALESCE(sum(i.active_unread_count)
                          , CAST(0 AS bigint)) AS integer) AS unread_count
            , COALESCE(json_agg(json_build_object('uuid', i.uuid, 'folder', i.folder, 'project_id', i.project_id, 'user_uuid', i.user_uuid, 'stream_uuid', i.stream_uuid, 'order_index', i.order_index, 'pinned_at', i.pinned_at, 'chat_type', i.chat_type, 'unread_count', i.unread_count, 'active_unread_count', i.active_unread_count, 'passive_unread_count', i.passive_unread_count, 'created_at', i.created_at, 'updated_at', i.updated_at) ORDER BY i.created_at
                                                                                                                                                                                                                                                                                                                                                                                                                                                         , i.uuid) FILTER (WHERE i.uuid IS NOT NULL)
                     , CAST('[]' AS pg_catalog.json)) AS folder_items
            , CAST('2000-01-01 00:00:02' AS timestamp) AS created_at
            , CAST('2000-01-01 00:00:02' AS timestamp) AS updated_at
       FROM project_users AS pu
            LEFT JOIN m_folder_channel_items_view AS i ON i.project_id = pu.project_id
                                                      AND i.user_uuid = pu.user_uuid
       GROUP BY pu.project_id, pu.user_uuid;

CREATE VIEW m_folder_created_view
  AS SELECT f.uuid
          , f.project_id
          , f.user_uuid
          , f.title
          , f.background_color_value
          , f.system_type
          , CAST(COALESCE(sum(COALESCE(fi.active_unread_count, 0))
                        , CAST(0 AS bigint)) AS integer) AS unread_count
          , COALESCE(json_agg(json_build_object('uuid', fi.uuid, 'folder_uuid', fi.folder_uuid, 'project_id', fi.project_id, 'user_uuid', fi.user_uuid, 'stream_uuid', fi.stream_uuid, 'order_index', fi.order_index, 'pinned_at', fi.pinned_at, 'chat_type', fi.chat_type, 'unread_count', fi.unread_count, 'active_unread_count', fi.active_unread_count, 'passive_unread_count', fi.passive_unread_count, 'created_at', fi.created_at, 'updated_at', fi.updated_at)) FILTER (WHERE fi.uuid IS NOT NULL)
                   , CAST('[]' AS pg_catalog.json)) AS folder_items
          , f.created_at
          , f.updated_at
     FROM m_folders AS f
          LEFT JOIN m_folder_items_created_view AS fi ON fi.folder_uuid = f.uuid
                                                     AND fi.user_uuid = f.user_uuid
                                                     AND fi.project_id = f.project_id
     GROUP BY f.uuid, f.project_id, f.user_uuid, f.title, f.background_color_value, f.system_type, f.created_at, f.updated_at;

CREATE VIEW m_folder_personal_view
  AS WITH project_users AS (
            SELECT m_workspace_stream_bindings.project_id
                 , m_workspace_stream_bindings.user_uuid
            FROM m_workspace_stream_bindings

            UNION

            SELECT m_folders.project_id
                 , m_folders.user_uuid
            FROM m_folders
            WHERE m_folders.project_id <> CAST('00000000-0000-0000-0000-000000000000' AS uuid)
              AND m_folders.user_uuid <> CAST('00000000-0000-0000-0000-000000000000' AS uuid)
          )
       SELECT CAST('00000000-0000-0000-0000-000000000001' AS uuid) AS uuid
            , pu.project_id
            , pu.user_uuid
            , CAST('Personal' AS varchar) AS title
            , CAST(11184810 AS bigint) AS background_color_value
            , CAST('all' AS varchar) AS system_type
            , CAST(COALESCE(sum(i.active_unread_count)
                          , CAST(0 AS bigint)) AS integer) AS unread_count
            , COALESCE(json_agg(json_build_object('uuid', i.uuid, 'folder', i.folder, 'project_id', i.project_id, 'user_uuid', i.user_uuid, 'stream_uuid', i.stream_uuid, 'order_index', i.order_index, 'pinned_at', i.pinned_at, 'chat_type', i.chat_type, 'unread_count', i.unread_count, 'active_unread_count', i.active_unread_count, 'passive_unread_count', i.passive_unread_count, 'created_at', i.created_at, 'updated_at', i.updated_at) ORDER BY i.created_at
                                                                                                                                                                                                                                                                                                                                                                                                                                                         , i.uuid) FILTER (WHERE i.uuid IS NOT NULL)
                     , CAST('[]' AS pg_catalog.json)) AS folder_items
            , CAST('2000-01-01 00:00:01' AS timestamp) AS created_at
            , CAST('2000-01-01 00:00:01' AS timestamp) AS updated_at
       FROM project_users AS pu
            LEFT JOIN m_folder_private_items_view AS i ON i.project_id = pu.project_id
                                                      AND i.user_uuid = pu.user_uuid
       GROUP BY pu.project_id, pu.user_uuid;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0188-Skip-tombstoned-streams-in-binding-mirrors-fd5982.py"
        ]

    @property
    def migration_id(self):
        return "6e254a08-9767-4bf4-8371-b49b481455a0"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(CLEANUP_SQL)

    def downgrade(self, session):
        raise RuntimeError("Removed provider metadata cannot be restored")


migration_step = MigrationStep()
