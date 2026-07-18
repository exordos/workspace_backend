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
        self._depends = ["0111-index-Messenger-event-retention-cutoff-117285.py"]

    @property
    def migration_id(self):
        return "6f42abaa-1d5c-439e-981d-55a26624e13f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE "m_workspace_event_audience_snapshots_v1" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "membership_digest" TEXT NOT NULL,
                "current_epoch_version" BIGINT NOT NULL DEFAULT 0,
                "pruned_through_epoch_version" BIGINT NOT NULL DEFAULT 0,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE ("project_id", "membership_digest"),
                CHECK (
                    "pruned_through_epoch_version" <= "current_epoch_version"
                )
            );
            CREATE TABLE "m_workspace_event_audience_members_v1" (
                "audience_snapshot_uuid" UUID NOT NULL REFERENCES
                    "m_workspace_event_audience_snapshots_v1" ("uuid")
                    ON DELETE CASCADE,
                "user_uuid" UUID NOT NULL,
                PRIMARY KEY ("audience_snapshot_uuid", "user_uuid")
            );
            CREATE INDEX "m_workspace_event_audience_user_idx"
                ON "m_workspace_event_audience_members_v1" (
                    "user_uuid", "audience_snapshot_uuid"
                );

            CREATE TABLE "m_workspace_broadcast_message_events_v1" (
                "epoch_version" BIGINT PRIMARY KEY DEFAULT nextval(
                    pg_get_serial_sequence(
                        'm_workspace_events', 'epoch_version'
                    )
                ),
                "uuid" UUID NOT NULL UNIQUE,
                "project_id" UUID NOT NULL,
                "entity_uuid" UUID NOT NULL,
                "audience_snapshot_uuid" UUID NOT NULL REFERENCES
                    "m_workspace_event_audience_snapshots_v1" ("uuid"),
                "schema_version" INTEGER NOT NULL DEFAULT 1,
                "object_type" TEXT NOT NULL,
                "action" TEXT NOT NULL,
                "payload" JSONB NOT NULL,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE "m_workspace_event_recipient_payloads_v1" (
                "event_uuid" UUID NOT NULL REFERENCES
                    "m_workspace_broadcast_message_events_v1" ("uuid")
                    ON DELETE CASCADE,
                "user_uuid" UUID NOT NULL,
                "payload" JSONB NOT NULL,
                PRIMARY KEY ("event_uuid", "user_uuid"),
                CHECK (jsonb_typeof("payload") = 'object')
            );
            CREATE INDEX "m_workspace_event_payload_user_idx"
                ON "m_workspace_event_recipient_payloads_v1" (
                    "user_uuid", "event_uuid"
                );
            CREATE INDEX "m_workspace_broadcast_events_retention_idx"
                ON "m_workspace_broadcast_message_events_v1" (
                    "created_at", "project_id", "epoch_version"
                );
            CREATE INDEX "m_workspace_broadcast_events_entity_idx"
                ON "m_workspace_broadcast_message_events_v1" (
                    "project_id", "entity_uuid", "epoch_version"
                );
            CREATE INDEX "m_workspace_broadcast_events_project_epoch_idx"
                ON "m_workspace_broadcast_message_events_v1" (
                    "project_id", "epoch_version"
                );
            CREATE INDEX "m_workspace_broadcast_events_audience_epoch_idx"
                ON "m_workspace_broadcast_message_events_v1" (
                    "audience_snapshot_uuid", "epoch_version"
                );

            DROP VIEW "m_workspace_visible_events";
            CREATE VIEW "m_workspace_visible_events" AS
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
                    b."payload" || COALESCE(override."payload", '{}'::jsonb)
                        || jsonb_build_object(
                            'user_uuid', recipient."user_uuid"
                        ) AS "payload",
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
                )
              AND (
                    e.payload->>'old_source_name' IS NULL
                    OR e.payload->>'old_source_name' = 'native'
                    OR old_access.user_uuid IS NOT NULL
                );

            CREATE TRIGGER m_workspace_broadcast_events_notify_created
                AFTER INSERT ON "m_workspace_broadcast_message_events_v1"
                FOR EACH ROW
                EXECUTE FUNCTION notify_workspace_event_created();
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP VIEW "m_workspace_visible_events";
            CREATE VIEW "m_workspace_visible_events" AS
            SELECT e.*
            FROM "m_workspace_events" AS e
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
                )
              AND (
                    e.payload->>'old_source_name' IS NULL
                    OR e.payload->>'old_source_name' = 'native'
                    OR old_access.user_uuid IS NOT NULL
                );
            DROP TABLE "m_workspace_event_recipient_payloads_v1";
            DROP TABLE "m_workspace_broadcast_message_events_v1";
            DROP TABLE "m_workspace_event_audience_members_v1";
            DROP TABLE "m_workspace_event_audience_snapshots_v1";
            """
        )


migration_step = MigrationStep()
