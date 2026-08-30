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

import os
import typing

from restalchemy.storage.sql import migrations

from workspace.external_bridge_control import identity_linking


UPGRADE_SQL = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE messenger_project_users (
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_uuid)
);

-- A normal unattended upgrade is deliberately bounded.  Sites above this
-- legacy-message limit must first rehearse the frozen cutover against a
-- restored copy and explicitly authorize the large transaction on the
-- migration connection with:
--
--   SET workspace.messenger_v2_large_cutover_authorized = 'on';
--
-- Migration runners can set the equivalent process environment variable
-- WORKSPACE_MESSENGER_V2_LARGE_CUTOVER_AUTHORIZED=on.
--
-- The optional row-limit setting exists for deterministic acceptance tests;
-- lowering it does not authorize a larger cutover.
DO $messenger_v2_cutover_size_guard$
DECLARE
    row_limit bigint := COALESCE(
        NULLIF(
            current_setting(
                'workspace.messenger_v2_cutover_row_limit', TRUE
            ),
            ''
        )::bigint,
        1000000
    );
    rows_seen bigint;
BEGIN
    IF row_limit < 1 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'messenger v2 migration blocked: cutover row limit must be positive';
    END IF;
    IF current_setting(
        'workspace.messenger_v2_large_cutover_authorized', TRUE
    ) IS DISTINCT FROM 'on' THEN
        IF row_limit > 1000000 THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'messenger v2 migration blocked: raising the unattended cutover limit requires explicit large-cutover authorization';
        END IF;
        SELECT count(*) INTO rows_seen
        FROM (
            SELECT 1
            FROM m_workspace_messages
            LIMIT row_limit + 1
        ) AS bounded_legacy_messages;
        IF rows_seen > row_limit THEN
            RAISE EXCEPTION USING
                ERRCODE = '54000',
                MESSAGE = format(
                    'messenger v2 migration blocked: more than %s legacy messages require an explicitly authorized rehearsed cutover',
                    row_limit
                );
        END IF;
    END IF;
END
$messenger_v2_cutover_size_guard$;

-- Do not wait indefinitely for a writer freeze.  The whole frozen conversion
-- is also bounded so an accidentally large/unrehearsed transaction rolls back
-- instead of holding application writers forever.
SET LOCAL lock_timeout = '30s';
SET LOCAL statement_timeout = '30min';

-- Freeze legacy producers for the copy-and-trigger cutover.  These locks are
-- held until commit, so no old-process write can land between a backfill
-- statement and installation of its rolling mirror trigger.
LOCK TABLE m_workspace_users, m_workspace_streams, m_workspace_stream_bindings,
    m_workspace_stream_topics, m_workspace_user_topic_flags,
    m_folders, m_folder_items, m_workspace_messages,
    m_workspace_user_message_flags, m_workspace_message_reactions,
    m_workspace_files, m_workspace_file_accesses,
    m_workspace_message_mentions_v1, m_workspace_drafts,
    m_workspace_read_memberships_v1,
    m_workspace_user_topic_read_stats_v1,
    m_workspace_topic_message_stats_v1,
    m_workspace_topic_summary_jobs,
    m_workspace_topic_summary_journal,
    m_workspace_events, m_workspace_broadcast_message_events_v1,
    m_workspace_event_audience_members_v1,
    m_workspace_event_recipient_payloads_v1,
    m_external_accounts_v2, m_external_chats_v2,
    m_external_provider_identity_links_v1,
    m_external_bridge_desired_resources_v1,
    m_external_bridge_desired_changes_v1,
    m_external_provider_events_v1, m_external_operations_v2,
    m_external_provider_operations_v1,
    m_external_provider_read_snapshots_v1,
    m_external_projection_transitions_v1
IN SHARE ROW EXCLUSIVE MODE;

SET LOCAL lock_timeout = '0';

-- Fail closed before destructive work unless every row carrying any Zulip
-- signal has either consistent inbound provenance or a durable local
-- message.create operation.  source_name alone is intentionally insufficient:
-- old deployments may have incomplete source fields, while local outbound
-- messages acquire provider identifiers after their echo is reconciled.
DO $messenger_v2_message_provenance_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM m_workspace_messages AS message
        WHERE (
                message.source_name = 'zulip'
                OR message.source->>'kind' = 'zulip'
                OR EXISTS (
                    SELECT 1
                    FROM m_external_accounts_v2 AS account
                    WHERE account.uuid = message.external_account_uuid
                      AND account.provider = 'zulip'
                )
              )
          AND NOT (
                (
                    message.source_name = 'zulip'
                    AND message.source->>'kind' = 'zulip'
                    AND message.source->>'message_id' IS NOT NULL
                    AND (
                        message.provider_external_id IS NULL
                        OR message.provider_external_id =
                           message.source->>'message_id'
                    )
                    AND (
                        message.external_account_uuid IS NULL
                        OR EXISTS (
                            SELECT 1
                            FROM m_external_accounts_v2 AS account
                            WHERE account.uuid =
                                  message.external_account_uuid
                              AND account.provider = 'zulip'
                        )
                    )
                    AND (
                        (
                            message.external_account_uuid IS NOT NULL
                            AND message.provider_external_id =
                                message.source->>'message_id'
                            AND EXISTS (
                                SELECT 1
                                FROM m_external_accounts_v2 AS account
                                WHERE account.uuid =
                                      message.external_account_uuid
                                  AND account.provider = 'zulip'
                            )
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM m_workspace_streams AS stream
                            WHERE stream.project_id = message.project_id
                              AND stream.uuid = message.stream_uuid
                              AND stream.source_name = 'zulip'
                              AND stream.source->>'kind' = 'zulip'
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM m_zulip_processed_entities AS entity
                            WHERE entity.project_id = message.project_id
                              AND entity.workspace_uuid = message.uuid
                              AND entity.entity_type = 'message'
                        )
                    )
                )
                OR EXISTS (
                    SELECT 1
                    FROM m_external_operations_v2 AS operation
                    WHERE operation.action = 'message.create'
                      AND operation.target_type = 'message'
                      AND operation.target_uuid = message.uuid
                      AND operation.owner_user_uuid = message.user_uuid
                      AND (
                            message.external_account_uuid IS NULL
                            OR operation.external_account_uuid =
                               message.external_account_uuid
                          )
                )
              )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'messenger v2 migration blocked: ambiguous legacy Zulip message provenance';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM m_workspace_messages AS message
        WHERE message.source_name = 'zulip'
          AND message.source->>'kind' = 'zulip'
          AND EXISTS (
                SELECT 1
                FROM m_external_operations_v2 AS operation
                WHERE operation.action = 'message.create'
                  AND operation.target_type = 'message'
                  AND operation.target_uuid = message.uuid
                  AND operation.owner_user_uuid = message.user_uuid
                  AND (
                        message.external_account_uuid IS NULL
                        OR operation.external_account_uuid =
                           message.external_account_uuid
                      )
          )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'messenger v2 migration blocked: conflicting inbound and local outbound message provenance';
    END IF;
END
$messenger_v2_message_provenance_guard$;

-- Messenger v2 owns each provider-realm chat in exactly one Workspace
-- project.  Legacy account-scoped projections can contain aliases inside one
-- project (collapsed below), but choosing a winner between projects would
-- silently move or hide native Workspace rows.  Reject that ambiguous legacy
-- state before the reset/copy performs any destructive work.
DO $messenger_v2_provider_scope_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM m_external_chats_v2 AS chat
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = chat.external_account_uuid
         AND account.provider = chat.provider
        WHERE chat.selected
          AND chat.project_id IS NOT NULL
          AND account.provider_realm_uuid IS NOT NULL
        GROUP BY chat.provider, account.provider_realm_uuid,
                 chat.provider_chat_id
        HAVING count(DISTINCT chat.project_id) > 1
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'messenger v2 migration blocked: a provider realm chat is selected in multiple projects';
    END IF;
END
$messenger_v2_provider_scope_guard$;

INSERT INTO messenger_project_users (project_id, user_uuid)
SELECT DISTINCT project_id, user_uuid
FROM (
    SELECT project_id, user_uuid FROM m_workspace_streams
    UNION ALL
    SELECT project_id, direct_user_uuid FROM m_workspace_streams
    WHERE direct_user_uuid IS NOT NULL
    UNION ALL
    SELECT project_id, user_uuid FROM m_workspace_stream_bindings
    UNION ALL
    SELECT project_id, who_uuid FROM m_workspace_stream_bindings
    UNION ALL
    SELECT project_id, user_uuid FROM m_workspace_messages
    UNION ALL
    SELECT project_id, user_uuid FROM m_workspace_message_reactions
    UNION ALL
    SELECT project_id, user_uuid FROM m_folders
    UNION ALL
    SELECT project_id, user_uuid FROM m_workspace_events
    UNION ALL
    SELECT event.project_id, member.user_uuid
    FROM m_workspace_broadcast_message_events_v1 AS event
    JOIN m_workspace_event_audience_members_v1 AS member
      ON member.audience_snapshot_uuid = event.audience_snapshot_uuid
) AS project_users
JOIN m_workspace_users AS existing_user
  ON existing_user.uuid = project_users.user_uuid
WHERE project_users.user_uuid IS NOT NULL
ON CONFLICT (project_id, user_uuid) DO NOTHING;

CREATE OR REPLACE FUNCTION messenger_uuid_v5(namespace_uuid uuid, name text)
RETURNS uuid
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
DECLARE
    value bytea;
BEGIN
    value := substring(
        digest(uuid_send(namespace_uuid) || convert_to(name, 'UTF8'), 'sha1')
        FROM 1 FOR 16
    );
    value := set_byte(value, 6, (get_byte(value, 6) & 15) | 80);
    value := set_byte(value, 8, (get_byte(value, 8) & 63) | 128);
    RETURN encode(value, 'hex')::uuid;
END;
$$;

CREATE OR REPLACE FUNCTION messenger_v2_provider_message_uuid(
    provider_metadata jsonb,
    provider_external_id text,
    fallback_uuid uuid
)
RETURNS uuid
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    provider_realm text;
BEGIN
    provider_realm := provider_metadata->>'provider_realm_uuid';
    IF provider_realm IS NOT NULL
       AND provider_external_id ~ '^(0|[1-9][0-9]*)$'
    THEN
        RETURN messenger_uuid_v5(
            provider_realm::uuid,
            'message:' || provider_external_id
        );
    END IF;
    RETURN fallback_uuid;
END;
$$;

-- Native Workspace data is authoritative and is copied below.  Zulip messages
-- and files are a rebuildable projection: remove the proven provider rows in
-- the same frozen migration, retain account/chat configuration, and publish a
-- monotonic reset generation that makes every Bridge perform a fresh import.
ALTER TABLE m_external_accounts_v2
    ADD COLUMN projection_reset_generation bigint NOT NULL DEFAULT 0;
ALTER TABLE m_external_accounts_v2
    ADD CONSTRAINT m_external_accounts_v2_projection_reset_generation_check
    CHECK (projection_reset_generation >= 0);

CREATE TABLE messenger_provider_file_cleanup_tasks (
    uuid uuid PRIMARY KEY,
    file_uuid uuid NOT NULL UNIQUE,
    storage_type varchar(32) NOT NULL,
    storage_id varchar(255) NOT NULL DEFAULT '',
    storage_object_id varchar(1024) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    safe_error text,
    lease_owner varchar(255),
    lease_expires_at timestamp with time zone,
    next_retry_at timestamp with time zone NOT NULL DEFAULT now(),
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    CHECK (attempts >= 0)
);
CREATE INDEX messenger_provider_file_cleanup_ready_idx
    ON messenger_provider_file_cleanup_tasks (
        status, next_retry_at, lease_expires_at, created_at, uuid
    ) WHERE status IN ('pending', 'running', 'failed');

CREATE TEMP TABLE messenger_v2_zulip_message_reset (
    project_id uuid NOT NULL,
    uuid uuid NOT NULL,
    PRIMARY KEY (project_id, uuid)
) ON COMMIT DROP;
INSERT INTO messenger_v2_zulip_message_reset (project_id, uuid)
SELECT message.project_id, message.uuid
FROM m_workspace_messages AS message
WHERE message.source_name = 'zulip'
  AND message.source->>'kind' = 'zulip'
  AND message.source->>'message_id' IS NOT NULL
  AND (
        message.provider_external_id IS NULL
        OR message.provider_external_id = message.source->>'message_id'
      )
  AND (
        message.external_account_uuid IS NULL
        OR EXISTS (
            SELECT 1
            FROM m_external_accounts_v2 AS account
            WHERE account.uuid = message.external_account_uuid
              AND account.provider = 'zulip'
        )
      )
  AND (
        (
            message.external_account_uuid IS NOT NULL
            AND message.provider_external_id = message.source->>'message_id'
            AND EXISTS (
                SELECT 1
                FROM m_external_accounts_v2 AS account
                WHERE account.uuid = message.external_account_uuid
                  AND account.provider = 'zulip'
            )
        )
        OR EXISTS (
            SELECT 1
            FROM m_workspace_streams AS stream
            WHERE stream.project_id = message.project_id
              AND stream.uuid = message.stream_uuid
              AND stream.source_name = 'zulip'
              AND stream.source->>'kind' = 'zulip'
        )
        OR EXISTS (
            SELECT 1
            FROM m_zulip_processed_entities AS entity
            WHERE entity.project_id = message.project_id
              AND entity.workspace_uuid = message.uuid
              AND entity.entity_type = 'message'
        )
      )
  AND NOT EXISTS (
        SELECT 1
        FROM m_external_operations_v2 AS operation
        WHERE operation.action = 'message.create'
          AND operation.target_type = 'message'
          AND operation.target_uuid = message.uuid
          AND operation.owner_user_uuid = message.user_uuid
          AND (
                message.external_account_uuid IS NULL
                OR operation.external_account_uuid =
                   message.external_account_uuid
              )
      );

CREATE TEMP TABLE messenger_v2_zulip_file_reset (
    uuid uuid PRIMARY KEY,
    storage_type varchar(32) NOT NULL,
    storage_id varchar(255) NOT NULL,
    storage_object_id varchar(1024) NOT NULL
) ON COMMIT DROP;
INSERT INTO messenger_v2_zulip_file_reset (
    uuid, storage_type, storage_id, storage_object_id
)
SELECT file.uuid, file.storage_type, COALESCE(file.storage_id, ''),
       file.storage_object_id
FROM m_workspace_files AS file
JOIN m_external_accounts_v2 AS account
  ON account.uuid = file.external_account_uuid
 AND account.provider = 'zulip'
WHERE file.storage_object_id LIKE 'external-content/sha256/%'
  AND NOT EXISTS (
        SELECT 1
        FROM m_workspace_messages AS retained
        WHERE NOT EXISTS (
                SELECT 1
                FROM messenger_v2_zulip_message_reset AS reset
                WHERE reset.project_id = retained.project_id
                  AND reset.uuid = retained.uuid
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
    uuid, file_uuid, storage_type, storage_id, storage_object_id
)
SELECT messenger_uuid_v5(file.uuid, 'zulip-file-cleanup'), file.uuid,
       file.storage_type, file.storage_id, file.storage_object_id
FROM messenger_v2_zulip_file_reset AS file;

CREATE TEMP TABLE messenger_v2_zulip_reaction_reset (
    project_id uuid NOT NULL,
    uuid uuid NOT NULL,
    PRIMARY KEY (project_id, uuid)
) ON COMMIT DROP;
INSERT INTO messenger_v2_zulip_reaction_reset (project_id, uuid)
SELECT reaction.project_id, reaction.uuid
FROM m_workspace_message_reactions AS reaction
JOIN messenger_v2_zulip_message_reset AS message
  ON message.project_id = reaction.project_id
 AND message.uuid = reaction.message_uuid
UNION
SELECT reaction.project_id, reaction.uuid
FROM m_workspace_message_reactions AS reaction
JOIN m_external_accounts_v2 AS account
  ON account.uuid = reaction.external_account_uuid
WHERE account.provider = 'zulip';

CREATE TEMP TABLE messenger_v2_zulip_event_entities (
    uuid uuid PRIMARY KEY,
    uuid_text text NOT NULL UNIQUE
) ON COMMIT DROP;
WITH entities AS (
    SELECT uuid FROM messenger_v2_zulip_message_reset
    UNION
    SELECT uuid FROM messenger_v2_zulip_file_reset
    UNION
    SELECT uuid FROM messenger_v2_zulip_reaction_reset
)
INSERT INTO messenger_v2_zulip_event_entities (uuid, uuid_text)
SELECT uuid, uuid::text FROM entities;

DELETE FROM m_workspace_events AS event
WHERE EXISTS (
        SELECT 1
        FROM messenger_v2_zulip_event_entities AS entity
        WHERE entity.uuid_text = event.payload->>'uuid'
           OR entity.uuid_text = event.payload->>'message_uuid'
           OR entity.uuid_text = event.payload->>'file_uuid'
      )
   OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(
            COALESCE(event.payload->'message_uuids', '[]'::jsonb)
        ) AS message_uuid(value)
        JOIN messenger_v2_zulip_event_entities AS entity
          ON entity.uuid_text = message_uuid.value
   );
DELETE FROM m_workspace_broadcast_message_events_v1 AS event
WHERE EXISTS (
        SELECT 1
        FROM messenger_v2_zulip_event_entities AS entity
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
        JOIN messenger_v2_zulip_event_entities AS entity
          ON entity.uuid_text = message_uuid.value
   );
DELETE FROM m_workspace_event_audience_snapshots_v1 AS snapshot
WHERE NOT EXISTS (
    SELECT 1
    FROM m_workspace_broadcast_message_events_v1 AS event
    WHERE event.audience_snapshot_uuid = snapshot.uuid
);

-- Resolve every message foreign-key dependency in set operations.  In
-- particular, the legacy summary FKs have no supporting indexes; allowing
-- PostgreSQL to execute their ON DELETE action once per provider message turns
-- a large reset into hundreds of millions of repeated scans.
CREATE INDEX IF NOT EXISTS
    m_workspace_stream_topics_summary_message_reset_idx
    ON m_workspace_stream_topics (summary_last_message_uuid)
    WHERE summary_last_message_uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS
    m_workspace_topic_summary_jobs_boundary_message_reset_idx
    ON m_workspace_topic_summary_jobs (boundary_message_uuid)
    WHERE boundary_message_uuid IS NOT NULL;
UPDATE m_workspace_stream_topics AS topic
SET summary_last_message_uuid = NULL, updated_at = NOW()
FROM messenger_v2_zulip_message_reset AS reset
WHERE topic.project_id = reset.project_id
  AND topic.summary_last_message_uuid = reset.uuid;
UPDATE m_workspace_topic_summary_jobs AS job
SET boundary_message_uuid = NULL, updated_at = NOW()
FROM messenger_v2_zulip_message_reset AS reset
WHERE job.project_id = reset.project_id
  AND job.boundary_message_uuid = reset.uuid;
DELETE FROM m_workspace_message_mentions_v1 AS mention
USING messenger_v2_zulip_message_reset AS reset
WHERE mention.project_id = reset.project_id
  AND mention.message_uuid = reset.uuid;
DELETE FROM m_workspace_user_message_flags AS flag
USING messenger_v2_zulip_message_reset AS reset
WHERE flag.project_id = reset.project_id
  AND flag.uuid = reset.uuid;
DELETE FROM m_workspace_message_reactions AS reaction
USING messenger_v2_zulip_reaction_reset AS reset
WHERE reaction.project_id = reset.project_id
  AND reaction.uuid = reset.uuid;

DELETE FROM m_workspace_files AS file
USING messenger_v2_zulip_file_reset AS reset
WHERE file.uuid = reset.uuid;
DELETE FROM m_workspace_messages AS message
USING messenger_v2_zulip_message_reset AS reset
WHERE message.project_id = reset.project_id
  AND message.uuid = reset.uuid;

-- Provider command idempotency belongs to the discarded projection.  Keeping
-- it would acknowledge the deterministic fresh-import command while leaving
-- the corresponding message absent.
DELETE FROM m_external_provider_events_v1 AS event
USING m_external_accounts_v2 AS account
WHERE event.external_account_uuid = account.uuid
  AND account.provider = 'zulip';

-- Older account-scoped projection UUIDs can materialize the same verified
-- realm/chat once per connected account.  Collapse those aliases before the
-- v2 copy so one project contains one stream/topic graph.  Provider messages
-- have already been discarded above; retained native/outbound rows are moved
-- to the canonical containers and remain authoritative.
CREATE TEMP TABLE messenger_v2_realm_chat_projection_aliases (
    chat_uuid uuid PRIMARY KEY,
    external_account_uuid uuid NOT NULL,
    owner_user_uuid uuid NOT NULL,
    provider text NOT NULL,
    provider_realm_uuid uuid NOT NULL,
    provider_chat_id text NOT NULL,
    project_id uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    canonical_chat_uuid uuid NOT NULL,
    canonical_stream_uuid uuid NOT NULL
) ON COMMIT DROP;
INSERT INTO messenger_v2_realm_chat_projection_aliases (
    chat_uuid, external_account_uuid, owner_user_uuid, provider,
    provider_realm_uuid, provider_chat_id, project_id, stream_uuid,
    canonical_chat_uuid, canonical_stream_uuid
)
WITH valid_projection AS (
    SELECT chat.uuid AS chat_uuid, chat.provider, chat.provider_chat_id,
           chat.project_id, chat.projection_stream_uuid AS stream_uuid,
           account.provider_realm_uuid,
           row_number() OVER (
               PARTITION BY chat.provider, account.provider_realm_uuid,
                            chat.provider_chat_id, chat.project_id
               ORDER BY chat.created_at, chat.uuid,
                        chat.projection_stream_uuid
           ) AS projection_rank
    FROM m_external_chats_v2 AS chat
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = chat.external_account_uuid
     AND account.provider = chat.provider
    JOIN m_workspace_streams AS stream
      ON stream.project_id = chat.project_id
     AND stream.uuid = chat.projection_stream_uuid
    WHERE chat.selected
      AND chat.project_id IS NOT NULL
      AND chat.projection_stream_uuid IS NOT NULL
      AND account.provider_realm_uuid IS NOT NULL
), canonical AS (
    SELECT provider, provider_realm_uuid, provider_chat_id, project_id,
           chat_uuid AS canonical_chat_uuid,
           stream_uuid AS canonical_stream_uuid
    FROM valid_projection
    WHERE projection_rank = 1
)
SELECT chat.uuid, chat.external_account_uuid, chat.owner_user_uuid,
       chat.provider, account.provider_realm_uuid, chat.provider_chat_id,
       chat.project_id, chat.projection_stream_uuid,
       canonical.canonical_chat_uuid, canonical.canonical_stream_uuid
FROM m_external_chats_v2 AS chat
JOIN m_external_accounts_v2 AS account
  ON account.uuid = chat.external_account_uuid
 AND account.provider = chat.provider
