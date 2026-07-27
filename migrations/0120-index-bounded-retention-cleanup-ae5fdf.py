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


def _visible_events_view(recipient_scope_payload):
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
                    || {recipient_scope_payload} AS "payload",
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
    """


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0119-add-canonical-Workspace-user-directory-view-52ef96.py"]

    @property
    def migration_id(self):
        return "ae5fdfd7-8767-45f7-8471-8448b5900782"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE INDEX "m_external_bridge_heartbeats_v1_retention_idx"
                ON "m_external_bridge_heartbeats_v1" (
                    "received_at", "heartbeat_uuid"
                );
            UPDATE "m_workspace_users"
            SET "status" = 'offline'
            WHERE "source" <> 'iam' AND "status" <> 'offline';
            """
        )
        session.execute(
            _visible_events_view(
                """
                CASE
                    WHEN b."object_type" = 'user' THEN '{}'::jsonb
                    ELSE jsonb_build_object(
                        'user_uuid', recipient."user_uuid"
                    )
                END
                """
            )
        )

    def downgrade(self, session):
        session.execute(
            _visible_events_view(
                """
                jsonb_build_object(
                    'user_uuid', recipient."user_uuid"
                )
                """
            )
        )
        session.execute('DROP INDEX "m_external_bridge_heartbeats_v1_retention_idx";')


migration_step = MigrationStep()
