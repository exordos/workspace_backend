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


class MigrationStep(migrations.AbstractMigrationStep):
    """Use an account UUID, not a shared server URL, as the access scope."""

    def __init__(self):
        self._depends = ["0116-index-Messenger-event-identity-reconciliation-72f59f.py"]

    @property
    def migration_id(self):
        return "4a927983-57be-43d1-979e-cef820b86b2d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
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

            UPDATE "m_workspace_streams"
            SET "source" = jsonb_set(
                "source",
                '{source_scope}',
                to_jsonb("external_account_uuid"::text),
                TRUE
            )
            WHERE "source_name" = 'zulip'
              AND "external_account_uuid" IS NOT NULL;

            UPDATE "m_workspace_stream_topics"
            SET "source" = jsonb_set(
                "source",
                '{source_scope}',
                to_jsonb("external_account_uuid"::text),
                TRUE
            )
            WHERE "source_name" = 'zulip'
              AND "external_account_uuid" IS NOT NULL;

            UPDATE "m_workspace_messages"
            SET "source" = jsonb_set(
                "source",
                '{source_scope}',
                to_jsonb("external_account_uuid"::text),
                TRUE
            )
            WHERE "source_name" = 'zulip'
              AND "external_account_uuid" IS NOT NULL;

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
            )
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_confirmed_external_account_access" AS
            SELECT DISTINCT
                chat.project_id,
                account.owner_user_uuid AS user_uuid,
                account.provider::varchar(32) AS account_type,
                (account.settings->>'server_url')::varchar(2048)
                    AS source_scope
            FROM "m_external_accounts_v2" AS account
            JOIN "m_external_chats_v2" AS chat
              ON chat.external_account_uuid = account.uuid
            WHERE chat.selected
              AND chat.project_id IS NOT NULL
              AND account.credential_present
              AND account.status NOT IN ('disconnected', 'suspended')
              AND account.settings->>'server_url' IS NOT NULL;

            UPDATE "m_workspace_streams"
            SET "source" = "source" - 'source_scope'
            WHERE "source_name" = 'zulip';

            UPDATE "m_workspace_stream_topics"
            SET "source" = "source" - 'source_scope'
            WHERE "source_name" = 'zulip';

            UPDATE "m_workspace_messages"
            SET "source" = "source" - 'source_scope'
            WHERE "source_name" = 'zulip';

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
            )
            """
        )


migration_step = MigrationStep()