JOIN canonical
  ON canonical.provider = chat.provider
 AND canonical.provider_realm_uuid = account.provider_realm_uuid
 AND canonical.provider_chat_id = chat.provider_chat_id
 AND canonical.project_id = chat.project_id
WHERE chat.selected
  AND chat.project_id IS NOT NULL
  AND chat.projection_stream_uuid IS NOT NULL;

CREATE TEMP TABLE messenger_v2_stream_projection_aliases (
    project_id uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    canonical_stream_uuid uuid NOT NULL,
    PRIMARY KEY (project_id, stream_uuid)
) ON COMMIT DROP;
INSERT INTO messenger_v2_stream_projection_aliases (
    project_id, stream_uuid, canonical_stream_uuid
)
SELECT DISTINCT project_id, stream_uuid, canonical_stream_uuid
FROM messenger_v2_realm_chat_projection_aliases
WHERE stream_uuid <> canonical_stream_uuid;

CREATE TEMP TABLE messenger_v2_topic_projection_aliases (
    project_id uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    canonical_topic_uuid uuid NOT NULL,
    canonical_stream_uuid uuid NOT NULL,
    PRIMARY KEY (project_id, topic_uuid)
) ON COMMIT DROP;
INSERT INTO messenger_v2_topic_projection_aliases (
    project_id, topic_uuid, canonical_topic_uuid, canonical_stream_uuid
)
WITH topic_members AS (
    SELECT projection.provider, projection.provider_realm_uuid,
           projection.provider_chat_id, projection.project_id,
           projection.chat_uuid, projection.canonical_chat_uuid,
           projection.canonical_stream_uuid,
           (source_topic->>'topic_uuid')::uuid AS topic_uuid,
           physical_topic.uuid IS NOT NULL AS physical_exists,
           COALESCE(
               source_topic->>'provider_topic_id',
               CASE WHEN COALESCE(
                    (source_topic->>'is_default')::boolean, false
               ) THEN '__workspace_default__' END
           ) AS provider_topic_key
    FROM messenger_v2_realm_chat_projection_aliases AS projection
    JOIN m_external_chats_v2 AS chat ON chat.uuid = projection.chat_uuid
    CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(chat.source->'topics', '[]'::jsonb)
    ) AS source_topic
    LEFT JOIN m_workspace_stream_topics AS physical_topic
      ON physical_topic.project_id = projection.project_id
     AND physical_topic.uuid = (source_topic->>'topic_uuid')::uuid
    WHERE (
            source_topic->>'provider_topic_id' IS NOT NULL
            OR COALESCE((source_topic->>'is_default')::boolean, false)
          )
      AND source_topic->>'topic_uuid' ~*
          '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
), ranked AS (
    SELECT topic_members.*,
           first_value(topic_uuid) OVER (
               PARTITION BY provider, provider_realm_uuid, provider_chat_id,
                            project_id, provider_topic_key
               ORDER BY physical_exists DESC,
                        (chat_uuid = canonical_chat_uuid) DESC,
                        chat_uuid, topic_uuid
           ) AS canonical_topic_uuid
    FROM topic_members
)
SELECT DISTINCT project_id, topic_uuid, canonical_topic_uuid,
       canonical_stream_uuid
FROM ranked;

-- Native topics created locally inside an old alias stream have no provider
-- topic identifier.  Keep their UUID and merely move them into the shared
-- stream instead of treating them as rebuildable provider state.
INSERT INTO messenger_v2_topic_projection_aliases (
    project_id, topic_uuid, canonical_topic_uuid, canonical_stream_uuid
)
SELECT stream_alias.project_id, topic.uuid, topic.uuid,
       stream_alias.canonical_stream_uuid
FROM messenger_v2_stream_projection_aliases AS stream_alias
JOIN m_workspace_stream_topics AS topic
  ON topic.project_id = stream_alias.project_id
 AND topic.stream_uuid = stream_alias.stream_uuid
ON CONFLICT (project_id, topic_uuid) DO NOTHING;

UPDATE m_external_chats_v2 AS chat
SET projection_stream_uuid = projection.canonical_stream_uuid,
    source = jsonb_set(
        chat.source,
        '{topics}',
        COALESCE(
            (
                SELECT jsonb_agg(
                    CASE
                        WHEN topic_alias.canonical_topic_uuid IS NULL
                        THEN source_topic
                        ELSE jsonb_set(
                            source_topic,
                            '{topic_uuid}',
                            to_jsonb(topic_alias.canonical_topic_uuid::text),
                            true
                        )
                    END
                    ORDER BY source_topic_order
                )
                FROM jsonb_array_elements(
                    COALESCE(chat.source->'topics', '[]'::jsonb)
                ) WITH ORDINALITY AS source_topics(
                    source_topic, source_topic_order
                )
                LEFT JOIN messenger_v2_topic_projection_aliases AS topic_alias
                  ON topic_alias.project_id = projection.project_id
                 AND topic_alias.topic_uuid =
                     (source_topic->>'topic_uuid')::uuid
            ),
            '[]'::jsonb
        ),
        true
    ),
    updated_at = NOW()
FROM messenger_v2_realm_chat_projection_aliases AS projection
WHERE chat.uuid = projection.chat_uuid;

-- Keep the durable desired-state snapshot aligned before its reset generation
-- is incremented below.  Older change-log entries retain their lower
-- generation and are ignored by a conforming Bridge.
UPDATE m_external_bridge_desired_resources_v1 AS desired
SET resource = jsonb_set(
        desired.resource,
        '{workspace_projection,stream,uuid}',
        to_jsonb(projection.canonical_stream_uuid::text),
        true
    ),
    updated_at = NOW()
FROM messenger_v2_realm_chat_projection_aliases AS projection
WHERE desired.provider_kind = projection.provider
  AND desired.resource_type = 'external_chat_assignment'
  AND desired.resource_uuid = projection.chat_uuid
  AND desired.operation = 'upsert';

UPDATE m_external_bridge_desired_resources_v1 AS desired
SET resource = jsonb_set(
        desired.resource,
        '{workspace_projection,stream,default_topic_uuid}',
        to_jsonb(topic_alias.canonical_topic_uuid::text),
        true
    ),
    updated_at = NOW()
FROM messenger_v2_realm_chat_projection_aliases AS projection
JOIN messenger_v2_topic_projection_aliases AS topic_alias
  ON topic_alias.project_id = projection.project_id
WHERE desired.provider_kind = projection.provider
  AND desired.resource_type = 'external_chat_assignment'
  AND desired.resource_uuid = projection.chat_uuid
  AND desired.operation = 'upsert'
  AND topic_alias.topic_uuid = NULLIF(
        desired.resource#>>'{workspace_projection,stream,default_topic_uuid}',
        ''
      )::uuid;

UPDATE m_external_bridge_desired_resources_v1 AS desired
SET resource = jsonb_set(
        desired.resource,
        '{workspace_projection,topics}',
        COALESCE(
            (
                SELECT jsonb_agg(
                    CASE
                        WHEN topic_alias.canonical_topic_uuid IS NULL
                        THEN desired_topic
                        ELSE jsonb_set(
                            desired_topic,
                            '{topic_uuid}',
                            to_jsonb(topic_alias.canonical_topic_uuid::text),
                            true
                        )
                    END
                    ORDER BY desired_topic_order
                )
                FROM jsonb_array_elements(
                    COALESCE(
                        desired.resource#>'{workspace_projection,topics}',
                        '[]'::jsonb
                    )
                ) WITH ORDINALITY AS desired_topics(
                    desired_topic, desired_topic_order
                )
                LEFT JOIN messenger_v2_topic_projection_aliases AS topic_alias
                  ON topic_alias.project_id = projection.project_id
                 AND topic_alias.topic_uuid =
                     (desired_topic->>'topic_uuid')::uuid
            ),
            '[]'::jsonb
        ),
        true
    ),
    updated_at = NOW()
FROM messenger_v2_realm_chat_projection_aliases AS projection
WHERE desired.provider_kind = projection.provider
  AND desired.resource_type = 'external_chat_assignment'
  AND desired.resource_uuid = projection.chat_uuid
  AND desired.operation = 'upsert';

-- Merge duplicate stream memberships and keep the most recently chosen local
-- notification mode before moving non-overlapping rows.
UPDATE m_workspace_stream_bindings AS canonical
SET notification_mode = CASE
        WHEN alias.notification_updated_at > canonical.notification_updated_at
        THEN alias.notification_mode ELSE canonical.notification_mode END,
    notification_updated_at = GREATEST(
        canonical.notification_updated_at, alias.notification_updated_at
    ),
    updated_at = GREATEST(canonical.updated_at, alias.updated_at)
FROM m_workspace_stream_bindings AS alias
JOIN messenger_v2_stream_projection_aliases AS stream_alias
  ON stream_alias.project_id = alias.project_id
 AND stream_alias.stream_uuid = alias.stream_uuid
WHERE canonical.project_id = alias.project_id
  AND canonical.stream_uuid = stream_alias.canonical_stream_uuid
  AND canonical.user_uuid = alias.user_uuid;
DELETE FROM m_workspace_stream_bindings AS alias
USING messenger_v2_stream_projection_aliases AS stream_alias
WHERE alias.project_id = stream_alias.project_id
  AND alias.stream_uuid = stream_alias.stream_uuid
  AND EXISTS (
      SELECT 1 FROM m_workspace_stream_bindings AS canonical
      WHERE canonical.project_id = alias.project_id
        AND canonical.stream_uuid = stream_alias.canonical_stream_uuid
        AND canonical.user_uuid = alias.user_uuid
  );
UPDATE m_workspace_stream_bindings AS binding
SET stream_uuid = stream_alias.canonical_stream_uuid,
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE binding.project_id = stream_alias.project_id
  AND binding.stream_uuid = stream_alias.stream_uuid;

INSERT INTO m_workspace_read_memberships_v1 (
    project_id, user_uuid, stream_uuid, last_detached_sequence,
    created_at, updated_at
)
SELECT membership.project_id, membership.user_uuid,
       stream_alias.canonical_stream_uuid,
       max(membership.last_detached_sequence), min(membership.created_at), NOW()
FROM m_workspace_read_memberships_v1 AS membership
JOIN messenger_v2_stream_projection_aliases AS stream_alias
  ON stream_alias.project_id = membership.project_id
 AND stream_alias.stream_uuid = membership.stream_uuid
GROUP BY membership.project_id, membership.user_uuid,
         stream_alias.canonical_stream_uuid
ON CONFLICT (project_id, user_uuid, stream_uuid) DO UPDATE
SET last_detached_sequence = GREATEST(
        m_workspace_read_memberships_v1.last_detached_sequence,
        EXCLUDED.last_detached_sequence
    ),
    updated_at = NOW();
DELETE FROM m_workspace_read_memberships_v1 AS membership
USING messenger_v2_stream_projection_aliases AS stream_alias
WHERE membership.project_id = stream_alias.project_id
  AND membership.stream_uuid = stream_alias.stream_uuid;

-- Folder placement is per user/folder, so collapse duplicates before changing
-- the stream UUID covered by its uniqueness constraint.
UPDATE m_folder_items AS canonical
SET order_index = COALESCE(canonical.order_index, alias.order_index),
    pinned_at = COALESCE(canonical.pinned_at, alias.pinned_at),
    updated_at = GREATEST(canonical.updated_at, alias.updated_at)
FROM m_folder_items AS alias
JOIN messenger_v2_stream_projection_aliases AS stream_alias
  ON stream_alias.project_id = alias.project_id
 AND stream_alias.stream_uuid = alias.stream_uuid
WHERE canonical.project_id = alias.project_id
  AND canonical.user_uuid = alias.user_uuid
  AND canonical.folder_uuid = alias.folder_uuid
  AND canonical.stream_uuid = stream_alias.canonical_stream_uuid;
DELETE FROM m_folder_items AS alias
USING messenger_v2_stream_projection_aliases AS stream_alias
WHERE alias.project_id = stream_alias.project_id
  AND alias.stream_uuid = stream_alias.stream_uuid
  AND EXISTS (
      SELECT 1 FROM m_folder_items AS canonical
      WHERE canonical.project_id = alias.project_id
        AND canonical.user_uuid = alias.user_uuid
        AND canonical.folder_uuid = alias.folder_uuid
        AND canonical.stream_uuid = stream_alias.canonical_stream_uuid
  );
UPDATE m_folder_items AS item
SET stream_uuid = stream_alias.canonical_stream_uuid,
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE item.project_id = stream_alias.project_id
  AND item.stream_uuid = stream_alias.stream_uuid;

UPDATE m_workspace_messages AS message
SET stream_uuid = stream_alias.canonical_stream_uuid,
    topic_uuid = COALESCE(topic_alias.canonical_topic_uuid, message.topic_uuid),
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
LEFT JOIN messenger_v2_topic_projection_aliases AS topic_alias
  ON topic_alias.project_id = stream_alias.project_id
WHERE message.project_id = stream_alias.project_id
  AND message.stream_uuid = stream_alias.stream_uuid
  AND (topic_alias.topic_uuid IS NULL OR topic_alias.topic_uuid = message.topic_uuid);
UPDATE m_workspace_drafts AS draft
SET stream_uuid = stream_alias.canonical_stream_uuid,
    topic_uuid = COALESCE(topic_alias.canonical_topic_uuid, draft.topic_uuid),
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
LEFT JOIN messenger_v2_topic_projection_aliases AS topic_alias
  ON topic_alias.project_id = stream_alias.project_id
WHERE draft.project_id = stream_alias.project_id
  AND draft.stream_uuid = stream_alias.stream_uuid
  AND (topic_alias.topic_uuid IS NULL OR topic_alias.topic_uuid = draft.topic_uuid);
UPDATE m_workspace_files AS file
SET stream_uuid = stream_alias.canonical_stream_uuid,
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE file.project_id = stream_alias.project_id
  AND file.stream_uuid = stream_alias.stream_uuid;

CREATE TEMP TABLE messenger_v2_topic_flag_merge ON COMMIT DROP AS
SELECT topic_alias.project_id, topic_alias.canonical_topic_uuid AS topic_uuid,
       flag.user_uuid, bool_or(flag.is_done) AS is_done,
       (array_agg(
           flag.notification_mode
           ORDER BY flag.notification_updated_at DESC, flag.updated_at DESC
       ))[1] AS notification_mode,
       max(flag.notification_updated_at) AS notification_updated_at,
       min(flag.created_at) AS created_at,
       max(flag.updated_at) AS updated_at
FROM m_workspace_user_topic_flags AS flag
JOIN messenger_v2_topic_projection_aliases AS topic_alias
  ON topic_alias.project_id = flag.project_id
 AND topic_alias.topic_uuid = flag.uuid
GROUP BY topic_alias.project_id, topic_alias.canonical_topic_uuid,
         flag.user_uuid;
DELETE FROM m_workspace_user_topic_flags AS flag
USING messenger_v2_topic_projection_aliases AS topic_alias
WHERE flag.project_id = topic_alias.project_id
  AND flag.uuid = topic_alias.topic_uuid;
INSERT INTO m_workspace_user_topic_flags (
    uuid, user_uuid, project_id, is_done, notification_mode,
    notification_updated_at, created_at, updated_at
)
SELECT topic_uuid, user_uuid, project_id, is_done, notification_mode,
       notification_updated_at, created_at, updated_at
FROM messenger_v2_topic_flag_merge
ON CONFLICT (uuid, user_uuid) DO UPDATE
SET is_done = m_workspace_user_topic_flags.is_done OR EXCLUDED.is_done,
    notification_mode = CASE
        WHEN EXCLUDED.notification_updated_at >
             m_workspace_user_topic_flags.notification_updated_at
        THEN EXCLUDED.notification_mode
        ELSE m_workspace_user_topic_flags.notification_mode END,
    notification_updated_at = GREATEST(
        m_workspace_user_topic_flags.notification_updated_at,
        EXCLUDED.notification_updated_at
    ),
    updated_at = GREATEST(
        m_workspace_user_topic_flags.updated_at, EXCLUDED.updated_at
    );

CREATE TEMP TABLE messenger_v2_topic_read_merge ON COMMIT DROP AS
SELECT stats.project_id, stats.user_uuid,
       topic_alias.canonical_topic_uuid AS topic_uuid,
       sum(stats.read_count)::bigint AS read_count,
       min(stats.created_at) AS created_at,
       max(stats.updated_at) AS updated_at
FROM m_workspace_user_topic_read_stats_v1 AS stats
JOIN messenger_v2_topic_projection_aliases AS topic_alias
  ON topic_alias.project_id = stats.project_id
 AND topic_alias.topic_uuid = stats.topic_uuid
GROUP BY stats.project_id, stats.user_uuid,
         topic_alias.canonical_topic_uuid;
DELETE FROM m_workspace_user_topic_read_stats_v1 AS stats
USING messenger_v2_topic_projection_aliases AS topic_alias
WHERE stats.project_id = topic_alias.project_id
  AND stats.topic_uuid = topic_alias.topic_uuid;
INSERT INTO m_workspace_user_topic_read_stats_v1 (
    project_id, user_uuid, topic_uuid, read_count, created_at, updated_at
)
SELECT project_id, user_uuid, topic_uuid, read_count, created_at, updated_at
FROM messenger_v2_topic_read_merge;

CREATE TEMP TABLE messenger_v2_topic_message_merge ON COMMIT DROP AS
SELECT topic_alias.canonical_topic_uuid AS topic_uuid,
       min(stats.project_id::text)::uuid AS project_id,
       min(topic_alias.canonical_stream_uuid::text)::uuid AS stream_uuid,
       sum(stats.message_count)::bigint AS message_count,
       max(stats.last_ingest_sequence) AS last_ingest_sequence,
       min(stats.created_at) AS created_at,
       max(stats.updated_at) AS updated_at
FROM m_workspace_topic_message_stats_v1 AS stats
JOIN messenger_v2_topic_projection_aliases AS topic_alias
  ON topic_alias.project_id = stats.project_id
 AND topic_alias.topic_uuid = stats.topic_uuid
GROUP BY topic_alias.canonical_topic_uuid;
DELETE FROM m_workspace_topic_message_stats_v1 AS stats
USING messenger_v2_topic_projection_aliases AS topic_alias
WHERE stats.project_id = topic_alias.project_id
  AND stats.topic_uuid = topic_alias.topic_uuid;
INSERT INTO m_workspace_topic_message_stats_v1 (
    topic_uuid, project_id, stream_uuid, message_count,
    last_ingest_sequence, created_at, updated_at
)
SELECT topic_uuid, project_id, stream_uuid, message_count,
       last_ingest_sequence, created_at, updated_at
FROM messenger_v2_topic_message_merge;

-- Summary work is derived and can be scheduled again; journal entries are
-- durable user-visible history and follow the canonical topic.
DELETE FROM m_workspace_topic_summary_jobs AS job
USING messenger_v2_topic_projection_aliases AS topic_alias
WHERE job.project_id = topic_alias.project_id
  AND job.topic_uuid = topic_alias.topic_uuid
  AND topic_alias.topic_uuid <> topic_alias.canonical_topic_uuid;
UPDATE m_workspace_topic_summary_journal AS journal
SET topic_uuid = topic_alias.canonical_topic_uuid
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE journal.project_id = topic_alias.project_id
  AND journal.topic_uuid = topic_alias.topic_uuid;

-- Preserve event and queued-operation routing for retained native rows.
UPDATE m_workspace_events AS event
SET payload = jsonb_set(
        event.payload, '{stream_uuid}',
        to_jsonb(stream_alias.canonical_stream_uuid::text), true
    )
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE event.project_id = stream_alias.project_id
  AND event.payload->>'stream_uuid' = stream_alias.stream_uuid::text;
UPDATE m_workspace_events AS event
SET payload = jsonb_set(
        event.payload, '{topic_uuid}',
        to_jsonb(topic_alias.canonical_topic_uuid::text), true
    )
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE event.project_id = topic_alias.project_id
  AND event.payload->>'topic_uuid' = topic_alias.topic_uuid::text;
UPDATE m_workspace_events AS event
SET payload = jsonb_set(
        event.payload, '{uuid}',
        to_jsonb(stream_alias.canonical_stream_uuid::text), true
    )
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE event.project_id = stream_alias.project_id
  AND event.object_type = 'stream'
  AND event.payload->>'uuid' = stream_alias.stream_uuid::text;
UPDATE m_workspace_events AS event
SET payload = jsonb_set(
        event.payload, '{uuid}',
        to_jsonb(topic_alias.canonical_topic_uuid::text), true
    )
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE event.project_id = topic_alias.project_id
  AND event.object_type = 'topic'
  AND event.payload->>'uuid' = topic_alias.topic_uuid::text;
UPDATE m_workspace_broadcast_message_events_v1 AS event
SET payload = jsonb_set(
        event.payload, '{stream_uuid}',
        to_jsonb(stream_alias.canonical_stream_uuid::text), true
    )
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE event.project_id = stream_alias.project_id
  AND event.payload->>'stream_uuid' = stream_alias.stream_uuid::text;
UPDATE m_workspace_broadcast_message_events_v1 AS event
SET entity_uuid = stream_alias.canonical_stream_uuid
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE event.project_id = stream_alias.project_id
  AND event.object_type = 'stream'
  AND event.entity_uuid = stream_alias.stream_uuid;
UPDATE m_workspace_broadcast_message_events_v1 AS event
SET payload = jsonb_set(
        event.payload, '{topic_uuid}',
        to_jsonb(topic_alias.canonical_topic_uuid::text), true
    )
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE event.project_id = topic_alias.project_id
  AND event.payload->>'topic_uuid' = topic_alias.topic_uuid::text;
UPDATE m_workspace_broadcast_message_events_v1 AS event
SET entity_uuid = topic_alias.canonical_topic_uuid
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE event.project_id = topic_alias.project_id
  AND event.object_type = 'topic'
  AND event.entity_uuid = topic_alias.topic_uuid;
UPDATE m_workspace_broadcast_message_events_v1 AS event
SET payload = jsonb_set(
        event.payload, '{uuid}',
        to_jsonb(stream_alias.canonical_stream_uuid::text), true
    )
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE event.project_id = stream_alias.project_id
  AND event.object_type = 'stream'
  AND event.payload->>'uuid' = stream_alias.stream_uuid::text;
UPDATE m_workspace_broadcast_message_events_v1 AS event
SET payload = jsonb_set(
        event.payload, '{uuid}',
        to_jsonb(topic_alias.canonical_topic_uuid::text), true
    )
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE event.project_id = topic_alias.project_id
  AND event.object_type = 'topic'
  AND event.payload->>'uuid' = topic_alias.topic_uuid::text;
UPDATE m_workspace_event_recipient_payloads_v1 AS recipient
SET payload = jsonb_set(
        recipient.payload, '{stream_uuid}',
        to_jsonb(stream_alias.canonical_stream_uuid::text), true
    )
