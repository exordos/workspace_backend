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
-- Remove Zulip-originated messages that survived the stream-scoped reset in
-- native Workspace containers, notably imported direct-message streams.
-- Native messages in the same containers remain intact and all affected
-- compact and Messenger v2 counters are rebuilt from the retained rows.
SET LOCAL lock_timeout = '30s';
SET LOCAL statement_timeout = '45min';

LOCK TABLE m_workspace_messages IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE messenger_messages IN SHARE ROW EXCLUSIVE MODE;

DO $zulip_message_scope_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM messenger_messages AS message
        WHERE (
                message.source_name = 'zulip'
                OR message.source->>'kind' = 'zulip'
              )
          AND NOT (
                message.source_name = 'zulip'
                AND message.source->>'kind' = 'zulip'
              )
    ) THEN
        RAISE EXCEPTION
            'Zulip reset found contradictory canonical message metadata';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM m_workspace_messages AS message
        WHERE (
                message.source_name = 'zulip'
                OR message.source->>'kind' = 'zulip'
              )
          AND NOT (
                message.source_name = 'zulip'
                AND message.source->>'kind' = 'zulip'
              )
    ) THEN
        RAISE EXCEPTION
            'Zulip reset found contradictory legacy message metadata';
    END IF;

END;
$zulip_message_scope_guard$;

CREATE TEMP TABLE messenger_v2_zulip_survivor_legacy_message_reset (
    project_id uuid NOT NULL,
    legacy_public_uuid uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    ingest_sequence bigint NOT NULL,
    PRIMARY KEY (project_id, legacy_public_uuid)
) ON COMMIT DROP;

INSERT INTO messenger_v2_zulip_survivor_legacy_message_reset (
    project_id, legacy_public_uuid, stream_uuid, topic_uuid, ingest_sequence
)
SELECT legacy.project_id,
       legacy.uuid,
       legacy.stream_uuid,
       legacy.topic_uuid,
       legacy.ingest_sequence
FROM m_workspace_messages AS legacy
WHERE legacy.source_name = 'zulip'
  AND legacy.source->>'kind' = 'zulip';

CREATE TEMP TABLE messenger_v2_zulip_survivor_message_reset (
    project_id uuid NOT NULL,
    canonical_uuid uuid NOT NULL,
    legacy_public_uuid uuid,
    ingest_sequence bigint NOT NULL,
    PRIMARY KEY (project_id, canonical_uuid)
) ON COMMIT DROP;

INSERT INTO messenger_v2_zulip_survivor_message_reset (
    project_id, canonical_uuid, legacy_public_uuid, ingest_sequence
)
SELECT canonical.project_id,
       canonical.uuid,
       COALESCE(
           canonical.legacy_public_uuid,
           (
               SELECT placement.legacy_public_uuid
               FROM messenger_message_placements AS placement
               JOIN messenger_v2_zulip_survivor_legacy_message_reset AS legacy
                 ON legacy.project_id = placement.project_id
                AND legacy.legacy_public_uuid = placement.legacy_public_uuid
               WHERE placement.project_id = canonical.project_id
                 AND placement.message_uuid = canonical.uuid
               ORDER BY placement.legacy_public_uuid
               LIMIT 1
           )
       ),
       canonical.ingest_sequence
FROM messenger_messages AS canonical
WHERE (
        canonical.source_name = 'zulip'
        AND canonical.source->>'kind' = 'zulip'
      )
   OR EXISTS (
        SELECT 1
        FROM messenger_v2_zulip_survivor_legacy_message_reset AS legacy
        LEFT JOIN messenger_message_placements AS placement
          ON placement.project_id = canonical.project_id
         AND placement.message_uuid = canonical.uuid
         AND placement.legacy_public_uuid = legacy.legacy_public_uuid
        WHERE legacy.project_id = canonical.project_id
          AND (
                legacy.legacy_public_uuid = canonical.legacy_public_uuid
                OR placement.uuid IS NOT NULL
              )
      );

CREATE TEMP TABLE messenger_v2_zulip_survivor_placement_reset (
    project_id uuid NOT NULL,
    canonical_uuid uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    PRIMARY KEY (project_id, canonical_uuid, stream_uuid, topic_uuid)
) ON COMMIT DROP;

INSERT INTO messenger_v2_zulip_survivor_placement_reset (
    project_id, canonical_uuid, stream_uuid, topic_uuid
)
SELECT placement.project_id,
       placement.message_uuid,
       placement.stream_uuid,
       placement.topic_uuid
FROM messenger_message_placements AS placement
JOIN messenger_v2_zulip_survivor_message_reset AS reset
  ON reset.project_id = placement.project_id
 AND reset.canonical_uuid = placement.message_uuid;

