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

    def __init__(self):
        self._depends = ["0092-allow-multiple-external-accounts-per-server-6e0ebe.py"]

    @property
    def migration_id(self):
        return "2386dcf7-87e8-41b6-86d8-d8447794fd86"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_accounts_project_user_account_type_server_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_accounts_project_user_account_type_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_external_account_user_syncs_server_url_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS "m_zulip_event_queue_states_owner_idx";
            """
        )
        session.execute(
            """
            WITH ranked_accounts AS (
                SELECT
                    "uuid",
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            "project_id",
                            "user_uuid",
                            "account_type",
                            regexp_replace("server_url", '/+$', '')
                        ORDER BY
                            CASE
                                WHEN "account_settings"->'credentials'
                                    IS NOT NULL
                                  AND "account_settings"->'credentials'
                                    <> 'null'::jsonb
                                THEN 0
                                ELSE 1
                            END,
                            CASE
                                WHEN "access_status" = 'confirmed'
                                THEN 0
                                ELSE 1
                            END,
                            "created_at" ASC,
                            "uuid" ASC
                    ) AS "position"
                FROM "m_external_accounts"
            )
            DELETE FROM "m_external_accounts"
            WHERE "uuid" IN (
                SELECT "uuid"
                FROM ranked_accounts
                WHERE "position" > 1
            );
            """
        )
        session.execute(
            """
            WITH ranked_syncs AS (
                SELECT
                    sync."uuid",
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            regexp_replace(sync."server_url", '/+$', '')
                        ORDER BY
                            CASE
                                WHEN account."uuid" IS NOT NULL
                                THEN 0
                                ELSE 1
                            END,
                            CASE
                                WHEN account."account_settings"->'credentials'
                                    IS NOT NULL
                                  AND account."account_settings"->'credentials'
                                    <> 'null'::jsonb
                                THEN 0
                                ELSE 1
                            END,
                            sync."created_at" ASC,
                            sync."uuid" ASC
                    ) AS "position"
                FROM "m_external_account_user_syncs" AS sync
                LEFT JOIN "m_external_accounts" AS account
                  ON account."uuid" = sync."external_account_uuid"
            )
            DELETE FROM "m_external_account_user_syncs"
            WHERE "uuid" IN (
                SELECT "uuid"
                FROM ranked_syncs
                WHERE "position" > 1
            );
            """
        )
        session.execute(
            """
            WITH ranked_entities AS (
                SELECT
                    "uuid",
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            "project_id",
                            regexp_replace("server_url", '/+$', ''),
                            "entity_type",
                            "entity_id"
                        ORDER BY "created_at" ASC, "uuid" ASC
                    ) AS "position"
                FROM "m_zulip_processed_entities"
            )
            DELETE FROM "m_zulip_processed_entities"
            WHERE "uuid" IN (
                SELECT "uuid"
                FROM ranked_entities
                WHERE "position" > 1
            );
            """
        )
        session.execute(
            """
            UPDATE "m_external_accounts"
            SET
                "server_url" = regexp_replace("server_url", '/+$', ''),
                "source_scope" = regexp_replace(
                    COALESCE("source_scope", "server_url"),
                    '/+$',
                    ''
                )
            WHERE "server_url" <> regexp_replace("server_url", '/+$', '')
               OR "source_scope" IS NULL
               OR "source_scope" <> regexp_replace("source_scope", '/+$', '');
            """
        )
        session.execute(
            """
            UPDATE "m_external_account_user_syncs"
            SET "server_url" = regexp_replace("server_url", '/+$', '')
            WHERE "server_url" <> regexp_replace("server_url", '/+$', '');
            """
        )
        session.execute(
            """
            UPDATE "m_zulip_event_queue_states"
            SET "server_url" = regexp_replace("server_url", '/+$', '')
            WHERE "server_url" <> regexp_replace("server_url", '/+$', '');
            """
        )
        session.execute(
            """
            UPDATE "m_zulip_history_sync_tasks"
            SET "server_url" = regexp_replace("server_url", '/+$', '')
            WHERE "server_url" <> regexp_replace("server_url", '/+$', '');
            """
        )
        session.execute(
            """
            UPDATE "m_zulip_processed_entities"
            SET "server_url" = regexp_replace("server_url", '/+$', '')
            WHERE "server_url" <> regexp_replace("server_url", '/+$', '');
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_streams"
            SET "source" = jsonb_set(
                "source",
                '{server_url}',
                to_jsonb(regexp_replace("source"->>'server_url', '/+$', ''))
            )
            WHERE "source_name" = 'zulip'
              AND "source" ? 'server_url'
              AND "source"->>'server_url' <>
                  regexp_replace("source"->>'server_url', '/+$', '');
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_stream_topics"
            SET "source" = jsonb_set(
                "source",
                '{server_url}',
                to_jsonb(regexp_replace("source"->>'server_url', '/+$', ''))
            )
            WHERE "source_name" = 'zulip'
              AND "source" ? 'server_url'
              AND "source"->>'server_url' <>
                  regexp_replace("source"->>'server_url', '/+$', '');
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_messages"
            SET "source" = jsonb_set(
                "source",
                '{server_url}',
                to_jsonb(regexp_replace("source"->>'server_url', '/+$', ''))
            )
            WHERE "source_name" = 'zulip'
              AND "source" ? 'server_url'
              AND "source"->>'server_url' <>
                  regexp_replace("source"->>'server_url', '/+$', '');
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_events"
            SET "payload" = jsonb_set(
                "payload",
                '{source,server_url}',
                to_jsonb(
                    regexp_replace(
                        "payload"->'source'->>'server_url',
                        '/+$',
                        ''
                    )
                )
            )
            WHERE "payload" ? 'source'
              AND "payload"->'source' ? 'server_url'
              AND "payload"->'source'->>'server_url' <>
                  regexp_replace(
                      "payload"->'source'->>'server_url',
                      '/+$',
                      ''
                  );
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_events"
            SET "payload" = jsonb_set(
                "payload",
                '{old_source,server_url}',
                to_jsonb(
                    regexp_replace(
                        "payload"->'old_source'->>'server_url',
                        '/+$',
                        ''
                    )
                )
            )
            WHERE "payload" ? 'old_source'
              AND "payload"->'old_source' ? 'server_url'
              AND "payload"->'old_source'->>'server_url' <>
                  regexp_replace(
                      "payload"->'old_source'->>'server_url',
                      '/+$',
                      ''
                  );
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_external_accounts_project_user_account_type_server_idx"
                ON "m_external_accounts"
                    ("project_id", "user_uuid", "account_type", "server_url");
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_external_account_user_syncs_server_url_idx"
                ON "m_external_account_user_syncs" ("server_url");
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_zulip_event_queue_states_owner_idx"
                ON "m_zulip_event_queue_states"
                    ("project_id", "server_url", "user_uuid");
            """
        )

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