FROM messenger_v2_stream_projection_aliases AS stream_alias,
     m_workspace_broadcast_message_events_v1 AS event
WHERE recipient.event_uuid = event.uuid
  AND event.project_id = stream_alias.project_id
  AND recipient.payload->>'stream_uuid' = stream_alias.stream_uuid::text;
UPDATE m_workspace_event_recipient_payloads_v1 AS recipient
SET payload = jsonb_set(
        recipient.payload, '{topic_uuid}',
        to_jsonb(topic_alias.canonical_topic_uuid::text), true
    )
FROM messenger_v2_topic_projection_aliases AS topic_alias,
     m_workspace_broadcast_message_events_v1 AS event
WHERE recipient.event_uuid = event.uuid
  AND event.project_id = topic_alias.project_id
  AND recipient.payload->>'topic_uuid' = topic_alias.topic_uuid::text;

UPDATE m_external_operations_v2 AS operation
SET details = jsonb_set(
        operation.details, '{stream_uuid}',
        to_jsonb(stream_alias.canonical_stream_uuid::text), true
    ),
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE operation.details->>'project_id' = stream_alias.project_id::text
  AND operation.details->>'stream_uuid' = stream_alias.stream_uuid::text;
UPDATE m_external_operations_v2 AS operation
SET details = jsonb_set(
        operation.details, '{topic_uuid}',
        to_jsonb(topic_alias.canonical_topic_uuid::text), true
    ),
    updated_at = NOW()
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE operation.details->>'project_id' = topic_alias.project_id::text
  AND operation.details->>'topic_uuid' = topic_alias.topic_uuid::text;
UPDATE m_external_operations_v2 AS operation
SET target_uuid = stream_alias.canonical_stream_uuid,
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE operation.target_type = 'stream'
  AND operation.target_uuid = stream_alias.stream_uuid;
UPDATE m_external_operations_v2 AS operation
SET target_uuid = topic_alias.canonical_topic_uuid,
    updated_at = NOW()
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE operation.target_type = 'topic'
  AND operation.target_uuid = topic_alias.topic_uuid;

UPDATE m_external_provider_operations_v1 AS operation
SET causal_lane = stream_alias.canonical_stream_uuid,
    payload = CASE
        WHEN operation.payload->>'stream_uuid' = stream_alias.stream_uuid::text
        THEN jsonb_set(
            operation.payload, '{stream_uuid}',
            to_jsonb(stream_alias.canonical_stream_uuid::text), true
        )
        ELSE operation.payload END,
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE operation.project_id = stream_alias.project_id
  AND (
      operation.causal_lane = stream_alias.stream_uuid
      OR operation.payload->>'stream_uuid' = stream_alias.stream_uuid::text
  );
UPDATE m_external_provider_operations_v1 AS operation
SET payload = jsonb_set(
        operation.payload, '{topic_uuid}',
        to_jsonb(topic_alias.canonical_topic_uuid::text), true
    ),
    updated_at = NOW()
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE operation.project_id = topic_alias.project_id
  AND operation.payload->>'topic_uuid' = topic_alias.topic_uuid::text;
UPDATE m_external_provider_read_snapshots_v1 AS snapshot
SET causal_lane = stream_alias.canonical_stream_uuid,
    payload = CASE
        WHEN snapshot.payload->>'stream_uuid' = stream_alias.stream_uuid::text
        THEN jsonb_set(
            snapshot.payload, '{stream_uuid}',
            to_jsonb(stream_alias.canonical_stream_uuid::text), true
        )
        ELSE snapshot.payload END,
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE snapshot.project_id = stream_alias.project_id
  AND (
      snapshot.causal_lane = stream_alias.stream_uuid
      OR snapshot.payload->>'stream_uuid' = stream_alias.stream_uuid::text
  );
UPDATE m_external_provider_read_snapshots_v1 AS snapshot
SET payload = jsonb_set(
        snapshot.payload, '{topic_uuid}',
        to_jsonb(topic_alias.canonical_topic_uuid::text), true
    ),
    updated_at = NOW()
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE snapshot.project_id = topic_alias.project_id
  AND snapshot.payload->>'topic_uuid' = topic_alias.topic_uuid::text;
UPDATE m_external_projection_transitions_v1 AS transition
SET stream_uuid = stream_alias.canonical_stream_uuid,
    updated_at = NOW()
FROM messenger_v2_stream_projection_aliases AS stream_alias
WHERE transition.old_project_uuid = stream_alias.project_id
  AND transition.stream_uuid = stream_alias.stream_uuid;

-- The legacy default-topic constraint is deferred, allowing canonical topics
-- to move first and alias streams to disappear at the end of the transaction.
UPDATE m_workspace_streams AS stream
SET default_topic_uuid = topic_alias.canonical_topic_uuid,
    updated_at = NOW()
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE stream.project_id = topic_alias.project_id
  AND stream.default_topic_uuid = topic_alias.topic_uuid;
UPDATE m_workspace_stream_topics AS topic
SET stream_uuid = topic_alias.canonical_stream_uuid,
    updated_at = NOW()
FROM messenger_v2_topic_projection_aliases AS topic_alias
WHERE topic.project_id = topic_alias.project_id
  AND topic.uuid = topic_alias.canonical_topic_uuid;
CREATE INDEX IF NOT EXISTS m_workspace_messages_topic_uuid_reset_idx
    ON m_workspace_messages (topic_uuid);
CREATE INDEX IF NOT EXISTS m_workspace_drafts_topic_uuid_reset_idx
    ON m_workspace_drafts (topic_uuid);
DELETE FROM m_workspace_stream_topics AS topic
USING messenger_v2_topic_projection_aliases AS topic_alias
WHERE topic.project_id = topic_alias.project_id
  AND topic.uuid = topic_alias.topic_uuid
  AND topic_alias.topic_uuid <> topic_alias.canonical_topic_uuid;
DELETE FROM m_workspace_streams AS stream
USING messenger_v2_stream_projection_aliases AS stream_alias
WHERE stream.project_id = stream_alias.project_id
  AND stream.uuid = stream_alias.stream_uuid;

WITH reset_accounts AS (
    UPDATE m_external_accounts_v2
    SET projection_reset_generation = projection_reset_generation + 1,
        desired_generation = desired_generation + 1,
        status = CASE
            WHEN status IN ('disconnected', 'suspended', 'auth_required')
            THEN status ELSE 'backfill' END,
        live_ready = FALSE,
        safe_error = NULL,
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
    UPDATE m_external_chats_v2
    SET status = 'syncing', safe_error = NULL,
        revision = revision + 1, updated_at = NOW()
    WHERE provider = 'zulip' AND selected
    RETURNING uuid, revision
), changed AS (
    UPDATE m_external_bridge_desired_resources_v1 AS desired
    SET generation = chat.revision,
        resource = jsonb_set(
            desired.resource, '{generation}', to_jsonb(chat.revision), true
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
WHERE EXISTS (
    SELECT 1
    FROM m_external_chats_v2 AS chat
    WHERE chat.provider = 'zulip'
      AND chat.selected
      AND chat.project_id = cursor.project_id
);

CREATE TABLE messenger_streams (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    name varchar(255) NOT NULL,
    description varchar(1024),
    owner_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid),
    source_name varchar(32) NOT NULL DEFAULT 'native',
    source jsonb NOT NULL DEFAULT '{"kind":"native"}'::jsonb,
    invite_only boolean NOT NULL DEFAULT false,
    announce boolean NOT NULL DEFAULT false,
    direct_user_uuid uuid REFERENCES m_workspace_users(uuid),
    private boolean NOT NULL DEFAULT false,
    is_archived boolean NOT NULL DEFAULT false,
    private_index varchar(73),
    color bigint NOT NULL DEFAULT floor(random() * 16777216)::bigint,
    default_topic_uuid uuid,
    provider jsonb,
    delivery jsonb,
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    deleted_at timestamp with time zone,
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    FOREIGN KEY (project_id, owner_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid),
    FOREIGN KEY (project_id, direct_user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid),
    CHECK (source_name IN ('native', 'zulip')),
    CHECK (color BETWEEN 0 AND 16777215),
    CHECK (direct_user_uuid IS NULL OR private)
);
CREATE INDEX messenger_streams_owner_idx
    ON messenger_streams (project_id, owner_uuid);
CREATE INDEX messenger_streams_direct_user_idx
    ON messenger_streams (project_id, direct_user_uuid)
    WHERE direct_user_uuid IS NOT NULL;

CREATE TABLE messenger_stream_bindings (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    stream_uuid uuid,
    user_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
    who_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid),
    active boolean NOT NULL DEFAULT true,
    membership_generation integer NOT NULL DEFAULT 1,
    membership_started_at timestamp with time zone NOT NULL DEFAULT now(),
    role varchar(32) NOT NULL DEFAULT 'member',
    notification_mode varchar(32) NOT NULL DEFAULT 'all_messages',
    notification_updated_at timestamp with time zone NOT NULL DEFAULT now(),
    unread_count integer NOT NULL DEFAULT 0,
    active_unread_count integer NOT NULL DEFAULT 0,
    passive_unread_count integer NOT NULL DEFAULT 0,
    last_message_uuid uuid,
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, user_uuid, stream_uuid),
    FOREIGN KEY (project_id, user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid),
    FOREIGN KEY (project_id, who_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid),
    FOREIGN KEY (project_id, stream_uuid)
        REFERENCES messenger_streams(project_id, uuid) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    CHECK (membership_generation >= 1),
    CHECK (unread_count >= 0),
    CHECK (active_unread_count >= 0),
    CHECK (passive_unread_count >= 0)
);
CREATE INDEX messenger_stream_bindings_fanout_idx
    ON messenger_stream_bindings (project_id, stream_uuid, user_uuid)
    WHERE active;
CREATE INDEX messenger_stream_bindings_viewer_idx
    ON messenger_stream_bindings (project_id, user_uuid, active, stream_uuid);

CREATE TABLE messenger_topics (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    name varchar(128) NOT NULL,
    color bigint NOT NULL DEFAULT floor(random() * 16777216)::bigint,
    source_name varchar(32) NOT NULL DEFAULT 'native',
    source jsonb NOT NULL DEFAULT '{"kind":"native"}'::jsonb,
    summary varchar(4096),
    summary_last_message_uuid uuid,
    summary_enabled boolean NOT NULL DEFAULT true,
    summary_system_prompt varchar(16384),
    summary_reasoning_effort varchar(16),
    provider jsonb,
    delivery jsonb,
    is_done boolean NOT NULL DEFAULT false,
    version integer NOT NULL DEFAULT 0,
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    deleted_at timestamp with time zone,
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, stream_uuid, uuid),
    FOREIGN KEY (project_id, stream_uuid)
        REFERENCES messenger_streams(project_id, uuid) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    CHECK (source_name IN ('native', 'zulip')),
    CHECK (color BETWEEN 0 AND 16777215),
    CHECK (version >= 0)
);
CREATE INDEX messenger_topics_stream_idx
    ON messenger_topics (project_id, stream_uuid, created_at, uuid);
ALTER TABLE messenger_streams
    ADD CONSTRAINT messenger_streams_default_topic_fk
    FOREIGN KEY (project_id, default_topic_uuid)
    REFERENCES messenger_topics(project_id, uuid)
    ON DELETE SET NULL (default_topic_uuid)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE messenger_user_topic_bindings (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
    topic_uuid uuid NOT NULL,
    notification_mode varchar(32) NOT NULL DEFAULT 'default',
    unread_count integer NOT NULL DEFAULT 0,
    active_unread_count integer NOT NULL DEFAULT 0,
    passive_unread_count integer NOT NULL DEFAULT 0,
    last_message_uuid uuid,
    summary_has_new_messages boolean,
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, user_uuid, topic_uuid),
    FOREIGN KEY (project_id, user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid),
    FOREIGN KEY (project_id, topic_uuid)
        REFERENCES messenger_topics(project_id, uuid) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    CHECK (unread_count >= 0),
    CHECK (active_unread_count >= 0),
    CHECK (passive_unread_count >= 0)
);
CREATE INDEX messenger_user_topic_bindings_viewer_idx
    ON messenger_user_topic_bindings (project_id, user_uuid, topic_uuid);

CREATE TABLE messenger_folders (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    title varchar(64) NOT NULL,
    background_color_value bigint,
    system_type varchar(32),
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    CHECK (system_type IS NULL OR system_type IN ('all', 'created')),
    CHECK (background_color_value IS NULL OR background_color_value >= 0)
);

CREATE TABLE messenger_user_folder_bindings (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
    folder_uuid uuid NOT NULL,
    rule varchar(32) NOT NULL DEFAULT 'custom',
    unread_count integer NOT NULL DEFAULT 0,
    mention_count integer NOT NULL DEFAULT 0,
    folder_items_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,
    snapshot_version bigint NOT NULL DEFAULT 0,
    snapshot_updated_at timestamp with time zone NOT NULL DEFAULT now(),
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (project_id, user_uuid, folder_uuid),
    FOREIGN KEY (project_id, user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, folder_uuid)
        REFERENCES messenger_folders(project_id, uuid) ON DELETE CASCADE,
    CHECK (rule IN ('all_chats', 'personal', 'channels', 'custom')),
    CHECK (unread_count >= 0),
    CHECK (mention_count >= 0),
    CHECK (snapshot_version >= 0)
);
CREATE INDEX messenger_user_folder_bindings_viewer_idx
    ON messenger_user_folder_bindings (project_id, user_uuid, folder_uuid);

CREATE TABLE messenger_folder_items (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
    folder_uuid uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    order_index integer,
    pinned_at timestamp with time zone,
    chat_type varchar(32) NOT NULL,
    automatic boolean NOT NULL DEFAULT false,
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_uuid, uuid),
    UNIQUE (project_id, user_uuid, folder_uuid, stream_uuid),
    FOREIGN KEY (project_id, user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, user_uuid, folder_uuid)
        REFERENCES messenger_user_folder_bindings(
            project_id, user_uuid, folder_uuid
        ) ON DELETE CASCADE,
    FOREIGN KEY (project_id, stream_uuid)
        REFERENCES messenger_streams(project_id, uuid) ON DELETE CASCADE,
    CHECK (chat_type IN ('stream', 'group', 'private'))
);
CREATE INDEX messenger_folder_items_view_idx
    ON messenger_folder_items (
        project_id, user_uuid, folder_uuid, created_at, uuid
    );

CREATE SEQUENCE messenger_messages_ingest_sequence_seq;
CREATE TABLE messenger_messages (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    legacy_public_uuid uuid,
    author_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid),
    payload jsonb NOT NULL,
    source_name varchar(32) NOT NULL DEFAULT 'native',
    source jsonb NOT NULL DEFAULT '{"kind":"native"}'::jsonb,
    provider_uuid uuid,
    external_account_uuid uuid,
    provider_external_id varchar(2048),
    provider_realm_uuid uuid,
    provider_message_id varchar(32),
    provider jsonb,
    delivery jsonb,
    reactions jsonb NOT NULL DEFAULT '{}'::jsonb,
    reaction_users jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingest_sequence bigint NOT NULL
        DEFAULT nextval('messenger_messages_ingest_sequence_seq'),
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    deleted_at timestamp with time zone,
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    FOREIGN KEY (project_id, author_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid),
    CHECK (source_name IN ('native', 'zulip')),
    CHECK (
        (provider_realm_uuid IS NULL AND provider_message_id IS NULL)
        OR (
            provider_realm_uuid IS NOT NULL
            AND provider_message_id ~ '^(0|[1-9][0-9]*)$'
        )
    )
);
ALTER SEQUENCE messenger_messages_ingest_sequence_seq
    OWNED BY messenger_messages.ingest_sequence;
CREATE UNIQUE INDEX messenger_messages_provider_identity_idx
    ON messenger_messages (
        project_id, external_account_uuid, provider_external_id
    )
    WHERE external_account_uuid IS NOT NULL
      AND provider_external_id IS NOT NULL;
CREATE UNIQUE INDEX messenger_messages_realm_provider_identity_idx
    ON messenger_messages (provider_realm_uuid, provider_message_id)
    WHERE provider_realm_uuid IS NOT NULL
      AND provider_message_id IS NOT NULL;
CREATE UNIQUE INDEX messenger_messages_legacy_public_uuid_idx
    ON messenger_messages (legacy_public_uuid)
    WHERE legacy_public_uuid IS NOT NULL;
CREATE INDEX messenger_messages_created_idx
    ON messenger_messages (project_id, created_at DESC, uuid DESC);
CREATE INDEX messenger_provider_chat_project_scope_idx
    ON m_external_chats_v2 (provider, provider_chat_id, project_id)
    WHERE selected;

CREATE TABLE messenger_message_placements (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    legacy_public_uuid uuid,
    message_uuid uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, message_uuid, stream_uuid, topic_uuid),
    FOREIGN KEY (project_id, message_uuid)
        REFERENCES messenger_messages(project_id, uuid) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    FOREIGN KEY (project_id, stream_uuid)
        REFERENCES messenger_streams(project_id, uuid) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    FOREIGN KEY (project_id, stream_uuid, topic_uuid)
        REFERENCES messenger_topics(project_id, stream_uuid, uuid)
        ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE
);
CREATE INDEX messenger_message_placements_topic_idx
    ON messenger_message_placements (
        project_id, topic_uuid, created_at DESC, uuid DESC
    );
CREATE INDEX messenger_message_placements_stream_idx
    ON messenger_message_placements (
        project_id, stream_uuid, created_at DESC, uuid DESC
    );
CREATE UNIQUE INDEX messenger_message_placements_legacy_public_uuid_idx
    ON messenger_message_placements (legacy_public_uuid)
    WHERE legacy_public_uuid IS NOT NULL;

CREATE TABLE messenger_user_message_bindings (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    placement_uuid uuid NOT NULL,
    user_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
    membership_generation integer NOT NULL,
    relation_role varchar(64) NOT NULL,
    visibility varchar(64) NOT NULL,
    permissions jsonb NOT NULL,
    created_at timestamp without time zone NOT NULL DEFAULT now(),
    updated_at timestamp without time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, placement_uuid, user_uuid),
    FOREIGN KEY (project_id, user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid),
    FOREIGN KEY (project_id, placement_uuid)
        REFERENCES messenger_message_placements(project_id, uuid)
        ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    CHECK (membership_generation >= 1)
);
CREATE INDEX messenger_user_message_bindings_view_idx
    ON messenger_user_message_bindings (
        project_id, user_uuid, placement_uuid, membership_generation
    );

CREATE TABLE messenger_user_message_states (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    placement_uuid uuid NOT NULL,
    user_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
    membership_generation integer NOT NULL,
    read_at timestamp with time zone,
    mentioned boolean NOT NULL DEFAULT false,
    starred boolean NOT NULL DEFAULT false,
    pinned boolean NOT NULL DEFAULT false,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, user_uuid, placement_uuid),
    FOREIGN KEY (project_id, user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid),
    FOREIGN KEY (project_id, placement_uuid)
        REFERENCES messenger_message_placements(project_id, uuid)
        ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    CHECK (membership_generation >= 1)
);
CREATE INDEX messenger_user_message_states_unread_idx
    ON messenger_user_message_states (project_id, user_uuid, placement_uuid)
    WHERE read_at IS NULL;

CREATE TABLE messenger_message_reaction_facts (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    canonical_message_uuid uuid NOT NULL,
    placement_uuid uuid NOT NULL,
    user_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
    emoji_name varchar(128) NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (
        project_id, canonical_message_uuid, user_uuid, emoji_name
    ),
    FOREIGN KEY (project_id, user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid),
    FOREIGN KEY (project_id, canonical_message_uuid)
        REFERENCES messenger_messages(project_id, uuid) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    FOREIGN KEY (project_id, placement_uuid)
        REFERENCES messenger_message_placements(project_id, uuid)
        ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE
);
CREATE INDEX messenger_message_reaction_facts_message_idx
    ON messenger_message_reaction_facts (
        project_id, canonical_message_uuid, created_at, uuid
    );

CREATE TABLE messenger_domain_outbox_events (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    event_kind varchar(64) NOT NULL,
    scope_kind varchar(64) NOT NULL,
    scope_key varchar(512) NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    CHECK (event_kind IN (
        'fanout', 'content_mentions', 'reaction_snapshot', 'read_counters',
        'folder_projection', 'delivery_snapshot_event',
        'topic_state_projection', 'topic_membership_policy_rebuild'
    ))
);
CREATE INDEX messenger_domain_outbox_events_created_idx
    ON messenger_domain_outbox_events (created_at, uuid);

CREATE TABLE messenger_projection_tasks (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    outbox_event_uuid uuid NOT NULL,
    task_kind varchar(64) NOT NULL,
    scope_kind varchar(64) NOT NULL,
    scope_key varchar(512) NOT NULL,
    ordering_key varchar(512) NOT NULL,
    ordering_created_at timestamp with time zone NOT NULL,
    payload jsonb NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'pending',
    lease_owner varchar(255),
    fencing_token bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamp with time zone,
    attempts integer NOT NULL DEFAULT 0,
    next_retry_at timestamp with time zone,
    last_error varchar(4096),
    progress_created_at timestamp without time zone,
    progress_uuid uuid,
    processed_count integer NOT NULL DEFAULT 0,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, outbox_event_uuid),
    FOREIGN KEY (project_id, outbox_event_uuid)
        REFERENCES messenger_domain_outbox_events(project_id, uuid)
        ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    CHECK (status IN (
        'pending', 'leased', 'running', 'completed', 'failed', 'dead_letter'
    )),
    CHECK (attempts >= 0),
    CHECK (processed_count >= 0)
);
CREATE INDEX messenger_projection_tasks_claim_idx
    ON messenger_projection_tasks (
        status, next_retry_at, ordering_created_at DESC, created_at, uuid
    )
    WHERE status IN ('pending', 'failed');
CREATE INDEX messenger_projection_tasks_expired_running_idx
    ON messenger_projection_tasks (lease_expires_at, created_at, uuid)
    WHERE status = 'running';
CREATE INDEX messenger_projection_tasks_predecessor_idx
    ON messenger_projection_tasks (
        project_id, scope_kind, scope_key, ordering_key, task_kind,
        created_at, uuid
    )
    WHERE status NOT IN ('completed', 'dead_letter');

CREATE TABLE messenger_projection_scope_leases (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    scope_kind varchar(64) NOT NULL,
    scope_key varchar(512) NOT NULL,
    owner varchar(255),
    fencing_token bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, scope_kind, scope_key)
);

CREATE TABLE messenger_fanout_roots (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    outbox_event_uuid uuid NOT NULL,
    placement_uuid uuid NOT NULL,
    next_user_uuid uuid,
    processed_count integer NOT NULL DEFAULT 0,
    status varchar(32) NOT NULL DEFAULT 'pending',
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, outbox_event_uuid),
    FOREIGN KEY (project_id, outbox_event_uuid)
        REFERENCES messenger_domain_outbox_events(project_id, uuid)
        ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    FOREIGN KEY (project_id, placement_uuid)
        REFERENCES messenger_message_placements(project_id, uuid)
        ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    CHECK (processed_count >= 0)
);

