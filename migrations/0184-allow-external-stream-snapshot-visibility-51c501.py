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


def _visible_events_view(allow_stream_source_access):
    stream_source_access = ""
    placement_stream_resolution = ""
    old_stream_resolution = ""
    old_stream_source_access = ""
    if allow_stream_source_access:
        stream_source_access = """
        OR (
            event_stream.stream_uuid IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM "m_confirmed_external_stream_access" AS source_access
                WHERE source_access."project_id" = e."project_id"
                  AND source_access."user_uuid" = e."user_uuid"
                  AND source_access."stream_uuid" = event_stream.stream_uuid
            )
        )
"""
        placement_stream_resolution = """,
        (
            SELECT placement."stream_uuid"
            FROM "messenger_message_placements" AS placement
            WHERE placement."project_id" = e."project_id"
              AND placement."uuid" =
                    NULLIF(e."payload"->>'message_uuid', '')::uuid
        )
"""
        old_stream_resolution = """,
    COALESCE(
        (
            SELECT message."stream_uuid"
            FROM "m_workspace_messages" AS message
            WHERE message."project_id" = e."project_id"
              AND message."uuid" =
                    NULLIF(e."payload"->>'old_message_uuid', '')::uuid
        ),
        (
            SELECT placement."stream_uuid"
            FROM "messenger_message_placements" AS placement
            WHERE placement."project_id" = e."project_id"
              AND placement."uuid" =
                    NULLIF(e."payload"->>'old_message_uuid', '')::uuid
        )
    ) AS old_stream_uuid
"""
        old_stream_source_access = """
        OR (
            event_stream.old_stream_uuid IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM "m_confirmed_external_stream_access" AS old_source_access
                WHERE old_source_access."project_id" = e."project_id"
                  AND old_source_access."user_uuid" = e."user_uuid"
                  AND old_source_access."stream_uuid" =
                        event_stream.old_stream_uuid
            )
        )
"""
    return f"""
CREATE OR REPLACE VIEW "m_workspace_visible_events_pre_messenger_v2" AS
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
      ON recipient."audience_snapshot_uuid" = b."audience_snapshot_uuid"
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
LEFT JOIN LATERAL (
    SELECT COALESCE(
        NULLIF(e."payload"->>'stream_uuid', '')::uuid,
        CASE
            WHEN e."object_type" = 'stream'
            THEN NULLIF(e."payload"->>'uuid', '')::uuid
        END,
        (
            SELECT message."stream_uuid"
            FROM "m_workspace_messages" AS message
            WHERE message."project_id" = e."project_id"
              AND message."uuid" =
                    NULLIF(e."payload"->>'message_uuid', '')::uuid
        )
        {placement_stream_resolution}
    ) AS stream_uuid
    {old_stream_resolution}
) AS event_stream ON TRUE
WHERE (
        COALESCE(e.payload->>'source_name', 'native') = 'native'
        OR access.user_uuid IS NOT NULL
        {stream_source_access}
        OR (
            e."object_type" = 'stream'
            AND e."action" = 'deleted'
        )
    )
  AND (
        e.payload->>'old_source_name' IS NULL
        OR e.payload->>'old_source_name' = 'native'
        OR old_access.user_uuid IS NOT NULL
        {old_stream_source_access}
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
  AND (
        (
            e."object_type" = 'stream'
            AND e."action" = 'deleted'
        )
        OR event_stream.stream_uuid IS NULL
        OR NOT EXISTS (
            SELECT 1
            FROM "m_workspace_streams" AS external_stream
            WHERE external_stream."project_id" = e."project_id"
              AND external_stream."uuid" = event_stream.stream_uuid
              AND external_stream."source_name" <> 'native'
        )
        OR EXISTS (
            SELECT 1
            FROM "m_confirmed_external_stream_access" AS stream_access
            WHERE stream_access."project_id" = e."project_id"
              AND stream_access."user_uuid" = e."user_uuid"
              AND stream_access."stream_uuid" = event_stream.stream_uuid
        )
    );
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
    def __init__(self):
        self._depends = ["0183-Backfill-Messenger-projection-task-gaps-bf0cd6.py"]

    @property
    def migration_id(self):
        return "51c501c5-30e2-44e6-a607-a86338a264c3"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(_visible_events_view(allow_stream_source_access=True))
        session.execute(RESET_EXTERNAL_EVENT_CURSORS_SQL)

    def downgrade(self, session):
        session.execute(_visible_events_view(allow_stream_source_access=False))
        session.execute(RESET_EXTERNAL_EVENT_CURSORS_SQL)


migration_step = MigrationStep()
