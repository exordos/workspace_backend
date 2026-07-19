#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
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


USER_STREAMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_streams" AS
SELECT
    s.uuid,
    CASE
        WHEN s.private THEN
            COALESCE(
                NULLIF(
                    TRIM(
                        COALESCE(peer_user.first_name, '') || ' ' ||
                        COALESCE(peer_user.last_name, '')
                    ),
                    ''
                ),
                peer_user.username,
                s.name
            )
        ELSE s.name
    END AS name,
    s.description,
    s.project_id,
    s.source_name,
    s.source,
    s.user_uuid AS owner,
    b.user_uuid AS user_uuid,
    b.role AS role,
    COALESCE(un.unread_count, 0) AS unread_count,
    s.invite_only,
    s.announce,
    s.private,
    s.created_at,
    s.updated_at,
    CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        ELSE NULL
    END AS direct_user_uuid,
    s.private_index
FROM "m_workspace_streams" AS s
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = s.uuid
    AND b.project_id = s.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON un.uuid = s.uuid
    AND un.user_uuid = b.user_uuid
    AND un.project_id = s.project_id
LEFT JOIN "m_workspace_users" AS peer_user
    ON peer_user.uuid = CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        WHEN s.private AND s.user_uuid <> b.user_uuid THEN s.user_uuid
        ELSE NULL
    END;
"""


LEGACY_USER_STREAMS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_streams" AS
SELECT
    s.uuid,
    CASE
        WHEN s.private AND s.user_uuid <> b.user_uuid THEN
            COALESCE(
                NULLIF(
                    TRIM(
                        COALESCE(u.first_name, '') || ' ' ||
                        COALESCE(u.last_name, '')
                    ),
                    ''
                ),
                u.username
            )
        ELSE s.name
    END AS name,
    s.description,
    s.project_id,
    s.source_name,
    s.source,
    s.user_uuid AS owner,
    b.user_uuid AS user_uuid,
    b.role AS role,
    COALESCE(un.unread_count, 0) AS unread_count,
    s.invite_only,
    s.announce,
    s.private,
    s.created_at,
    s.updated_at
FROM "m_workspace_streams" AS s
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = s.uuid
    AND b.project_id = s.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON un.uuid = s.uuid
    AND un.user_uuid = b.user_uuid
    AND un.project_id = s.project_id
LEFT JOIN "m_workspace_users" AS u
    ON u.uuid = s.user_uuid;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0045-notify-workspace-events-on-insert-9b7c1d.py"]

    @property
    def migration_id(self):
        return "59b9b5e3-4707-457a-81c4-ca10687ed645"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_workspace_streams"
                ADD COLUMN IF NOT EXISTS "direct_user_uuid" UUID NULL;
            """,
            """
            ALTER TABLE "m_workspace_streams"
                ADD COLUMN IF NOT EXISTS "private_index" VARCHAR(73) NULL;
            """,
            """
            WITH private_pairs AS (
                SELECT
                    s.uuid,
                    s.project_id,
                    s.user_uuid,
                    array_agg(
                        DISTINCT b.user_uuid::text
                        ORDER BY b.user_uuid::text
                    ) AS participant_uuid_texts
                FROM "m_workspace_streams" AS s
                JOIN "m_workspace_stream_bindings" AS b
                    ON b.stream_uuid = s.uuid
                    AND b.project_id = s.project_id
                WHERE s.private
                GROUP BY s.uuid, s.project_id, s.user_uuid
                HAVING COUNT(DISTINCT b.user_uuid) = 2
            ),
            ranked_private_pairs AS (
                SELECT
                    uuid,
                    project_id,
                    user_uuid,
                    participant_uuid_texts,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            project_id,
                            participant_uuid_texts[1],
                            participant_uuid_texts[2]
                        ORDER BY uuid
                    ) AS pair_rank
                FROM private_pairs
            )
            UPDATE "m_workspace_streams" AS s
            SET
                direct_user_uuid = CASE
                    WHEN s.user_uuid::text = p.participant_uuid_texts[1]
                        THEN p.participant_uuid_texts[2]::uuid
                    ELSE p.participant_uuid_texts[1]::uuid
                END,
                private_index = CASE
                    WHEN p.pair_rank = 1 THEN
                        p.participant_uuid_texts[1] || ':' ||
                        p.participant_uuid_texts[2]
                    ELSE NULL
                END
            FROM ranked_private_pairs AS p
            WHERE s.uuid = p.uuid
                AND s.project_id = p.project_id
                AND s.direct_user_uuid IS NULL;
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_workspace_streams_private_index_unique_idx"
                ON "m_workspace_streams" ("project_id", "private_index")
                WHERE "private_index" IS NOT NULL;
            """,
            USER_STREAMS_VIEW_SQL,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            DROP VIEW IF EXISTS "m_workspace_user_streams" CASCADE;
            """,
            LEGACY_USER_STREAMS_VIEW_SQL,
            """
            DROP INDEX IF EXISTS
                "m_workspace_streams_private_index_unique_idx";
            """,
            """
            ALTER TABLE "m_workspace_streams"
                DROP COLUMN IF EXISTS "private_index";
            """,
            """
            ALTER TABLE "m_workspace_streams"
                DROP COLUMN IF EXISTS "direct_user_uuid";
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