CREATE TABLE messenger_fanout_batch_tasks (
    uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    fanout_root_uuid uuid NOT NULL,
    batch_no integer NOT NULL,
    start_user_uuid uuid,
    end_user_uuid uuid,
    batch_size integer NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'pending',
    lease_owner varchar(255),
    fencing_token bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamp with time zone,
    attempts integer NOT NULL DEFAULT 0,
    next_retry_at timestamp with time zone,
    last_error varchar(4096),
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, fanout_root_uuid, batch_no),
    FOREIGN KEY (project_id, fanout_root_uuid)
        REFERENCES messenger_fanout_roots(project_id, uuid) ON DELETE CASCADE
        DEFERRABLE INITIALLY IMMEDIATE,
    CHECK (batch_no >= 0),
    CHECK (batch_size BETWEEN 1 AND 5000),
    CHECK (attempts >= 0)
);

CREATE TABLE messenger_event_membership_guards (
    event_uuid uuid NOT NULL,
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
    stream_uuid uuid,
    membership_generation integer NOT NULL,
    control_effect boolean NOT NULL DEFAULT false,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (event_uuid, user_uuid),
    FOREIGN KEY (project_id, user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, stream_uuid)
        REFERENCES messenger_streams(project_id, uuid)
        ON DELETE SET NULL (stream_uuid),
    CHECK (membership_generation >= 1)
);
CREATE INDEX messenger_event_membership_guards_visibility_idx
    ON messenger_event_membership_guards (
        project_id, user_uuid, stream_uuid, membership_generation
    );
CREATE OR REPLACE FUNCTION messenger_v2_delete_event_guards()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    DELETE FROM messenger_event_membership_guards
    WHERE event_uuid = OLD.uuid;
    RETURN OLD;
END;
$$;
CREATE TRIGGER messenger_v2_delete_direct_event_guards
AFTER DELETE ON m_workspace_events
FOR EACH ROW EXECUTE FUNCTION messenger_v2_delete_event_guards();
CREATE TRIGGER messenger_v2_delete_broadcast_event_guards
AFTER DELETE ON m_workspace_broadcast_message_events_v1
FOR EACH ROW EXECUTE FUNCTION messenger_v2_delete_event_guards();

ALTER VIEW m_workspace_visible_events
    RENAME TO m_workspace_visible_events_pre_messenger_v2;
CREATE VIEW m_workspace_visible_events AS
SELECT event.*
FROM m_workspace_visible_events_pre_messenger_v2 AS event
WHERE NOT EXISTS (
        SELECT 1
        FROM messenger_event_membership_guards AS guard
        WHERE guard.event_uuid = event.uuid
          AND guard.user_uuid = event.user_uuid
    )
   OR EXISTS (
        SELECT 1
        FROM messenger_event_membership_guards AS guard
        LEFT JOIN messenger_stream_bindings AS binding
          ON binding.project_id = guard.project_id
         AND binding.stream_uuid = guard.stream_uuid
         AND binding.user_uuid = guard.user_uuid
         AND binding.active
         AND binding.membership_generation = guard.membership_generation
        WHERE guard.event_uuid = event.uuid
          AND guard.user_uuid = event.user_uuid
          AND (guard.control_effect OR binding.uuid IS NOT NULL)
    );

INSERT INTO messenger_streams (
    uuid, project_id, name, description, owner_uuid, source_name, source,
    invite_only, announce, direct_user_uuid, private, is_archived,
    private_index, color, default_topic_uuid, provider, delivery,
    created_at, updated_at
)
SELECT
    uuid, project_id, name, description, user_uuid, source_name, source,
    invite_only, announce, direct_user_uuid, private, is_archived,
    private_index, color, default_topic_uuid, provider_metadata,
    delivery_metadata, created_at, updated_at
FROM m_workspace_streams;

INSERT INTO messenger_stream_bindings (
    uuid, project_id, stream_uuid, user_uuid, who_uuid, active,
    membership_generation, membership_started_at, role, notification_mode,
    notification_updated_at, unread_count, active_unread_count,
    passive_unread_count, last_message_uuid, created_at, updated_at
)
SELECT
    binding.uuid, binding.project_id, binding.stream_uuid, binding.user_uuid,
    binding.who_uuid, true, 1,
    binding.created_at AT TIME ZONE current_setting('TIMEZONE'),
    binding.role, binding.notification_mode,
    binding.notification_updated_at,
    COALESCE(user_stream.unread_count, 0),
    COALESCE(user_stream.active_unread_count, 0),
    COALESCE(user_stream.passive_unread_count, 0),
    user_stream.last_message_uuid, binding.created_at, binding.updated_at
FROM m_workspace_stream_bindings AS binding
LEFT JOIN m_workspace_user_streams AS user_stream
  ON user_stream.project_id = binding.project_id
 AND user_stream.user_uuid = binding.user_uuid
 AND user_stream.uuid = binding.stream_uuid;

WITH event_rows AS (
    SELECT event.uuid, event.project_id, event.user_uuid, event.payload,
           event.created_at, event.object_type, event.action
    FROM m_workspace_events AS event
    UNION ALL
    SELECT event.uuid, event.project_id, audience.user_uuid,
           event.payload || COALESCE(recipient.payload, '{}'::jsonb),
           event.created_at, event.object_type, event.action
    FROM m_workspace_broadcast_message_events_v1 AS event
    JOIN m_workspace_event_audience_members_v1 AS audience
      ON audience.audience_snapshot_uuid = event.audience_snapshot_uuid
    LEFT JOIN m_workspace_event_recipient_payloads_v1 AS recipient
      ON recipient.event_uuid = event.uuid
     AND recipient.user_uuid = audience.user_uuid
), resolved AS (
    SELECT event.*,
           CASE
               WHEN event.payload->>'stream_uuid' IS NOT NULL
               THEN (event.payload->>'stream_uuid')::uuid
               WHEN event.object_type = 'stream'
                    AND event.payload->>'uuid' IS NOT NULL
               THEN (event.payload->>'uuid')::uuid
               ELSE NULL
           END AS stream_uuid
    FROM event_rows AS event
), historical_memberships AS (
    SELECT resolved.project_id, resolved.user_uuid, resolved.stream_uuid,
           min(resolved.created_at) AS first_event_at
    FROM resolved
    JOIN messenger_streams AS stream
      ON stream.project_id = resolved.project_id
     AND stream.uuid = resolved.stream_uuid
    WHERE resolved.stream_uuid IS NOT NULL
    GROUP BY resolved.project_id, resolved.user_uuid, resolved.stream_uuid
)
INSERT INTO messenger_stream_bindings (
    uuid, project_id, stream_uuid, user_uuid, who_uuid, active,
    membership_generation, membership_started_at, role, notification_mode,
    created_at, updated_at
)
SELECT messenger_uuid_v5(
           membership.stream_uuid,
           'historical-membership:' || membership.user_uuid::text
       ),
       membership.project_id, membership.stream_uuid, membership.user_uuid,
       stream.owner_uuid, false, 1,
       membership.first_event_at,
       'member', 'default',
       membership.first_event_at AT TIME ZONE current_setting('TIMEZONE'),
       membership.first_event_at AT TIME ZONE current_setting('TIMEZONE')
FROM historical_memberships AS membership
JOIN messenger_streams AS stream
  ON stream.project_id = membership.project_id
 AND stream.uuid = membership.stream_uuid
JOIN messenger_project_users AS project_user
  ON project_user.project_id = membership.project_id
 AND project_user.user_uuid = membership.user_uuid
WHERE NOT EXISTS (
    SELECT 1
    FROM messenger_stream_bindings AS current
    WHERE current.project_id = membership.project_id
      AND current.stream_uuid = membership.stream_uuid
      AND current.user_uuid = membership.user_uuid
);

WITH event_rows AS (
    SELECT event.uuid, event.project_id, event.user_uuid, event.payload,
           event.created_at, event.object_type, event.action
    FROM m_workspace_events AS event
    UNION ALL
    SELECT event.uuid, event.project_id, audience.user_uuid,
           event.payload || COALESCE(recipient.payload, '{}'::jsonb),
           event.created_at, event.object_type, event.action
    FROM m_workspace_broadcast_message_events_v1 AS event
    JOIN m_workspace_event_audience_members_v1 AS audience
      ON audience.audience_snapshot_uuid = event.audience_snapshot_uuid
    LEFT JOIN m_workspace_event_recipient_payloads_v1 AS recipient
      ON recipient.event_uuid = event.uuid
     AND recipient.user_uuid = audience.user_uuid
), resolved AS (
    SELECT event.*,
           CASE
               WHEN event.payload->>'stream_uuid' IS NOT NULL
               THEN (event.payload->>'stream_uuid')::uuid
               WHEN event.object_type = 'stream'
                    AND event.payload->>'uuid' IS NOT NULL
               THEN (event.payload->>'uuid')::uuid
               ELSE NULL
           END AS stream_uuid
    FROM event_rows AS event
)
INSERT INTO messenger_event_membership_guards (
    event_uuid, project_id, user_uuid, stream_uuid,
    membership_generation, control_effect, created_at
)
SELECT resolved.uuid, resolved.project_id, resolved.user_uuid,
       resolved.stream_uuid,
       COALESCE(binding.membership_generation, 1),
       resolved.object_type = 'stream' AND resolved.action = 'deleted',
       resolved.created_at
FROM resolved
JOIN messenger_streams AS stream
  ON stream.project_id = resolved.project_id
 AND stream.uuid = resolved.stream_uuid
JOIN messenger_project_users AS project_user
  ON project_user.project_id = resolved.project_id
 AND project_user.user_uuid = resolved.user_uuid
LEFT JOIN messenger_stream_bindings AS binding
  ON binding.project_id = resolved.project_id
 AND binding.stream_uuid = resolved.stream_uuid
 AND binding.user_uuid = resolved.user_uuid
WHERE resolved.stream_uuid IS NOT NULL
ON CONFLICT (event_uuid, user_uuid) DO NOTHING;

UPDATE m_workspace_event_cursors AS cursor
SET epoch_generation = gen_random_uuid(),
    pruned_through_epoch_version = GREATEST(
        cursor.pruned_through_epoch_version,
        cursor.current_epoch_version
    ),
    updated_at = NOW()
WHERE EXISTS (
    SELECT 1 FROM messenger_event_membership_guards AS guard
    WHERE guard.project_id = cursor.project_id
      AND guard.user_uuid = cursor.user_uuid
);

INSERT INTO messenger_topics (
    uuid, project_id, stream_uuid, name, color, source_name, source,
    summary, summary_last_message_uuid, summary_enabled,
    summary_system_prompt, summary_reasoning_effort, provider, delivery,
    is_done, created_at, updated_at
)
SELECT
    topic.uuid, topic.project_id, topic.stream_uuid, topic.name, topic.color,
    topic.source_name, topic.source, topic.summary,
    topic.summary_last_message_uuid, topic.summary_enabled,
    topic.summary_system_prompt, topic.summary_reasoning_effort,
    topic.provider_metadata, topic.delivery_metadata,
    COALESCE(flags.is_done, false), topic.created_at, topic.updated_at
FROM m_workspace_stream_topics AS topic
LEFT JOIN (
    SELECT project_id, uuid, bool_or(is_done) AS is_done
    FROM m_workspace_user_topic_flags
    GROUP BY project_id, uuid
) AS flags
  ON flags.project_id = topic.project_id AND flags.uuid = topic.uuid;

INSERT INTO messenger_user_topic_bindings (
    uuid, project_id, user_uuid, topic_uuid, notification_mode,
    unread_count, active_unread_count, passive_unread_count,
    last_message_uuid, summary_has_new_messages, created_at, updated_at
)
SELECT
    messenger_uuid_v5(topic.uuid, binding.user_uuid::text),
    topic.project_id, binding.user_uuid, topic.uuid,
    COALESCE(flags.notification_mode, 'default'),
    COALESCE(user_topic.unread_count, 0),
    COALESCE(user_topic.active_unread_count, 0),
    COALESCE(user_topic.passive_unread_count, 0),
    user_topic.last_message_uuid,
    user_topic.summary_has_new_messages,
    LEAST(topic.created_at, binding.created_at),
    GREATEST(topic.updated_at, binding.updated_at)
FROM messenger_topics AS topic
JOIN messenger_stream_bindings AS binding
  ON binding.project_id = topic.project_id
 AND binding.stream_uuid = topic.stream_uuid
 AND binding.active
LEFT JOIN m_workspace_user_topic_flags AS flags
  ON flags.project_id = topic.project_id
 AND flags.user_uuid = binding.user_uuid
 AND flags.uuid = topic.uuid
LEFT JOIN m_workspace_user_topics_view AS user_topic
  ON user_topic.project_id = topic.project_id
 AND user_topic.user_uuid = binding.user_uuid
 AND user_topic.uuid = topic.uuid;

INSERT INTO messenger_folders (
    uuid, project_id, title, background_color_value, system_type,
    created_at, updated_at
)
SELECT template.uuid, project.project_id, template.title, 11184810, 'all',
       template.created_at, template.created_at
FROM (SELECT DISTINCT project_id FROM messenger_project_users) AS project
CROSS JOIN (
    VALUES
        ('00000000-0000-0000-0000-000000000000'::uuid,
         'All chats'::varchar, '2000-01-01 00:00:00'::timestamp),
        ('00000000-0000-0000-0000-000000000001'::uuid,
         'Personal'::varchar, '2000-01-01 00:00:01'::timestamp),
        ('00000000-0000-0000-0000-000000000002'::uuid,
         'Channels'::varchar, '2000-01-01 00:00:02'::timestamp)
) AS template(uuid, title, created_at);

INSERT INTO messenger_folders (
    uuid, project_id, title, background_color_value, system_type,
    created_at, updated_at
)
SELECT uuid, project_id, title, background_color_value, system_type,
       created_at, updated_at
FROM m_folders
WHERE uuid NOT IN (
    '00000000-0000-0000-0000-000000000000'::uuid,
    '00000000-0000-0000-0000-000000000001'::uuid,
    '00000000-0000-0000-0000-000000000002'::uuid
);

INSERT INTO messenger_user_folder_bindings (
    uuid, project_id, user_uuid, folder_uuid, rule,
    created_at, updated_at, snapshot_updated_at
)
SELECT messenger_uuid_v5(template.folder_uuid, member.user_uuid::text),
       member.project_id, member.user_uuid, template.folder_uuid, template.rule,
       template.created_at, template.created_at,
       template.created_at AT TIME ZONE current_setting('TIMEZONE')
FROM messenger_project_users AS member
CROSS JOIN (
    VALUES
        ('00000000-0000-0000-0000-000000000000'::uuid,
         'all_chats'::varchar, '2000-01-01 00:00:00'::timestamp),
        ('00000000-0000-0000-0000-000000000001'::uuid,
         'personal'::varchar, '2000-01-01 00:00:01'::timestamp),
        ('00000000-0000-0000-0000-000000000002'::uuid,
         'channels'::varchar, '2000-01-01 00:00:02'::timestamp)
) AS template(folder_uuid, rule, created_at);

INSERT INTO messenger_user_folder_bindings (
    uuid, project_id, user_uuid, folder_uuid, rule,
    created_at, updated_at, snapshot_updated_at
)
SELECT messenger_uuid_v5(folder.uuid, folder.user_uuid::text),
       folder.project_id, folder.user_uuid, folder.uuid, 'custom',
       folder.created_at, folder.updated_at,
       folder.updated_at AT TIME ZONE current_setting('TIMEZONE')
FROM m_folders AS folder
WHERE folder.uuid NOT IN (
    '00000000-0000-0000-0000-000000000000'::uuid,
    '00000000-0000-0000-0000-000000000001'::uuid,
    '00000000-0000-0000-0000-000000000002'::uuid
);

INSERT INTO messenger_folder_items (
    uuid, project_id, user_uuid, folder_uuid, stream_uuid,
    order_index, pinned_at, chat_type, automatic, created_at, updated_at
)
SELECT item.uuid, item.project_id, item.user_uuid, item.folder_uuid,
       item.stream_uuid, item.order_index, item.pinned_at, item.chat_type,
       item.folder_uuid IN (
           '00000000-0000-0000-0000-000000000000'::uuid,
           '00000000-0000-0000-0000-000000000001'::uuid,
           '00000000-0000-0000-0000-000000000002'::uuid
       ),
       item.created_at, item.updated_at
FROM m_folder_items AS item
JOIN messenger_user_folder_bindings AS binding
  ON binding.project_id = item.project_id
 AND binding.user_uuid = item.user_uuid
 AND binding.folder_uuid = item.folder_uuid
JOIN messenger_streams AS stream
  ON stream.project_id = item.project_id AND stream.uuid = item.stream_uuid;

INSERT INTO messenger_folder_items (
    uuid, project_id, user_uuid, folder_uuid, stream_uuid,
    chat_type, automatic, created_at, updated_at
)
SELECT
    (template.uuid_prefix || substr(stream.uuid::text, 3))::uuid,
    binding.project_id, binding.user_uuid, binding.folder_uuid, stream.uuid,
    CASE WHEN template.rule = 'personal' OR stream.private
         THEN 'private' ELSE 'stream' END,
    true, binding.created_at, binding.updated_at
FROM messenger_user_folder_bindings AS binding
JOIN messenger_stream_bindings AS membership
  ON membership.project_id = binding.project_id
 AND membership.user_uuid = binding.user_uuid
 AND membership.active
JOIN messenger_streams AS stream
  ON stream.project_id = membership.project_id
 AND stream.uuid = membership.stream_uuid
 AND NOT stream.is_archived
JOIN (
    VALUES
        ('00000000-0000-0000-0000-000000000000'::uuid,
         'all_chats'::varchar, '00'::text),
        ('00000000-0000-0000-0000-000000000001'::uuid,
         'personal'::varchar, '11'::text),
        ('00000000-0000-0000-0000-000000000002'::uuid,
         'channels'::varchar, '22'::text)
) AS template(folder_uuid, rule, uuid_prefix)
  ON template.folder_uuid = binding.folder_uuid
WHERE binding.rule = template.rule
  AND (template.rule = 'all_chats'
       OR (template.rule = 'personal' AND stream.private)
       OR (template.rule = 'channels' AND NOT stream.private))
ON CONFLICT (project_id, user_uuid, folder_uuid, stream_uuid) DO UPDATE SET
    chat_type = EXCLUDED.chat_type,
    automatic = true,
    updated_at = GREATEST(messenger_folder_items.updated_at, EXCLUDED.updated_at);

WITH snapshots AS (
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
                       'active_unread_count',
                           stream_binding.active_unread_count,
                       'passive_unread_count',
                           stream_binding.passive_unread_count,
                       'created_at', item.created_at,
                       'updated_at', item.updated_at
                   ) ORDER BY item.pinned_at DESC NULLS LAST,
                              item.order_index ASC NULLS LAST,
                              item.created_at, item.uuid
               ) FILTER (
                   WHERE item.uuid IS NOT NULL
                     AND stream_binding.user_uuid IS NOT NULL
                     AND visible_stream.uuid IS NOT NULL
               ),
               '[]'::jsonb
           ) AS folder_items_snapshot
    FROM messenger_user_folder_bindings AS binding
    LEFT JOIN messenger_folder_items AS item
      ON item.project_id = binding.project_id
     AND item.user_uuid = binding.user_uuid
     AND item.folder_uuid = binding.folder_uuid
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
    GROUP BY binding.project_id, binding.user_uuid, binding.folder_uuid
)
UPDATE messenger_user_folder_bindings AS binding
SET unread_count = snapshot.unread_count,
    folder_items_snapshot = snapshot.folder_items_snapshot,
    snapshot_version = 1,
    snapshot_updated_at = now()
FROM snapshots AS snapshot
WHERE binding.project_id = snapshot.project_id
  AND binding.user_uuid = snapshot.user_uuid
  AND binding.folder_uuid = snapshot.folder_uuid;

INSERT INTO messenger_messages (
    uuid, project_id, legacy_public_uuid, author_uuid, payload, source_name, source,
    provider_uuid, external_account_uuid, provider_external_id,
    provider, delivery, reaction_users, ingest_sequence,
    created_at, updated_at
)
SELECT
    uuid, project_id, uuid, user_uuid, payload, source_name, source,
    provider_uuid, external_account_uuid, provider_external_id,
    provider_metadata, delivery_metadata, reaction_users,
    COALESCE(
        ingest_sequence,
        nextval('messenger_messages_ingest_sequence_seq')
    ),
    created_at, updated_at
FROM m_workspace_messages;
SELECT setval(
    'messenger_messages_ingest_sequence_seq',
    GREATEST(COALESCE((SELECT max(ingest_sequence) FROM messenger_messages), 0), 1),
    EXISTS(SELECT 1 FROM messenger_messages)
);

INSERT INTO messenger_message_placements (
    uuid, project_id, legacy_public_uuid, message_uuid, stream_uuid, topic_uuid,
    created_at, updated_at
)
SELECT
    messenger_uuid_v5(message.topic_uuid, lower(message.uuid::text)),
    message.project_id, message.uuid, message.uuid,
    message.stream_uuid, message.topic_uuid,
    message.created_at, message.updated_at
FROM m_workspace_messages AS message;

INSERT INTO messenger_user_message_bindings (
    uuid, project_id, placement_uuid, user_uuid, membership_generation,
    relation_role, visibility, permissions, created_at, updated_at
)
SELECT
    messenger_uuid_v5(placement.uuid, binding.user_uuid::text),
    placement.project_id, placement.uuid, binding.user_uuid,
    binding.membership_generation,
    CASE WHEN message.author_uuid = binding.user_uuid THEN 'author' ELSE 'member' END,
    'visible',
    '{"read":true,"react":true,"star":true,"pin":true}'::jsonb,
    placement.created_at, placement.updated_at
FROM messenger_message_placements AS placement
JOIN messenger_messages AS message
  ON message.project_id = placement.project_id
 AND message.uuid = placement.message_uuid
JOIN messenger_stream_bindings AS binding
  ON binding.project_id = placement.project_id
 AND binding.stream_uuid = placement.stream_uuid
 AND binding.active;

INSERT INTO messenger_user_message_states (
    uuid, project_id, placement_uuid, user_uuid, membership_generation,
    read_at, mentioned, starred, pinned, created_at, updated_at
)
SELECT
    messenger_uuid_v5(placement.uuid, binding.user_uuid::text),
    placement.project_id, placement.uuid, binding.user_uuid,
    binding.membership_generation,
    CASE WHEN COALESCE(flags.read, false)
         THEN COALESCE(flags.updated_at, now()) ELSE NULL END,
    POSITION(
        '](' || 'urn:user:' || lower(binding.user_uuid::text) || ')'
        IN lower(COALESCE(message.payload->>'content', ''))
    ) > 0,
    COALESCE(flags.starred, false), COALESCE(flags.pinned, false),
    COALESCE(flags.created_at, now()), COALESCE(flags.updated_at, now())
