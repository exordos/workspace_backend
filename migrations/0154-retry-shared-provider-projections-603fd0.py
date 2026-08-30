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


UPGRADE_SQL = """
-- Provider API v2 originally rejected the second owner of a realm-global
-- stream. Reissue the monotonic projection reset after the shared-projection
-- gate is fixed so a Bridge discards quarantined deliveries and performs one
-- complete, idempotent history pass. A fresh 1.0.x installation applies 0152
-- and this migration before the Bridge observes either generation, therefore
-- it still performs only one reset.
DO $shared_projection_account_desired_guard$
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
        RAISE EXCEPTION
            'Zulip projection retry requires every account desired resource';
    END IF;
END;
$shared_projection_account_desired_guard$;

DO $shared_projection_chat_desired_guard$
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
        RAISE EXCEPTION
            'Zulip projection retry requires every selected chat assignment';
    END IF;
END;
$shared_projection_chat_desired_guard$;

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
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self) -> None:
        self._depends = ["0153-page-external-bridge-snapshots-75ad6f.py"]

    @property
    def migration_id(self) -> str:
        return "603fd077-99da-421a-baf6-2b3abc6312ee"

    @property
    def is_manual(self) -> bool:
        return False

    def upgrade(self, session) -> None:
        session.execute(UPGRADE_SQL)

    def downgrade(self, session) -> None:
        # Reset generations are monotonic fences. Rolling the schema ledger
        # back must not make a Bridge accept stale projection state.
        session.execute("SELECT 1")


migration_step = MigrationStep()
