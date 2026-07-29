# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations


INDEXES = (
    (
        "m_workspace_messages_topic_boundary_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_messages_topic_boundary_idx"
            ON "m_workspace_messages" (
                "project_id",
                "stream_uuid",
                "topic_uuid",
                "created_at",
                "uuid"
            )
        """,
    ),
    (
        "m_workspace_unread_flags_user_message_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_unread_flags_user_message_idx"
            ON "m_workspace_user_message_flags" (
                "project_id",
                "user_uuid",
                "uuid"
            )
            WHERE "read" = FALSE
        """,
    ),
)


def _run_online_index_ddl(session, *, create):
    # PostgreSQL's concurrent index operations cannot run in a transaction.
    # Commit preceding migrations, then use a dedicated autocommit connection;
    # the migration marker is saved by RESTAlchemy in the caller's new
    # transaction only after all online DDL has completed successfully.
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
        self._depends = [
            "0125-scope-external-visibility-to-canonical-streams-e82c02.py"
        ]

    @property
    def migration_id(self):
        return "20ae2266-265f-488d-a306-f299160a1b25"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _run_online_index_ddl(session, create=True)

    def downgrade(self, session):
        _run_online_index_ddl(session, create=False)


migration_step = MigrationStep()
