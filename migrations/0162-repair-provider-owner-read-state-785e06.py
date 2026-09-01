# Copyright 2016 Eugene Frolov <eugene@frolov.net.ru>
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations

COMPACT_AWARE_STATE_MIRROR = """
CREATE OR REPLACE FUNCTION messenger_v2_mirror_state_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    legacy_message_uuid uuid;
    target_project_id uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    target_project_id := CASE WHEN TG_OP = 'DELETE'
                              THEN OLD.project_id ELSE NEW.project_id END;
    IF EXISTS (
        SELECT 1
        FROM m_workspace_read_state_projects_v1 AS read_project
        WHERE read_project.project_id = target_project_id
          AND read_project.mode = 'compact'
    ) THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT COALESCE(placement.legacy_public_uuid, placement.uuid)
          INTO legacy_message_uuid
        FROM messenger_message_placements AS placement
        WHERE placement.project_id = OLD.project_id
          AND placement.uuid = OLD.placement_uuid;
        DELETE FROM m_workspace_user_message_flags
        WHERE uuid = legacy_message_uuid AND user_uuid = OLD.user_uuid;
        RETURN OLD;
    END IF;
    SELECT COALESCE(placement.legacy_public_uuid, placement.uuid)
      INTO legacy_message_uuid
    FROM messenger_message_placements AS placement
    WHERE placement.project_id = NEW.project_id
      AND placement.uuid = NEW.placement_uuid;
    INSERT INTO m_workspace_user_message_flags (
        uuid, user_uuid, project_id, read, pinned, starred,
        created_at, updated_at
    ) VALUES (
        legacy_message_uuid, NEW.user_uuid, NEW.project_id,
        NEW.read_at IS NOT NULL, NEW.pinned, NEW.starred,
        NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (uuid, user_uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        read = EXCLUDED.read,
        pinned = EXCLUDED.pinned,
        starred = EXCLUDED.starred,
        updated_at = EXCLUDED.updated_at;
    RETURN NEW;
END;
$$;
"""


