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


MEMBER_ACCESS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_confirmed_external_account_access" AS
SELECT DISTINCT
    chat.project_id,
    account.owner_user_uuid AS user_uuid,
    account.provider::varchar(32) AS account_type,
    account.uuid::text::varchar(2048) AS source_scope
FROM "m_external_accounts_v2" AS account
JOIN "m_external_chats_v2" AS chat
  ON chat.external_account_uuid = account.uuid
WHERE chat.selected
  AND chat.project_id IS NOT NULL
  AND account.credential_present
  AND account.status NOT IN ('disconnected', 'suspended')
UNION
SELECT DISTINCT
    chat.project_id,
    binding.user_uuid,
    account.provider::varchar(32) AS account_type,
    account.uuid::text::varchar(2048) AS source_scope
FROM "m_external_accounts_v2" AS account
JOIN "m_external_chats_v2" AS chat
  ON chat.external_account_uuid = account.uuid
JOIN "m_workspace_stream_bindings" AS binding
  ON binding.project_id = chat.project_id
 AND binding.stream_uuid = chat.projection_stream_uuid
WHERE chat.selected
  AND chat.project_id IS NOT NULL
  AND chat.projection_stream_uuid IS NOT NULL
  AND account.credential_present
  AND account.status NOT IN ('disconnected', 'suspended');
"""


CANONICAL_MEMBER_ACCESS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_confirmed_external_account_access" AS
WITH candidate_rows AS (
    SELECT
        chat.project_id,
        account.owner_user_uuid AS user_uuid,
        account.provider::varchar(32) AS account_type,
        account.uuid::text::varchar(2048) AS source_scope,
        COALESCE(
            NULLIF(chat.source->>'provider_realm_uuid', ''),
            NULLIF(account.settings->>'server_url', ''),
            account.uuid::text
        )::varchar(2048) AS provider_realm_id,
        chat.provider_chat_id,
        1 AS owner_priority
    FROM "m_external_accounts_v2" AS account
    JOIN "m_external_chats_v2" AS chat
      ON chat.external_account_uuid = account.uuid
    WHERE chat.selected
      AND chat.project_id IS NOT NULL
      AND account.credential_present
      AND account.status NOT IN ('disconnected', 'suspended')
    UNION ALL
    SELECT
        chat.project_id,
        binding.user_uuid,
        account.provider::varchar(32) AS account_type,
        account.uuid::text::varchar(2048) AS source_scope,
        COALESCE(
            NULLIF(chat.source->>'provider_realm_uuid', ''),
            NULLIF(account.settings->>'server_url', ''),
            account.uuid::text
        )::varchar(2048) AS provider_realm_id,
        chat.provider_chat_id,
        CASE
            WHEN account.owner_user_uuid = binding.user_uuid THEN 1
            ELSE 0
        END AS owner_priority
    FROM "m_external_accounts_v2" AS account
    JOIN "m_external_chats_v2" AS chat
      ON chat.external_account_uuid = account.uuid
    JOIN "m_workspace_stream_bindings" AS binding
      ON binding.project_id = chat.project_id
     AND binding.stream_uuid = chat.projection_stream_uuid
    WHERE chat.selected
      AND chat.project_id IS NOT NULL
      AND chat.projection_stream_uuid IS NOT NULL
      AND account.credential_present
      AND account.status NOT IN ('disconnected', 'suspended')
),
deduplicated_candidates AS (
    SELECT
        candidate.project_id,
        candidate.user_uuid,
        candidate.account_type,
        candidate.source_scope,
        candidate.provider_realm_id,
        candidate.provider_chat_id,
        MAX(candidate.owner_priority) AS owner_priority
    FROM candidate_rows AS candidate
    WHERE NOT EXISTS (
        SELECT 1
        FROM "m_workspace_external_chat_membership_revocations" AS revocation
        WHERE revocation.project_id = candidate.project_id
          AND revocation.user_uuid = candidate.user_uuid
          AND revocation.provider = candidate.account_type
          AND revocation.provider_realm_id = candidate.provider_realm_id
          AND revocation.provider_chat_id = candidate.provider_chat_id
    )
    GROUP BY
        candidate.project_id,
        candidate.user_uuid,
        candidate.account_type,
        candidate.source_scope,
        candidate.provider_realm_id,
        candidate.provider_chat_id
),
ranked_candidates AS (
    SELECT
        candidate.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                candidate.project_id,
                candidate.user_uuid,
                candidate.account_type,
                candidate.provider_realm_id,
                candidate.provider_chat_id
            ORDER BY
                candidate.owner_priority DESC,
                candidate.source_scope
        ) AS projection_rank
    FROM deduplicated_candidates AS candidate
)
SELECT
    project_id,
    user_uuid,
    account_type,
    source_scope
FROM ranked_candidates
WHERE projection_rank = 1;
"""


