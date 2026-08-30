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
-- This is the join head for the pre-0152 preparation branch and the normal
-- 0152 -> 0154 chain.  RestAlchemy applies dependencies in declaration order,
-- so a v1 database is prepared before the immutable released cutover runs.
-- Databases that already applied 0152 reach this migration with their existing
-- canonical rows and are repaired forward here.
SET LOCAL lock_timeout = '30s';
SET LOCAL statement_timeout = '45min';
LOCK TABLE
    messenger_messages,
    messenger_message_placements,
    m_workspace_messages,
    m_external_accounts_v2,
    m_external_operations_v2
IN SHARE ROW EXCLUSIVE MODE;
SET LOCAL lock_timeout = '0';

CREATE OR REPLACE FUNCTION messenger_v2_has_terminal_message_create(
    message_uuid uuid,
    owner_uuid uuid,
    account_uuid uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
STRICT
PARALLEL SAFE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM m_external_operations_v2 AS operation
        WHERE operation.action = 'message.create'
          AND operation.target_type = 'message'
          AND operation.target_uuid = message_uuid
          AND operation.owner_user_uuid = owner_uuid
          AND operation.external_account_uuid = account_uuid
          AND operation.status = 'succeeded'
          AND (
                operation.details->'provider_result'->>'status' = 'succeeded'
                OR operation.reconciliation_state = 'committed_match'
              )
    );
$$;