CREATE TEMP TABLE messenger_v2_zulip_survivor_stream_reset (
    project_id uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    PRIMARY KEY (project_id, stream_uuid)
) ON COMMIT DROP;

INSERT INTO messenger_v2_zulip_survivor_stream_reset (project_id, stream_uuid)
SELECT DISTINCT placement.project_id, placement.stream_uuid
FROM messenger_v2_zulip_survivor_placement_reset AS placement
UNION
SELECT legacy.project_id, legacy.stream_uuid
FROM messenger_v2_zulip_survivor_legacy_message_reset AS legacy;

CREATE TEMP TABLE messenger_v2_zulip_survivor_topic_reset (
    project_id uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    PRIMARY KEY (project_id, topic_uuid)
) ON COMMIT DROP;

INSERT INTO messenger_v2_zulip_survivor_topic_reset (
    project_id, stream_uuid, topic_uuid
)
SELECT DISTINCT placement.project_id,
       placement.stream_uuid,
       placement.topic_uuid
FROM messenger_v2_zulip_survivor_placement_reset AS placement
UNION
SELECT legacy.project_id, legacy.stream_uuid, legacy.topic_uuid
FROM messenger_v2_zulip_survivor_legacy_message_reset AS legacy
UNION
SELECT topic.project_id, topic.stream_uuid, topic.uuid
FROM m_workspace_stream_topics AS topic
JOIN messenger_v2_zulip_survivor_stream_reset AS stream
  ON stream.project_id = topic.project_id
 AND stream.stream_uuid = topic.stream_uuid
UNION
SELECT topic.project_id, topic.stream_uuid, topic.uuid
FROM messenger_topics AS topic
JOIN messenger_v2_zulip_survivor_stream_reset AS stream
  ON stream.project_id = topic.project_id
 AND stream.stream_uuid = topic.stream_uuid
WHERE topic.deleted_at IS NULL;

DO $zulip_reset_limit_guard$
BEGIN
    IF (SELECT count(*) FROM messenger_v2_zulip_survivor_message_reset) > 1000000
       OR (
            SELECT count(*)
            FROM messenger_v2_zulip_survivor_legacy_message_reset
          ) > 1000000 THEN
        RAISE EXCEPTION 'Zulip reset exceeds the unattended message limit';
    END IF;
END;
$zulip_reset_limit_guard$;

CREATE TEMP TABLE messenger_v2_zulip_survivor_reaction_reset (
    project_id uuid NOT NULL,
    uuid uuid NOT NULL,
    PRIMARY KEY (project_id, uuid)
) ON COMMIT DROP;

INSERT INTO messenger_v2_zulip_survivor_reaction_reset (project_id, uuid)
SELECT reaction.project_id, reaction.uuid
FROM m_workspace_message_reactions AS reaction
LEFT JOIN messenger_v2_zulip_survivor_legacy_message_reset AS reset
  ON reset.project_id = reaction.project_id
 AND reset.legacy_public_uuid = reaction.message_uuid
LEFT JOIN m_external_accounts_v2 AS account
  ON account.uuid = reaction.external_account_uuid
 AND account.provider = 'zulip'
WHERE reset.legacy_public_uuid IS NOT NULL OR account.uuid IS NOT NULL;

CREATE TEMP TABLE messenger_v2_zulip_survivor_file_reset (
    uuid uuid PRIMARY KEY,
    storage_type varchar(32) NOT NULL,
    storage_id varchar(255) NOT NULL,
    storage_object_id varchar(1024) NOT NULL
) ON COMMIT DROP;

INSERT INTO messenger_v2_zulip_survivor_file_reset (
    uuid, storage_type, storage_id, storage_object_id
)
SELECT file.uuid,
       file.storage_type,
       COALESCE(file.storage_id, ''),
       file.storage_object_id
FROM m_workspace_files AS file
JOIN m_external_accounts_v2 AS account
  ON account.uuid = file.external_account_uuid
 AND account.provider = 'zulip'
WHERE file.storage_object_id LIKE 'external-content/sha256/%'
  AND NOT EXISTS (
        SELECT 1
        FROM messenger_messages AS retained
        WHERE NOT EXISTS (
                SELECT 1
                FROM messenger_v2_zulip_survivor_message_reset AS reset
                WHERE reset.project_id = retained.project_id
                  AND reset.canonical_uuid = retained.uuid
              )
          AND (
                retained.payload::text LIKE
                    '%urn:file:' || file.uuid::text || '%'
                OR retained.payload::text LIKE
                    '%urn:image:' || file.uuid::text || '%'
                OR retained.payload::text LIKE
                    '%urn:video:' || file.uuid::text || '%'
              )
      );

