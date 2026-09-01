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
-- Migration 0152 shipped in 1.0.0 and is immutable.  This branch starts at
-- 0151 and is listed first by the join migration, so RestAlchemy prepares the
-- observed legacy provenance before an unmodified 0152 is allowed to run.
-- Installations that already recorded 0152 skip this pre-cutover work and are
-- repaired forward by the join migration instead.
-- The immutable cutover creates pgcrypto, but this branch must calculate the
-- same UUIDv5 identities before 0152 runs.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION messenger_v2_prepare_uuid_v5(
    namespace_uuid uuid,
    name text
)
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

CREATE OR REPLACE FUNCTION messenger_v2_prepare_legacy_zulip_message_uuid(
    account_uuid uuid,
    provider_message_id text
)
RETURNS uuid
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
    SELECT messenger_v2_prepare_uuid_v5(
        '9a1d0e75-50a5-413c-b3e8-d070232ef57f'::uuid,
        'zulip:' || account_uuid::text || ':message:' || provider_message_id
    );
$$;

DO $messenger_v2_prepare_legacy_provenance$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM ra_migrations
        WHERE uuid = 'b59d875a-561f-4166-8198-331c23bc89fb'
          AND applied
    ) THEN
        RETURN;
    END IF;

    PERFORM set_config('lock_timeout', '30s', true);
    PERFORM set_config('statement_timeout', '45min', true);
    EXECUTE $lock$
        LOCK TABLE
            m_workspace_users,
            m_workspace_streams,
            m_workspace_stream_bindings,
            m_workspace_stream_topics,
            m_workspace_user_topic_flags,
            m_folders,
            m_folder_items,
            m_workspace_messages,
            m_workspace_user_message_flags,
            m_workspace_message_reactions,
            m_workspace_files,
            m_workspace_file_accesses,
            m_workspace_message_mentions_v1,
            m_workspace_drafts,
            m_workspace_read_memberships_v1,
            m_workspace_user_topic_read_stats_v1,
            m_workspace_topic_message_stats_v1,
            m_workspace_topic_summary_jobs,
            m_workspace_topic_summary_journal,
            m_workspace_events,
            m_workspace_broadcast_message_events_v1,
            m_workspace_event_audience_members_v1,
            m_workspace_event_recipient_payloads_v1,
            m_external_accounts_v2,
            m_external_chats_v2,
            m_external_provider_identity_links_v1,
            m_external_bridge_desired_resources_v1,
            m_external_bridge_desired_changes_v1,
            m_external_provider_events_v1,
            m_external_operations_v2,
            m_external_provider_operations_v1,
            m_external_provider_read_snapshots_v1,
            m_external_projection_transitions_v1
        IN SHARE ROW EXCLUSIVE MODE
    $lock$;
    PERFORM set_config('lock_timeout', '0', true);

    -- The published guard probes message.create by the complete target tuple.
    -- Keep that lookup bounded on large legacy databases, then remove the
    -- migration-only index at the join head.
    EXECUTE $index$
        CREATE INDEX IF NOT EXISTS
            messenger_v2_prepare_message_create_target_idx
        ON m_external_operations_v2 (
            target_uuid, owner_user_uuid, external_account_uuid
        )
        WHERE action = 'message.create' AND target_type = 'message'
    $index$;

    IF EXISTS (
        SELECT 1
        FROM m_workspace_messages AS message
        WHERE message.source_name = 'zulip'
          AND message.source->>'kind' = 'zulip'
          AND message.source->>'message_id' IS NOT NULL
          AND message.provider_external_id IS NOT NULL
          AND message.source->>'message_id' <>
              message.provider_external_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'messenger v2 preparation blocked: contradictory legacy Zulip message identifiers';
    END IF;

    -- A terminal local operation wins over an inbound-looking echo.  A queued,
    -- running, failed, discarded, or mismatched operation is not provenance.
    -- Require both the public terminal state and its provider result (or the
    -- explicit committed-match reconciliation state) before normalization.
    UPDATE m_workspace_messages AS message
    SET source_name = 'native',
        source = '{"kind":"native"}'::jsonb,
        updated_at = message.updated_at
    FROM m_external_accounts_v2 AS account
    WHERE account.uuid = message.external_account_uuid
      AND account.provider = 'zulip'
      AND message.source_name = 'zulip'
      AND message.source->>'kind' = 'zulip'
      AND (
            message.source->>'message_id' IS NULL
            OR message.source->>'message_id' = message.provider_external_id
          )
      AND message.provider_external_id ~ '^(0|[1-9][0-9]*)$'
      AND char_length(message.provider_external_id) <= 32
      AND (
            message.provider_metadata->>'provider_realm_uuid' IS NULL
            OR message.provider_metadata->>'provider_realm_uuid' =
               account.provider_realm_uuid::text
          )
      AND EXISTS (
            SELECT 1
            FROM m_external_operations_v2 AS operation
            WHERE operation.action = 'message.create'
              AND operation.target_type = 'message'
              AND operation.target_uuid = message.uuid
              AND operation.owner_user_uuid = message.user_uuid
              AND operation.external_account_uuid =
                  message.external_account_uuid
              AND operation.status = 'succeeded'
              AND (
                    operation.details->'provider_result'->>'status' =
                        'succeeded'
                    OR operation.reconciliation_state = 'committed_match'
                  )
          );

    -- Bridge <= 0.0.45 used this exact account-scoped UUID.  Only that full
    -- identity may repair a missing source message_id for an inbound row.
    UPDATE m_workspace_messages AS message
    SET source = jsonb_set(
            message.source,
            '{message_id}',
            to_jsonb(message.provider_external_id),
            true
        ),
        updated_at = message.updated_at
    FROM m_external_accounts_v2 AS account
    WHERE message.source_name = 'zulip'
      AND message.source->>'kind' = 'zulip'
      AND message.source->>'message_id' IS NULL
      AND message.external_account_uuid = account.uuid
      AND account.provider = 'zulip'
      AND message.provider_external_id ~ '^(0|[1-9][0-9]*)$'
      AND char_length(message.provider_external_id) <= 32
      AND message.uuid = messenger_v2_prepare_legacy_zulip_message_uuid(
            message.external_account_uuid,
            message.provider_external_id
          );

    -- Messages created before the durable operation queue have authoritative
    -- paired native provenance.  The immutable 0152 guard only understands an
    -- operation, so add a deterministic discarded marker.  It cannot enter a
    -- provider queue and the join migration deletes it before HEAD commits.
    IF EXISTS (
        SELECT 1
        FROM m_workspace_messages AS message
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = message.external_account_uuid
         AND account.provider = 'zulip'
        JOIN m_external_operations_v2 AS operation
          ON operation.uuid = messenger_v2_prepare_uuid_v5(
                message.uuid,
                'messenger-v2-pre-operation-native-provenance'
             )
        WHERE message.source_name = 'native'
          AND message.source->>'kind' = 'native'
          AND NOT (
                operation.external_account_uuid =
                    message.external_account_uuid
                AND operation.owner_user_uuid = message.user_uuid
                AND operation.action = 'message.create'
                AND operation.target_type = 'message'
                AND operation.target_uuid = message.uuid
                AND operation.details = jsonb_build_object(
                    'migration_provenance',
                    'pre_operation_native_echo',
                    'provider_external_id',
                    message.provider_external_id
                )
                AND operation.status = 'discarded'
                AND operation.attempt = 0
                AND operation.reconciliation_state = 'not_required'
              )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23505',
            MESSAGE = 'messenger v2 preparation blocked: transient provenance marker UUID collision';
    END IF;

    INSERT INTO m_external_operations_v2 (
        uuid, external_account_uuid, owner_user_uuid,
        action, target_type, target_uuid, details,
        status, attempt, reconciliation_state, reconciliation_evidence,
        created_at, updated_at
    )
    SELECT messenger_v2_prepare_uuid_v5(
               message.uuid,
               'messenger-v2-pre-operation-native-provenance'
           ),
           message.external_account_uuid,
           message.user_uuid,
           'message.create',
           'message',
           message.uuid,
           jsonb_build_object(
               'migration_provenance', 'pre_operation_native_echo',
               'provider_external_id', message.provider_external_id
           ),
           'discarded',
           0,
           'not_required',
           '{}'::jsonb,
           message.created_at,
           message.updated_at
    FROM m_workspace_messages AS message
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = message.external_account_uuid
     AND account.provider = 'zulip'
    WHERE message.source_name = 'native'
      AND message.source->>'kind' = 'native'
      AND NOT EXISTS (
            SELECT 1
            FROM m_external_operations_v2 AS operation
            WHERE operation.action = 'message.create'
              AND operation.target_type = 'message'
              AND operation.target_uuid = message.uuid
              AND operation.owner_user_uuid = message.user_uuid
              AND operation.external_account_uuid =
                  message.external_account_uuid
          )
    ON CONFLICT (uuid) DO NOTHING;

    -- The released file-retention predicate uses leading-wildcard LIKE with a
    -- file UUID supplied by the outer row.  A trigram expression index turns
    -- each lookup into a bounded index probe while keeping 0152 byte-for-byte
    -- identical to the published migration.
    EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_trgm';
    EXECUTE $index$
        CREATE INDEX IF NOT EXISTS
            messenger_v2_prepare_message_payload_trgm_idx
        ON m_workspace_messages
        USING gin ((payload::text) gin_trgm_ops)
    $index$;
    -- pg_dump does not preserve planner statistics.  Rehearsals and disaster
    -- recovery therefore reach this cutover with production rows but empty
    -- statistics, which can turn the immutable set-based updates into nested
    -- scans that exceed their safety deadline.  Refresh every frozen input so
    -- the same head is bounded on both a live database and a restored backup.
    EXECUTE $analyze$
        ANALYZE
            m_workspace_users,
            m_workspace_streams,
            m_workspace_stream_bindings,
            m_workspace_stream_topics,
            m_workspace_user_topic_flags,
            m_folders,
            m_folder_items,
            m_workspace_messages,
            m_workspace_user_message_flags,
            m_workspace_message_reactions,
            m_workspace_files,
            m_workspace_file_accesses,
            m_workspace_message_mentions_v1,
            m_workspace_drafts,
            m_workspace_read_memberships_v1,
            m_workspace_user_topic_read_stats_v1,
            m_workspace_topic_message_stats_v1,
            m_workspace_topic_summary_jobs,
            m_workspace_topic_summary_journal,
            m_workspace_events,
            m_workspace_broadcast_message_events_v1,
            m_workspace_event_audience_members_v1,
            m_workspace_event_recipient_payloads_v1,
            m_external_accounts_v2,
            m_external_chats_v2,
            m_external_provider_identity_links_v1,
            m_external_bridge_desired_resources_v1,
            m_external_bridge_desired_changes_v1,
            m_external_provider_events_v1,
            m_external_operations_v2,
            m_external_provider_operations_v1,
            m_external_provider_read_snapshots_v1,
            m_external_projection_transitions_v1
    $analyze$;
END
$messenger_v2_prepare_legacy_provenance$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self) -> None:
        self._depends = ["0151-index-detached-compact-read-memberships-743353.py"]

    @property
    def migration_id(self) -> str:
        return "8870659b-eeb7-4e1c-9f3a-d84ff25dea96"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session) -> None:
        session.execute(UPGRADE_SQL)

    def downgrade(self, session) -> None:
        # Provenance normalization is monotonic.  Reverting it would make a
        # later 0152 retry ambiguous again.
        session.execute("SELECT 1")


migration_step = MigrationStep()