-- The first post-0152 Bridge release used the legacy payload
-- shape: source.kind identified Zulip, while source.message_id was absent.
-- The provider id was still repeated in provider_metadata and accompanied by
-- the original provider URL.  Treat that exact, non-contradictory shape as a
-- verified provider projection so forward repair can attach the realm-global
-- identity (or detach an account-alias copy) without weakening the guard for
-- partially populated or contradictory rows.
CREATE OR REPLACE FUNCTION messenger_v2_is_verified_legacy_zulip_projection(
    legacy_source_name text,
    legacy_source jsonb,
    external_message_id text,
    legacy_provider_metadata jsonb,
    account_realm_uuid uuid
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
    SELECT legacy_source_name = 'zulip'
       AND legacy_source->>'kind' = 'zulip'
       AND external_message_id ~ '^(0|[1-9][0-9]*)$'
       AND char_length(external_message_id) <= 32
       AND legacy_source->>'message_id' IS NULL
       AND legacy_provider_metadata->>'external_id' = external_message_id
       AND legacy_provider_metadata->>'provider_original_url' IS NOT NULL
       AND (
            legacy_provider_metadata->>'provider_realm_uuid' IS NULL
            OR legacy_provider_metadata->>'provider_realm_uuid' =
               account_realm_uuid::text
       );
$$;

-- Historical account projections duplicated one physical Zulip message before
-- realm-global identity was enforced.  Preserve every
-- Workspace message and public UUID, but keep provider linkage on exactly one
-- deterministic winner.  Automatic detachment is allowed only when all rows
-- prove the same physical object.  Alias-only duplicates must agree on
-- realm/id, project, author, distinct accounts, and provider original URL.  A
-- timestamp or payload difference can represent import lag or a later edit,
-- so neither is used as physical identity evidence.  A
-- retained native row may also yield to an already keyed imported row when
-- both sides agree on realm/id, project, author, and provider original URL.
-- Every other collision remains a blocker.
CREATE TEMP TABLE messenger_v2_safe_provider_alias_losers
ON COMMIT DROP
AS
WITH candidates AS (
    SELECT message.project_id,
           message.uuid,
           COALESCE(message.legacy_public_uuid, message.uuid)
               AS legacy_public_uuid,
           message.author_uuid,
           message.external_account_uuid,
           message.provider_external_id,
           account.provider_realm_uuid,
           message.created_at,
           message.updated_at,
           legacy.provider_metadata,
           messenger_v2_has_terminal_message_create(
               COALESCE(message.legacy_public_uuid, message.uuid),
               message.author_uuid,
               message.external_account_uuid
           ) AS terminal_operation
    FROM messenger_messages AS message
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = message.external_account_uuid
     AND account.provider = 'zulip'
    JOIN m_workspace_messages AS legacy
      ON legacy.project_id = message.project_id
     AND legacy.uuid = COALESCE(message.legacy_public_uuid, message.uuid)
    WHERE account.provider_realm_uuid IS NOT NULL
      AND message.provider_realm_uuid IS NULL
      AND message.provider_external_id ~ '^(0|[1-9][0-9]*)$'
      AND char_length(message.provider_external_id) <= 32
      AND (
            (
                message.source_name = 'native'
                AND message.source->>'kind' = 'native'
            )
            OR messenger_v2_is_verified_legacy_zulip_projection(
                message.source_name,
                message.source,
                message.provider_external_id,
                legacy.provider_metadata,
                account.provider_realm_uuid
            )
          )
), safe_keys AS (
    SELECT provider_realm_uuid, provider_external_id
    FROM candidates
    GROUP BY provider_realm_uuid, provider_external_id
    HAVING count(*) > 1
       AND count(DISTINCT project_id) = 1
       AND count(DISTINCT author_uuid) = 1
       AND count(DISTINCT external_account_uuid) = count(*)
       AND min(provider_metadata->>'provider_original_url') IS NOT NULL
       AND count(provider_metadata->>'provider_original_url') = count(*)
       AND count(DISTINCT provider_metadata->>'provider_original_url') = 1
       AND count(provider_metadata->>'external_id') = count(*)
       AND bool_and(
            provider_metadata->>'external_id' = provider_external_id
       )
       AND bool_and(
            provider_metadata->>'provider_realm_uuid' IS NULL
            OR provider_metadata->>'provider_realm_uuid' =
               provider_realm_uuid::text
       )
), ranked AS (
    SELECT candidate.*,
           row_number() OVER (
               PARTITION BY candidate.provider_realm_uuid,
                            candidate.provider_external_id
               ORDER BY
                   candidate.terminal_operation DESC,
                   (
                       candidate.provider_metadata->>'lossy_conversion' =
                           'false'
                   ) DESC,
                   candidate.updated_at DESC,
                   candidate.uuid
           ) AS alias_rank
    FROM candidates AS candidate
    JOIN safe_keys USING (provider_realm_uuid, provider_external_id)
), safe_unkeyed_losers AS (
    SELECT project_id, uuid, legacy_public_uuid
    FROM ranked
    WHERE alias_rank > 1
), safe_existing_losers AS (
    SELECT candidate.project_id,
           candidate.uuid,
           candidate.legacy_public_uuid
    FROM candidates AS candidate
    JOIN messenger_messages AS existing
      ON existing.provider_realm_uuid = candidate.provider_realm_uuid
     AND existing.provider_message_id = candidate.provider_external_id
     AND (existing.project_id, existing.uuid) <>
         (candidate.project_id, candidate.uuid)
    JOIN m_workspace_messages AS existing_legacy
      ON existing_legacy.project_id = existing.project_id
     AND existing_legacy.uuid = COALESCE(
            existing.legacy_public_uuid,
            existing.uuid
         )
    WHERE existing.source_name = 'zulip'
      AND existing.source->>'kind' = 'zulip'
      AND existing.project_id = candidate.project_id
      AND existing.author_uuid = candidate.author_uuid
      AND candidate.provider_metadata->>'external_id' =
          candidate.provider_external_id
      AND existing_legacy.provider_metadata->>'external_id' =
          existing.provider_message_id
      AND candidate.provider_metadata->>'provider_original_url' IS NOT NULL
      AND candidate.provider_metadata->>'provider_original_url' =
          existing_legacy.provider_metadata->>'provider_original_url'
      AND (
            candidate.provider_metadata->>'provider_realm_uuid' IS NULL
            OR candidate.provider_metadata->>'provider_realm_uuid' =
               candidate.provider_realm_uuid::text
          )
      AND (
            existing_legacy.provider_metadata->>'provider_realm_uuid' IS NULL
            OR existing_legacy.provider_metadata->>'provider_realm_uuid' =
               candidate.provider_realm_uuid::text
          )
)
SELECT project_id, uuid, legacy_public_uuid
FROM safe_unkeyed_losers
UNION
SELECT project_id, uuid, legacy_public_uuid
FROM safe_existing_losers;

ALTER TABLE m_workspace_messages
    DISABLE TRIGGER messenger_v2_import_legacy_message;
ALTER TABLE messenger_messages
    DISABLE TRIGGER messenger_v2_mirror_message_update_to_legacy;

UPDATE m_workspace_messages AS legacy
SET external_account_uuid = NULL,
    provider_external_id = NULL,
    updated_at = legacy.updated_at
FROM messenger_v2_safe_provider_alias_losers AS loser
WHERE legacy.project_id = loser.project_id
  AND legacy.uuid = loser.legacy_public_uuid;

UPDATE messenger_messages AS message
SET external_account_uuid = NULL,
    provider_external_id = NULL,
    updated_at = message.updated_at
FROM messenger_v2_safe_provider_alias_losers AS loser
WHERE message.project_id = loser.project_id
  AND message.uuid = loser.uuid;

ALTER TABLE messenger_messages
    ENABLE TRIGGER messenger_v2_mirror_message_update_to_legacy;
ALTER TABLE m_workspace_messages
    ENABLE TRIGGER messenger_v2_import_legacy_message;

DO $retained_provider_identity_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM messenger_messages AS message
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = message.external_account_uuid
         AND account.provider = 'zulip'
        WHERE account.provider_realm_uuid IS NOT NULL
          AND message.provider_realm_uuid IS NULL
          AND (
                (
                    message.source_name = 'native'
                    AND message.source->>'kind' = 'native'
                    AND message.provider_external_id IS NOT NULL
                    AND (
                        message.provider_external_id !~
                            '^(0|[1-9][0-9]*)$'
                        OR char_length(message.provider_external_id) > 32
                    )
                )
                OR (
                    message.source_name = 'zulip'
                    AND message.source->>'kind' = 'zulip'
                    AND NOT (
                        messenger_v2_is_verified_legacy_zulip_projection(
                            message.source_name,
                            message.source,
                            message.provider_external_id,
                            message.provider,
                            account.provider_realm_uuid
                        )
                        OR (
                            message.provider_external_id ~
                                '^(0|[1-9][0-9]*)$'
                            AND char_length(message.provider_external_id) <= 32
                            AND message.source->>'message_id' =
                                message.provider_external_id
                            AND messenger_v2_has_terminal_message_create(
                                COALESCE(
                                    message.legacy_public_uuid,
                                    message.uuid
                                ),
                                message.author_uuid,
                                message.external_account_uuid
                            )
                        )
                    )
                )
              )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'messenger v2 provider identity repair blocked: ambiguous retained provider message provenance';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM messenger_messages AS message
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = message.external_account_uuid
         AND account.provider = 'zulip'
        JOIN m_workspace_messages AS legacy
          ON legacy.project_id = message.project_id
         AND legacy.uuid = COALESCE(
                message.legacy_public_uuid,
                message.uuid
             )
        WHERE account.provider_realm_uuid IS NOT NULL
          AND message.provider_external_id ~ '^(0|[1-9][0-9]*)$'
          AND char_length(message.provider_external_id) <= 32
          AND message.provider_realm_uuid IS NULL
          AND legacy.provider_metadata->>'external_id' IS NOT NULL
          AND legacy.provider_metadata->>'external_id' <>
              message.provider_external_id
          AND (
                (
                    message.source_name = 'native'
                    AND message.source->>'kind' = 'native'
                )
                OR (
                    message.source_name = 'zulip'
                    AND message.source->>'kind' = 'zulip'
                    AND message.source->>'message_id' =
                        message.provider_external_id
                    AND messenger_v2_has_terminal_message_create(
                        COALESCE(message.legacy_public_uuid, message.uuid),
                        message.author_uuid,
                        message.external_account_uuid
                    )
                )
              )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'messenger v2 provider identity repair blocked: legacy metadata external id conflicts with provider identity';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM messenger_messages AS message
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = message.external_account_uuid
         AND account.provider = 'zulip'
        JOIN m_workspace_messages AS legacy
          ON legacy.project_id = message.project_id
         AND legacy.uuid = COALESCE(
                message.legacy_public_uuid,
                message.uuid
             )
        WHERE account.provider_realm_uuid IS NOT NULL
          AND message.provider_external_id ~ '^(0|[1-9][0-9]*)$'
          AND char_length(message.provider_external_id) <= 32
          AND message.provider_realm_uuid IS NULL
          AND legacy.provider_metadata->>'provider_realm_uuid' IS NOT NULL
          AND legacy.provider_metadata->>'provider_realm_uuid' <>
              account.provider_realm_uuid::text
          AND (
                (
                    message.source_name = 'native'
                    AND message.source->>'kind' = 'native'
                )
                OR (
                    message.source_name = 'zulip'
                    AND message.source->>'kind' = 'zulip'
                    AND message.source->>'message_id' =
                        message.provider_external_id
                    AND messenger_v2_has_terminal_message_create(
                        COALESCE(message.legacy_public_uuid, message.uuid),
                        message.author_uuid,
                        message.external_account_uuid
                    )
                )
              )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'messenger v2 provider identity repair blocked: legacy metadata realm conflicts with account realm';
    END IF;

    IF EXISTS (
        WITH candidates AS (
            SELECT message.project_id,
                   message.uuid,
                   account.provider_realm_uuid,
                   message.provider_external_id AS provider_message_id
            FROM messenger_messages AS message
            JOIN m_external_accounts_v2 AS account
              ON account.uuid = message.external_account_uuid
             AND account.provider = 'zulip'
            WHERE account.provider_realm_uuid IS NOT NULL
              AND message.provider_external_id ~ '^(0|[1-9][0-9]*)$'
              AND char_length(message.provider_external_id) <= 32
              AND message.provider_realm_uuid IS NULL
              AND (
                    (
                        message.source_name = 'native'
                        AND message.source->>'kind' = 'native'
                    )
                    OR (
                        message.source_name = 'zulip'
                        AND message.source->>'kind' = 'zulip'
                        AND (
                            messenger_v2_is_verified_legacy_zulip_projection(
                                message.source_name,
                                message.source,
                                message.provider_external_id,
                                message.provider,
                                account.provider_realm_uuid
                            )
                            OR (
                                message.source->>'message_id' =
                                    message.provider_external_id
                                AND messenger_v2_has_terminal_message_create(
                                    COALESCE(
                                        message.legacy_public_uuid,
                                        message.uuid
                                    ),
                                    message.author_uuid,
                                    message.external_account_uuid
                                )
                            )
                        )
                    )
                  )
        )
        SELECT 1
        FROM candidates
        GROUP BY provider_realm_uuid, provider_message_id
        HAVING count(DISTINCT (project_id, uuid)) > 1
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23505',
            MESSAGE = 'messenger v2 provider identity repair blocked: multiple retained messages claim one realm message';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM messenger_messages AS candidate
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = candidate.external_account_uuid
         AND account.provider = 'zulip'
        JOIN messenger_messages AS existing
          ON existing.provider_realm_uuid = account.provider_realm_uuid
         AND existing.provider_message_id =
             candidate.provider_external_id
         AND (existing.project_id, existing.uuid) <>
             (candidate.project_id, candidate.uuid)
        WHERE account.provider_realm_uuid IS NOT NULL
          AND candidate.provider_external_id ~ '^(0|[1-9][0-9]*)$'
          AND char_length(candidate.provider_external_id) <= 32
          AND candidate.provider_realm_uuid IS NULL
          AND (
                (
                    candidate.source_name = 'native'
                    AND candidate.source->>'kind' = 'native'
                )
                OR (
                    candidate.source_name = 'zulip'
                    AND candidate.source->>'kind' = 'zulip'
                    AND (
                        messenger_v2_is_verified_legacy_zulip_projection(
                            candidate.source_name,
                            candidate.source,
                            candidate.provider_external_id,
                            candidate.provider,
                            account.provider_realm_uuid
                        )
                        OR (
                            candidate.source->>'message_id' =
                                candidate.provider_external_id
                            AND messenger_v2_has_terminal_message_create(
                                COALESCE(
                                    candidate.legacy_public_uuid,
                                    candidate.uuid
                                ),
                                candidate.author_uuid,
                                candidate.external_account_uuid
                            )
                        )
                    )
                )
              )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23505',
            MESSAGE = 'messenger v2 provider identity repair blocked: retained and imported messages claim one realm message';
    END IF;
END
$retained_provider_identity_guard$;

UPDATE messenger_messages AS message
SET provider_realm_uuid = account.provider_realm_uuid,
    provider_message_id = message.provider_external_id,
    updated_at = message.updated_at
FROM m_external_accounts_v2 AS account
WHERE account.uuid = message.external_account_uuid
  AND account.provider = 'zulip'
  AND account.provider_realm_uuid IS NOT NULL
  AND message.provider_external_id ~ '^(0|[1-9][0-9]*)$'
  AND char_length(message.provider_external_id) <= 32
  AND message.provider_realm_uuid IS NULL
  AND (
        (
            message.source_name = 'native'
            AND message.source->>'kind' = 'native'
        )
        OR (
            message.source_name = 'zulip'
            AND message.source->>'kind' = 'zulip'
            AND (
                messenger_v2_is_verified_legacy_zulip_projection(
                    message.source_name,
                    message.source,
                    message.provider_external_id,
                    message.provider,
                    account.provider_realm_uuid
                )
                OR (
                    message.source->>'message_id' =
                        message.provider_external_id
                    AND messenger_v2_has_terminal_message_create(
                        COALESCE(message.legacy_public_uuid, message.uuid),
                        message.author_uuid,
                        message.external_account_uuid
                    )
                )
            )
        )
      );

-- These rows existed only to adapt the immutable 0152 guard.  They were
-- discarded from creation and never had provider queue or outbox records.
DELETE FROM m_external_operations_v2 AS operation
WHERE operation.action = 'message.create'
  AND operation.target_type = 'message'
  AND operation.status = 'discarded'
  AND operation.details->>'migration_provenance' =
      'pre_operation_native_echo';

CREATE OR REPLACE FUNCTION messenger_v2_apply_legacy_provider_identity(
    legacy_project_uuid uuid,
    legacy_message_uuid uuid,
    account_uuid uuid,
    external_message_id text,
    legacy_source_name text,
    legacy_source jsonb,
    legacy_provider_metadata jsonb
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    realm_uuid uuid;
    canonical_uuid uuid;
    current_realm_uuid uuid;
    current_message_id text;
BEGIN
    IF external_message_id IS NULL THEN
        RETURN;
    END IF;

    SELECT account.provider_realm_uuid
      INTO realm_uuid
    FROM m_external_accounts_v2 AS account
    WHERE account.uuid = account_uuid
      AND account.provider = 'zulip';
    IF realm_uuid IS NULL THEN
        RETURN;
    END IF;
    IF legacy_source_name = 'native'
       AND legacy_source->>'kind' = 'native'
    THEN
        IF external_message_id !~ '^(0|[1-9][0-9]*)$'
           OR char_length(external_message_id) > 32
        THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'legacy native message has an invalid provider message identifier';
        END IF;
    ELSIF legacy_source_name = 'zulip'
          AND legacy_source->>'kind' = 'zulip'
    THEN
        IF external_message_id !~ '^(0|[1-9][0-9]*)$'
           OR char_length(external_message_id) > 32
           OR (
                legacy_source->>'message_id' IS NOT NULL
                AND legacy_source->>'message_id' <> external_message_id
              )
           OR (
                legacy_source->>'message_id' IS NULL
                AND (
                    legacy_provider_metadata->>'external_id' IS DISTINCT FROM
                        external_message_id
                    OR legacy_provider_metadata->>'provider_original_url'
                        IS NULL
                )
              )
        THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'legacy Zulip message has an invalid provider message identity';
        END IF;
    ELSE
        RETURN;
    END IF;
    IF legacy_provider_metadata->>'external_id' IS NOT NULL
       AND legacy_provider_metadata->>'external_id' <> external_message_id
    THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'legacy message metadata external id conflicts with provider identity';
    END IF;
    IF legacy_provider_metadata->>'provider_realm_uuid' IS NOT NULL
       AND legacy_provider_metadata->>'provider_realm_uuid' <>
           realm_uuid::text
    THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'legacy message metadata realm conflicts with account realm';
    END IF;

    SELECT placement.message_uuid
      INTO canonical_uuid
    FROM messenger_message_placements AS placement
    WHERE placement.project_id = legacy_project_uuid
      AND (
            placement.legacy_public_uuid = legacy_message_uuid
            OR placement.uuid = legacy_message_uuid
            OR placement.message_uuid = legacy_message_uuid
          )
    ORDER BY (placement.legacy_public_uuid = legacy_message_uuid) DESC,
             (placement.message_uuid = legacy_message_uuid) DESC,
             placement.uuid
    LIMIT 1;
    IF canonical_uuid IS NULL THEN
        RETURN;
    END IF;

    SELECT message.provider_realm_uuid, message.provider_message_id
      INTO current_realm_uuid, current_message_id
    FROM messenger_messages AS message
    WHERE message.project_id = legacy_project_uuid
      AND message.uuid = canonical_uuid;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF current_realm_uuid IS NULL THEN
        UPDATE messenger_messages
        SET provider_realm_uuid = realm_uuid,
            provider_message_id = external_message_id
        WHERE project_id = legacy_project_uuid
          AND uuid = canonical_uuid;
    ELSIF current_realm_uuid IS DISTINCT FROM realm_uuid
       OR current_message_id IS DISTINCT FROM external_message_id
    THEN
        RAISE EXCEPTION USING
            ERRCODE = '23505',
            MESSAGE = 'legacy message provider identity conflicts with canonical identity';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION
messenger_v2_repair_legacy_message_provider_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN NEW;
    END IF;
    PERFORM messenger_v2_apply_legacy_provider_identity(
        NEW.project_id,
        NEW.uuid,
        NEW.external_account_uuid,
        NEW.provider_external_id,
        NEW.source_name,
        NEW.source,
        NEW.provider_metadata
    );
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION
messenger_v2_repair_legacy_message_provider_identity_inserts()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    message record;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;
    FOR message IN
        SELECT project_id, uuid, external_account_uuid,
               provider_external_id, source_name, source,
               provider_metadata
        FROM inserted_legacy_provider_messages
    LOOP
        PERFORM messenger_v2_apply_legacy_provider_identity(
            message.project_id,
            message.uuid,
            message.external_account_uuid,
            message.provider_external_id,
            message.source_name,
            message.source,
            message.provider_metadata
        );
    END LOOP;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS
    messenger_v2_repair_legacy_message_provider_identity
    ON m_workspace_messages;
CREATE TRIGGER messenger_v2_repair_legacy_message_provider_identity
AFTER UPDATE OF external_account_uuid, provider_external_id,
    source_name, source, provider_metadata
ON m_workspace_messages
FOR EACH ROW
EXECUTE FUNCTION messenger_v2_repair_legacy_message_provider_identity();

DROP TRIGGER IF EXISTS
    messenger_v2_repair_legacy_message_provider_identity_inserts
    ON m_workspace_messages;
CREATE TRIGGER messenger_v2_repair_legacy_message_provider_identity_inserts
AFTER INSERT ON m_workspace_messages
REFERENCING NEW TABLE AS inserted_legacy_provider_messages
FOR EACH STATEMENT
EXECUTE FUNCTION
    messenger_v2_repair_legacy_message_provider_identity_inserts();

DROP INDEX IF EXISTS messenger_v2_prepare_message_payload_trgm_idx;
DROP INDEX IF EXISTS messenger_v2_prepare_message_create_target_idx;
DROP FUNCTION IF EXISTS
    messenger_v2_has_terminal_message_create(uuid, uuid, uuid);
DROP FUNCTION IF EXISTS
    messenger_v2_is_verified_legacy_zulip_projection(
        text, jsonb, text, jsonb, uuid
    );
DROP FUNCTION IF EXISTS
    messenger_v2_prepare_legacy_zulip_message_uuid(uuid, text);
DROP FUNCTION IF EXISTS messenger_v2_prepare_uuid_v5(uuid, text);
"""


DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS
    messenger_v2_repair_legacy_message_provider_identity_inserts
    ON m_workspace_messages;
DROP TRIGGER IF EXISTS
    messenger_v2_repair_legacy_message_provider_identity
    ON m_workspace_messages;
DROP FUNCTION IF EXISTS
    messenger_v2_repair_legacy_message_provider_identity_inserts();
DROP FUNCTION IF EXISTS
    messenger_v2_repair_legacy_message_provider_identity();
DROP FUNCTION IF EXISTS
    messenger_v2_apply_legacy_provider_identity(
        uuid, uuid, uuid, text, text, jsonb, jsonb
    );

-- 0155 is a sibling preparation branch, so RestAlchemy does not reach it when
-- a rollback starts at the published 0152 chain.  Make the preparation branch
-- eligible again before a later HEAD apply reruns the immutable cutover.
UPDATE ra_migrations
SET applied = FALSE
WHERE uuid = '8870659b-eeb7-4e1c-9f3a-d84ff25dea96';
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self) -> None:
        self._depends = [
            "0155-prepare-immutable-messenger-v2-cutover-887065.py",
            "0154-retry-shared-provider-projections-603fd0.py",
        ]

    @property
    def migration_id(self) -> str:
        return "2022d56e-484d-4047-8e65-f37c65da229d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session) -> None:
        session.execute(UPGRADE_SQL)

    def downgrade(self, session) -> None:
        # Realm-global identities are monotonic deduplication keys and remain
        # populated if only the compatibility triggers are rolled back.
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