INSERT INTO messenger_provider_file_cleanup_tasks (
    uuid, file_uuid, storage_type, storage_id, storage_object_id,
    status, attempts, safe_error, lease_owner, lease_expires_at,
    next_retry_at, created_at, updated_at
)
SELECT messenger_uuid_v5(file.uuid, 'zulip-file-cleanup'),
       file.uuid, file.storage_type, file.storage_id,
       file.storage_object_id, 'pending', 0, NULL, NULL, NULL,
       NOW(), NOW(), NOW()
FROM messenger_v2_zulip_survivor_file_reset AS file
ON CONFLICT (uuid) DO UPDATE
SET file_uuid = EXCLUDED.file_uuid,
    storage_type = EXCLUDED.storage_type,
    storage_id = EXCLUDED.storage_id,
    storage_object_id = EXCLUDED.storage_object_id,
    status = 'pending',
    attempts = 0,
    safe_error = NULL,
    lease_owner = NULL,
    lease_expires_at = NULL,
    next_retry_at = NOW(),
    updated_at = NOW();

CREATE TEMP TABLE messenger_v2_zulip_survivor_event_entities (
    uuid uuid PRIMARY KEY,
    uuid_text text NOT NULL UNIQUE
) ON COMMIT DROP;

INSERT INTO messenger_v2_zulip_survivor_event_entities (uuid, uuid_text)
SELECT entity.uuid, entity.uuid::text
FROM (
    SELECT canonical_uuid AS uuid FROM messenger_v2_zulip_survivor_message_reset
    UNION
    SELECT legacy_public_uuid
    FROM messenger_v2_zulip_survivor_legacy_message_reset
    UNION
    SELECT uuid FROM messenger_v2_zulip_survivor_reaction_reset
    UNION
    SELECT uuid FROM messenger_v2_zulip_survivor_file_reset
) AS entity;

DELETE FROM m_workspace_events AS event
WHERE EXISTS (
        SELECT 1
        FROM messenger_v2_zulip_survivor_event_entities AS entity
        WHERE entity.uuid_text = event.payload->>'uuid'
           OR entity.uuid_text = event.payload->>'message_uuid'
           OR entity.uuid_text = event.payload->>'file_uuid'
      )
   OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(
            COALESCE(event.payload->'message_uuids', '[]'::jsonb)
        ) AS message_uuid(value)
        JOIN messenger_v2_zulip_survivor_event_entities AS entity
          ON entity.uuid_text = message_uuid.value
      );

DELETE FROM m_workspace_broadcast_message_events_v1 AS event
WHERE EXISTS (
        SELECT 1
        FROM messenger_v2_zulip_survivor_event_entities AS entity
        WHERE entity.uuid = event.entity_uuid
           OR entity.uuid_text = event.payload->>'uuid'
           OR entity.uuid_text = event.payload->>'message_uuid'
           OR entity.uuid_text = event.payload->>'file_uuid'
      )
   OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(
            COALESCE(event.payload->'message_uuids', '[]'::jsonb)
        ) AS message_uuid(value)
        JOIN messenger_v2_zulip_survivor_event_entities AS entity
          ON entity.uuid_text = message_uuid.value
      );

DELETE FROM m_workspace_event_audience_snapshots_v1 AS snapshot
WHERE NOT EXISTS (
    SELECT 1
    FROM m_workspace_broadcast_message_events_v1 AS event
    WHERE event.audience_snapshot_uuid = snapshot.uuid
);

DELETE FROM messenger_domain_outbox_events AS event
WHERE EXISTS (
        SELECT 1
        FROM messenger_v2_zulip_survivor_message_reset AS reset
        WHERE reset.project_id = event.project_id
          AND (
                event.scope_key = reset.project_id::text || ':' ||
                    reset.canonical_uuid::text
                OR event.payload->>'canonical_message_uuid' =
                    reset.canonical_uuid::text
              )
      )
   OR EXISTS (
        SELECT 1
        FROM messenger_v2_zulip_survivor_event_entities AS entity
        WHERE entity.uuid_text = event.payload->>'uuid'
           OR entity.uuid_text = event.payload->>'message_uuid'
           OR entity.uuid_text = event.payload->>'file_uuid'
      );

WITH reset_sequences AS (
    SELECT ingest_sequence FROM messenger_v2_zulip_survivor_message_reset
    UNION
    SELECT ingest_sequence
    FROM messenger_v2_zulip_survivor_legacy_message_reset
), masks AS (
    SELECT reset.ingest_sequence / 4096 AS chunk_number,
           bit_or(
               set_bit(
                   B'0'::bit(4096),
                   (reset.ingest_sequence % 4096)::integer,
                   1
               )
           ) AS covered_bits
    FROM reset_sequences AS reset
    GROUP BY reset.ingest_sequence / 4096
)
UPDATE m_workspace_user_read_chunks_v1 AS chunk
SET read_bits = chunk.read_bits & ~masks.covered_bits,
    updated_at = NOW()