FROM messenger_message_placements AS placement
JOIN messenger_messages AS message
  ON message.project_id = placement.project_id
 AND message.uuid = placement.message_uuid
JOIN messenger_stream_bindings AS binding
  ON binding.project_id = placement.project_id
 AND binding.stream_uuid = placement.stream_uuid
 AND binding.active
LEFT JOIN m_workspace_user_message_flags AS flags
  ON flags.project_id = placement.project_id
 AND flags.user_uuid = binding.user_uuid
 AND flags.uuid = message.uuid;

INSERT INTO messenger_message_reaction_facts (
    uuid, project_id, canonical_message_uuid, placement_uuid,
    user_uuid, emoji_name, created_at, updated_at
)
SELECT
    reaction.uuid, reaction.project_id, placement.message_uuid,
    placement.uuid, reaction.user_uuid, reaction.emoji_name,
    reaction.created_at, reaction.updated_at
FROM m_workspace_message_reactions AS reaction
JOIN messenger_message_placements AS placement
  ON placement.project_id = reaction.project_id
 AND placement.message_uuid = reaction.message_uuid;

UPDATE messenger_messages AS message
SET reactions = snapshot.reactions,
    reaction_users = snapshot.reaction_users
FROM (
    SELECT
        project_id,
        canonical_message_uuid,
        jsonb_object_agg(emoji_name, reaction_count) AS reactions,
        jsonb_object_agg(emoji_name, users) AS reaction_users
    FROM (
        SELECT
            project_id,
            canonical_message_uuid,
            emoji_name,
            count(*) AS reaction_count,
            jsonb_agg(user_uuid::text ORDER BY created_at, uuid) AS users
        FROM messenger_message_reaction_facts
        GROUP BY project_id, canonical_message_uuid, emoji_name
    ) AS grouped
    GROUP BY project_id, canonical_message_uuid
) AS snapshot
WHERE snapshot.project_id = message.project_id
  AND snapshot.canonical_message_uuid = message.uuid;

UPDATE messenger_topics AS topic
SET summary_last_message_uuid = placement.uuid
FROM messenger_message_placements AS placement
WHERE placement.project_id = topic.project_id
  AND placement.message_uuid = topic.summary_last_message_uuid;
UPDATE messenger_stream_bindings AS binding
SET last_message_uuid = placement.uuid
FROM messenger_message_placements AS placement
WHERE placement.project_id = binding.project_id
  AND placement.message_uuid = binding.last_message_uuid;
UPDATE messenger_user_topic_bindings AS binding
SET last_message_uuid = placement.uuid
FROM messenger_message_placements AS placement
WHERE placement.project_id = binding.project_id
  AND placement.message_uuid = binding.last_message_uuid;

ALTER TABLE messenger_topics
    ADD CONSTRAINT messenger_topics_summary_last_message_fk
    FOREIGN KEY (project_id, summary_last_message_uuid)
    REFERENCES messenger_message_placements(project_id, uuid)
    ON DELETE SET NULL (summary_last_message_uuid)
    DEFERRABLE INITIALLY IMMEDIATE;
ALTER TABLE messenger_stream_bindings
    ADD CONSTRAINT messenger_stream_bindings_last_message_fk
    FOREIGN KEY (project_id, last_message_uuid)
    REFERENCES messenger_message_placements(project_id, uuid)
    ON DELETE SET NULL (last_message_uuid)
    DEFERRABLE INITIALLY IMMEDIATE;
ALTER TABLE messenger_user_topic_bindings
    ADD CONSTRAINT messenger_user_topic_bindings_last_message_fk
    FOREIGN KEY (project_id, last_message_uuid)
    REFERENCES messenger_message_placements(project_id, uuid)
    ON DELETE SET NULL (last_message_uuid)
    DEFERRABLE INITIALLY IMMEDIATE;

