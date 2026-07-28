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
SELECT DISTINCT
    project_id,
    user_uuid,
    account_type,
    source_scope
FROM ranked_candidates
WHERE projection_rank = 1;
"""


PREVIOUS_MEMBER_ACCESS_VIEW_SQL = CANONICAL_MEMBER_ACCESS_VIEW_SQL.replace(
    "SELECT DISTINCT\n"
    "    project_id,\n"
    "    user_uuid,\n"
    "    account_type,\n"
    "    source_scope\n"
    "FROM ranked_candidates",
    "SELECT\n"
    "    project_id,\n"
    "    user_uuid,\n"
    "    account_type,\n"
    "    source_scope\n"
    "FROM ranked_candidates",
    1,
)


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
    """Return one account-access row for each canonical account scope."""

    def __init__(self):
        self._depends = [
            "0123-deduplicate-and-revoke-external-chat-memberships-aadb67.py"
        ]

    @property
    def migration_id(self):
        return "78c745a8-08a2-4432-a511-9e0875cc35db"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(CANONICAL_MEMBER_ACCESS_VIEW_SQL)
        session.execute(RESET_EXTERNAL_EVENT_CURSORS_SQL)

    def downgrade(self, session):
        session.execute(PREVIOUS_MEMBER_ACCESS_VIEW_SQL)
        session.execute(RESET_EXTERNAL_EVENT_CURSORS_SQL)


migration_step = MigrationStep()