FROM masks
WHERE chunk.chunk_number = masks.chunk_number
  AND (chunk.read_bits & masks.covered_bits) <> B'0'::bit(4096);

DELETE FROM m_workspace_user_read_chunks_v1
WHERE bit_count(read_bits) = 0;

ALTER TABLE m_workspace_messages DISABLE TRIGGER USER;
ALTER TABLE m_workspace_message_reactions DISABLE TRIGGER USER;
ALTER TABLE m_workspace_user_message_flags DISABLE TRIGGER USER;

DELETE FROM m_workspace_message_mentions_v1 AS mention
USING messenger_v2_zulip_survivor_legacy_message_reset AS reset
WHERE mention.project_id = reset.project_id
  AND mention.message_uuid = reset.legacy_public_uuid;

DELETE FROM m_workspace_user_message_flags AS flag
USING messenger_v2_zulip_survivor_legacy_message_reset AS reset
WHERE flag.project_id = reset.project_id
  AND flag.uuid = reset.legacy_public_uuid;

DELETE FROM m_workspace_message_reactions AS reaction
USING messenger_v2_zulip_survivor_reaction_reset AS reset
WHERE reaction.project_id = reset.project_id
  AND reaction.uuid = reset.uuid;

DELETE FROM m_workspace_messages AS message
USING messenger_v2_zulip_survivor_legacy_message_reset AS reset
WHERE message.project_id = reset.project_id
  AND message.uuid = reset.legacy_public_uuid;

ALTER TABLE m_workspace_messages ENABLE TRIGGER USER;
ALTER TABLE m_workspace_message_reactions ENABLE TRIGGER USER;
ALTER TABLE m_workspace_user_message_flags ENABLE TRIGGER USER;

DELETE FROM messenger_messages AS message
USING messenger_v2_zulip_survivor_message_reset AS reset
WHERE message.project_id = reset.project_id
  AND message.uuid = reset.canonical_uuid;

DELETE FROM m_workspace_files AS file
USING messenger_v2_zulip_survivor_file_reset AS reset
WHERE file.uuid = reset.uuid;

DELETE FROM m_external_provider_events_v1 AS event
USING m_external_accounts_v2 AS account
WHERE event.external_account_uuid = account.uuid
  AND account.provider = 'zulip';

INSERT INTO m_workspace_topic_message_stats_v1 (
    topic_uuid, project_id, stream_uuid, message_count,
    last_ingest_sequence, created_at, updated_at
)
SELECT reset.topic_uuid,
       reset.project_id,
       reset.stream_uuid,
       count(message.uuid),
       max(message.ingest_sequence),
       NOW(),
       NOW()
FROM messenger_v2_zulip_survivor_topic_reset AS reset
LEFT JOIN m_workspace_messages AS message
  ON message.project_id = reset.project_id
 AND message.topic_uuid = reset.topic_uuid
GROUP BY reset.project_id, reset.stream_uuid, reset.topic_uuid
ON CONFLICT (topic_uuid) DO UPDATE
SET project_id = EXCLUDED.project_id,
    stream_uuid = EXCLUDED.stream_uuid,
    message_count = EXCLUDED.message_count,
    last_ingest_sequence = EXCLUDED.last_ingest_sequence,
    updated_at = NOW();

WITH scopes AS (
    SELECT reset.project_id, binding.user_uuid, reset.topic_uuid
    FROM messenger_v2_zulip_survivor_topic_reset AS reset
    JOIN m_workspace_stream_bindings AS binding
      ON binding.project_id = reset.project_id
     AND binding.stream_uuid = reset.stream_uuid
), canonical AS (
    SELECT scope.project_id,
           scope.user_uuid,
           scope.topic_uuid,
           count(message.uuid) FILTER (
               WHERE COALESCE(
                   get_bit(
                       chunk.read_bits,
                       (message.ingest_sequence % 4096)::integer
                   ),
                   0
               ) = 1
           ) AS read_count
    FROM scopes AS scope
    LEFT JOIN m_workspace_messages AS message
      ON message.project_id = scope.project_id
     AND message.topic_uuid = scope.topic_uuid
    LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
      ON chunk.user_uuid = scope.user_uuid
     AND chunk.chunk_number = message.ingest_sequence / 4096
    GROUP BY scope.project_id, scope.user_uuid, scope.topic_uuid
)
INSERT INTO m_workspace_user_topic_read_stats_v1 (
    project_id, user_uuid, topic_uuid, read_count, created_at, updated_at
)
SELECT project_id, user_uuid, topic_uuid, read_count, NOW(), NOW()
FROM canonical
ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE
SET read_count = EXCLUDED.read_count,
    updated_at = NOW();