CREATE OR REPLACE FUNCTION messenger_v2_rewrite_event_payload(
    target_project_id uuid,
    event_object_type text,
    payload_value jsonb,
    is_root boolean DEFAULT true,
    to_placement boolean DEFAULT true
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    item_key text;
    item_value jsonb;
    rewritten jsonb;
    mapped_uuid uuid;
BEGIN
    IF payload_value IS NULL THEN
        RETURN NULL;
    END IF;
    IF jsonb_typeof(payload_value) = 'array' THEN
        rewritten := '[]'::jsonb;
        FOR item_value IN SELECT value FROM jsonb_array_elements(payload_value)
        LOOP
            rewritten := rewritten || jsonb_build_array(
                messenger_v2_rewrite_event_payload(
                    target_project_id,
                    event_object_type,
                    item_value,
                    false,
                    to_placement
                )
            );
        END LOOP;
        RETURN rewritten;
    END IF;
    IF jsonb_typeof(payload_value) <> 'object' THEN
        RETURN payload_value;
    END IF;
    rewritten := '{}'::jsonb;
    FOR item_key, item_value IN SELECT key, value FROM jsonb_each(payload_value)
    LOOP
        mapped_uuid := NULL;
        IF jsonb_typeof(item_value) = 'string'
           AND (
                item_key IN (
                    'message_uuid', 'old_message_uuid', 'last_message_uuid',
                    'summary_last_message_uuid'
                )
                OR (
                    is_root AND event_object_type = 'message'
                    AND item_key = 'uuid'
                )
           )
        THEN
            IF to_placement THEN
                SELECT placement.uuid INTO mapped_uuid
                FROM messenger_messages AS message
                JOIN messenger_message_placements AS placement
                  ON placement.project_id = message.project_id
                 AND placement.message_uuid = message.uuid
                WHERE message.project_id = target_project_id
                  AND message.legacy_public_uuid::text = (item_value #>> '{}')
                  AND placement.legacy_public_uuid = message.legacy_public_uuid
                ORDER BY placement.uuid
                LIMIT 1;
            ELSE
                SELECT message.legacy_public_uuid INTO mapped_uuid
                FROM messenger_message_placements AS placement
                JOIN messenger_messages AS message
                  ON message.project_id = placement.project_id
                 AND message.uuid = placement.message_uuid
                WHERE placement.project_id = target_project_id
                  AND placement.uuid::text = (item_value #>> '{}')
                  AND placement.legacy_public_uuid IS NOT NULL
                LIMIT 1;
            END IF;
        END IF;
        IF mapped_uuid IS NOT NULL THEN
            item_value := to_jsonb(mapped_uuid::text);
        ELSE
            item_value := messenger_v2_rewrite_event_payload(
                target_project_id,
                event_object_type,
                item_value,
                false,
                to_placement
            );
        END IF;
        rewritten := rewritten || jsonb_build_object(item_key, item_value);
    END LOOP;
    RETURN rewritten;
END;
$$;

UPDATE m_workspace_events
SET payload = messenger_v2_rewrite_event_payload(
    project_id, object_type, payload, true, true
);
UPDATE m_workspace_broadcast_message_events_v1
SET payload = messenger_v2_rewrite_event_payload(
    project_id, object_type, payload, true, true
);
UPDATE m_workspace_event_recipient_payloads_v1 AS recipient
SET payload = messenger_v2_rewrite_event_payload(
    event.project_id, event.object_type, recipient.payload, true, true
)
FROM m_workspace_broadcast_message_events_v1 AS event
WHERE event.uuid = recipient.event_uuid;

CREATE VIEW messenger_api_user_folders_v1 AS
SELECT
    folder.uuid,
    folder.project_id,
    binding.user_uuid,
    folder.title,
    folder.background_color_value,
    folder.system_type,
    binding.unread_count,
    binding.folder_items_snapshot AS folder_items,
    folder.created_at,
    folder.updated_at
FROM messenger_user_folder_bindings AS binding
JOIN messenger_folders AS folder
  ON folder.project_id = binding.project_id
 AND folder.uuid = binding.folder_uuid;

CREATE VIEW messenger_api_user_folder_items_v1 AS
SELECT
    item.uuid,
    item.project_id,
    item.user_uuid,
    item.folder_uuid,
    item.stream_uuid,
    item.order_index,
    item.pinned_at,
    item.chat_type,
    COALESCE(binding.unread_count, 0) AS unread_count,
    COALESCE(binding.active_unread_count, 0) AS active_unread_count,
    COALESCE(binding.passive_unread_count, 0) AS passive_unread_count,
    item.created_at,
    item.updated_at
FROM messenger_folder_items AS item
JOIN messenger_stream_bindings AS binding
  ON binding.project_id = item.project_id
 AND binding.user_uuid = item.user_uuid
 AND binding.stream_uuid = item.stream_uuid
 AND binding.active
JOIN messenger_streams AS stream
  ON stream.project_id = item.project_id
 AND stream.uuid = item.stream_uuid
 AND stream.deleted_at IS NULL
 AND NOT stream.is_archived;

CREATE VIEW messenger_api_user_messages_v1 AS
SELECT
    binding.uuid AS binding_uuid,
    placement.uuid,
    message.uuid AS canonical_message_uuid,
    binding.user_uuid,
    placement.project_id,
    message.created_at,
    message.updated_at,
    placement.stream_uuid,
    placement.topic_uuid,
    message.payload,
    message.author_uuid,
    state.read_at IS NOT NULL AS read,
    state.pinned,
    state.starred,
    message.author_uuid = binding.user_uuid AS is_own,
    state.mentioned,
    message.reactions,
    message.reaction_users,
    message.source_name,
    message.source,
    message.provider,
    message.delivery,
    COALESCE(message.deleted_at, stream.deleted_at, topic.deleted_at)
        AS deleted_at,
    COALESCE(message.deleted_at, stream.deleted_at, topic.deleted_at)
        IS NULL AS visible
FROM messenger_user_message_bindings AS binding
JOIN messenger_message_placements AS placement
  ON placement.project_id = binding.project_id
 AND placement.uuid = binding.placement_uuid
JOIN messenger_messages AS message
  ON message.project_id = placement.project_id
 AND message.uuid = placement.message_uuid
JOIN messenger_user_message_states AS state
  ON state.project_id = binding.project_id
 AND state.user_uuid = binding.user_uuid
 AND state.placement_uuid = binding.placement_uuid
 AND state.membership_generation = binding.membership_generation
JOIN messenger_stream_bindings AS membership
  ON membership.project_id = binding.project_id
 AND membership.user_uuid = binding.user_uuid
 AND membership.stream_uuid = placement.stream_uuid
 AND membership.active
 AND membership.membership_generation = binding.membership_generation
JOIN messenger_streams AS stream
  ON stream.project_id = placement.project_id
 AND stream.uuid = placement.stream_uuid
JOIN messenger_topics AS topic
  ON topic.project_id = placement.project_id
 AND topic.uuid = placement.topic_uuid
WHERE binding.visibility = 'visible';

CREATE VIEW messenger_api_user_streams_v1 AS
SELECT
    stream.uuid,
    binding.user_uuid,
    stream.name,
    stream.description,
    stream.project_id,
    stream.created_at,
    stream.updated_at,
    stream.owner_uuid AS owner,
    binding.role,
    binding.notification_mode,
    binding.unread_count,
    binding.active_unread_count,
    binding.passive_unread_count,
    stream.source_name,
    stream.source,
    stream.invite_only,
    stream.announce,
    stream.private,
    stream.is_archived,
    CASE
        WHEN stream.direct_user_uuid IS NULL THEN NULL
        WHEN binding.user_uuid = stream.owner_uuid THEN stream.direct_user_uuid
        ELSE stream.owner_uuid
    END AS direct_user_uuid,
    stream.private_index,
    stream.color,
    binding.last_message_uuid,
    stream.default_topic_uuid,
    stream.provider,
    stream.delivery,
    stream.deleted_at,
    stream.deleted_at IS NULL AS visible
FROM messenger_stream_bindings AS binding
JOIN messenger_streams AS stream
  ON stream.project_id = binding.project_id
 AND stream.uuid = binding.stream_uuid
WHERE binding.active;

CREATE VIEW messenger_api_stream_bindings_v1 AS
SELECT
    target.uuid,
    viewer.project_id,
    viewer.user_uuid AS viewer_user_uuid,
    target.stream_uuid,
    target.user_uuid,
    target.who_uuid,
    target.role,
    target.notification_mode,
    target.notification_updated_at,
    target.created_at,
    target.updated_at,
    stream.deleted_at,
    stream.deleted_at IS NULL AS visible
FROM messenger_stream_bindings AS viewer
JOIN messenger_stream_bindings AS target
  ON target.project_id = viewer.project_id
 AND target.stream_uuid = viewer.stream_uuid
 AND target.active
JOIN messenger_streams AS stream
 ON stream.project_id = viewer.project_id
 AND stream.uuid = viewer.stream_uuid
WHERE viewer.active;

CREATE VIEW messenger_api_user_topics_v1 AS
SELECT
    topic.uuid,
    binding.user_uuid,
    topic.project_id,
    topic.created_at,
    topic.updated_at,
    topic.name,
    topic.stream_uuid,
    topic.color,
    binding.last_message_uuid,
    binding.unread_count,
    binding.active_unread_count,
    binding.passive_unread_count,
    stream.default_topic_uuid = topic.uuid AS is_default,
    topic.is_done,
    binding.notification_mode,
    topic.summary,
    topic.summary_last_message_uuid,
    binding.summary_has_new_messages,
    topic.summary_enabled,
    topic.summary_system_prompt,
    topic.summary_reasoning_effort,
    topic.source_name,
    topic.source,
    topic.provider,
    topic.delivery,
    COALESCE(topic.deleted_at, stream.deleted_at) AS deleted_at,
    COALESCE(topic.deleted_at, stream.deleted_at) IS NULL AS visible
FROM messenger_user_topic_bindings AS binding
JOIN messenger_topics AS topic
  ON topic.project_id = binding.project_id
 AND topic.uuid = binding.topic_uuid
JOIN messenger_streams AS stream
  ON stream.project_id = topic.project_id
 AND stream.uuid = topic.stream_uuid
JOIN messenger_stream_bindings AS membership
  ON membership.project_id = topic.project_id
 AND membership.user_uuid = binding.user_uuid
 AND membership.stream_uuid = topic.stream_uuid
 AND membership.active
;

CREATE VIEW messenger_api_message_reactions_v1 AS
SELECT
    fact.uuid,
    fact.project_id,
    binding.user_uuid AS viewer_user_uuid,
    fact.placement_uuid AS message_uuid,
    fact.user_uuid,
    fact.emoji_name,
    fact.created_at,
    fact.updated_at,
    message.provider,
    message.delivery,
    COALESCE(message.deleted_at, stream.deleted_at) AS deleted_at,
    COALESCE(message.deleted_at, stream.deleted_at) IS NULL AS visible
FROM messenger_message_reaction_facts AS fact
JOIN messenger_messages AS message
  ON message.project_id = fact.project_id
 AND message.uuid = fact.canonical_message_uuid
JOIN messenger_user_message_bindings AS binding
  ON binding.project_id = fact.project_id
 AND binding.placement_uuid = fact.placement_uuid
JOIN messenger_message_placements AS placement
  ON placement.project_id = fact.project_id
 AND placement.uuid = fact.placement_uuid
JOIN messenger_stream_bindings AS membership
  ON membership.project_id = fact.project_id
 AND membership.user_uuid = binding.user_uuid
 AND membership.stream_uuid = placement.stream_uuid
 AND membership.active
 AND membership.membership_generation = binding.membership_generation
JOIN messenger_streams AS stream
  ON stream.project_id = placement.project_id
 AND stream.uuid = placement.stream_uuid
WHERE binding.visibility = 'visible';

CREATE OR REPLACE FUNCTION messenger_v2_mirror_folder_binding_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    canonical messenger_folders%ROWTYPE;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.rule = 'custom' THEN
            DELETE FROM m_folders
            WHERE project_id = OLD.project_id AND user_uuid = OLD.user_uuid
              AND uuid = OLD.folder_uuid;
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.rule != 'custom' THEN
        RETURN NEW;
    END IF;
    SELECT * INTO canonical
    FROM messenger_folders
    WHERE project_id = NEW.project_id AND uuid = NEW.folder_uuid;
    INSERT INTO m_folders (
        uuid, project_id, user_uuid, title, background_color_value,
        system_type, created_at, updated_at
    ) VALUES (
        canonical.uuid, canonical.project_id, NEW.user_uuid, canonical.title,
        canonical.background_color_value, canonical.system_type,
        canonical.created_at, canonical.updated_at
    )
    ON CONFLICT (uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        user_uuid = EXCLUDED.user_uuid,
        title = EXCLUDED.title,
        background_color_value = EXCLUDED.background_color_value,
        system_type = EXCLUDED.system_type,
        updated_at = EXCLUDED.updated_at;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_mirror_folder_binding_to_legacy
AFTER INSERT OR UPDATE OR DELETE ON messenger_user_folder_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_folder_binding_to_legacy();

CREATE OR REPLACE FUNCTION messenger_v2_mirror_folder_item_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        DELETE FROM m_folder_items
        WHERE project_id = OLD.project_id AND user_uuid = OLD.user_uuid
          AND uuid = OLD.uuid;
        RETURN OLD;
    END IF;
    IF NEW.automatic AND NEW.pinned_at IS NULL AND NEW.order_index IS NULL THEN
        DELETE FROM m_folder_items
        WHERE project_id = NEW.project_id AND user_uuid = NEW.user_uuid
          AND folder_uuid = NEW.folder_uuid AND stream_uuid = NEW.stream_uuid;
        RETURN NEW;
    END IF;
    INSERT INTO m_folder_items (
        uuid, project_id, user_uuid, folder_uuid, stream_uuid,
        order_index, pinned_at, chat_type, created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.user_uuid, NEW.folder_uuid,
        NEW.stream_uuid, NEW.order_index, NEW.pinned_at, NEW.chat_type,
        NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, user_uuid, stream_uuid, folder_uuid)
    DO UPDATE SET
        uuid = EXCLUDED.uuid,
        project_id = EXCLUDED.project_id,
        user_uuid = EXCLUDED.user_uuid,
        folder_uuid = EXCLUDED.folder_uuid,
        stream_uuid = EXCLUDED.stream_uuid,
        order_index = EXCLUDED.order_index,
        pinned_at = EXCLUDED.pinned_at,
        chat_type = EXCLUDED.chat_type,
        updated_at = EXCLUDED.updated_at;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_mirror_folder_item_to_legacy
AFTER INSERT OR UPDATE OR DELETE ON messenger_folder_items
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_folder_item_to_legacy();

CREATE OR REPLACE FUNCTION messenger_v2_mirror_stream_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
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
    INSERT INTO m_workspace_streams (
        uuid, project_id, name, description, source_name, source, user_uuid,
        created_at, updated_at, invite_only, announce, private,
        direct_user_uuid, private_index, is_archived, color,
        default_topic_uuid, provider_metadata, delivery_metadata
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.name, NEW.description,
        NEW.source_name, NEW.source, NEW.owner_uuid, NEW.created_at,
        NEW.updated_at, NEW.invite_only, NEW.announce, NEW.private,
        NEW.direct_user_uuid, NEW.private_index, NEW.is_archived, NEW.color,
        NEW.default_topic_uuid, NEW.provider, NEW.delivery
    )
    ON CONFLICT (uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        user_uuid = EXCLUDED.user_uuid,
        updated_at = EXCLUDED.updated_at,
        invite_only = EXCLUDED.invite_only,
        announce = EXCLUDED.announce,
        private = EXCLUDED.private,
        direct_user_uuid = EXCLUDED.direct_user_uuid,
        private_index = EXCLUDED.private_index,
        is_archived = EXCLUDED.is_archived,
        color = EXCLUDED.color,
        default_topic_uuid = EXCLUDED.default_topic_uuid,
        provider_metadata = EXCLUDED.provider_metadata,
        delivery_metadata = EXCLUDED.delivery_metadata;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_mirror_stream_to_legacy
AFTER INSERT OR UPDATE OR DELETE ON messenger_streams
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_stream_to_legacy();

CREATE OR REPLACE FUNCTION messenger_v2_mirror_binding_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        DELETE FROM m_workspace_stream_bindings WHERE uuid = OLD.uuid;
        RETURN OLD;
    END IF;
    IF NOT NEW.active THEN
        DELETE FROM m_workspace_stream_bindings WHERE uuid = NEW.uuid;
        RETURN NEW;
    END IF;
    INSERT INTO m_workspace_stream_bindings (
        uuid, project_id, stream_uuid, user_uuid, who_uuid,
        role, notification_mode, notification_updated_at,
        created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.stream_uuid, NEW.user_uuid,
        NEW.who_uuid, NEW.role, NEW.notification_mode,
        NEW.notification_updated_at, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        stream_uuid = EXCLUDED.stream_uuid,
        user_uuid = EXCLUDED.user_uuid,
        who_uuid = EXCLUDED.who_uuid,
        role = EXCLUDED.role,
        notification_mode = EXCLUDED.notification_mode,
        notification_updated_at = EXCLUDED.notification_updated_at,
        updated_at = EXCLUDED.updated_at;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_mirror_binding_to_legacy
AFTER INSERT OR UPDATE OR DELETE ON messenger_stream_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_binding_to_legacy();

CREATE OR REPLACE FUNCTION messenger_v2_mirror_topic_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
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
    IF NEW.deleted_at IS NOT NULL THEN
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
    INSERT INTO m_workspace_stream_topics (
        uuid, project_id, stream_uuid, name, color, source_name, source,
        summary, summary_last_message_uuid, summary_enabled,
        summary_system_prompt, summary_reasoning_effort,
        provider_metadata, delivery_metadata, created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.stream_uuid, NEW.name, NEW.color,
        NEW.source_name, NEW.source, NEW.summary,
        legacy_summary_uuid, NEW.summary_enabled,
        NEW.summary_system_prompt, NEW.summary_reasoning_effort,
        NEW.provider, NEW.delivery, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        stream_uuid = EXCLUDED.stream_uuid,
        name = EXCLUDED.name,
        color = EXCLUDED.color,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        summary = EXCLUDED.summary,
        summary_last_message_uuid = EXCLUDED.summary_last_message_uuid,
        summary_enabled = EXCLUDED.summary_enabled,
        summary_system_prompt = EXCLUDED.summary_system_prompt,
        summary_reasoning_effort = EXCLUDED.summary_reasoning_effort,
        provider_metadata = EXCLUDED.provider_metadata,
        delivery_metadata = EXCLUDED.delivery_metadata,
        updated_at = EXCLUDED.updated_at;
    UPDATE m_workspace_user_topic_flags
    SET is_done = NEW.is_done, updated_at = NEW.updated_at
    WHERE project_id = NEW.project_id AND uuid = NEW.uuid;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_mirror_topic_to_legacy
AFTER INSERT OR UPDATE OR DELETE ON messenger_topics
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_topic_to_legacy();

CREATE OR REPLACE FUNCTION messenger_v2_mirror_topic_binding_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    done_value boolean;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        DELETE FROM m_workspace_user_topic_flags
        WHERE project_id = OLD.project_id
          AND user_uuid = OLD.user_uuid
          AND uuid = OLD.topic_uuid;
        RETURN OLD;
    END IF;
    SELECT is_done INTO done_value
    FROM messenger_topics
    WHERE project_id = NEW.project_id AND uuid = NEW.topic_uuid;
    INSERT INTO m_workspace_user_topic_flags (
        uuid, user_uuid, project_id, is_done, notification_mode,
        created_at, updated_at
    ) VALUES (
        NEW.topic_uuid, NEW.user_uuid, NEW.project_id,
        COALESCE(done_value, false), NEW.notification_mode,
        NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (uuid, user_uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        is_done = EXCLUDED.is_done,
        notification_mode = EXCLUDED.notification_mode,
        updated_at = EXCLUDED.updated_at;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_mirror_topic_binding_to_legacy
AFTER INSERT OR UPDATE OR DELETE ON messenger_user_topic_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_topic_binding_to_legacy();

CREATE OR REPLACE FUNCTION messenger_v2_mirror_placement_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
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
    INSERT INTO m_workspace_messages (
        uuid, project_id, stream_uuid, user_uuid, payload,
        created_at, updated_at, topic_uuid, source_name, source,
        provider_uuid, external_account_uuid, provider_external_id,
        provider_metadata, delivery_metadata, reaction_users
    ) VALUES (
        COALESCE(NEW.legacy_public_uuid, NEW.uuid), NEW.project_id,
        NEW.stream_uuid, source_row.author_uuid,
        source_row.payload, source_row.created_at, source_row.updated_at,
        NEW.topic_uuid, source_row.source_name, source_row.source,
        source_row.provider_uuid, source_row.external_account_uuid,
        source_row.provider_external_id, source_row.provider,
        source_row.delivery, source_row.reaction_users
    )
    ON CONFLICT (uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        stream_uuid = EXCLUDED.stream_uuid,
        user_uuid = EXCLUDED.user_uuid,
        payload = EXCLUDED.payload,
        updated_at = EXCLUDED.updated_at,
        topic_uuid = EXCLUDED.topic_uuid,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        provider_uuid = EXCLUDED.provider_uuid,
        external_account_uuid = EXCLUDED.external_account_uuid,
        provider_external_id = EXCLUDED.provider_external_id,
        provider_metadata = EXCLUDED.provider_metadata,
        delivery_metadata = EXCLUDED.delivery_metadata,
        reaction_users = EXCLUDED.reaction_users;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_mirror_placement_to_legacy
AFTER INSERT OR UPDATE OR DELETE ON messenger_message_placements
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_placement_to_legacy();

CREATE OR REPLACE FUNCTION messenger_v2_mirror_message_update_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
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
    UPDATE m_workspace_messages AS legacy
    SET user_uuid = NEW.author_uuid,
        payload = NEW.payload,
        source_name = NEW.source_name,
        source = NEW.source,
        provider_uuid = NEW.provider_uuid,
        external_account_uuid = NEW.external_account_uuid,
        provider_external_id = NEW.provider_external_id,
        provider_metadata = NEW.provider,
        delivery_metadata = NEW.delivery,
        reaction_users = NEW.reaction_users,
        updated_at = NEW.updated_at
    FROM messenger_message_placements AS placement
    WHERE placement.project_id = NEW.project_id
      AND placement.message_uuid = NEW.uuid
      AND legacy.uuid = COALESCE(
          placement.legacy_public_uuid, placement.uuid
      );
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_mirror_message_update_to_legacy
BEFORE UPDATE OR DELETE ON messenger_messages
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_message_update_to_legacy();

CREATE OR REPLACE FUNCTION messenger_v2_mirror_state_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    legacy_message_uuid uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
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
CREATE TRIGGER messenger_v2_mirror_state_to_legacy
AFTER INSERT OR UPDATE OR DELETE ON messenger_user_message_states
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_state_to_legacy();

CREATE OR REPLACE FUNCTION messenger_v2_mirror_reaction_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    legacy_message_uuid uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        DELETE FROM m_workspace_message_reactions WHERE uuid = OLD.uuid;
        RETURN OLD;
    END IF;
    SELECT COALESCE(placement.legacy_public_uuid, placement.uuid)
      INTO legacy_message_uuid
    FROM messenger_message_placements AS placement
    WHERE placement.project_id = NEW.project_id
      AND placement.uuid = NEW.placement_uuid;
    INSERT INTO m_workspace_message_reactions (
        uuid, project_id, message_uuid, user_uuid, emoji_name,
        created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, legacy_message_uuid, NEW.user_uuid,
        NEW.emoji_name, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        message_uuid = EXCLUDED.message_uuid,
        user_uuid = EXCLUDED.user_uuid,
        emoji_name = EXCLUDED.emoji_name,
        updated_at = EXCLUDED.updated_at;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_mirror_reaction_to_legacy
AFTER INSERT OR UPDATE OR DELETE ON messenger_message_reaction_facts
FOR EACH ROW EXECUTE FUNCTION messenger_v2_mirror_reaction_to_legacy();

-- Rolling-update compatibility.  Direct writes from a previous server process
-- are projected into v2, while pg_trigger_depth prevents mirror recursion.
CREATE OR REPLACE FUNCTION messenger_v2_register_project_user(
    target_project_id uuid, target_user_uuid uuid
)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO messenger_project_users (project_id, user_uuid)
    SELECT target_project_id, target_user_uuid
    WHERE target_user_uuid IS NOT NULL
    ON CONFLICT (project_id, user_uuid) DO UPDATE SET updated_at = NOW();

    INSERT INTO messenger_folders (
        uuid, project_id, title, background_color_value, system_type,
        created_at, updated_at
    )
    SELECT template.uuid, target_project_id, template.title, 11184810, 'all',
           template.created_at, template.created_at
    FROM (
        VALUES
            ('00000000-0000-0000-0000-000000000000'::uuid,
             'All chats'::varchar, '2000-01-01 00:00:00'::timestamp),
            ('00000000-0000-0000-0000-000000000001'::uuid,
             'Personal'::varchar, '2000-01-01 00:00:01'::timestamp),
            ('00000000-0000-0000-0000-000000000002'::uuid,
             'Channels'::varchar, '2000-01-01 00:00:02'::timestamp)
    ) AS template(uuid, title, created_at)
    WHERE target_user_uuid IS NOT NULL
    ON CONFLICT (project_id, uuid) DO NOTHING;

    INSERT INTO messenger_user_folder_bindings (
        uuid, project_id, user_uuid, folder_uuid, rule,
        created_at, updated_at, snapshot_updated_at
    )
    SELECT messenger_uuid_v5(template.folder_uuid, target_user_uuid::text),
           target_project_id, target_user_uuid, template.folder_uuid,
           template.rule, template.created_at, template.created_at,
           template.created_at AT TIME ZONE current_setting('TIMEZONE')
    FROM (
        VALUES
            ('00000000-0000-0000-0000-000000000000'::uuid,
             'all_chats'::varchar, '2000-01-01 00:00:00'::timestamp),
            ('00000000-0000-0000-0000-000000000001'::uuid,
             'personal'::varchar, '2000-01-01 00:00:01'::timestamp),
            ('00000000-0000-0000-0000-000000000002'::uuid,
             'channels'::varchar, '2000-01-01 00:00:02'::timestamp)
    ) AS template(folder_uuid, rule, created_at)
    WHERE target_user_uuid IS NOT NULL
    ON CONFLICT (project_id, user_uuid, folder_uuid) DO NOTHING;
$$;

CREATE OR REPLACE FUNCTION messenger_v2_move_canonical_stream_project(
    target_stream_uuid uuid,
    source_project_id uuid,
    destination_project_id uuid
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    target_user_uuid uuid;
    topic_uuids uuid[];
    placement_uuids uuid[];
    message_uuids uuid[];
    event_uuids uuid[];
BEGIN
    IF source_project_id = destination_project_id THEN
        RETURN;
    END IF;
    SET CONSTRAINTS ALL DEFERRED;
    SELECT COALESCE(array_agg(topic.uuid), ARRAY[]::uuid[])
      INTO topic_uuids
    FROM messenger_topics AS topic
    WHERE topic.project_id = source_project_id
      AND topic.stream_uuid = target_stream_uuid;
    SELECT COALESCE(array_agg(placement.uuid), ARRAY[]::uuid[]),
           COALESCE(array_agg(DISTINCT placement.message_uuid), ARRAY[]::uuid[])
      INTO placement_uuids, message_uuids
    FROM messenger_message_placements AS placement
    WHERE placement.project_id = source_project_id
      AND placement.stream_uuid = target_stream_uuid;

    FOR target_user_uuid IN
        SELECT project_user.user_uuid
        FROM messenger_project_users AS project_user
        WHERE project_user.project_id = source_project_id
          AND (
              EXISTS (
                  SELECT 1 FROM messenger_stream_bindings AS binding
                  WHERE binding.project_id = source_project_id
                    AND binding.stream_uuid = target_stream_uuid
                    AND project_user.user_uuid IN (
                        binding.user_uuid, binding.who_uuid
                    )
              )
              OR EXISTS (
                  SELECT 1 FROM messenger_messages AS message
                  WHERE message.project_id = source_project_id
                    AND message.uuid = ANY(message_uuids)
                    AND message.author_uuid = project_user.user_uuid
              )
              OR EXISTS (
                  SELECT 1 FROM messenger_message_reaction_facts AS reaction
                  WHERE reaction.project_id = source_project_id
                    AND reaction.canonical_message_uuid = ANY(message_uuids)
                    AND reaction.user_uuid = project_user.user_uuid
              )
          )
    LOOP
        PERFORM messenger_v2_register_project_user(
            destination_project_id, target_user_uuid
        );
    END LOOP;

    SELECT COALESCE(array_agg(event.uuid), ARRAY[]::uuid[])
      INTO event_uuids
    FROM messenger_domain_outbox_events AS event
    WHERE event.project_id = source_project_id
      AND (
          event.payload->>'stream_uuid' = target_stream_uuid::text
          OR (event.payload->>'topic_uuid')::uuid = ANY(topic_uuids)
          OR (event.payload->>'placement_uuid')::uuid = ANY(placement_uuids)
          OR (event.payload#>>'{placement,uuid}')::uuid = ANY(placement_uuids)
          OR (event.payload->>'canonical_message_uuid')::uuid = ANY(message_uuids)
      );

    DELETE FROM messenger_projection_scope_leases AS lease
    WHERE lease.project_id = source_project_id
      AND EXISTS (
          SELECT 1 FROM messenger_projection_tasks AS task
          WHERE task.project_id = source_project_id
            AND task.outbox_event_uuid = ANY(event_uuids)
            AND task.scope_kind = lease.scope_kind
            AND task.scope_key = lease.scope_key
      );
    UPDATE messenger_fanout_batch_tasks AS batch
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE batch.project_id = source_project_id
      AND EXISTS (
          SELECT 1 FROM messenger_fanout_roots AS root
          WHERE root.project_id = source_project_id
            AND root.uuid = batch.fanout_root_uuid
            AND root.placement_uuid = ANY(placement_uuids)
      );
    UPDATE messenger_fanout_roots
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND placement_uuid = ANY(placement_uuids);
    UPDATE messenger_projection_tasks
    SET project_id = destination_project_id,
        scope_key = CASE
            WHEN scope_key LIKE source_project_id::text || ':%'
            THEN destination_project_id::text ||
                 substr(scope_key, length(source_project_id::text) + 1)
            ELSE scope_key
        END,
        updated_at = NOW()
    WHERE project_id = source_project_id
      AND outbox_event_uuid = ANY(event_uuids);
    UPDATE messenger_domain_outbox_events
    SET project_id = destination_project_id,
        scope_key = CASE
            WHEN scope_key LIKE source_project_id::text || ':%'
            THEN destination_project_id::text ||
                 substr(scope_key, length(source_project_id::text) + 1)
            ELSE scope_key
        END,
        updated_at = NOW()
    WHERE project_id = source_project_id AND uuid = ANY(event_uuids);

    UPDATE messenger_event_membership_guards
    SET stream_uuid = NULL
    WHERE project_id = source_project_id
      AND stream_uuid = target_stream_uuid;
    DELETE FROM messenger_folder_items
    WHERE project_id = source_project_id
      AND stream_uuid = target_stream_uuid;
    UPDATE messenger_user_message_bindings
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND placement_uuid = ANY(placement_uuids);
    UPDATE messenger_user_message_states
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND placement_uuid = ANY(placement_uuids);
    UPDATE messenger_message_reaction_facts
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND canonical_message_uuid = ANY(message_uuids);
    UPDATE messenger_user_topic_bindings
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND topic_uuid = ANY(topic_uuids);
    UPDATE messenger_message_placements
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND uuid = ANY(placement_uuids);
    UPDATE messenger_messages
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND uuid = ANY(message_uuids);
    UPDATE messenger_topics
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND uuid = ANY(topic_uuids);
    UPDATE messenger_stream_bindings
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND stream_uuid = target_stream_uuid;
    UPDATE messenger_streams
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE project_id = source_project_id
      AND uuid = target_stream_uuid;
END;
$$;

CREATE OR REPLACE FUNCTION messenger_v2_move_canonical_message_project(
    target_legacy_uuid uuid,
    source_project_id uuid,
    destination_project_id uuid,
    destination_stream_uuid uuid,
    destination_topic_uuid uuid
)
RETURNS void LANGUAGE plpgsql AS $$
<<message_move>>
DECLARE
    canonical_uuid uuid;
    placement_uuid uuid;
    target_user_uuid uuid;
    event_uuids uuid[];
BEGIN
    IF source_project_id = destination_project_id THEN
        RETURN;
    END IF;
    SELECT placement.message_uuid, placement.uuid
      INTO canonical_uuid, placement_uuid
    FROM messenger_message_placements AS placement
    WHERE placement.project_id = source_project_id
      AND (
          placement.legacy_public_uuid = target_legacy_uuid
          OR placement.uuid = target_legacy_uuid
          OR placement.message_uuid = target_legacy_uuid
      )
    ORDER BY (placement.legacy_public_uuid = target_legacy_uuid) DESC,
             placement.uuid
    LIMIT 1;
    IF message_move.canonical_uuid IS NULL THEN
        RETURN;
    END IF;
    SET CONSTRAINTS ALL DEFERRED;
    FOR target_user_uuid IN
        SELECT message.author_uuid
        FROM messenger_messages AS message
        WHERE message.project_id = source_project_id
          AND message.uuid = message_move.canonical_uuid
        UNION
        SELECT binding.user_uuid
        FROM messenger_user_message_bindings AS binding
        WHERE binding.project_id = source_project_id
          AND binding.placement_uuid = message_move.placement_uuid
        UNION
        SELECT state.user_uuid
        FROM messenger_user_message_states AS state
        WHERE state.project_id = source_project_id
          AND state.placement_uuid = message_move.placement_uuid
        UNION
        SELECT reaction.user_uuid
        FROM messenger_message_reaction_facts AS reaction
        WHERE reaction.project_id = source_project_id
          AND reaction.canonical_message_uuid = message_move.canonical_uuid
    LOOP
        PERFORM messenger_v2_register_project_user(
            destination_project_id, target_user_uuid
        );
    END LOOP;
    SELECT COALESCE(array_agg(event.uuid), ARRAY[]::uuid[])
      INTO event_uuids
    FROM messenger_domain_outbox_events AS event
    WHERE event.project_id = source_project_id
      AND (
          event.payload->>'placement_uuid' =
              message_move.placement_uuid::text
          OR event.payload#>>'{placement,uuid}' =
              message_move.placement_uuid::text
          OR event.payload->>'canonical_message_uuid' =
              message_move.canonical_uuid::text
          OR (
              event.scope_kind = 'message'
              AND event.scope_key =
                  source_project_id::text || ':' ||
                  message_move.canonical_uuid::text
          )
      );
    DELETE FROM messenger_projection_scope_leases AS lease
    WHERE lease.project_id = source_project_id
      AND EXISTS (
          SELECT 1 FROM messenger_projection_tasks AS task
          WHERE task.project_id = source_project_id
            AND task.outbox_event_uuid = ANY(event_uuids)
            AND task.scope_kind = lease.scope_kind
            AND task.scope_key = lease.scope_key
      );
    UPDATE messenger_fanout_batch_tasks AS batch
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE batch.project_id = source_project_id
      AND EXISTS (
          SELECT 1 FROM messenger_fanout_roots AS root
          WHERE root.project_id = source_project_id
            AND root.uuid = batch.fanout_root_uuid
            AND root.placement_uuid = message_move.placement_uuid
      );
    UPDATE messenger_fanout_roots AS root
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE root.project_id = source_project_id
      AND root.placement_uuid = message_move.placement_uuid;
    UPDATE messenger_projection_tasks
    SET project_id = destination_project_id,
        scope_key = CASE
            WHEN scope_key LIKE source_project_id::text || ':%'
            THEN destination_project_id::text ||
                 substr(scope_key, length(source_project_id::text) + 1)
            ELSE scope_key
        END,
        payload = jsonb_set(
            jsonb_set(payload, '{stream_uuid}',
                      to_jsonb(destination_stream_uuid::text), true),
            '{topic_uuid}', to_jsonb(destination_topic_uuid::text), true
        ),
        updated_at = NOW()
    WHERE project_id = source_project_id
      AND outbox_event_uuid = ANY(event_uuids);
    UPDATE messenger_domain_outbox_events
    SET project_id = destination_project_id,
        scope_key = CASE
            WHEN scope_key LIKE source_project_id::text || ':%'
            THEN destination_project_id::text ||
                 substr(scope_key, length(source_project_id::text) + 1)
            ELSE scope_key
        END,
        payload = jsonb_set(
            jsonb_set(payload, '{stream_uuid}',
                      to_jsonb(destination_stream_uuid::text), true),
            '{topic_uuid}', to_jsonb(destination_topic_uuid::text), true
        ),
        updated_at = NOW()
    WHERE project_id = source_project_id AND uuid = ANY(event_uuids);
    UPDATE messenger_user_message_bindings AS binding
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE binding.project_id = source_project_id
      AND binding.placement_uuid = message_move.placement_uuid;
    UPDATE messenger_user_message_states AS state
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE state.project_id = source_project_id
      AND state.placement_uuid = message_move.placement_uuid;
    UPDATE messenger_message_reaction_facts AS reaction
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE reaction.project_id = source_project_id
      AND reaction.canonical_message_uuid = message_move.canonical_uuid;
    UPDATE messenger_message_placements AS placement
    SET project_id = destination_project_id,
        stream_uuid = destination_stream_uuid,
        topic_uuid = destination_topic_uuid,
        updated_at = NOW()
    WHERE placement.project_id = source_project_id
      AND placement.uuid = message_move.placement_uuid;
    UPDATE messenger_messages AS message
    SET project_id = destination_project_id, updated_at = NOW()
    WHERE message.project_id = source_project_id
      AND message.uuid = message_move.canonical_uuid;
END;
$$;

CREATE OR REPLACE FUNCTION messenger_v2_rewrite_rolling_event_payload()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.payload := messenger_v2_rewrite_event_payload(
        NEW.project_id, NEW.object_type, NEW.payload, true, true
    );
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_rewrite_direct_event_payload
BEFORE INSERT OR UPDATE OF payload ON m_workspace_events
FOR EACH ROW EXECUTE FUNCTION messenger_v2_rewrite_rolling_event_payload();
CREATE TRIGGER messenger_v2_rewrite_broadcast_event_payload
BEFORE INSERT OR UPDATE OF payload ON m_workspace_broadcast_message_events_v1
FOR EACH ROW EXECUTE FUNCTION messenger_v2_rewrite_rolling_event_payload();

CREATE OR REPLACE FUNCTION messenger_v2_rewrite_rolling_recipient_payload()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_event m_workspace_broadcast_message_events_v1%ROWTYPE;
BEGIN
    SELECT * INTO target_event
    FROM m_workspace_broadcast_message_events_v1
    WHERE uuid = NEW.event_uuid;
    IF target_event.uuid IS NOT NULL THEN
        NEW.payload := messenger_v2_rewrite_event_payload(
            target_event.project_id, target_event.object_type,
            NEW.payload, true, true
        );
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_rewrite_recipient_event_payload
BEFORE INSERT OR UPDATE OF payload ON m_workspace_event_recipient_payloads_v1
FOR EACH ROW EXECUTE FUNCTION messenger_v2_rewrite_rolling_recipient_payload();

CREATE OR REPLACE FUNCTION messenger_v2_guard_rolling_direct_event()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_stream_uuid uuid;
    target_generation integer;
BEGIN
    target_stream_uuid := CASE
        WHEN NEW.payload->>'stream_uuid' IS NOT NULL
        THEN (NEW.payload->>'stream_uuid')::uuid
        WHEN NEW.object_type = 'stream' AND NEW.payload->>'uuid' IS NOT NULL
        THEN (NEW.payload->>'uuid')::uuid
    END;
    IF target_stream_uuid IS NULL OR NOT EXISTS (
        SELECT 1 FROM messenger_streams
        WHERE project_id = NEW.project_id AND uuid = target_stream_uuid
    ) THEN
        RETURN NEW;
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
    INSERT INTO messenger_stream_bindings (
        uuid, project_id, stream_uuid, user_uuid, who_uuid, active,
        membership_generation, membership_started_at, role, notification_mode,
        created_at, updated_at
    )
    SELECT messenger_uuid_v5(
               target_stream_uuid,
               'historical-membership:' || NEW.user_uuid::text
           ),
           NEW.project_id, target_stream_uuid, NEW.user_uuid, stream.owner_uuid,
           false, 1, NEW.created_at, 'member', 'default',
           NEW.created_at AT TIME ZONE current_setting('TIMEZONE'),
           NEW.created_at AT TIME ZONE current_setting('TIMEZONE')
    FROM messenger_streams AS stream
    WHERE stream.project_id = NEW.project_id AND stream.uuid = target_stream_uuid
      AND NOT EXISTS (
          SELECT 1 FROM messenger_stream_bindings AS binding
          WHERE binding.project_id = NEW.project_id
            AND binding.stream_uuid = target_stream_uuid
            AND binding.user_uuid = NEW.user_uuid
      );
    SELECT membership_generation INTO target_generation
    FROM messenger_stream_bindings
    WHERE project_id = NEW.project_id AND stream_uuid = target_stream_uuid
      AND user_uuid = NEW.user_uuid;
    INSERT INTO messenger_event_membership_guards (
        event_uuid, project_id, user_uuid, stream_uuid,
        membership_generation, control_effect, created_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.user_uuid, target_stream_uuid,
        target_generation,
        NEW.object_type = 'stream' AND NEW.action = 'deleted',
        NEW.created_at
    )
    ON CONFLICT (event_uuid, user_uuid) DO NOTHING;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_guard_rolling_direct_event
AFTER INSERT ON m_workspace_events
FOR EACH ROW EXECUTE FUNCTION messenger_v2_guard_rolling_direct_event();

CREATE OR REPLACE FUNCTION messenger_v2_guard_rolling_broadcast_event()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_stream_uuid uuid;
BEGIN
    target_stream_uuid := CASE
        WHEN NEW.payload->>'stream_uuid' IS NOT NULL
        THEN (NEW.payload->>'stream_uuid')::uuid
        WHEN NEW.object_type = 'stream' AND NEW.payload->>'uuid' IS NOT NULL
        THEN (NEW.payload->>'uuid')::uuid
    END;
    IF target_stream_uuid IS NULL OR NOT EXISTS (
        SELECT 1 FROM messenger_streams
        WHERE project_id = NEW.project_id AND uuid = target_stream_uuid
    ) THEN
        RETURN NEW;
    END IF;
    INSERT INTO messenger_project_users (project_id, user_uuid)
    SELECT NEW.project_id, audience.user_uuid
    FROM m_workspace_event_audience_members_v1 AS audience
    WHERE audience.audience_snapshot_uuid = NEW.audience_snapshot_uuid
    ON CONFLICT (project_id, user_uuid) DO UPDATE SET updated_at = NOW();
    INSERT INTO messenger_stream_bindings (
        uuid, project_id, stream_uuid, user_uuid, who_uuid, active,
        membership_generation, membership_started_at, role, notification_mode,
        created_at, updated_at
    )
    SELECT messenger_uuid_v5(
               target_stream_uuid,
               'historical-membership:' || audience.user_uuid::text
           ),
           NEW.project_id, target_stream_uuid, audience.user_uuid,
           stream.owner_uuid, false, 1, NEW.created_at,
           'member', 'default',
           NEW.created_at AT TIME ZONE current_setting('TIMEZONE'),
           NEW.created_at AT TIME ZONE current_setting('TIMEZONE')
    FROM m_workspace_event_audience_members_v1 AS audience
    JOIN messenger_streams AS stream
      ON stream.project_id = NEW.project_id AND stream.uuid = target_stream_uuid
    WHERE audience.audience_snapshot_uuid = NEW.audience_snapshot_uuid
      AND NOT EXISTS (
          SELECT 1 FROM messenger_stream_bindings AS binding
          WHERE binding.project_id = NEW.project_id
            AND binding.stream_uuid = target_stream_uuid
            AND binding.user_uuid = audience.user_uuid
      );
    INSERT INTO messenger_event_membership_guards (
        event_uuid, project_id, user_uuid, stream_uuid,
        membership_generation, control_effect, created_at
    )
    SELECT NEW.uuid, NEW.project_id, audience.user_uuid, target_stream_uuid,
           binding.membership_generation,
           NEW.object_type = 'stream' AND NEW.action = 'deleted',
           NEW.created_at
    FROM m_workspace_event_audience_members_v1 AS audience
    JOIN messenger_stream_bindings AS binding
      ON binding.project_id = NEW.project_id
     AND binding.stream_uuid = target_stream_uuid
     AND binding.user_uuid = audience.user_uuid
    WHERE audience.audience_snapshot_uuid = NEW.audience_snapshot_uuid
    ON CONFLICT (event_uuid, user_uuid) DO NOTHING;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_guard_rolling_broadcast_event
AFTER INSERT ON m_workspace_broadcast_message_events_v1
FOR EACH ROW EXECUTE FUNCTION messenger_v2_guard_rolling_broadcast_event();

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_folder()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF COALESCE(NEW.uuid, OLD.uuid) IN (
        '00000000-0000-0000-0000-000000000000'::uuid,
        '00000000-0000-0000-0000-000000000001'::uuid,
        '00000000-0000-0000-0000-000000000002'::uuid
    ) THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload
        ) VALUES (
            gen_random_uuid(), OLD.project_id, 'folder_projection',
            'user-folder',
            OLD.project_id::text || ':' || OLD.user_uuid::text || ':' ||
                OLD.uuid::text,
            jsonb_build_object(
                'source_kind', 'folder.deleted',
                'user_uuid', OLD.user_uuid,
                'folder_uuid', OLD.uuid,
                'emit_public_event', false
            )
        );
        DELETE FROM messenger_user_folder_bindings
        WHERE project_id = OLD.project_id AND user_uuid = OLD.user_uuid
          AND folder_uuid = OLD.uuid;
        DELETE FROM messenger_folders
        WHERE project_id = OLD.project_id AND uuid = OLD.uuid;
        RETURN OLD;
    END IF;
    PERFORM messenger_v2_register_project_user(NEW.project_id, NEW.user_uuid);
    INSERT INTO messenger_folders (
        uuid, project_id, title, background_color_value, system_type,
        created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.title, NEW.background_color_value,
        NEW.system_type, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        title = EXCLUDED.title,
        background_color_value = EXCLUDED.background_color_value,
        system_type = EXCLUDED.system_type,
        updated_at = EXCLUDED.updated_at;
    INSERT INTO messenger_user_folder_bindings (
        uuid, project_id, user_uuid, folder_uuid, rule,
        created_at, updated_at, snapshot_updated_at
    ) VALUES (
        messenger_uuid_v5(NEW.uuid, NEW.user_uuid::text),
        NEW.project_id, NEW.user_uuid, NEW.uuid, 'custom',
        NEW.created_at, NEW.updated_at,
        NEW.updated_at AT TIME ZONE current_setting('TIMEZONE')
    )
    ON CONFLICT (project_id, user_uuid, folder_uuid) DO UPDATE SET
        updated_at = EXCLUDED.updated_at;
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    ) VALUES (
        gen_random_uuid(), NEW.project_id, 'folder_projection',
        'user-folder',
        NEW.project_id::text || ':' || NEW.user_uuid::text || ':' ||
            NEW.uuid::text,
        jsonb_build_object(
            'source_kind', CASE WHEN TG_OP = 'INSERT'
                                THEN 'folder.created'
                                ELSE 'folder.updated' END,
            'user_uuid', NEW.user_uuid,
            'folder_uuid', NEW.uuid,
            'emit_public_event', false
        )
    );
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_import_legacy_folder
AFTER INSERT OR UPDATE OR DELETE ON m_folders
FOR EACH ROW EXECUTE FUNCTION messenger_v2_import_legacy_folder();

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_folder_item()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    is_system boolean;
    source_event_uuid uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        is_system := OLD.folder_uuid IN (
            '00000000-0000-0000-0000-000000000000'::uuid,
            '00000000-0000-0000-0000-000000000001'::uuid,
            '00000000-0000-0000-0000-000000000002'::uuid
        );
        IF is_system THEN
            UPDATE messenger_folder_items
            SET order_index = NULL, pinned_at = NULL, updated_at = NOW()
            WHERE project_id = OLD.project_id AND user_uuid = OLD.user_uuid
              AND folder_uuid = OLD.folder_uuid
              AND stream_uuid = OLD.stream_uuid;
        ELSE
            DELETE FROM messenger_folder_items
            WHERE project_id = OLD.project_id AND user_uuid = OLD.user_uuid
              AND uuid = OLD.uuid;
        END IF;
        source_event_uuid := gen_random_uuid();
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload
        ) VALUES (
            source_event_uuid, OLD.project_id, 'folder_projection',
            'user-folder',
            OLD.project_id::text || ':' || OLD.user_uuid::text || ':' ||
                OLD.folder_uuid::text,
            jsonb_build_object(
                'source_kind', 'folder_item.deleted',
                'user_uuid', OLD.user_uuid,
                'folder_uuid', OLD.folder_uuid,
                'stream_uuid', OLD.stream_uuid,
                'item_uuid', OLD.uuid,
                'emit_public_event', false
            )
        );
        RETURN OLD;
    END IF;
    PERFORM messenger_v2_register_project_user(NEW.project_id, NEW.user_uuid);
    is_system := NEW.folder_uuid IN (
        '00000000-0000-0000-0000-000000000000'::uuid,
        '00000000-0000-0000-0000-000000000001'::uuid,
        '00000000-0000-0000-0000-000000000002'::uuid
    );
    INSERT INTO messenger_folder_items (
        uuid, project_id, user_uuid, folder_uuid, stream_uuid,
        order_index, pinned_at, chat_type, automatic,
        created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.user_uuid, NEW.folder_uuid,
        NEW.stream_uuid, NEW.order_index, NEW.pinned_at, NEW.chat_type,
        is_system, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, user_uuid, folder_uuid, stream_uuid) DO UPDATE SET
        order_index = EXCLUDED.order_index,
        pinned_at = EXCLUDED.pinned_at,
        chat_type = EXCLUDED.chat_type,
        updated_at = EXCLUDED.updated_at;
    source_event_uuid := gen_random_uuid();
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    ) VALUES (
        source_event_uuid, NEW.project_id, 'folder_projection', 'user-folder',
        NEW.project_id::text || ':' || NEW.user_uuid::text || ':' ||
            NEW.folder_uuid::text,
        jsonb_build_object(
            'source_kind', CASE WHEN TG_OP = 'INSERT'
                                THEN 'folder_item.created'
                                ELSE 'folder_item.updated' END,
            'user_uuid', NEW.user_uuid,
            'folder_uuid', NEW.folder_uuid,
            'stream_uuid', NEW.stream_uuid,
            'item_uuid', NEW.uuid,
            'emit_public_event', false
        )
    );
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_import_legacy_folder_item
AFTER INSERT OR UPDATE OR DELETE ON m_folder_items
FOR EACH ROW EXECUTE FUNCTION messenger_v2_import_legacy_folder_item();

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_stream()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        UPDATE messenger_streams
        SET deleted_at = NOW()
        WHERE project_id = OLD.project_id AND uuid = OLD.uuid;
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload
        ) VALUES (
            gen_random_uuid(), OLD.project_id, 'delivery_snapshot_event',
            'resource',
            OLD.project_id::text || ':stream:' || OLD.uuid::text,
            jsonb_build_object(
                'source_kind', 'stream.deleted',
                'stream_uuid', OLD.uuid,
                'source_name', OLD.source_name,
                'source', OLD.source,
                'all_recipients', true,
                'private', OLD.private,
                'emit_public_event', false
            )
        );
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
    INSERT INTO messenger_streams (
        uuid, project_id, name, description, owner_uuid, source_name, source,
        invite_only, announce, direct_user_uuid, private, is_archived,
        private_index, color, default_topic_uuid, provider, delivery,
        created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.name, NEW.description, NEW.user_uuid,
        NEW.source_name, NEW.source, NEW.invite_only, NEW.announce,
        NEW.direct_user_uuid, NEW.private, NEW.is_archived, NEW.private_index,
        NEW.color,
        CASE WHEN EXISTS (
            SELECT 1 FROM messenger_topics
            WHERE project_id = NEW.project_id AND uuid = NEW.default_topic_uuid
        ) THEN NEW.default_topic_uuid ELSE NULL END,
        NEW.provider_metadata,
        NEW.delivery_metadata, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        owner_uuid = EXCLUDED.owner_uuid,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        invite_only = EXCLUDED.invite_only,
        announce = EXCLUDED.announce,
        direct_user_uuid = EXCLUDED.direct_user_uuid,
        private = EXCLUDED.private,
        is_archived = EXCLUDED.is_archived,
        private_index = EXCLUDED.private_index,
        color = EXCLUDED.color,
        default_topic_uuid = EXCLUDED.default_topic_uuid,
        provider = EXCLUDED.provider,
        delivery = EXCLUDED.delivery,
        deleted_at = NULL,
        updated_at = EXCLUDED.updated_at;
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    ) VALUES (
        gen_random_uuid(), NEW.project_id, 'delivery_snapshot_event',
        'resource', NEW.project_id::text || ':stream:' || NEW.uuid::text,
        jsonb_build_object(
            'source_kind', CASE WHEN TG_OP = 'INSERT'
                                THEN 'stream.created'
                                ELSE 'stream.updated' END,
            'resource_kind', 'stream',
            'resource_uuid', NEW.uuid,
            'stream_uuid', NEW.uuid,
            'emit_public_event', false
        )
    );
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_import_legacy_stream
AFTER INSERT OR UPDATE OR DELETE ON m_workspace_streams
FOR EACH ROW EXECUTE FUNCTION messenger_v2_import_legacy_stream();

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_generation integer;
    target_started_at timestamp with time zone;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        UPDATE messenger_stream_bindings
        SET active = false,
            membership_generation = membership_generation + 1,
            updated_at = now()
        WHERE project_id = OLD.project_id
          AND stream_uuid = OLD.stream_uuid
          AND user_uuid = OLD.user_uuid;
        SELECT membership_generation INTO target_generation
        FROM messenger_stream_bindings
        WHERE project_id = OLD.project_id
          AND stream_uuid = OLD.stream_uuid
          AND user_uuid = OLD.user_uuid;
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload
        )
        SELECT messenger_uuid_v5(
                   COALESCE(
                       OLD.uuid,
                       messenger_uuid_v5(
                           OLD.stream_uuid, OLD.user_uuid::text
                       )
                   ),
                   'legacy-binding-delete:' || target_generation::text || ':' ||
                   folder.folder_uuid::text
               ),
               OLD.project_id, 'folder_projection', 'user-folder',
               OLD.project_id::text || ':' || OLD.user_uuid::text || ':' ||
                   folder.folder_uuid::text,
               jsonb_build_object(
                   'source_kind', 'stream.deleted',
                   'user_uuid', OLD.user_uuid,
                   'stream_uuid', OLD.stream_uuid,
                   'folder_uuid', folder.folder_uuid
               )
        FROM (
            SELECT '00000000-0000-0000-0000-000000000000'::uuid
                       AS folder_uuid
            UNION
            SELECT CASE WHEN stream.private
                        THEN '00000000-0000-0000-0000-000000000001'::uuid
                        ELSE '00000000-0000-0000-0000-000000000002'::uuid END
            FROM messenger_streams AS stream
            WHERE stream.project_id = OLD.project_id
              AND stream.uuid = OLD.stream_uuid
            UNION
            SELECT DISTINCT item.folder_uuid
            FROM messenger_folder_items AS item
            WHERE item.project_id = OLD.project_id
              AND item.user_uuid = OLD.user_uuid
              AND item.stream_uuid = OLD.stream_uuid
        ) AS folder
        ON CONFLICT (project_id, uuid) DO NOTHING;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.project_id IS DISTINCT FROM NEW.project_id THEN
        RETURN NEW;
    END IF;
    PERFORM messenger_v2_register_project_user(NEW.project_id, NEW.user_uuid);
    PERFORM messenger_v2_register_project_user(NEW.project_id, NEW.who_uuid);
    INSERT INTO messenger_stream_bindings (
        uuid, project_id, stream_uuid, user_uuid, who_uuid, active,
        membership_generation, membership_started_at, role, notification_mode,
        notification_updated_at, created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.stream_uuid, NEW.user_uuid, NEW.who_uuid,
        true, 1,
        NEW.created_at AT TIME ZONE current_setting('TIMEZONE'),
        NEW.role, NEW.notification_mode,
        NEW.notification_updated_at, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, user_uuid, stream_uuid) DO UPDATE SET
        active = true,
        membership_generation = CASE
            WHEN messenger_stream_bindings.active
            THEN messenger_stream_bindings.membership_generation
            ELSE messenger_stream_bindings.membership_generation + 1
        END,
        membership_started_at = CASE
            WHEN messenger_stream_bindings.active
            THEN messenger_stream_bindings.membership_started_at
            ELSE EXCLUDED.updated_at AT TIME ZONE current_setting('TIMEZONE')
        END,
        who_uuid = EXCLUDED.who_uuid,
        role = EXCLUDED.role,
        notification_mode = EXCLUDED.notification_mode,
        notification_updated_at = EXCLUDED.notification_updated_at,
        updated_at = EXCLUDED.updated_at;
    SELECT membership_generation, membership_started_at
      INTO target_generation, target_started_at
    FROM messenger_stream_bindings
    WHERE project_id = NEW.project_id AND stream_uuid = NEW.stream_uuid
      AND user_uuid = NEW.user_uuid;
    INSERT INTO messenger_user_topic_bindings (
        uuid, project_id, user_uuid, topic_uuid, notification_mode,
        created_at, updated_at
    )
    SELECT messenger_uuid_v5(topic.uuid, NEW.user_uuid::text),
           NEW.project_id, NEW.user_uuid, topic.uuid, 'default',
           NEW.created_at, NEW.updated_at
    FROM messenger_topics AS topic
    WHERE topic.project_id = NEW.project_id
      AND topic.stream_uuid = NEW.stream_uuid
    ON CONFLICT (project_id, user_uuid, topic_uuid) DO NOTHING;
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    )
    SELECT messenger_uuid_v5(
               topic.uuid,
               'legacy-membership:' || NEW.user_uuid::text || ':' ||
               target_generation::text
           ),
           NEW.project_id, 'topic_membership_policy_rebuild', 'topic',
           NEW.project_id::text || ':' || topic.uuid::text,
           jsonb_build_object(
               'source_kind', 'stream_binding.created',
               'user_uuid', NEW.user_uuid,
               'membership_generation', target_generation,
               'membership_started_at', target_started_at,
               'stream_uuid', NEW.stream_uuid,
               'topic_uuid', topic.uuid
           )
    FROM messenger_topics AS topic
    WHERE topic.project_id = NEW.project_id
      AND topic.stream_uuid = NEW.stream_uuid
    ON CONFLICT (project_id, uuid) DO NOTHING;
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_import_legacy_binding
AFTER INSERT OR UPDATE OR DELETE ON m_workspace_stream_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_v2_import_legacy_binding();

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_topic()
RETURNS trigger LANGUAGE plpgsql AS $$
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
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload
        ) VALUES (
            gen_random_uuid(), OLD.project_id, 'delivery_snapshot_event',
            'topic', OLD.project_id::text || ':' || OLD.uuid::text,
            jsonb_build_object(
                'source_kind', 'topic.deleted',
                'topic_uuid', OLD.uuid,
                'stream_uuid', OLD.stream_uuid,
                'source_name', OLD.source_name,
                'source', OLD.source,
                'emit_public_event', false
            )
        );
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
    INSERT INTO messenger_topics (
        uuid, project_id, stream_uuid, name, color, source_name, source,
        summary, summary_last_message_uuid, summary_enabled,
        summary_system_prompt, summary_reasoning_effort, provider, delivery,
        is_done, created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.stream_uuid, NEW.name, NEW.color,
        NEW.source_name, NEW.source, NEW.summary,
        summary_placement, NEW.summary_enabled,
        NEW.summary_system_prompt, NEW.summary_reasoning_effort,
        NEW.provider_metadata, NEW.delivery_metadata, done_value,
        NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        stream_uuid = EXCLUDED.stream_uuid,
        name = EXCLUDED.name,
        color = EXCLUDED.color,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        summary = EXCLUDED.summary,
        summary_last_message_uuid = EXCLUDED.summary_last_message_uuid,
        summary_enabled = EXCLUDED.summary_enabled,
        summary_system_prompt = EXCLUDED.summary_system_prompt,
        summary_reasoning_effort = EXCLUDED.summary_reasoning_effort,
        provider = EXCLUDED.provider,
        delivery = EXCLUDED.delivery,
        is_done = EXCLUDED.is_done,
        deleted_at = NULL,
        updated_at = EXCLUDED.updated_at;
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
CREATE TRIGGER messenger_v2_import_legacy_topic
AFTER INSERT OR UPDATE OR DELETE ON m_workspace_stream_topics
FOR EACH ROW EXECUTE FUNCTION messenger_v2_import_legacy_topic();

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_topic_flags()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_project_id uuid;
    target_user_uuid uuid;
    target_topic_uuid uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        target_project_id := OLD.project_id;
        target_user_uuid := OLD.user_uuid;
        target_topic_uuid := OLD.uuid;
        UPDATE messenger_topics
        SET is_done = COALESCE((
                SELECT bool_or(flag.is_done)
                FROM m_workspace_user_topic_flags AS flag
                WHERE flag.project_id = OLD.project_id
                  AND flag.uuid = OLD.uuid
            ), false),
            version = version + 1,
            updated_at = NOW()
        WHERE project_id = OLD.project_id AND uuid = OLD.uuid;
        UPDATE messenger_user_topic_bindings
        SET notification_mode = 'default', updated_at = NOW()
        WHERE project_id = OLD.project_id AND user_uuid = OLD.user_uuid
          AND topic_uuid = OLD.uuid;
    ELSE
        target_project_id := NEW.project_id;
        target_user_uuid := NEW.user_uuid;
        target_topic_uuid := NEW.uuid;
        IF NOT EXISTS (
            SELECT 1 FROM messenger_topics
            WHERE project_id = NEW.project_id AND uuid = NEW.uuid
        ) THEN
            RETURN NEW;
        END IF;
        PERFORM messenger_v2_register_project_user(
            NEW.project_id, NEW.user_uuid
        );
        UPDATE messenger_topics
        SET is_done = NEW.is_done, version = version + 1,
            updated_at = NEW.updated_at
        WHERE project_id = NEW.project_id AND uuid = NEW.uuid;
        INSERT INTO messenger_user_topic_bindings (
            uuid, project_id, user_uuid, topic_uuid, notification_mode,
            created_at, updated_at
        ) VALUES (
            messenger_uuid_v5(NEW.uuid, NEW.user_uuid::text),
            NEW.project_id, NEW.user_uuid, NEW.uuid, NEW.notification_mode,
            NEW.created_at, NEW.updated_at
        )
        ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE SET
            notification_mode = EXCLUDED.notification_mode,
            updated_at = EXCLUDED.updated_at;
    END IF;
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    ) VALUES (
        gen_random_uuid(), target_project_id, 'topic_state_projection',
        'topic', target_project_id::text || ':' || target_topic_uuid::text,
        jsonb_build_object(
            'source_kind', 'topic.updated',
            'topic_uuid', target_topic_uuid,
            'recipient_uuid', target_user_uuid,
            'emit_public_event', false
        )
    );
    RETURN COALESCE(NEW, OLD);
END;
$$;
CREATE TRIGGER messenger_v2_import_legacy_topic_flags
AFTER INSERT OR UPDATE OR DELETE ON m_workspace_user_topic_flags
FOR EACH ROW EXECUTE FUNCTION messenger_v2_import_legacy_topic_flags();

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_message()
RETURNS trigger LANGUAGE plpgsql AS $$
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
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                gen_random_uuid(), OLD.project_id, 'delivery_snapshot_event',
                'message', OLD.project_id::text || ':' ||
                    target_canonical_uuid::text,
                jsonb_build_object(
                    'source_kind', 'message.deleted',
                    'placement', jsonb_build_object(
                        'uuid', target_placement.uuid,
                        'stream_uuid', target_placement.stream_uuid,
                        'topic_uuid', target_placement.topic_uuid
                    ),
                    'canonical_message_uuid', target_canonical_uuid,
                    'message_created_at', OLD.created_at,
                    'author_uuid', OLD.user_uuid,
                    'source_name', OLD.source_name,
                    'source', OLD.source,
                    'emit_public_event', false
                )
            );
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
        IF NEW.source_name = 'zulip'
           AND NEW.provider_metadata->>'provider_realm_uuid' IS NOT NULL
           AND NEW.provider_external_id ~ '^(0|[1-9][0-9]*)$'
        THEN
            target_canonical_uuid := messenger_v2_provider_message_uuid(
                NEW.provider_metadata,
                NEW.provider_external_id,
                NEW.uuid
            );
        ELSE
            target_canonical_uuid := NEW.uuid;
        END IF;
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
    INSERT INTO messenger_messages (
        uuid, project_id, legacy_public_uuid, author_uuid, payload,
        source_name, source,
        provider_uuid, external_account_uuid, provider_external_id,
        provider_realm_uuid, provider_message_id,
        provider, delivery, reaction_users, ingest_sequence,
        created_at, updated_at
    ) VALUES (
        target_canonical_uuid, NEW.project_id,
        NEW.uuid,
        NEW.user_uuid, NEW.payload,
        NEW.source_name, NEW.source, NEW.provider_uuid,
        NEW.external_account_uuid, NEW.provider_external_id,
        CASE
            WHEN NEW.source_name = 'zulip'
             AND NEW.provider_metadata->>'provider_realm_uuid' IS NOT NULL
             AND NEW.provider_external_id ~ '^(0|[1-9][0-9]*)$'
            THEN (NEW.provider_metadata->>'provider_realm_uuid')::uuid
        END,
        CASE
            WHEN NEW.source_name = 'zulip'
             AND NEW.provider_metadata->>'provider_realm_uuid' IS NOT NULL
             AND NEW.provider_external_id ~ '^(0|[1-9][0-9]*)$'
            THEN NEW.provider_external_id
        END,
        NEW.provider_metadata, NEW.delivery_metadata, NEW.reaction_users,
        COALESCE(
            NEW.ingest_sequence,
            nextval('messenger_messages_ingest_sequence_seq')
        ),
        NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        legacy_public_uuid = COALESCE(
            messenger_messages.legacy_public_uuid,
            EXCLUDED.legacy_public_uuid
        ),
        author_uuid = EXCLUDED.author_uuid,
        payload = EXCLUDED.payload,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        provider_uuid = EXCLUDED.provider_uuid,
        external_account_uuid = EXCLUDED.external_account_uuid,
        provider_external_id = EXCLUDED.provider_external_id,
        provider_realm_uuid = COALESCE(
            EXCLUDED.provider_realm_uuid,
            messenger_messages.provider_realm_uuid
        ),
        provider_message_id = COALESCE(
            EXCLUDED.provider_message_id,
            messenger_messages.provider_message_id
        ),
        provider = EXCLUDED.provider,
        delivery = EXCLUDED.delivery,
        reaction_users = EXCLUDED.reaction_users,
        deleted_at = NULL,
        updated_at = EXCLUDED.updated_at;
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
CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_message_inserts()
RETURNS trigger LANGUAGE plpgsql AS $$
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
    INSERT INTO messenger_messages (
        uuid, project_id, legacy_public_uuid, author_uuid, payload,
        source_name, source, provider_uuid, external_account_uuid,
        provider_external_id, provider_realm_uuid, provider_message_id,
        provider, delivery, reaction_users,
        ingest_sequence, created_at, updated_at
    )
    SELECT messenger_v2_provider_message_uuid(
               message.provider_metadata,
               message.provider_external_id,
               message.uuid
           ),
           message.project_id, message.uuid, message.user_uuid,
           message.payload, message.source_name, message.source,
           message.provider_uuid, message.external_account_uuid,
           message.provider_external_id,
           CASE
               WHEN message.source_name = 'zulip'
                AND message.provider_metadata->>'provider_realm_uuid' IS NOT NULL
                AND message.provider_external_id ~ '^(0|[1-9][0-9]*)$'
               THEN (message.provider_metadata->>'provider_realm_uuid')::uuid
           END,
           CASE
               WHEN message.source_name = 'zulip'
                AND message.provider_metadata->>'provider_realm_uuid' IS NOT NULL
                AND message.provider_external_id ~ '^(0|[1-9][0-9]*)$'
               THEN message.provider_external_id
           END,
           message.provider_metadata,
           message.delivery_metadata, message.reaction_users,
           COALESCE(
               message.ingest_sequence,
               nextval('messenger_messages_ingest_sequence_seq')
           ),
           message.created_at, message.updated_at
    FROM inserted_legacy_messages AS message
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        author_uuid = EXCLUDED.author_uuid,
        payload = EXCLUDED.payload,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        provider_uuid = EXCLUDED.provider_uuid,
        external_account_uuid = EXCLUDED.external_account_uuid,
        provider_external_id = EXCLUDED.provider_external_id,
        provider_realm_uuid = COALESCE(
            EXCLUDED.provider_realm_uuid,
            messenger_messages.provider_realm_uuid
        ),
        provider_message_id = COALESCE(
            EXCLUDED.provider_message_id,
            messenger_messages.provider_message_id
        ),
        provider = EXCLUDED.provider,
        delivery = EXCLUDED.delivery,
        reaction_users = EXCLUDED.reaction_users,
        deleted_at = NULL,
        updated_at = EXCLUDED.updated_at;
    INSERT INTO messenger_message_placements (
        uuid, project_id, legacy_public_uuid, message_uuid, stream_uuid,
        topic_uuid, created_at, updated_at
    )
    SELECT messenger_uuid_v5(
               message.topic_uuid,
               lower(messenger_v2_provider_message_uuid(
                   message.provider_metadata,
                   message.provider_external_id,
                   message.uuid
               )::text)
           ),
           message.project_id, message.uuid,
           messenger_v2_provider_message_uuid(
               message.provider_metadata,
               message.provider_external_id,
               message.uuid
           ),
           message.stream_uuid, message.topic_uuid,
           message.created_at, message.updated_at
    FROM inserted_legacy_messages AS message
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        legacy_public_uuid = EXCLUDED.legacy_public_uuid,
        stream_uuid = EXCLUDED.stream_uuid,
        topic_uuid = EXCLUDED.topic_uuid,
        updated_at = EXCLUDED.updated_at;
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
CREATE TRIGGER messenger_v2_import_legacy_message
AFTER UPDATE OR DELETE ON m_workspace_messages
FOR EACH ROW EXECUTE FUNCTION messenger_v2_import_legacy_message();
CREATE TRIGGER messenger_v2_import_legacy_message_inserts
AFTER INSERT ON m_workspace_messages
REFERENCING NEW TABLE AS inserted_legacy_messages
FOR EACH STATEMENT EXECUTE FUNCTION messenger_v2_import_legacy_message_inserts();

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_message_flags()
RETURNS trigger LANGUAGE plpgsql AS $$
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
    RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_message_flag_inserts()
RETURNS trigger LANGUAGE plpgsql AS $$
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
    );
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_v2_import_legacy_message_flags
AFTER UPDATE OR DELETE ON m_workspace_user_message_flags
FOR EACH ROW EXECUTE FUNCTION messenger_v2_import_legacy_message_flags();
CREATE TRIGGER messenger_v2_import_legacy_message_flag_inserts
AFTER INSERT ON m_workspace_user_message_flags
REFERENCING NEW TABLE AS inserted_legacy_message_flags
FOR EACH STATEMENT
EXECUTE FUNCTION messenger_v2_import_legacy_message_flag_inserts();

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_reaction()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    canonical_uuid uuid;
    target_placement_uuid uuid;
    source_event_uuid uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT uuid, message_uuid INTO target_placement_uuid, canonical_uuid
        FROM messenger_message_placements
        WHERE project_id = OLD.project_id
          AND (uuid = OLD.message_uuid OR legacy_public_uuid = OLD.message_uuid)
        ORDER BY (uuid = OLD.message_uuid) DESC, uuid
        LIMIT 1;
        DELETE FROM messenger_message_reaction_facts
        WHERE project_id = OLD.project_id AND uuid = OLD.uuid;
        IF target_placement_uuid IS NOT NULL THEN
            source_event_uuid := gen_random_uuid();
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                source_event_uuid, OLD.project_id, 'reaction_snapshot',
                'message', OLD.project_id::text || ':' || canonical_uuid::text,
                jsonb_build_object(
                    'source_kind', 'legacy_reaction.changed',
                    'placement_uuid', target_placement_uuid,
                    'emit_reaction_event', false,
                    'emit_message_updated', false
                )
            );
        END IF;
        RETURN OLD;
    END IF;
    PERFORM messenger_v2_register_project_user(NEW.project_id, NEW.user_uuid);
    SELECT uuid, message_uuid INTO target_placement_uuid, canonical_uuid
    FROM messenger_message_placements
    WHERE project_id = NEW.project_id
      AND (uuid = NEW.message_uuid OR legacy_public_uuid = NEW.message_uuid)
    ORDER BY (uuid = NEW.message_uuid) DESC, uuid
    LIMIT 1;
    IF canonical_uuid IS NULL THEN
        RETURN NEW;
    END IF;
    INSERT INTO messenger_message_reaction_facts (
        uuid, project_id, canonical_message_uuid, placement_uuid,
        user_uuid, emoji_name, created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, canonical_uuid, target_placement_uuid,
        NEW.user_uuid, NEW.emoji_name, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        canonical_message_uuid = EXCLUDED.canonical_message_uuid,
        placement_uuid = EXCLUDED.placement_uuid,
        user_uuid = EXCLUDED.user_uuid,
        emoji_name = EXCLUDED.emoji_name,
        updated_at = EXCLUDED.updated_at;
    source_event_uuid := gen_random_uuid();
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    ) VALUES (
        source_event_uuid, NEW.project_id, 'reaction_snapshot',
        'message', NEW.project_id::text || ':' || canonical_uuid::text,
        jsonb_build_object(
            'source_kind', 'legacy_reaction.changed',
            'placement_uuid', target_placement_uuid,
            'emit_reaction_event', false,
            'emit_message_updated', false
        )
    );
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_v2_import_legacy_reaction
AFTER INSERT OR UPDATE OR DELETE ON m_workspace_message_reactions
FOR EACH ROW EXECUTE FUNCTION messenger_v2_import_legacy_reaction();
"""


DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS messenger_v2_guard_rolling_broadcast_event
    ON m_workspace_broadcast_message_events_v1;
DROP FUNCTION IF EXISTS messenger_v2_guard_rolling_broadcast_event();
DROP TRIGGER IF EXISTS messenger_v2_guard_rolling_direct_event
    ON m_workspace_events;
DROP FUNCTION IF EXISTS messenger_v2_guard_rolling_direct_event();
DROP TRIGGER IF EXISTS messenger_v2_rewrite_recipient_event_payload
    ON m_workspace_event_recipient_payloads_v1;
DROP FUNCTION IF EXISTS messenger_v2_rewrite_rolling_recipient_payload();
DROP TRIGGER IF EXISTS messenger_v2_rewrite_broadcast_event_payload
    ON m_workspace_broadcast_message_events_v1;
DROP TRIGGER IF EXISTS messenger_v2_rewrite_direct_event_payload
    ON m_workspace_events;
DROP FUNCTION IF EXISTS messenger_v2_rewrite_rolling_event_payload();

UPDATE m_workspace_events
SET payload = messenger_v2_rewrite_event_payload(
    project_id, object_type, payload, true, false
);
UPDATE m_workspace_broadcast_message_events_v1
SET payload = messenger_v2_rewrite_event_payload(
    project_id, object_type, payload, true, false
);
UPDATE m_workspace_event_recipient_payloads_v1 AS recipient
SET payload = messenger_v2_rewrite_event_payload(
    event.project_id, event.object_type, recipient.payload, true, false
)
FROM m_workspace_broadcast_message_events_v1 AS event
WHERE event.uuid = recipient.event_uuid;

UPDATE m_workspace_event_cursors AS cursor
SET epoch_generation = gen_random_uuid(),
    pruned_through_epoch_version = GREATEST(
        cursor.pruned_through_epoch_version,
        cursor.current_epoch_version
    ),
    updated_at = NOW()
WHERE EXISTS (
    SELECT 1 FROM messenger_event_membership_guards AS guard
    WHERE guard.project_id = cursor.project_id
      AND guard.user_uuid = cursor.user_uuid
);

DELETE FROM m_workspace_events AS event
USING messenger_event_membership_guards AS guard
WHERE guard.event_uuid = event.uuid
  AND guard.user_uuid = event.user_uuid
  AND NOT guard.control_effect
  AND NOT EXISTS (
      SELECT 1 FROM messenger_stream_bindings AS binding
      WHERE binding.project_id = guard.project_id
        AND binding.stream_uuid = guard.stream_uuid
        AND binding.user_uuid = guard.user_uuid
        AND binding.active
        AND binding.membership_generation = guard.membership_generation
  );

DELETE FROM m_workspace_event_recipient_payloads_v1 AS recipient
USING m_workspace_broadcast_message_events_v1 AS event,
      messenger_event_membership_guards AS guard
WHERE recipient.event_uuid = event.uuid
  AND guard.event_uuid = event.uuid
  AND guard.user_uuid = recipient.user_uuid
  AND NOT guard.control_effect
  AND NOT EXISTS (
      SELECT 1 FROM messenger_stream_bindings AS binding
      WHERE binding.project_id = guard.project_id
        AND binding.stream_uuid = guard.stream_uuid
        AND binding.user_uuid = guard.user_uuid
        AND binding.active
        AND binding.membership_generation = guard.membership_generation
  );

DELETE FROM m_workspace_event_audience_members_v1 AS member
USING m_workspace_broadcast_message_events_v1 AS event,
      messenger_event_membership_guards AS guard
WHERE member.audience_snapshot_uuid = event.audience_snapshot_uuid
  AND guard.event_uuid = event.uuid
  AND guard.user_uuid = member.user_uuid
  AND NOT guard.control_effect
  AND NOT EXISTS (
      SELECT 1 FROM messenger_stream_bindings AS binding
      WHERE binding.project_id = guard.project_id
        AND binding.stream_uuid = guard.stream_uuid
        AND binding.user_uuid = guard.user_uuid
        AND binding.active
        AND binding.membership_generation = guard.membership_generation
  );
DROP FUNCTION IF EXISTS messenger_v2_rewrite_event_payload(
    uuid, text, jsonb, boolean, boolean
);
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_reaction
    ON m_workspace_message_reactions;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_reaction();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_message_flag_inserts
    ON m_workspace_user_message_flags;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_message_flag_inserts();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_message_flags
    ON m_workspace_user_message_flags;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_message_flags();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_message_inserts
    ON m_workspace_messages;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_message_inserts();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_message
    ON m_workspace_messages;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_message();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_topic_flags
    ON m_workspace_user_topic_flags;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_topic_flags();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_topic
    ON m_workspace_stream_topics;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_topic();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_binding
    ON m_workspace_stream_bindings;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_binding();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_stream
    ON m_workspace_streams;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_stream();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_folder_item
    ON m_folder_items;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_folder_item();
DROP TRIGGER IF EXISTS messenger_v2_import_legacy_folder
    ON m_folders;
DROP FUNCTION IF EXISTS messenger_v2_import_legacy_folder();
DROP FUNCTION IF EXISTS messenger_v2_move_canonical_message_project(
    uuid, uuid, uuid, uuid, uuid
);
DROP FUNCTION IF EXISTS messenger_v2_move_canonical_stream_project(
    uuid, uuid, uuid
);
DROP FUNCTION IF EXISTS messenger_v2_register_project_user(uuid, uuid);
DROP TRIGGER IF EXISTS messenger_v2_mirror_reaction_to_legacy
    ON messenger_message_reaction_facts;
DROP FUNCTION IF EXISTS messenger_v2_mirror_reaction_to_legacy();
DROP TRIGGER IF EXISTS messenger_v2_mirror_state_to_legacy
    ON messenger_user_message_states;
DROP FUNCTION IF EXISTS messenger_v2_mirror_state_to_legacy();
DROP TRIGGER IF EXISTS messenger_v2_mirror_message_update_to_legacy
    ON messenger_messages;
DROP FUNCTION IF EXISTS messenger_v2_mirror_message_update_to_legacy();
DROP TRIGGER IF EXISTS messenger_v2_mirror_placement_to_legacy
    ON messenger_message_placements;
DROP FUNCTION IF EXISTS messenger_v2_mirror_placement_to_legacy();
DROP TRIGGER IF EXISTS messenger_v2_mirror_topic_binding_to_legacy
    ON messenger_user_topic_bindings;
DROP FUNCTION IF EXISTS messenger_v2_mirror_topic_binding_to_legacy();
DROP TRIGGER IF EXISTS messenger_v2_mirror_topic_to_legacy
    ON messenger_topics;
DROP FUNCTION IF EXISTS messenger_v2_mirror_topic_to_legacy();
DROP TRIGGER IF EXISTS messenger_v2_mirror_binding_to_legacy
    ON messenger_stream_bindings;
DROP FUNCTION IF EXISTS messenger_v2_mirror_binding_to_legacy();
DROP TRIGGER IF EXISTS messenger_v2_mirror_stream_to_legacy
    ON messenger_streams;
DROP FUNCTION IF EXISTS messenger_v2_mirror_stream_to_legacy();
DROP TRIGGER IF EXISTS messenger_v2_mirror_folder_item_to_legacy
    ON messenger_folder_items;
DROP FUNCTION IF EXISTS messenger_v2_mirror_folder_item_to_legacy();
DROP TRIGGER IF EXISTS messenger_v2_mirror_folder_binding_to_legacy
    ON messenger_user_folder_bindings;
DROP FUNCTION IF EXISTS messenger_v2_mirror_folder_binding_to_legacy();
DROP VIEW IF EXISTS messenger_api_message_reactions_v1;
DROP VIEW IF EXISTS messenger_api_user_topics_v1;
DROP VIEW IF EXISTS messenger_api_stream_bindings_v1;
DROP VIEW IF EXISTS messenger_api_user_streams_v1;
DROP VIEW IF EXISTS messenger_api_user_messages_v1;
DROP VIEW IF EXISTS messenger_api_user_folder_items_v1;
DROP VIEW IF EXISTS messenger_api_user_folders_v1;
DROP VIEW IF EXISTS m_workspace_visible_events;
ALTER VIEW m_workspace_visible_events_pre_messenger_v2
    RENAME TO m_workspace_visible_events;
DROP TRIGGER IF EXISTS messenger_v2_delete_broadcast_event_guards
    ON m_workspace_broadcast_message_events_v1;
DROP TRIGGER IF EXISTS messenger_v2_delete_direct_event_guards
    ON m_workspace_events;
DROP FUNCTION IF EXISTS messenger_v2_delete_event_guards();
ALTER TABLE messenger_streams
    DROP CONSTRAINT IF EXISTS messenger_streams_default_topic_fk;
ALTER TABLE messenger_topics
    DROP CONSTRAINT IF EXISTS messenger_topics_summary_last_message_fk;
ALTER TABLE messenger_stream_bindings
    DROP CONSTRAINT IF EXISTS messenger_stream_bindings_last_message_fk;
ALTER TABLE messenger_user_topic_bindings
    DROP CONSTRAINT IF EXISTS messenger_user_topic_bindings_last_message_fk;
DROP TABLE IF EXISTS messenger_fanout_batch_tasks;
DROP TABLE IF EXISTS messenger_fanout_roots;
DROP TABLE IF EXISTS messenger_event_membership_guards;
DROP TABLE IF EXISTS messenger_projection_scope_leases;
DROP TABLE IF EXISTS messenger_projection_tasks;
DROP TABLE IF EXISTS messenger_domain_outbox_events;
DROP TABLE IF EXISTS messenger_message_reaction_facts;
DROP TABLE IF EXISTS messenger_user_message_states;
DROP TABLE IF EXISTS messenger_user_message_bindings;
DROP TABLE IF EXISTS messenger_message_placements;
DROP TABLE IF EXISTS messenger_messages;
DROP SEQUENCE IF EXISTS messenger_messages_ingest_sequence_seq;
DROP INDEX IF EXISTS messenger_provider_chat_project_scope_idx;
DROP TABLE IF EXISTS messenger_user_topic_bindings;
DROP TABLE IF EXISTS messenger_topics;
DROP TABLE IF EXISTS messenger_folder_items;
DROP TABLE IF EXISTS messenger_user_folder_bindings;
DROP TABLE IF EXISTS messenger_folders;
DROP TABLE IF EXISTS messenger_stream_bindings;
DROP TABLE IF EXISTS messenger_streams;
DROP TABLE IF EXISTS messenger_project_users;
DROP TABLE IF EXISTS messenger_provider_file_cleanup_tasks;
UPDATE m_external_bridge_desired_resources_v1
SET resource = resource - 'projection_reset_generation'
WHERE provider_kind = 'zulip'
  AND resource_type = 'external_account'
  AND operation = 'upsert';
ALTER TABLE m_external_accounts_v2
    DROP CONSTRAINT IF EXISTS
        m_external_accounts_v2_projection_reset_generation_check;
ALTER TABLE m_external_accounts_v2
    DROP COLUMN IF EXISTS projection_reset_generation;
DROP FUNCTION IF EXISTS messenger_v2_provider_message_uuid(jsonb, text, uuid);
DROP FUNCTION IF EXISTS messenger_uuid_v5(uuid, text);
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self) -> None:
        self._depends = ["0151-index-detached-compact-read-memberships-743353.py"]

    @property
    def migration_id(self) -> str:
        return "b59d875a-561f-4166-8198-331c23bc89fb"

    @property
    def is_manual(self) -> bool:
        return False

    def upgrade(self, session: typing.Any) -> None:
        row_limit = os.environ.get("WORKSPACE_MESSENGER_V2_CUTOVER_ROW_LIMIT")
        if row_limit is not None:
            session.execute(
                "SELECT set_config("
                "'workspace.messenger_v2_cutover_row_limit', %s, TRUE)",
                (row_limit,),
            )
        if os.environ.get("WORKSPACE_MESSENGER_V2_LARGE_CUTOVER_AUTHORIZED") == "on":
            session.execute(
                "SELECT set_config("
                "'workspace.messenger_v2_large_cutover_authorized', "
                "'on', TRUE)"
            )
        session.execute(UPGRADE_SQL)
        identity_linking.reconcile_legacy_provider_identity_links(session)

    def downgrade(self, session: typing.Any) -> None:
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
