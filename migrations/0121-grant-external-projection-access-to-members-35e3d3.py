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


OWNER_ACCESS_VIEW_SQL = """
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
  AND account.status NOT IN ('disconnected', 'suspended');
"""


RESET_ACCESSIBLE_EVENT_CURSORS_SQL = """
UPDATE "m_workspace_event_cursors" AS cursor
SET "epoch_generation" = gen_random_uuid(),
    "pruned_through_epoch_version" = GREATEST(
        cursor."pruned_through_epoch_version",
        cursor."current_epoch_version"
    ),
    "updated_at" = NOW()
WHERE EXISTS (
    SELECT 1
    FROM "m_confirmed_external_account_access" AS access
    WHERE access."project_id" = cursor."project_id"
      AND access."user_uuid" = cursor."user_uuid"
);
"""


class MigrationStep(migrations.AbstractMigrationStep):
    """Authorize provider projections through their canonical stream bindings."""

    def __init__(self):
        self._depends = ["0120-index-bounded-retention-cleanup-ae5fdf.py"]

    @property
    def migration_id(self):
        return "35e3d356-9fe8-4dd4-b6db-6c9da527d891"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(MEMBER_ACCESS_VIEW_SQL)
        session.execute(RESET_ACCESSIBLE_EVENT_CURSORS_SQL)

    def downgrade(self, session):
        session.execute(OWNER_ACCESS_VIEW_SQL)
        session.execute(RESET_ACCESSIBLE_EVENT_CURSORS_SQL)


migration_step = MigrationStep()