LEGACY_STATE_MIRROR = COMPACT_AWARE_STATE_MIRROR.replace(
    """    target_project_id := CASE WHEN TG_OP = 'DELETE'
                              THEN OLD.project_id ELSE NEW.project_id END;
    IF EXISTS (
        SELECT 1
        FROM m_workspace_read_state_projects_v1 AS read_project
        WHERE read_project.project_id = target_project_id
          AND read_project.mode = 'compact'
    ) THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
""",
    "",
).replace("    target_project_id uuid;\n", "")


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0161-Unblock-interleaved-provider-read-pages-d06433.py"]

    @property
    def migration_id(self):
        return "785e0630-5d35-4324-bd53-4a03ce63c08c"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(COMPACT_AWARE_STATE_MIRROR)
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                m_external_provider_events_v1_applied_message_target_idx
                ON m_external_provider_events_v1 (
                    project_id, target_uuid, external_account_uuid
                )
                WHERE status = 'applied'
                  AND event_kind = 'message.upsert'
                  AND target_uuid IS NOT NULL;

            CREATE TEMP TABLE messenger_v2_provider_owner_repair_targets (
                project_id uuid NOT NULL,
                owner_user_uuid uuid NOT NULL,
                legacy_message_uuid uuid NOT NULL,
                placement_uuid uuid NOT NULL,
                stream_uuid uuid NOT NULL,
                topic_uuid uuid NOT NULL,
                membership_generation integer NOT NULL,
                author_uuid uuid NOT NULL,
                effective_read boolean NOT NULL,
                mentioned boolean NOT NULL,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                PRIMARY KEY (project_id, owner_user_uuid, placement_uuid)
            ) ON COMMIT DROP;

            INSERT INTO messenger_v2_provider_owner_repair_targets (
                project_id, owner_user_uuid, legacy_message_uuid,
                placement_uuid, stream_uuid, topic_uuid,
                membership_generation, author_uuid, effective_read,
                mentioned, created_at, updated_at
            )
            SELECT event.project_id, account.owner_user_uuid, legacy.uuid,
                   placement.uuid, placement.stream_uuid, placement.topic_uuid,
                   stream_binding.membership_generation,
                   canonical.author_uuid,
                   CASE
                       WHEN read_project.mode IN ('compact', 'rollback') THEN
                           COALESCE(
                               get_bit(
                                   read_chunk.read_bits,
                                   (legacy.ingest_sequence % 4096)::integer
                               ),
                               0
                           ) = 1
                       ELSE COALESCE(legacy_flag.read, FALSE)
                   END,
                   POSITION(
                       '](urn:user:' || lower(account.owner_user_uuid::text) || ')'
                       IN lower(COALESCE(canonical.payload->>'content', ''))
                   ) > 0,
                   canonical.created_at, canonical.updated_at
            FROM m_external_provider_events_v1 AS event
            JOIN m_external_accounts_v2 AS account
              ON account.uuid = event.external_account_uuid
             AND account.provider = 'zulip'
            JOIN m_workspace_messages AS legacy
              ON legacy.project_id = event.project_id
             AND legacy.uuid = event.target_uuid
             AND legacy.source_name = 'zulip'
            JOIN messenger_message_placements AS placement
              ON placement.project_id = legacy.project_id
             AND placement.legacy_public_uuid = legacy.uuid
            JOIN messenger_messages AS canonical
              ON canonical.project_id = placement.project_id
             AND canonical.uuid = placement.message_uuid
             AND canonical.deleted_at IS NULL
            JOIN messenger_stream_bindings AS stream_binding
              ON stream_binding.project_id = placement.project_id
             AND stream_binding.stream_uuid = placement.stream_uuid
             AND stream_binding.user_uuid = account.owner_user_uuid
             AND stream_binding.active
            LEFT JOIN m_workspace_read_state_projects_v1 AS read_project
              ON read_project.project_id = event.project_id
            LEFT JOIN m_workspace_user_message_flags AS legacy_flag
              ON legacy_flag.project_id = event.project_id
             AND legacy_flag.user_uuid = account.owner_user_uuid
             AND legacy_flag.uuid = legacy.uuid
            LEFT JOIN m_workspace_user_read_chunks_v1 AS read_chunk
              ON read_chunk.user_uuid = account.owner_user_uuid
             AND read_chunk.chunk_number = legacy.ingest_sequence / 4096
            WHERE event.status = 'applied'
              AND event.event_kind = 'message.upsert'
              AND event.target_uuid IS NOT NULL
            ON CONFLICT (project_id, owner_user_uuid, placement_uuid)
            DO NOTHING;

            INSERT INTO messenger_user_message_bindings (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, relation_role, visibility,
                permissions, created_at, updated_at
            )
            SELECT messenger_uuid_v5(
                       target.placement_uuid,
                       target.owner_user_uuid::text
                   ),
                   target.project_id, target.placement_uuid,
                   target.owner_user_uuid, target.membership_generation,
                   CASE WHEN target.author_uuid = target.owner_user_uuid
                        THEN 'author' ELSE 'member' END,
                   'visible',
                   '{"read":true,"react":true,"star":true,"pin":true}'::jsonb,
                   target.created_at, target.updated_at
            FROM messenger_v2_provider_owner_repair_targets AS target
            ON CONFLICT (project_id, placement_uuid, user_uuid) DO UPDATE SET
                membership_generation = EXCLUDED.membership_generation,
                relation_role = EXCLUDED.relation_role,
                visibility = EXCLUDED.visibility,
                permissions = EXCLUDED.permissions,
                updated_at = EXCLUDED.updated_at;

            INSERT INTO messenger_user_message_states (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, read_at, mentioned,
                created_at, updated_at
            )
            SELECT messenger_uuid_v5(
                       target.placement_uuid,
                       target.owner_user_uuid::text
                   ),
                   target.project_id, target.placement_uuid,
                   target.owner_user_uuid, target.membership_generation,
                   CASE WHEN target.effective_read THEN target.updated_at END,
                   target.mentioned, target.created_at, target.updated_at
            FROM messenger_v2_provider_owner_repair_targets AS target
            ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE SET
                membership_generation = EXCLUDED.membership_generation,
                read_at = EXCLUDED.read_at,
                mentioned = EXCLUDED.mentioned,
                updated_at = EXCLUDED.updated_at;

            CREATE TEMP TABLE messenger_v2_provider_owner_repair_scopes
            ON COMMIT DROP AS
            SELECT DISTINCT project_id, owner_user_uuid AS user_uuid,
                            stream_uuid
            FROM messenger_v2_provider_owner_repair_targets;

            WITH snapshots AS MATERIALIZED (
                SELECT target.project_id, target.user_uuid, target.stream_uuid,
                       count(state.uuid) FILTER (
                           WHERE state.read_at IS NULL
                       )::integer AS unread_count,
                       count(state.uuid) FILTER (
                           WHERE state.read_at IS NULL AND CASE
                               WHEN topic_binding.notification_mode = 'mute'
                                   THEN FALSE
                               WHEN topic_binding.notification_mode = 'follow'
                                   THEN TRUE
                               WHEN topic_binding.notification_mode = 'unmute'
                                   THEN state.mentioned
                               WHEN target.notification_mode = 'all_messages'
                                   THEN TRUE
                               WHEN target.notification_mode = 'mentions_only'
                                   THEN state.mentioned
                               ELSE FALSE
                           END
                       )::integer AS active_unread_count,
                       (array_agg(
                           placement.uuid
                           ORDER BY message.created_at DESC, placement.uuid DESC
                       ))[1] AS last_message_uuid
                FROM messenger_stream_bindings AS target
                JOIN messenger_v2_provider_owner_repair_scopes AS scope
                  ON scope.project_id = target.project_id
                 AND scope.user_uuid = target.user_uuid
                 AND scope.stream_uuid = target.stream_uuid
                LEFT JOIN messenger_message_placements AS placement
                  ON placement.project_id = target.project_id
                 AND placement.stream_uuid = target.stream_uuid
                LEFT JOIN messenger_messages AS message
                  ON message.project_id = placement.project_id
                 AND message.uuid = placement.message_uuid
                 AND message.deleted_at IS NULL
                LEFT JOIN messenger_user_message_states AS state
                  ON state.project_id = placement.project_id
                 AND state.placement_uuid = placement.uuid
                 AND state.user_uuid = target.user_uuid
                 AND state.membership_generation = target.membership_generation
                LEFT JOIN messenger_user_topic_bindings AS topic_binding
                  ON topic_binding.project_id = placement.project_id
                 AND topic_binding.topic_uuid = placement.topic_uuid
                 AND topic_binding.user_uuid = target.user_uuid
                WHERE target.active
                  AND message.uuid IS NOT NULL
                GROUP BY target.project_id, target.user_uuid,
                         target.stream_uuid
            )
            UPDATE messenger_stream_bindings AS binding
            SET unread_count = snapshot.unread_count,
                active_unread_count = snapshot.active_unread_count,
                passive_unread_count =
                    snapshot.unread_count - snapshot.active_unread_count,
                last_message_uuid = snapshot.last_message_uuid,
                updated_at = NOW()
            FROM snapshots AS snapshot
            WHERE binding.project_id = snapshot.project_id
              AND binding.user_uuid = snapshot.user_uuid
              AND binding.stream_uuid = snapshot.stream_uuid;

            WITH snapshots AS MATERIALIZED (
                SELECT target.project_id, target.user_uuid, target.topic_uuid,
                       count(state.uuid) FILTER (
                           WHERE state.read_at IS NULL
                       )::integer AS unread_count,
                       count(state.uuid) FILTER (
                           WHERE state.read_at IS NULL AND CASE
                               WHEN target.notification_mode = 'mute' THEN FALSE
                               WHEN target.notification_mode = 'follow' THEN TRUE
                               WHEN target.notification_mode = 'unmute'
                                   THEN state.mentioned
                               WHEN stream_binding.notification_mode =
                                    'all_messages' THEN TRUE
                               WHEN stream_binding.notification_mode =
                                    'mentions_only' THEN state.mentioned
                               ELSE FALSE
                           END
                       )::integer AS active_unread_count,
                       (array_agg(
                           placement.uuid
                           ORDER BY message.created_at DESC, placement.uuid DESC
                       ))[1] AS last_message_uuid
                FROM messenger_user_topic_bindings AS target
                JOIN messenger_topics AS topic
                  ON topic.project_id = target.project_id
                 AND topic.uuid = target.topic_uuid
                 AND topic.deleted_at IS NULL
                JOIN messenger_v2_provider_owner_repair_scopes AS scope
                  ON scope.project_id = topic.project_id
                 AND scope.user_uuid = target.user_uuid
                 AND scope.stream_uuid = topic.stream_uuid
                JOIN messenger_stream_bindings AS stream_binding
                  ON stream_binding.project_id = topic.project_id
                 AND stream_binding.stream_uuid = topic.stream_uuid
                 AND stream_binding.user_uuid = target.user_uuid
                 AND stream_binding.active
                LEFT JOIN messenger_message_placements AS placement
                  ON placement.project_id = target.project_id
                 AND placement.topic_uuid = target.topic_uuid
                LEFT JOIN messenger_messages AS message
                  ON message.project_id = placement.project_id
                 AND message.uuid = placement.message_uuid
                 AND message.deleted_at IS NULL
                LEFT JOIN messenger_user_message_states AS state
                  ON state.project_id = placement.project_id
                 AND state.placement_uuid = placement.uuid
                 AND state.user_uuid = target.user_uuid
                 AND state.membership_generation =
                     stream_binding.membership_generation
                WHERE message.uuid IS NOT NULL
                GROUP BY target.project_id, target.user_uuid,
                         target.topic_uuid
            )
            UPDATE messenger_user_topic_bindings AS binding
            SET unread_count = snapshot.unread_count,
                active_unread_count = snapshot.active_unread_count,
                passive_unread_count =
                    snapshot.unread_count - snapshot.active_unread_count,
                last_message_uuid = snapshot.last_message_uuid,
                updated_at = NOW()
            FROM snapshots AS snapshot
            WHERE binding.project_id = snapshot.project_id
              AND binding.user_uuid = snapshot.user_uuid
              AND binding.topic_uuid = snapshot.topic_uuid;

            WITH affected_users AS MATERIALIZED (
                SELECT DISTINCT project_id, user_uuid
                FROM messenger_v2_provider_owner_repair_scopes
            ), snapshots AS MATERIALIZED (
                SELECT target.project_id, target.user_uuid, target.folder_uuid,
                       COALESCE(sum(
                           stream_binding.active_unread_count
                       ), 0)::integer AS unread_count,
                       COALESCE(
                           jsonb_agg(
                               jsonb_build_object(
                                   'uuid', item.uuid,
                                   CASE WHEN target.rule = 'custom'
                                        THEN 'folder_uuid' ELSE 'folder' END,
                                   item.folder_uuid,
                                   'project_id', item.project_id,
                                   'user_uuid', item.user_uuid,
                                   'stream_uuid', item.stream_uuid,
                                   'order_index', item.order_index,
                                   'pinned_at', item.pinned_at::timestamp,
                                   'chat_type', item.chat_type,
                                   'unread_count', stream_binding.unread_count,
                                   'active_unread_count',
                                       stream_binding.active_unread_count,
                                   'passive_unread_count',
                                       stream_binding.passive_unread_count,
                                   'created_at', item.created_at,
                                   'updated_at', item.updated_at
                               )
                               ORDER BY item.pinned_at DESC NULLS LAST,
                                        item.order_index ASC NULLS LAST,
                                        item.created_at, item.uuid
                           ) FILTER (
                               WHERE item.uuid IS NOT NULL
                                 AND stream_binding.user_uuid IS NOT NULL
                                 AND visible_stream.uuid IS NOT NULL
                           ),
                           '[]'::jsonb
                       ) AS folder_items_snapshot
                FROM messenger_user_folder_bindings AS target
                JOIN affected_users AS affected
                  ON affected.project_id = target.project_id
                 AND affected.user_uuid = target.user_uuid
                LEFT JOIN messenger_folder_items AS item
                  ON item.project_id = target.project_id
                 AND item.user_uuid = target.user_uuid
                 AND item.folder_uuid = target.folder_uuid
                LEFT JOIN messenger_stream_bindings AS stream_binding
                  ON stream_binding.project_id = item.project_id
                 AND stream_binding.user_uuid = item.user_uuid
                 AND stream_binding.stream_uuid = item.stream_uuid
                 AND stream_binding.active
                LEFT JOIN messenger_streams AS visible_stream
                  ON visible_stream.project_id = item.project_id
                 AND visible_stream.uuid = item.stream_uuid
                 AND NOT visible_stream.is_archived
                 AND visible_stream.deleted_at IS NULL
                GROUP BY target.project_id, target.user_uuid,
                         target.folder_uuid, target.rule
            )
            UPDATE messenger_user_folder_bindings AS binding
            SET unread_count = snapshot.unread_count,
                folder_items_snapshot = snapshot.folder_items_snapshot,
                snapshot_version = binding.snapshot_version + 1,
                snapshot_updated_at = NOW(), updated_at = NOW()
            FROM snapshots AS snapshot
            WHERE binding.project_id = snapshot.project_id
              AND binding.user_uuid = snapshot.user_uuid
              AND binding.folder_uuid = snapshot.folder_uuid;

            DO $provider_owner_repair_guard$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM messenger_v2_provider_owner_repair_targets AS target
                    LEFT JOIN messenger_user_message_bindings AS binding
                      ON binding.project_id = target.project_id
                     AND binding.placement_uuid = target.placement_uuid
                     AND binding.user_uuid = target.owner_user_uuid
                     AND binding.membership_generation =
                         target.membership_generation
                    LEFT JOIN messenger_user_message_states AS state
                      ON state.project_id = target.project_id
                     AND state.placement_uuid = target.placement_uuid
                     AND state.user_uuid = target.owner_user_uuid
                     AND state.membership_generation =
                         target.membership_generation
                    WHERE binding.uuid IS NULL OR state.uuid IS NULL
                       OR (state.read_at IS NOT NULL) IS DISTINCT FROM
                          target.effective_read
                ) THEN
                    RAISE EXCEPTION
                        'Provider owner message state repair is incomplete';
                END IF;
            END;
            $provider_owner_repair_guard$;
            """
        )

    def downgrade(self, session):
        session.execute(LEGACY_STATE_MIRROR)


migration_step = MigrationStep()