BACKFILL_EXTERNAL_CHAT_MEMBERSHIP_REVOCATIONS_SQL = """
WITH deleted_memberships AS (
    SELECT DISTINCT
        event.project_id,
        event.user_uuid,
        chat.provider::varchar(32) AS provider,
        COALESCE(
            NULLIF(chat.source->>'provider_realm_uuid', ''),
            NULLIF(account.settings->>'server_url', ''),
            account.uuid::text
        )::varchar(2048) AS provider_realm_id,
        chat.provider_chat_id
    FROM "m_workspace_events" AS event
    JOIN "m_external_chats_v2" AS chat
      ON chat.project_id = event.project_id
     AND chat.projection_stream_uuid =
            (event.payload->>'uuid')::uuid
    JOIN "m_external_accounts_v2" AS account
      ON account.uuid = chat.external_account_uuid
    WHERE event.object_type = 'stream'
      AND event.action = 'deleted'
      AND COALESCE(event.payload->>'source_name', 'native') <> 'native'
)
INSERT INTO "m_workspace_external_chat_membership_revocations" (
    project_id, user_uuid, provider, provider_realm_id, provider_chat_id
)
SELECT
    deleted.project_id,
    deleted.user_uuid,
    deleted.provider,
    deleted.provider_realm_id,
    deleted.provider_chat_id
FROM deleted_memberships AS deleted
WHERE NOT EXISTS (
    SELECT 1
    FROM "m_external_chats_v2" AS current_chat
    JOIN "m_external_accounts_v2" AS current_account
      ON current_account.uuid = current_chat.external_account_uuid
    JOIN "m_workspace_stream_bindings" AS current_binding
      ON current_binding.project_id = current_chat.project_id
     AND current_binding.stream_uuid =
            current_chat.projection_stream_uuid
     AND current_binding.user_uuid = deleted.user_uuid
    WHERE current_chat.project_id = deleted.project_id
      AND current_chat.provider = deleted.provider
      AND COALESCE(
            NULLIF(current_chat.source->>'provider_realm_uuid', ''),
            NULLIF(current_account.settings->>'server_url', ''),
            current_account.uuid::text
          ) = deleted.provider_realm_id
      AND current_chat.provider_chat_id = deleted.provider_chat_id
)
ON CONFLICT (
    project_id, user_uuid, provider, provider_realm_id, provider_chat_id
) DO NOTHING;
"""


def _visible_events_view(external_stream_visibility):
    return f"""
        CREATE OR REPLACE VIEW "m_workspace_visible_events" AS
        WITH event_rows AS (
            SELECT
                e."epoch_version", e."uuid", e."project_id", e."user_uuid",
                e."payload", e."created_at", e."updated_at",
                e."schema_version", e."object_type", e."action"
            FROM "m_workspace_events" AS e
            UNION ALL
            SELECT
                b."epoch_version", b."uuid", b."project_id",
                recipient."user_uuid",
                b."payload" || COALESCE(override."payload", '{{}}'::jsonb)
                    || CASE
                        WHEN b."object_type" = 'user' THEN '{{}}'::jsonb
                        ELSE jsonb_build_object(
                            'user_uuid', recipient."user_uuid"
                        )
                    END AS "payload",
                b."created_at", b."updated_at", b."schema_version",
                b."object_type", b."action"
            FROM "m_workspace_broadcast_message_events_v1" AS b
            JOIN "m_workspace_event_audience_members_v1" AS recipient
              ON recipient."audience_snapshot_uuid"
               = b."audience_snapshot_uuid"
            LEFT JOIN "m_workspace_event_recipient_payloads_v1" AS override
              ON override."event_uuid" = b."uuid"
             AND override."user_uuid" = recipient."user_uuid"
        )
        SELECT e.*
        FROM event_rows AS e
        LEFT JOIN "m_confirmed_external_account_access" AS access
          ON access.project_id = e.project_id
         AND access.user_uuid = e.user_uuid
         AND access.account_type = e.payload->>'source_name'
         AND access.source_scope = COALESCE(
                e.payload->'source'->>'source_scope',
                e.payload->'source'->>'server_url'
             )
        LEFT JOIN "m_confirmed_external_account_access" AS old_access
          ON old_access.project_id = e.project_id
         AND old_access.user_uuid = e.user_uuid
         AND old_access.account_type = e.payload->>'old_source_name'
         AND old_access.source_scope = COALESCE(
                e.payload->'old_source'->>'source_scope',
                e.payload->'old_source'->>'server_url'
             )
        WHERE (
                COALESCE(e.payload->>'source_name', 'native') = 'native'
                OR access.user_uuid IS NOT NULL
                OR (
                    e."object_type" = 'stream'
                    AND e."action" = 'deleted'
                )
            )
          AND (
                e.payload->>'old_source_name' IS NULL
                OR e.payload->>'old_source_name' = 'native'
                OR old_access.user_uuid IS NOT NULL
            )
          AND (
                e."object_type" <> 'message'
                OR e."payload"->>'stream_uuid' IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM "m_workspace_stream_bindings" AS binding
                    WHERE binding."project_id" = e."project_id"
                      AND binding."stream_uuid" =
                          (e."payload"->>'stream_uuid')::uuid
                      AND binding."user_uuid" = e."user_uuid"
                )
            )
          AND ({external_stream_visibility});
    """


