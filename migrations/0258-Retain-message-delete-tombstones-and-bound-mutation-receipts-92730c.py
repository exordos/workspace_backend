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


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0257-Order-batched-flag-journal-writes-and-suppress-consumer-echoes-c445d0.py"
        ]

    @property
    def migration_id(self):
        return "92730c78-2682-44cc-a2e8-3d3425b754ff"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TABLE messenger_message_delete_tombstones (
                project_id UUID NOT NULL,
                canonical_message_uuid UUID NOT NULL,
                ingest_sequence BIGINT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                PRIMARY KEY (project_id, canonical_message_uuid, ingest_sequence)
            );
            INSERT INTO messenger_message_delete_tombstones
                (project_id, canonical_message_uuid, ingest_sequence)
            SELECT command.project_id, (topic->>'canonical_message_uuid')::uuid,
                   (topic->>'ingest_sequence')::bigint
            FROM messenger_mutation_commands AS command
            CROSS JOIN LATERAL jsonb_array_elements(command.payload->'topics') AS topic
            WHERE command.operation='delete' AND command.status='applied'
            ON CONFLICT DO NOTHING;
            ALTER TABLE messenger_mutation_commands ADD COLUMN admission_slot INTEGER;
            WITH pending AS (
                SELECT uuid, row_number() OVER (
                    PARTITION BY project_id,user_uuid ORDER BY sequence)-1 AS slot
                FROM messenger_mutation_commands WHERE status='pending'
            ) UPDATE messenger_mutation_commands AS command SET admission_slot=pending.slot
              FROM pending WHERE command.uuid=pending.uuid;
            CREATE UNIQUE INDEX messenger_mutation_pending_slot_idx
                ON messenger_mutation_commands(project_id,user_uuid,admission_slot)
                WHERE status='pending';
            CREATE INDEX messenger_mutation_terminal_time_idx
                ON messenger_mutation_commands(finished_at,uuid)
                WHERE status IN ('applied','rejected');
        """)

    def downgrade(self, session):
        if session.execute(
            "SELECT 1 FROM messenger_message_delete_tombstones LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                "Delete tombstones cannot be represented by the old schema. "
                "Restore a consistent pre-upgrade snapshot to roll back."
            )
        session.execute("""
            DROP INDEX messenger_mutation_terminal_time_idx;
            DROP INDEX messenger_mutation_pending_slot_idx;
            ALTER TABLE messenger_mutation_commands DROP COLUMN admission_slot;
            DROP TABLE messenger_message_delete_tombstones;
        """)


migration_step = MigrationStep()