-- The published v2 cutover originally derived read state only from legacy
-- flag rows.  Compact projects keep the authoritative read bit in chunks, so
-- real native Direct messages can otherwise become unread during cutover.
WITH expected AS MATERIALIZED (
    SELECT state.project_id,
           state.uuid AS state_uuid,
           CASE
               WHEN read_project.mode IN ('compact', 'rollback') THEN
                   COALESCE(
                       get_bit(
                           chunk.read_bits,
                           (legacy.ingest_sequence % 4096)::integer
                       ),
                       0
                   ) = 1
               ELSE COALESCE(flag.read, FALSE)
           END AS is_read
    FROM messenger_user_message_states AS state
    JOIN messenger_message_placements AS placement
      ON placement.project_id = state.project_id
     AND placement.uuid = state.placement_uuid
    JOIN messenger_v2_zulip_survivor_stream_reset AS reset
      ON reset.project_id = placement.project_id
     AND reset.stream_uuid = placement.stream_uuid
    JOIN m_workspace_messages AS legacy
      ON legacy.project_id = placement.project_id
     AND legacy.uuid = COALESCE(
            placement.legacy_public_uuid,
            placement.uuid
         )
    LEFT JOIN m_workspace_read_state_projects_v1 AS read_project
      ON read_project.project_id = state.project_id
    LEFT JOIN m_workspace_user_message_flags AS flag
      ON flag.project_id = state.project_id
     AND flag.user_uuid = state.user_uuid
     AND flag.uuid = legacy.uuid
    LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
      ON chunk.user_uuid = state.user_uuid
     AND chunk.chunk_number = legacy.ingest_sequence / 4096
)
UPDATE messenger_user_message_states AS state
SET read_at = CASE
        WHEN expected.is_read THEN COALESCE(state.read_at, NOW())
        ELSE NULL
    END,
    updated_at = NOW()
FROM expected
WHERE state.project_id = expected.project_id
  AND state.uuid = expected.state_uuid
  AND (state.read_at IS NOT NULL) IS DISTINCT FROM expected.is_read;

WITH snapshots AS MATERIALIZED (
    SELECT target.project_id,
           target.user_uuid,
           target.stream_uuid,
           COALESCE(legacy.unread_count, 0)::integer AS unread_count,
           COALESCE(legacy.active_unread_count, 0)::integer
               AS active_unread_count,
           COALESCE(legacy.passive_unread_count, 0)::integer
               AS passive_unread_count,
           last_placement.uuid AS last_message_uuid
    FROM messenger_stream_bindings AS target
    JOIN messenger_v2_zulip_survivor_stream_reset AS reset
      ON reset.project_id = target.project_id
     AND reset.stream_uuid = target.stream_uuid
    LEFT JOIN m_workspace_user_streams AS legacy
      ON legacy.project_id = target.project_id
     AND legacy.user_uuid = target.user_uuid
     AND legacy.uuid = target.stream_uuid
    LEFT JOIN messenger_message_placements AS last_placement
      ON last_placement.project_id = target.project_id
     AND COALESCE(
            last_placement.legacy_public_uuid,
            last_placement.uuid
         ) = legacy.last_message_uuid
    WHERE target.active
)
UPDATE messenger_stream_bindings AS binding
SET unread_count = snapshot.unread_count,
    active_unread_count = snapshot.active_unread_count,
    passive_unread_count = snapshot.passive_unread_count,
    last_message_uuid = snapshot.last_message_uuid,
    updated_at = NOW()
FROM snapshots AS snapshot
WHERE binding.project_id = snapshot.project_id
  AND binding.user_uuid = snapshot.user_uuid
  AND binding.stream_uuid = snapshot.stream_uuid;

WITH snapshots AS MATERIALIZED (
    SELECT target.project_id,
           target.user_uuid,
           target.topic_uuid,
           COALESCE(legacy.unread_count, 0)::integer AS unread_count,
           COALESCE(legacy.active_unread_count, 0)::integer
               AS active_unread_count,
           COALESCE(legacy.passive_unread_count, 0)::integer
               AS passive_unread_count,
           last_placement.uuid AS last_message_uuid
    FROM messenger_user_topic_bindings AS target
    JOIN messenger_v2_zulip_survivor_topic_reset AS reset
      ON reset.project_id = target.project_id
     AND reset.topic_uuid = target.topic_uuid
    JOIN messenger_stream_bindings AS stream_binding
      ON stream_binding.project_id = reset.project_id
     AND stream_binding.stream_uuid = reset.stream_uuid
     AND stream_binding.user_uuid = target.user_uuid
     AND stream_binding.active
    LEFT JOIN m_workspace_user_topics_view AS legacy
      ON legacy.project_id = target.project_id
     AND legacy.user_uuid = target.user_uuid
     AND legacy.uuid = target.topic_uuid
    LEFT JOIN messenger_message_placements AS last_placement
      ON last_placement.project_id = target.project_id
     AND COALESCE(
            last_placement.legacy_public_uuid,
            last_placement.uuid
         ) = legacy.last_message_uuid
)
UPDATE messenger_user_topic_bindings AS binding
SET unread_count = snapshot.unread_count,
    active_unread_count = snapshot.active_unread_count,
    passive_unread_count = snapshot.passive_unread_count,
    last_message_uuid = snapshot.last_message_uuid,
    updated_at = NOW()
