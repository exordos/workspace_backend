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


GLOBAL_PARTITION_INDEX_NAME = "messenger_projection_tasks_global_partition_idx"
USER_PARTITION_INDEX_NAME = "messenger_projection_tasks_user_partition_idx"
CREATE_INDEX = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS {GLOBAL_PARTITION_INDEX_NAME}
    ON messenger_projection_tasks (
        project_id, created_at, ordering_created_at, outbox_event_uuid
    )
    INCLUDE (
        status, next_retry_at, lease_expires_at,
        scope_kind, scope_key, ordering_key, task_kind
    )
    WHERE status NOT IN ('completed', 'dead_letter')
      AND NOT (
          task_kind IN ('read_counters', 'folder_projection')
          AND payload->>'user_uuid' IS NOT NULL
      )
"""
CREATE_USER_PARTITION_INDEX = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS {USER_PARTITION_INDEX_NAME}
    ON messenger_projection_tasks (
        project_id, (payload->>'user_uuid'), created_at,
        ordering_created_at, outbox_event_uuid
    )
    INCLUDE (
        uuid, task_kind, scope_kind, scope_key, ordering_key,
        status, next_retry_at, lease_expires_at
    )
    WHERE status NOT IN ('completed', 'dead_letter')
      AND task_kind IN ('read_counters', 'folder_projection')
      AND payload->>'user_uuid' IS NOT NULL
"""
INDEX_DEFINITIONS = {
    GLOBAL_PARTITION_INDEX_NAME: CREATE_INDEX,
    USER_PARTITION_INDEX_NAME: CREATE_USER_PARTITION_INDEX,
}


def _run_online_index_ddl(session, *, create):
    session.commit()
    connection = session.engine.get_connection()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            if create:
                for index_name, definition in INDEX_DEFINITIONS.items():
                    cursor.execute(
                        """
                        SELECT target_index.indisvalid
                        FROM pg_index AS target_index
                        WHERE target_index.indexrelid = to_regclass(%s)
                        """,
                        (index_name,),
                    )
                    existing = cursor.fetchone()
                    if existing is not None and not existing[0]:
                        cursor.execute(
                            f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}"
                        )
                    cursor.execute(definition)
            else:
                for index_name in reversed(INDEX_DEFINITIONS):
                    cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0180-Cascade-bridge-history-scopes-with-online-cleanup-indexes-f3bf9f.py"
        ]

    @property
    def migration_id(self):
        return "81bfd995-0ed7-497b-bea5-79cb795dc505"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _run_online_index_ddl(session, create=True)

    def downgrade(self, session):
        _run_online_index_ddl(session, create=False)


migration_step = MigrationStep()
