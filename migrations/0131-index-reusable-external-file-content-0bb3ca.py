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


INDEX_NAME = "m_workspace_files_external_content_hash_size_idx"
CREATE_INDEX = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
    ON m_workspace_files (hash, size_bytes)
    INCLUDE (storage_type, storage_id, storage_object_id)
    WHERE external_account_uuid IS NOT NULL
      AND storage_object_id LIKE 'external-content/sha256/%'
"""


def _run_online_index_ddl(session, *, create):
    # PostgreSQL concurrent index DDL must run outside a transaction. Match the
    # established online-index migration path so a large files table remains
    # writable while this release is applied.
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
                    cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
                cursor.execute(CREATE_INDEX)
            else:
                cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0130-split-active-and-passive-unread-counters-36e14b.py"]

    @property
    def migration_id(self):
        return "0bb3cac3-2f35-44a1-9cca-b91886bfa0da"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _run_online_index_ddl(session, create=True)

    def downgrade(self, session):
        _run_online_index_ddl(session, create=False)


migration_step = MigrationStep()