FROM snapshots AS snapshot
WHERE binding.project_id = snapshot.project_id
  AND binding.user_uuid = snapshot.user_uuid
  AND binding.topic_uuid = snapshot.topic_uuid;

-- Collection endpoints consume the canonical folder snapshots, so rebuild
-- them after the mixed native streams have their counters recomputed.
WITH snapshots AS MATERIALIZED (
    SELECT
        target.project_id,
        target.user_uuid,
        target.folder_uuid,
        COALESCE(sum(stream_binding.active_unread_count), 0)::integer
            AS unread_count,
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
                         item.created_at,
                         item.uuid
            ) FILTER (
                WHERE item.uuid IS NOT NULL
                  AND stream_binding.user_uuid IS NOT NULL
                  AND visible_stream.uuid IS NOT NULL
            ),
            '[]'::jsonb
        ) AS folder_items_snapshot
    FROM messenger_user_folder_bindings AS target
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
    snapshot_updated_at = NOW(),
    updated_at = NOW()
FROM snapshots AS snapshot
WHERE binding.project_id = snapshot.project_id
  AND binding.user_uuid = snapshot.user_uuid
  AND binding.folder_uuid = snapshot.folder_uuid;

DO $zulip_counter_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM messenger_messages AS message
        WHERE message.source_name = 'zulip'
          AND message.source->>'kind' = 'zulip'
    ) OR EXISTS (
        SELECT 1
        FROM m_workspace_messages AS message
        WHERE message.source_name = 'zulip'
          AND message.source->>'kind' = 'zulip'
    ) THEN
        RAISE EXCEPTION 'Zulip reset left provider messages behind';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM messenger_v2_zulip_survivor_topic_reset AS reset
        JOIN m_workspace_topic_message_stats_v1 AS stats
          ON stats.project_id = reset.project_id
         AND stats.topic_uuid = reset.topic_uuid
        LEFT JOIN LATERAL (
            SELECT count(message.uuid) AS message_count,
                   max(message.ingest_sequence) AS last_ingest_sequence
            FROM m_workspace_messages AS message
            WHERE message.project_id = reset.project_id
              AND message.topic_uuid = reset.topic_uuid
        ) AS actual ON TRUE
        WHERE stats.message_count IS DISTINCT FROM actual.message_count
           OR stats.last_ingest_sequence IS DISTINCT FROM
                actual.last_ingest_sequence
    ) THEN
        RAISE EXCEPTION 'Zulip reset left compact topic statistics inconsistent';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM messenger_v2_zulip_survivor_topic_reset AS reset
        JOIN m_workspace_stream_bindings AS binding
          ON binding.project_id = reset.project_id
         AND binding.stream_uuid = reset.stream_uuid
        JOIN m_workspace_user_topic_read_stats_v1 AS stats
          ON stats.project_id = reset.project_id
         AND stats.user_uuid = binding.user_uuid
         AND stats.topic_uuid = reset.topic_uuid
        LEFT JOIN LATERAL (
            SELECT count(message.uuid) FILTER (
                       WHERE COALESCE(
                           get_bit(
                               chunk.read_bits,
                               (message.ingest_sequence % 4096)::integer
                           ),
                           0
                       ) = 1
                   ) AS read_count
            FROM m_workspace_messages AS message
            LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
              ON chunk.user_uuid = binding.user_uuid
             AND chunk.chunk_number = message.ingest_sequence / 4096
            WHERE message.project_id = reset.project_id
              AND message.topic_uuid = reset.topic_uuid
        ) AS actual ON TRUE
        WHERE stats.read_count IS DISTINCT FROM actual.read_count
    ) THEN
        RAISE EXCEPTION 'Zulip reset left compact read statistics inconsistent';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM messenger_user_message_states AS state
        JOIN messenger_message_placements AS placement
          ON placement.project_id = state.project_id
         AND placement.uuid = state.placement_uuid
        JOIN messenger_v2_zulip_survivor_stream_reset AS reset
          ON reset.project_id = placement.project_id
         AND reset.stream_uuid = placement.stream_uuid
        JOIN m_workspace_messages AS legacy
          ON legacy.project_id = placement.project_id
         AND legacy.uuid = COALESCE(
                placement.legacy_public_uuid,
                placement.uuid
             )
        LEFT JOIN m_workspace_read_state_projects_v1 AS read_project
          ON read_project.project_id = state.project_id
        LEFT JOIN m_workspace_user_message_flags AS flag
          ON flag.project_id = state.project_id
         AND flag.user_uuid = state.user_uuid
         AND flag.uuid = legacy.uuid
        LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
          ON chunk.user_uuid = state.user_uuid
         AND chunk.chunk_number = legacy.ingest_sequence / 4096
        WHERE (state.read_at IS NOT NULL) IS DISTINCT FROM CASE
            WHEN read_project.mode IN ('compact', 'rollback') THEN
                COALESCE(
                    get_bit(
                        chunk.read_bits,
                        (legacy.ingest_sequence % 4096)::integer
                    ),
                    0
                ) = 1
            ELSE COALESCE(flag.read, FALSE)
        END
    ) THEN
        RAISE EXCEPTION 'Zulip reset left canonical read state inconsistent';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM messenger_stream_bindings AS canonical
        JOIN messenger_v2_zulip_survivor_stream_reset AS reset
          ON reset.project_id = canonical.project_id
         AND reset.stream_uuid = canonical.stream_uuid
        LEFT JOIN m_workspace_user_streams AS legacy
          ON legacy.project_id = canonical.project_id
         AND legacy.user_uuid = canonical.user_uuid
         AND legacy.uuid = canonical.stream_uuid
        LEFT JOIN messenger_message_placements AS last_placement
          ON last_placement.project_id = canonical.project_id
         AND COALESCE(
                last_placement.legacy_public_uuid,
                last_placement.uuid
             ) = legacy.last_message_uuid
        WHERE canonical.active
          AND (
                canonical.unread_count IS DISTINCT FROM
                    COALESCE(legacy.unread_count, 0)
                OR canonical.active_unread_count IS DISTINCT FROM
                    COALESCE(legacy.active_unread_count, 0)
                OR canonical.passive_unread_count IS DISTINCT FROM
                    COALESCE(legacy.passive_unread_count, 0)
                OR canonical.last_message_uuid IS DISTINCT FROM
                    last_placement.uuid
              )
    ) THEN
        RAISE EXCEPTION 'Zulip reset left canonical stream counters inconsistent';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM messenger_user_topic_bindings AS canonical
        JOIN messenger_v2_zulip_survivor_topic_reset AS reset
          ON reset.project_id = canonical.project_id
         AND reset.topic_uuid = canonical.topic_uuid
        JOIN messenger_stream_bindings AS membership
          ON membership.project_id = reset.project_id
         AND membership.stream_uuid = reset.stream_uuid
         AND membership.user_uuid = canonical.user_uuid
         AND membership.active
        LEFT JOIN m_workspace_user_topics_view AS legacy
          ON legacy.project_id = canonical.project_id
         AND legacy.user_uuid = canonical.user_uuid
         AND legacy.uuid = canonical.topic_uuid
        LEFT JOIN messenger_message_placements AS last_placement
          ON last_placement.project_id = canonical.project_id
         AND COALESCE(
                last_placement.legacy_public_uuid,
                last_placement.uuid
             ) = legacy.last_message_uuid
        WHERE canonical.unread_count IS DISTINCT FROM
                    COALESCE(legacy.unread_count, 0)
           OR canonical.active_unread_count IS DISTINCT FROM
                    COALESCE(legacy.active_unread_count, 0)
           OR canonical.passive_unread_count IS DISTINCT FROM
                    COALESCE(legacy.passive_unread_count, 0)
           OR canonical.last_message_uuid IS DISTINCT FROM
                    last_placement.uuid
    ) THEN
        RAISE EXCEPTION 'Zulip reset left canonical topic counters inconsistent';
    END IF;
