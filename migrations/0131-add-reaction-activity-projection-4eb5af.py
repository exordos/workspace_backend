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


INDEX_NAME = "m_workspace_messages_reaction_activity_idx"
CREATE_INDEX_SQL = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS "{INDEX_NAME}"
        ON "m_workspace_messages" (
            "project_id",
            "user_uuid",
            "latest_reaction_at" DESC,
            "uuid" DESC
        )
        WHERE "reaction_count" > 0
"""


def _run_online_index_ddl(session, *, create):
    # PostgreSQL's concurrent index operations cannot run in a transaction.
    # Commit the column/backfill work, then use a dedicated autocommit
    # connection. RESTAlchemy records the migration only after this returns.
    session.commit()
    connection = session.engine.get_connection()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            if create:
                cursor.execute(
                    """
                    SELECT target_index.indisvalid
                    FROM pg_index AS target_index
                    WHERE target_index.indexrelid = to_regclass(%s)
                    """,
                    (INDEX_NAME,),
                )
                row = cursor.fetchone()
                if row is not None and not row[0]:
                    cursor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{INDEX_NAME}"')
                cursor.execute(CREATE_INDEX_SQL)
            else:
                cursor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{INDEX_NAME}"')
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0130-split-active-and-passive-unread-counters-36e14b.py"]

    @property
    def migration_id(self):
        return "4eb5af2b-b607-4753-9302-fd856b4856c0"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_messages"
                ADD COLUMN IF NOT EXISTS "reaction_count"
                    INTEGER NOT NULL DEFAULT 0
                    CHECK ("reaction_count" >= 0),
                ADD COLUMN IF NOT EXISTS "latest_reaction_at" TIMESTAMPTZ;

            UPDATE "m_workspace_messages" AS message
            SET
                "reaction_count" = activity."reaction_count",
                "latest_reaction_at" = activity."latest_reaction_at"
            FROM (
                SELECT
                    reaction."project_id",
                    reaction."message_uuid",
                    COUNT(*)::INTEGER AS "reaction_count",
                    MAX(GREATEST(
                        reaction."created_at",
                        reaction."updated_at"
                    )) AS "latest_reaction_at"
                FROM "m_workspace_message_reactions" AS reaction
                GROUP BY reaction."project_id", reaction."message_uuid"
            ) AS activity
            WHERE message."project_id" = activity."project_id"
              AND message."uuid" = activity."message_uuid";
            """
        )
        _run_online_index_ddl(session, create=True)

    def downgrade(self, session):
        _run_online_index_ddl(session, create=False)
        session.execute(
            """
            ALTER TABLE "m_workspace_messages"
                DROP COLUMN IF EXISTS "latest_reaction_at",
                DROP COLUMN IF EXISTS "reaction_count";
            """
        )


migration_step = MigrationStep()
