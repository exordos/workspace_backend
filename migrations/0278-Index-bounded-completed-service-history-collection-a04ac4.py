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


INDEX_DEFINITIONS = {
    "messenger_completed_history_idx": "ON messenger_projection_tasks(updated_at,uuid) INCLUDE(project_id,outbox_event_uuid) WHERE status='completed'",
    "zulip_incoming_completed_history_idx": "ON zulip_incoming_events(processed_at,id) WHERE processed_at IS NOT NULL AND error IS NULL",
    "zulip_outgoing_completed_history_idx": "ON zulip_outgoing_intents(completed_at,id) WHERE status='done'",
    "zulip_conversion_cache_history_idx": "ON bridge_zulip_v5_content_snapshots(created_at,uuid) WHERE delivery_uuid IS NULL",
    "messenger_unused_contents_history_idx": "ON messenger_message_contents(created_at,uuid)",
    "zulip_unused_contents_history_idx": "ON bridge_zulip_v5_message_contents(updated_at,uuid)",
}


def _indexes(session, *, create):
    session.commit()
    connection = session.engine.get_connection()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for name, definition in INDEX_DEFINITIONS.items():
                if create:
                    cursor.execute(
                        "SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)",
                        (name,),
                    )
                    existing = cursor.fetchone()
                    if existing is not None and not existing[0]:
                        cursor.execute("DROP INDEX CONCURRENTLY IF EXISTS " + name)
                    cursor.execute(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                        + name
                        + " "
                        + definition
                    )
                else:
                    cursor.execute("DROP INDEX CONCURRENTLY IF EXISTS " + name)
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0277-Repair-imported-Zulip-media-reference-types-without-mutating-content-pages-45f9ca.py"
        ]

    @property
    def migration_id(self):
        return "a04ac473-ace6-4c00-87ce-ad1aa32c9b85"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _indexes(session, create=True)

    def downgrade(self, session):
        _indexes(session, create=False)


migration_step = MigrationStep()