END;
$zulip_counter_guard$;

DO $desired_account_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM m_external_accounts_v2 AS account
        WHERE account.provider = 'zulip'
          AND NOT EXISTS (
              SELECT 1
              FROM m_external_bridge_desired_resources_v1 AS desired
              WHERE desired.provider_kind = 'zulip'
                AND desired.resource_type = 'external_account'
                AND desired.resource_uuid = account.uuid
                AND desired.operation = 'upsert'
          )
    ) THEN
        RAISE EXCEPTION 'Zulip reset requires every account desired resource';
    END IF;
END;
$desired_account_guard$;

DO $desired_chat_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM m_external_chats_v2 AS chat
        WHERE chat.provider = 'zulip' AND chat.selected
          AND NOT EXISTS (
              SELECT 1
              FROM m_external_bridge_desired_resources_v1 AS desired
              WHERE desired.provider_kind = 'zulip'
                AND desired.resource_type = 'external_chat_assignment'
                AND desired.resource_uuid = chat.uuid
                AND desired.operation = 'upsert'
          )
    ) THEN
        RAISE EXCEPTION 'Zulip reset requires every selected chat resource';
    END IF;
END;
$desired_chat_guard$;

WITH reset_accounts AS (
    UPDATE m_external_accounts_v2
    SET projection_reset_generation = projection_reset_generation + 1,
        desired_generation = desired_generation + 1,
        status = CASE
            WHEN status IN ('disconnected', 'suspended', 'auth_required')
            THEN status ELSE 'backfill' END,
        live_ready = FALSE,
        safe_error = CASE
            WHEN status IN ('disconnected', 'suspended', 'auth_required')
            THEN safe_error ELSE NULL END,
        revision = revision + 1,
        updated_at = NOW()
    WHERE provider = 'zulip'
    RETURNING uuid, desired_generation, projection_reset_generation
), changed AS (
    UPDATE m_external_bridge_desired_resources_v1 AS desired
    SET generation = account.desired_generation,
        resource = jsonb_set(
            jsonb_set(
                desired.resource,
                '{generation}',
                to_jsonb(account.desired_generation),
                true
            ),
            '{projection_reset_generation}',
            to_jsonb(account.projection_reset_generation),
            true
        ),
        updated_at = NOW()
    FROM reset_accounts AS account
    WHERE desired.provider_kind = 'zulip'
      AND desired.resource_type = 'external_account'
      AND desired.resource_uuid = account.uuid
      AND desired.operation = 'upsert'
    RETURNING desired.*
)
INSERT INTO m_external_bridge_desired_changes_v1 (
    change_uuid, bridge_instance_uuid, provider_kind, resource_type,
    resource_uuid, operation, generation, required_capabilities, resource
)
SELECT gen_random_uuid(), bridge_instance_uuid, provider_kind, resource_type,
       resource_uuid, operation, generation, required_capabilities, resource
