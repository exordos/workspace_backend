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
        "m_workspace_read_state_active_maintenance_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_read_state_active_maintenance_idx"
            ON "m_workspace_read_state_projects_v1" (
                "updated_at", "project_id"
            )
            WHERE "mode" IN ('legacy', 'preparing', 'dual')
        """,
    ),
    (
        "m_workspace_read_state_cleanup_maintenance_idx",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            "m_workspace_read_state_cleanup_maintenance_idx"
            ON "m_workspace_read_state_projects_v1" (
                "updated_at", "project_id"
            )
            WHERE "mode" = 'compact'
        """,
    ),
)


def _ensure_indexes(session):
    # Online DDL commits independently from the migration bookkeeping. A
    # failed recursive downgrade can therefore leave 0145 recorded as applied
    # after its indexes were removed. This forward correction is deliberately
    # repeatable and repairs that split state on the next upgrade.
    session.commit()
    connection = session.engine.get_connection()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for index_name, create_statement in INDEXES:
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
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0146-register-rolling-read-state-projects-4c8dc3.py",
        ]

    @property
    def migration_id(self):
        return "804f7723-4d44-4d32-914e-3f9dfe90eee1"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _ensure_indexes(session)

    def downgrade(self, session):
        # The indexes belong to 0145. Leaving them in place keeps 0145's
        # applied schema intact when only this repair step is rolled back.
        return None


migration_step = MigrationStep()
