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


INDEXES = (
    (
        "m_external_history_imports_scope_cleanup_idx",
        "m_external_history_imports_v1",
        "bridge_uuid, project_uuid, provider_realm_uuid",
    ),
    (
        "m_external_history_hashes_job_cleanup_idx",
        "m_external_history_message_hashes_v1",
        "job_uuid",
    ),
    (
        "m_external_history_hashes_message_cleanup_idx",
        "m_external_history_message_hashes_v1",
        "message_uuid",
    ),
)


def _require_existing_bridges(session):
    orphan = session.execute("""
        SELECT 1 FROM m_external_history_scopes_v1 AS scope
        WHERE NOT EXISTS (
            SELECT 1 FROM m_external_bridge_instances_v2 AS bridge
            WHERE bridge.uuid = scope.bridge_uuid
        ) LIMIT 1
    """).fetchone()
    if orphan is not None:
        raise RuntimeError(
            "History scopes reference missing bridges; recover their original "
            "bridge records or clean their operational jobs in bounded steps "
            "before applying migration 0180. Canonical messages and files must "
            "be preserved."
        )


def _ensure_indexes(session):
    # Follow the existing online-migration pattern: build one index at a time,
    # outside the migration transaction. Interrupted builds are safe to retry.
    session.commit()
    connection = session.engine.get_connection()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for index_name, table_name, columns in INDEXES:
                cursor.execute(
                    "SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)",
                    (index_name,),
                )
                existing = cursor.fetchone()
                if existing is not None and not existing[0]:
                    cursor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{index_name}"')
                cursor.execute(
                    f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" '
                    f'ON "{table_name}" ({columns})'
                )
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0179-Bound-consecutive-history-processing-failures-07fee1.py"]

    @property
    def migration_id(self):
        return "f3bf9f2b-84db-4153-964d-437ab09f9e0d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Do not cascade-delete possibly millions of orphaned descendants in a
        # schema migration, or fabricate a bridge identity to adopt them.
        session.execute("SET LOCAL lock_timeout='50ms'")
        session.execute("SET LOCAL statement_timeout='500ms'")
        _require_existing_bridges(session)
        _ensure_indexes(session)
        session.execute("SET LOCAL lock_timeout='50ms'")
        session.execute("SET LOCAL statement_timeout='500ms'")
        session.execute("""
            ALTER TABLE m_external_history_scopes_v1
                ADD CONSTRAINT m_external_history_scopes_bridge_fkey
                FOREIGN KEY (bridge_uuid)
                REFERENCES m_external_bridge_instances_v2(uuid)
                ON DELETE CASCADE NOT VALID
        """)
        # Recheck under the FK DDL locks in case a bridge disappeared during
        # online index construction. Validation scans scopes, not descendants.
        _require_existing_bridges(session)
        session.execute("""
            ALTER TABLE m_external_history_scopes_v1
                VALIDATE CONSTRAINT m_external_history_scopes_bridge_fkey
        """)

    def downgrade(self, session):
        session.execute("SET LOCAL lock_timeout='50ms'")
        session.execute("SET LOCAL statement_timeout='500ms'")
        session.execute("""
            ALTER TABLE m_external_history_scopes_v1
                DROP CONSTRAINT m_external_history_scopes_bridge_fkey
        """)
        # Keep the online indexes for rolling rollback: the previous schema
        # already cascades job and canonical-message deletion. Migration 0175
        # removes these indexes when it removes their tables.


migration_step = MigrationStep()