FROM changed;

WITH reset_chats AS (
    UPDATE m_external_chats_v2 AS chat
    SET status = CASE
            WHEN account.status IN ('disconnected', 'suspended')
            THEN 'deselected'
            WHEN account.status = 'auth_required'
            THEN 'degraded'
            ELSE 'syncing'
        END,
        safe_error = CASE
            WHEN account.status IN ('disconnected', 'suspended', 'auth_required')
            THEN chat.safe_error ELSE NULL END,
        revision = chat.revision + 1,
        updated_at = NOW()
    FROM m_external_accounts_v2 AS account
    WHERE chat.provider = 'zulip' AND chat.selected
      AND account.uuid = chat.external_account_uuid
      AND account.provider = chat.provider
    RETURNING chat.uuid, chat.revision
), changed AS (
    UPDATE m_external_bridge_desired_resources_v1 AS desired
    SET generation = chat.revision,
        resource = jsonb_set(
            desired.resource,
            '{generation}',
            to_jsonb(chat.revision),
            true
        ),
        updated_at = NOW()
    FROM reset_chats AS chat
    WHERE desired.provider_kind = 'zulip'
      AND desired.resource_type = 'external_chat_assignment'
      AND desired.resource_uuid = chat.uuid
      AND desired.operation = 'upsert'
    RETURNING desired.*
)
INSERT INTO m_external_bridge_desired_changes_v1 (
    change_uuid, bridge_instance_uuid, provider_kind, resource_type,
    resource_uuid, operation, generation, required_capabilities, resource
)
SELECT gen_random_uuid(), bridge_instance_uuid, provider_kind, resource_type,
       resource_uuid, operation, generation, required_capabilities, resource
FROM changed;

UPDATE m_workspace_event_cursors AS cursor
SET epoch_generation = gen_random_uuid(),
    pruned_through_epoch_version = GREATEST(
        cursor.pruned_through_epoch_version,
        cursor.current_epoch_version
    ),
    updated_at = NOW()
WHERE cursor.project_id IN (
    SELECT DISTINCT project_id FROM messenger_v2_zulip_survivor_stream_reset
);

UPDATE m_workspace_read_state_projects_v1 AS state
SET structure_revision = state.structure_revision + 1,
    updated_at = NOW()
WHERE state.project_id IN (
    SELECT DISTINCT project_id FROM messenger_v2_zulip_survivor_stream_reset
);

SELECT 'deleted_messages', count(*) FROM messenger_v2_zulip_survivor_message_reset;
SELECT 'deleted_reactions', count(*) FROM messenger_v2_zulip_survivor_reaction_reset;
SELECT 'deleted_files', count(*) FROM messenger_v2_zulip_survivor_file_reset;

"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self) -> None:
        self._depends = ["0157-reset-zulip-projections-9a596b.py"]

    @property
    def migration_id(self) -> str:
        return "c1e8bf60-ff3c-4027-9b8c-410bec2c959d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session) -> None:
        session.execute(UPGRADE_SQL)

    def downgrade(self, session) -> None:
        # Provider projections are restored by a fresh Bridge backfill rather
        # than by replaying potentially stale database rows.
        session.execute("SELECT 1")


migration_step = MigrationStep()