EXTERNAL_STREAM_VISIBILITY_SQL = """
e."object_type" <> 'message'
OR e."payload"->>'stream_uuid' IS NULL
OR NOT EXISTS (
    SELECT 1
    FROM "m_workspace_streams" AS external_stream
    WHERE external_stream."project_id" = e."project_id"
      AND external_stream."uuid" =
          (e."payload"->>'stream_uuid')::uuid
      AND external_stream."source_name" <> 'native'
)
OR EXISTS (
    SELECT 1
    FROM "m_workspace_streams" AS external_stream
    JOIN "m_confirmed_external_account_access" AS stream_access
      ON stream_access."project_id" = external_stream."project_id"
     AND stream_access."user_uuid" = e."user_uuid"
     AND stream_access."account_type" = external_stream."source_name"
     AND stream_access."source_scope" = COALESCE(
            external_stream."source"->>'source_scope',
            external_stream."source"->>'server_url'
         )
    WHERE external_stream."project_id" = e."project_id"
      AND external_stream."uuid" =
          (e."payload"->>'stream_uuid')::uuid
)
"""


RESET_EXTERNAL_EVENT_CURSORS_SQL = """
UPDATE "m_workspace_event_cursors" AS cursor
SET "epoch_generation" = gen_random_uuid(),
    "pruned_through_epoch_version" = GREATEST(
        cursor."pruned_through_epoch_version",
        cursor."current_epoch_version"
    ),
    "updated_at" = NOW()
WHERE EXISTS (
    SELECT 1
    FROM "m_external_chats_v2" AS chat
    WHERE chat."project_id" = cursor."project_id"
);
"""


class MigrationStep(migrations.AbstractMigrationStep):
    """Deduplicate provider projections and persist local membership revocation."""

    def __init__(self):
        self._depends = [
            "0122-revoke-external-projection-access-on-stream-removal-640b9d.py"
        ]

    @property
    def migration_id(self):
        return "aadb67c9-c716-4066-9867-b82079c1c283"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS
                "m_workspace_external_chat_membership_revocations" (
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "provider" VARCHAR(32) NOT NULL,
                "provider_realm_id" VARCHAR(2048) NOT NULL,
                "provider_chat_id" VARCHAR(512) NOT NULL,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (
                    "project_id", "user_uuid", "provider",
                    "provider_realm_id", "provider_chat_id"
                )
            );
            """
        )
        session.execute(BACKFILL_EXTERNAL_CHAT_MEMBERSHIP_REVOCATIONS_SQL)
        session.execute(CANONICAL_MEMBER_ACCESS_VIEW_SQL)
        session.execute(_visible_events_view(EXTERNAL_STREAM_VISIBILITY_SQL))
        session.execute(RESET_EXTERNAL_EVENT_CURSORS_SQL)

    def downgrade(self, session):
        session.execute(MEMBER_ACCESS_VIEW_SQL)
        session.execute(_visible_events_view("TRUE"))
        session.execute(
            'DROP TABLE IF EXISTS '
            '"m_workspace_external_chat_membership_revocations";'
        )
        session.execute(RESET_EXTERNAL_EVENT_CURSORS_SQL)


migration_step = MigrationStep()
