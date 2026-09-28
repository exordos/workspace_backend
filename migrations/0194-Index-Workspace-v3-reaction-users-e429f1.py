# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import typing

from restalchemy.storage.sql import migrations


INDEX_NAME = "message_reactions_user_idx"
QUALIFIED_INDEX_NAME = f"workspace_v3.{INDEX_NAME}"
CREATE_INDEX = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
    ON workspace_v3.message_reactions (project_id, user_uuid)
"""


def _run_online_index_ddl(session: typing.Any, *, create: bool) -> None:
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
                    (QUALIFIED_INDEX_NAME,),
                )
                existing = cursor.fetchone()
                if existing is not None and not existing[0]:
                    cursor.execute(
                        f"DROP INDEX CONCURRENTLY IF EXISTS {QUALIFIED_INDEX_NAME}"
                    )
                cursor.execute(CREATE_INDEX)
            else:
                cursor.execute(
                    f"DROP INDEX CONCURRENTLY IF EXISTS {QUALIFIED_INDEX_NAME}"
                )
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self) -> None:
        self._depends = [
            "0193-Backfill-legacy-native-messenger-data-into-Workspace-v3-2ee199.py"
        ]

    @property
    def migration_id(self) -> str:
        return "e429f180-4174-45af-99cb-147052372d29"

    @property
    def is_manual(self) -> bool:
        return False

    def upgrade(self, session: typing.Any) -> None:
        _run_online_index_ddl(session, create=True)

    def downgrade(self, session: typing.Any) -> None:
        _run_online_index_ddl(session, create=False)


migration_step = MigrationStep()
