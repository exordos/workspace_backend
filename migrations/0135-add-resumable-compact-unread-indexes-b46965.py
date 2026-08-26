# Copyright 2026 Genesis Corporation.
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

INDEXES = (
    (
        "m_workspace_messages_ingest_sequence_idx",
        """
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_messages_ingest_sequence_idx"
            ON "m_workspace_messages" ("ingest_sequence")
            WHERE "ingest_sequence" IS NOT NULL
        """,
    ),
    (
        "m_workspace_messages_project_ingest_sequence_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_messages_project_ingest_sequence_idx"
            ON "m_workspace_messages" (
                "project_id", "ingest_sequence", "uuid"
            )
            WHERE "ingest_sequence" IS NOT NULL
        """,
    ),
    (
        "m_workspace_messages_topic_ingest_sequence_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_messages_topic_ingest_sequence_idx"
            ON "m_workspace_messages" (
                "project_id", "topic_uuid", "ingest_sequence"
            )
            WHERE "ingest_sequence" IS NOT NULL
        """,
    ),
    (
        "m_workspace_messages_stream_read_page_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_messages_stream_read_page_idx"
            ON "m_workspace_messages" (
                "project_id", "stream_uuid", "created_at", "uuid"
            ) INCLUDE ("topic_uuid", "ingest_sequence")
        """,
    ),
    (
        "m_workspace_messages_topic_read_page_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_messages_topic_read_page_idx"
            ON "m_workspace_messages" (
                "project_id", "stream_uuid", "topic_uuid",
                "created_at", "uuid"
            ) INCLUDE ("ingest_sequence")
        """,
    ),
    (
        "m_workspace_messages_stream_ingest_sequence_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_messages_stream_ingest_sequence_idx"
            ON "m_workspace_messages" (
                "project_id", "stream_uuid", "ingest_sequence"
            )
        """,
    ),
    (
        "m_workspace_read_flags_project_message_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_read_flags_project_message_idx"
            ON "m_workspace_user_message_flags" (
                "project_id", "uuid", "user_uuid"
            )
            WHERE "read" = TRUE
        """,
    ),
    (
        "m_workspace_flags_project_message_user_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_flags_project_message_user_idx"
            ON "m_workspace_user_message_flags" (
                "project_id", "uuid", "user_uuid"
            )
        """,
    ),
    (
        "m_workspace_user_read_chunks_chunk_user_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_user_read_chunks_chunk_user_idx"
            ON "m_workspace_user_read_chunks_v1" (
                "chunk_number", "user_uuid"
            )
        """,
    ),
)


def _run_online_index_ddl(session, *, create):
    # Concurrent index operations cannot run inside the migration transaction.
    # The migration contains no other DDL, so an interrupted build can safely
    # restart after removing any invalid index left by PostgreSQL.
    session.commit()
    connection = session.engine.get_connection()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for index_name, create_statement in INDEXES:
                if create:
                    cursor.execute(
                        """
                        SELECT target_index.indisvalid
                        FROM pg_index AS target_index
                        WHERE target_index.indexrelid = to_regclass(%s)
                        """,
                        (index_name,),
                    )
                    row = cursor.fetchone()
                    if row is not None and not row[0]:
                        cursor.execute(
                            f'DROP INDEX CONCURRENTLY IF EXISTS "{index_name}"'
                        )
                    cursor.execute(create_statement)
                else:
                    cursor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{index_name}"')
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0134-add-compact-workspace-unread-state-e84da8.py"]

    @property
    def migration_id(self):
        return "b469650b-f613-4f57-869a-1dd7f6f373c3"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _run_online_index_ddl(session, create=True)

    def downgrade(self, session):
        # 0134 owns the compact schema and drops these indexes only after its
        # exclusive writer fence and aggregate-delivery gate succeed. Leaving
        # them in place here keeps a blocked/resumed downgrade fully usable.
        del session


migration_step = MigrationStep()
