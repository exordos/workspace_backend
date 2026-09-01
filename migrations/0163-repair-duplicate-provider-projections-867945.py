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


UPGRADE_SQL = r"""
-- A provider echo could be imported as a second canonical message before the
-- successful native send was bound to its realm-global Zulip message id.  The
-- two rows represent one physical message only when every content/location
-- field agrees, they were created within the echo window, and each side has
-- exactly one counterpart.  Leave every ambiguous pair untouched.
SET LOCAL lock_timeout = '30s';
SET LOCAL statement_timeout = '10min';
LOCK TABLE
    m_workspace_messages,
    messenger_messages,
    messenger_message_placements,
    messenger_user_message_bindings,
    messenger_user_message_states,
    messenger_stream_bindings,
    messenger_user_topic_bindings,
    messenger_user_folder_bindings
IN SHARE ROW EXCLUSIVE MODE;
SET LOCAL lock_timeout = '0';

CREATE TEMP TABLE messenger_v2_duplicate_provider_projection_pairs (
    project_id uuid NOT NULL,
    native_legacy_uuid uuid NOT NULL,
    projected_legacy_uuid uuid NOT NULL,
    native_message_uuid uuid NOT NULL,
    projected_message_uuid uuid NOT NULL,
    native_placement_uuid uuid NOT NULL,
    projected_placement_uuid uuid NOT NULL,
    provider_uuid uuid NOT NULL,
    external_account_uuid uuid NOT NULL,
    provider_external_id text NOT NULL,
    provider_realm_uuid uuid NOT NULL,
    provider_message_id text NOT NULL,
    stream_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    PRIMARY KEY (project_id, native_legacy_uuid),
    UNIQUE (project_id, projected_legacy_uuid)
) ON COMMIT DROP;

WITH raw_pairs AS (
    SELECT native.project_id,
           native.uuid AS native_legacy_uuid,
           projected.uuid AS projected_legacy_uuid,
           native_message.uuid AS native_message_uuid,
           projected_message.uuid AS projected_message_uuid,
           native_placement.uuid AS native_placement_uuid,
           projected_placement.uuid AS projected_placement_uuid,
           projected_message.provider_uuid,
           projected_message.external_account_uuid,
           projected_message.provider_external_id,
           projected_message.provider_realm_uuid,
           projected_message.provider_message_id,
           native.stream_uuid,
           native.topic_uuid,
           count(*) OVER (
               PARTITION BY native.project_id, native.uuid
           ) AS native_match_count,
           count(*) OVER (
               PARTITION BY projected.project_id, projected.uuid
           ) AS projected_match_count
    FROM m_workspace_messages AS native
    JOIN messenger_message_placements AS native_placement
      ON native_placement.project_id = native.project_id
     AND native_placement.legacy_public_uuid = native.uuid
    JOIN messenger_messages AS native_message
      ON native_message.project_id = native_placement.project_id
     AND native_message.uuid = native_placement.message_uuid
     AND native_message.deleted_at IS NULL
    JOIN m_workspace_messages AS projected
      ON projected.project_id = native.project_id
     AND projected.stream_uuid = native.stream_uuid
     AND projected.topic_uuid = native.topic_uuid
     AND projected.payload = native.payload
     AND abs(extract(epoch FROM projected.created_at - native.created_at)) <= 10
    JOIN messenger_message_placements AS projected_placement
      ON projected_placement.project_id = projected.project_id
     AND projected_placement.legacy_public_uuid = projected.uuid
    JOIN messenger_messages AS projected_message
      ON projected_message.project_id = projected_placement.project_id
     AND projected_message.uuid = projected_placement.message_uuid
     AND projected_message.deleted_at IS NULL
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = projected_message.external_account_uuid
     AND account.provider = 'zulip'
     AND account.provider_realm_uuid = projected_message.provider_realm_uuid
    WHERE native.source_name = 'native'
      AND native.source->>'kind' = 'native'
      AND native.provider_uuid IS NULL
      AND native.external_account_uuid IS NULL
      AND native.provider_external_id IS NULL
      AND native_message.provider_realm_uuid IS NULL
      AND native_message.provider_message_id IS NULL
      AND projected.source_name = 'zulip'
      AND projected.source->>'kind' = 'zulip'
      AND projected.provider_uuid = projected_message.provider_uuid
      AND projected.external_account_uuid =
          projected_message.external_account_uuid
      AND projected.provider_external_id =
          projected_message.provider_external_id
      AND projected_message.provider_realm_uuid IS NOT NULL
      AND projected_message.provider_message_id IS NOT NULL
      AND projected_message.provider_message_id =
          projected_message.provider_external_id
      AND projected_message.provider_message_id ~ '^(0|[1-9][0-9]*)$'
      AND char_length(projected_message.provider_message_id) <= 32
      AND projected.provider_metadata->>'kind' = 'zulip'
      AND projected.provider_metadata->>'external_id' =
          projected_message.provider_message_id
      AND projected.provider_metadata->>'provider_realm_uuid' =
          projected_message.provider_realm_uuid::text
      -- This is the incident window that began with the v2 Orion cutover.
      AND native.created_at >= timestamptz '2026-08-31 00:00+00'
)
INSERT INTO messenger_v2_duplicate_provider_projection_pairs (
    project_id, native_legacy_uuid, projected_legacy_uuid,
    native_message_uuid, projected_message_uuid,
    native_placement_uuid, projected_placement_uuid,
    provider_uuid, external_account_uuid, provider_external_id,
    provider_realm_uuid, provider_message_id, stream_uuid, topic_uuid
)
SELECT project_id, native_legacy_uuid, projected_legacy_uuid,
       native_message_uuid, projected_message_uuid,
       native_placement_uuid, projected_placement_uuid,
       provider_uuid, external_account_uuid, provider_external_id,
       provider_realm_uuid, provider_message_id, stream_uuid, topic_uuid
FROM raw_pairs
WHERE native_match_count = 1 AND projected_match_count = 1;

-- Carry user-visible state to the native placement before removing the echo.
-- A read on either duplicate means the one surviving message is read; boolean
-- flags are likewise unioned so cleanup cannot resurrect unread notifications
-- or discard a user action.
WITH projected_users AS (
    SELECT project_id, placement_uuid, user_uuid,
           min(created_at) AS created_at, max(updated_at) AS updated_at
    FROM (
        SELECT project_id, placement_uuid, user_uuid, created_at, updated_at
        FROM messenger_user_message_bindings
        UNION ALL
        SELECT project_id, placement_uuid, user_uuid, created_at, updated_at
        FROM messenger_user_message_states
    ) AS sources
    GROUP BY project_id, placement_uuid, user_uuid
)
INSERT INTO messenger_user_message_bindings (
    uuid, project_id, placement_uuid, user_uuid, membership_generation,
    relation_role, visibility, permissions, created_at, updated_at
)
SELECT messenger_uuid_v5(pair.native_placement_uuid, binding.user_uuid::text),
       pair.project_id, pair.native_placement_uuid, binding.user_uuid,
       stream_binding.membership_generation,
       CASE WHEN native_message.author_uuid = binding.user_uuid
            THEN 'author' ELSE 'member' END,
       'visible',
       '{"read":true,"react":true,"star":true,"pin":true}'::jsonb,
       binding.created_at, binding.updated_at
FROM messenger_v2_duplicate_provider_projection_pairs AS pair
JOIN projected_users AS binding
  ON binding.project_id = pair.project_id
 AND binding.placement_uuid = pair.projected_placement_uuid
JOIN messenger_stream_bindings AS stream_binding
  ON stream_binding.project_id = pair.project_id
 AND stream_binding.stream_uuid = pair.stream_uuid
 AND stream_binding.user_uuid = binding.user_uuid
 AND stream_binding.active
JOIN messenger_messages AS native_message
  ON native_message.project_id = pair.project_id
 AND native_message.uuid = pair.native_message_uuid
ON CONFLICT (project_id, placement_uuid, user_uuid) DO UPDATE SET
    membership_generation = EXCLUDED.membership_generation,
    relation_role = EXCLUDED.relation_role,
    visibility = EXCLUDED.visibility,
    permissions = EXCLUDED.permissions,
    updated_at = GREATEST(
        messenger_user_message_bindings.updated_at, EXCLUDED.updated_at
    );

WITH state_union AS (
    SELECT pair.project_id, pair.native_placement_uuid,
           stream_binding.membership_generation,
           COALESCE(native_state.user_uuid, projected_state.user_uuid)
               AS user_uuid,
           CASE
               WHEN native_state.read_at IS NULL
                AND projected_state.read_at IS NULL THEN NULL
               WHEN native_state.read_at IS NULL THEN projected_state.read_at
               WHEN projected_state.read_at IS NULL THEN native_state.read_at
               ELSE GREATEST(native_state.read_at, projected_state.read_at)
           END AS read_at,
           COALESCE(native_state.mentioned, FALSE)
             OR COALESCE(projected_state.mentioned, FALSE) AS mentioned,
           COALESCE(native_state.starred, FALSE)
             OR COALESCE(projected_state.starred, FALSE) AS starred,
           COALESCE(native_state.pinned, FALSE)
             OR COALESCE(projected_state.pinned, FALSE) AS pinned,
           LEAST(
               COALESCE(native_state.created_at, projected_state.created_at),
               COALESCE(projected_state.created_at, native_state.created_at)
           ) AS created_at,
           GREATEST(
               COALESCE(native_state.updated_at, projected_state.updated_at),
               COALESCE(projected_state.updated_at, native_state.updated_at)
           ) AS updated_at
    FROM messenger_v2_duplicate_provider_projection_pairs AS pair
    JOIN messenger_stream_bindings AS stream_binding
      ON stream_binding.project_id = pair.project_id
     AND stream_binding.stream_uuid = pair.stream_uuid
     AND stream_binding.active
    LEFT JOIN messenger_user_message_states AS native_state
      ON native_state.project_id = pair.project_id
     AND native_state.placement_uuid = pair.native_placement_uuid
     AND native_state.user_uuid = stream_binding.user_uuid
    LEFT JOIN messenger_user_message_states AS projected_state
      ON projected_state.project_id = pair.project_id
     AND projected_state.placement_uuid = pair.projected_placement_uuid
     AND projected_state.user_uuid = stream_binding.user_uuid
    WHERE COALESCE(native_state.user_uuid, projected_state.user_uuid)
          IS NOT NULL
)
INSERT INTO messenger_user_message_states (
    uuid, project_id, placement_uuid, user_uuid, membership_generation,
    read_at, mentioned, starred, pinned, created_at, updated_at
)
SELECT messenger_uuid_v5(native_placement_uuid, user_uuid::text),
       project_id, native_placement_uuid, user_uuid, membership_generation,
       read_at, mentioned, starred, pinned, created_at, updated_at
FROM state_union
ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE SET
    membership_generation = EXCLUDED.membership_generation,
    read_at = CASE
        WHEN messenger_user_message_states.read_at IS NULL THEN EXCLUDED.read_at
        WHEN EXCLUDED.read_at IS NULL THEN messenger_user_message_states.read_at
        ELSE GREATEST(messenger_user_message_states.read_at, EXCLUDED.read_at)
    END,
    mentioned = messenger_user_message_states.mentioned OR EXCLUDED.mentioned,
    starred = messenger_user_message_states.starred OR EXCLUDED.starred,
    pinned = messenger_user_message_states.pinned OR EXCLUDED.pinned,
    created_at = LEAST(
        messenger_user_message_states.created_at, EXCLUDED.created_at
    ),
    updated_at = GREATEST(
        messenger_user_message_states.updated_at, EXCLUDED.updated_at
    );

-- Release the realm-global key from the imported echo, then bind the native
-- row through the normal legacy-to-canonical trigger path.  The latter keeps
-- both legacy and v2 representations in lockstep.
UPDATE messenger_messages AS projected
SET provider_uuid = NULL,
    external_account_uuid = NULL,
    provider_external_id = NULL,
    provider_realm_uuid = NULL,
    provider_message_id = NULL,
    provider = '{}'::jsonb,
    updated_at = projected.updated_at
FROM messenger_v2_duplicate_provider_projection_pairs AS pair
WHERE projected.project_id = pair.project_id
  AND projected.uuid = pair.projected_message_uuid;

UPDATE m_workspace_messages AS native
SET provider_uuid = pair.provider_uuid,
    external_account_uuid = pair.external_account_uuid,
    provider_external_id = pair.provider_external_id,
    provider_metadata = jsonb_build_object(
        'kind', 'zulip',
        'account_uuid', pair.external_account_uuid::text,
        'external_id', pair.provider_external_id,
        'provider_realm_uuid', pair.provider_realm_uuid::text,
        'capabilities', '{}'::jsonb
    ),
    updated_at = native.updated_at
FROM messenger_v2_duplicate_provider_projection_pairs AS pair
WHERE native.project_id = pair.project_id
  AND native.uuid = pair.native_legacy_uuid;

DELETE FROM messenger_messages AS projected
USING messenger_v2_duplicate_provider_projection_pairs AS pair
WHERE projected.project_id = pair.project_id
  AND projected.uuid = pair.projected_message_uuid;

CREATE TEMP TABLE messenger_v2_duplicate_provider_projection_scopes
ON COMMIT DROP AS
SELECT DISTINCT project_id, stream_uuid, topic_uuid
FROM messenger_v2_duplicate_provider_projection_pairs;

WITH snapshots AS MATERIALIZED (
    SELECT binding.project_id, binding.user_uuid, binding.stream_uuid,
           count(state.uuid) FILTER (
               WHERE state.read_at IS NULL
           )::integer AS unread_count,
           count(state.uuid) FILTER (
               WHERE state.read_at IS NULL AND CASE
                   WHEN topic_binding.notification_mode = 'mute' THEN FALSE
                   WHEN topic_binding.notification_mode = 'follow' THEN TRUE
                   WHEN topic_binding.notification_mode = 'unmute'
                       THEN state.mentioned
                   WHEN binding.notification_mode = 'all_messages' THEN TRUE
                   WHEN binding.notification_mode = 'mentions_only'
                       THEN state.mentioned
                   ELSE FALSE
               END
           )::integer AS active_unread_count,
           (array_agg(
               placement.uuid ORDER BY message.created_at DESC, placement.uuid DESC
           ))[1] AS last_message_uuid
    FROM messenger_stream_bindings AS binding
    JOIN messenger_v2_duplicate_provider_projection_scopes AS scope
      ON scope.project_id = binding.project_id
     AND scope.stream_uuid = binding.stream_uuid
    LEFT JOIN messenger_message_placements AS placement
      ON placement.project_id = binding.project_id
     AND placement.stream_uuid = binding.stream_uuid
    LEFT JOIN messenger_messages AS message
      ON message.project_id = placement.project_id
     AND message.uuid = placement.message_uuid
     AND message.deleted_at IS NULL
    LEFT JOIN messenger_user_message_states AS state
      ON state.project_id = placement.project_id
     AND state.placement_uuid = placement.uuid
     AND state.user_uuid = binding.user_uuid
     AND state.membership_generation = binding.membership_generation
    LEFT JOIN messenger_user_topic_bindings AS topic_binding
      ON topic_binding.project_id = placement.project_id
     AND topic_binding.topic_uuid = placement.topic_uuid
     AND topic_binding.user_uuid = binding.user_uuid
    WHERE binding.active AND message.uuid IS NOT NULL
    GROUP BY binding.project_id, binding.user_uuid, binding.stream_uuid
)
UPDATE messenger_stream_bindings AS binding
SET unread_count = snapshot.unread_count,
    active_unread_count = snapshot.active_unread_count,
    passive_unread_count = snapshot.unread_count - snapshot.active_unread_count,
    last_message_uuid = snapshot.last_message_uuid,
    updated_at = NOW()
FROM snapshots AS snapshot
WHERE binding.project_id = snapshot.project_id
  AND binding.user_uuid = snapshot.user_uuid
  AND binding.stream_uuid = snapshot.stream_uuid;

WITH snapshots AS MATERIALIZED (
    SELECT binding.project_id, binding.user_uuid, binding.topic_uuid,
           count(state.uuid) FILTER (
               WHERE state.read_at IS NULL
           )::integer AS unread_count,
           count(state.uuid) FILTER (
               WHERE state.read_at IS NULL AND CASE
                   WHEN binding.notification_mode = 'mute' THEN FALSE
                   WHEN binding.notification_mode = 'follow' THEN TRUE
                   WHEN binding.notification_mode = 'unmute' THEN state.mentioned
                   WHEN stream_binding.notification_mode = 'all_messages' THEN TRUE
                   WHEN stream_binding.notification_mode = 'mentions_only'
                       THEN state.mentioned
                   ELSE FALSE
               END
           )::integer AS active_unread_count,
           (array_agg(
               placement.uuid ORDER BY message.created_at DESC, placement.uuid DESC
           ))[1] AS last_message_uuid
    FROM messenger_user_topic_bindings AS binding
    JOIN messenger_topics AS topic
      ON topic.project_id = binding.project_id
     AND topic.uuid = binding.topic_uuid
     AND topic.deleted_at IS NULL
    JOIN messenger_v2_duplicate_provider_projection_scopes AS scope
      ON scope.project_id = topic.project_id
     AND scope.topic_uuid = topic.uuid
    JOIN messenger_stream_bindings AS stream_binding
      ON stream_binding.project_id = topic.project_id
     AND stream_binding.stream_uuid = topic.stream_uuid
     AND stream_binding.user_uuid = binding.user_uuid
     AND stream_binding.active
    LEFT JOIN messenger_message_placements AS placement
      ON placement.project_id = binding.project_id
     AND placement.topic_uuid = binding.topic_uuid
    LEFT JOIN messenger_messages AS message
      ON message.project_id = placement.project_id
     AND message.uuid = placement.message_uuid
     AND message.deleted_at IS NULL
    LEFT JOIN messenger_user_message_states AS state
      ON state.project_id = placement.project_id
     AND state.placement_uuid = placement.uuid
     AND state.user_uuid = binding.user_uuid
     AND state.membership_generation = stream_binding.membership_generation
    WHERE message.uuid IS NOT NULL
    GROUP BY binding.project_id, binding.user_uuid, binding.topic_uuid
)
UPDATE messenger_user_topic_bindings AS binding
SET unread_count = snapshot.unread_count,
    active_unread_count = snapshot.active_unread_count,
    passive_unread_count = snapshot.unread_count - snapshot.active_unread_count,
    last_message_uuid = snapshot.last_message_uuid,
    updated_at = NOW()
FROM snapshots AS snapshot
WHERE binding.project_id = snapshot.project_id
  AND binding.user_uuid = snapshot.user_uuid
  AND binding.topic_uuid = snapshot.topic_uuid;

WITH affected_users AS MATERIALIZED (
    SELECT DISTINCT binding.project_id, binding.user_uuid
    FROM messenger_stream_bindings AS binding
    JOIN messenger_v2_duplicate_provider_projection_scopes AS scope
      ON scope.project_id = binding.project_id
     AND scope.stream_uuid = binding.stream_uuid
    WHERE binding.active
), snapshots AS MATERIALIZED (
    SELECT binding.project_id, binding.user_uuid, binding.folder_uuid,
           COALESCE(sum(stream_binding.active_unread_count), 0)::integer
               AS unread_count,
           COALESCE(
               jsonb_agg(
                   jsonb_build_object(
                       'uuid', item.uuid,
                       CASE WHEN binding.rule = 'custom'
                            THEN 'folder_uuid' ELSE 'folder' END,
                       item.folder_uuid,
                       'project_id', item.project_id,
                       'user_uuid', item.user_uuid,
                       'stream_uuid', item.stream_uuid,
                       'order_index', item.order_index,
                       'pinned_at', item.pinned_at::timestamp,
                       'chat_type', item.chat_type,
                       'unread_count', stream_binding.unread_count,
                       'active_unread_count', stream_binding.active_unread_count,
                       'passive_unread_count', stream_binding.passive_unread_count,
                       'created_at', item.created_at,
                       'updated_at', item.updated_at
                   ) ORDER BY item.pinned_at DESC NULLS LAST,
                              item.order_index ASC NULLS LAST,
                              item.created_at, item.uuid
               ) FILTER (
                   WHERE item.uuid IS NOT NULL
                     AND stream_binding.user_uuid IS NOT NULL
                     AND stream.uuid IS NOT NULL
               ),
               '[]'::jsonb
           ) AS folder_items_snapshot
    FROM messenger_user_folder_bindings AS binding
    JOIN affected_users AS affected
      ON affected.project_id = binding.project_id
     AND affected.user_uuid = binding.user_uuid
    LEFT JOIN messenger_folder_items AS item
      ON item.project_id = binding.project_id
     AND item.user_uuid = binding.user_uuid
     AND item.folder_uuid = binding.folder_uuid
    LEFT JOIN messenger_stream_bindings AS stream_binding
      ON stream_binding.project_id = item.project_id
     AND stream_binding.user_uuid = item.user_uuid
     AND stream_binding.stream_uuid = item.stream_uuid
     AND stream_binding.active
    LEFT JOIN messenger_streams AS stream
      ON stream.project_id = item.project_id
     AND stream.uuid = item.stream_uuid
     AND NOT stream.is_archived
     AND stream.deleted_at IS NULL
    GROUP BY binding.project_id, binding.user_uuid, binding.folder_uuid,
             binding.rule
)
UPDATE messenger_user_folder_bindings AS binding
SET unread_count = snapshot.unread_count,
    folder_items_snapshot = snapshot.folder_items_snapshot,
    snapshot_version = binding.snapshot_version + 1,
    snapshot_updated_at = NOW(),
    updated_at = NOW()
FROM snapshots AS snapshot
WHERE binding.project_id = snapshot.project_id
  AND binding.user_uuid = snapshot.user_uuid
  AND binding.folder_uuid = snapshot.folder_uuid;

DO $duplicate_provider_projection_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM messenger_v2_duplicate_provider_projection_pairs AS pair
        LEFT JOIN messenger_messages AS native_message
          ON native_message.project_id = pair.project_id
         AND native_message.uuid = pair.native_message_uuid
        LEFT JOIN m_workspace_messages AS native
          ON native.project_id = pair.project_id
         AND native.uuid = pair.native_legacy_uuid
        LEFT JOIN messenger_messages AS projected_message
          ON projected_message.project_id = pair.project_id
         AND projected_message.uuid = pair.projected_message_uuid
        LEFT JOIN m_workspace_messages AS projected
          ON projected.project_id = pair.project_id
         AND projected.uuid = pair.projected_legacy_uuid
        WHERE native_message.uuid IS NULL
           OR native_message.provider_realm_uuid IS DISTINCT FROM
              pair.provider_realm_uuid
           OR native_message.provider_message_id IS DISTINCT FROM
              pair.provider_message_id
           OR native.provider_uuid IS DISTINCT FROM pair.provider_uuid
           OR native.external_account_uuid IS DISTINCT FROM
              pair.external_account_uuid
           OR native.provider_external_id IS DISTINCT FROM
              pair.provider_external_id
           OR projected_message.uuid IS NOT NULL
           OR projected.uuid IS NOT NULL
    ) THEN
        RAISE EXCEPTION
            'Messenger v2 duplicate provider projection repair is incomplete';
    END IF;
END;
$duplicate_provider_projection_guard$;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0162-repair-provider-owner-read-state-785e06.py"]

    @property
    def migration_id(self):
        return "86794513-1402-47ed-b2a5-3f3e9aa05577"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
