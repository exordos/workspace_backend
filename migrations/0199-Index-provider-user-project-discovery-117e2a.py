# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import typing

from restalchemy.storage.sql import migrations


INDEX_NAME = "provider_entity_states_entity_projects_idx"
QUALIFIED_INDEX_NAME = f"workspace_v3.{INDEX_NAME}"
CREATE_INDEX = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
    ON workspace_v3.provider_entity_states (
        entity_type, entity_uuid, project_id
    )
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
            "0198-Support-v3-summaries-and-flag-rebind-projections-c84af7.py"
        ]

    @property
    def migration_id(self) -> str:
        return "117e2aaa-9cc4-4aa0-a562-6fdcbddb2463"

    @property
    def is_manual(self) -> bool:
        return False

    def upgrade(self, session: typing.Any) -> None:
        _run_online_index_ddl(session, create=True)

    def downgrade(self, session: typing.Any) -> None:
        _run_online_index_ddl(session, create=False)


migration_step = MigrationStep()
