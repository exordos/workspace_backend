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

INDEX_NAME = "m_workspace_read_memberships_stream_user_idx"
CREATE_INDEX = """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS
        "m_workspace_read_memberships_stream_user_idx"
        ON "m_workspace_read_memberships_v1" (
            "project_id", "stream_uuid", "user_uuid"
        )
        WHERE "last_detached_sequence" IS NOT NULL
"""


def _create_online_index(session):
    # Concurrent index operations cannot run inside the migration transaction.
    # An interrupted build can safely restart after removing any invalid index
    # left by PostgreSQL.
    session.commit()
    connection = session.engine.get_connection()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
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
            cursor.execute(CREATE_INDEX)
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0150-fence-compact-unread-legacy-gaps-8e6948.py"]

    @property
    def migration_id(self):
        return "7433535e-646d-4557-8f7e-5688aae458db"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _create_online_index(session)

    def downgrade(self, session):
        # Keep the index while the preceding rolling fence still exists. Its
        # downgrade removes the index atomically with the guarded schema.
        pass


migration_step = MigrationStep()
