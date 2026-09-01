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


UPGRADE_SQL = r"""
SET LOCAL lock_timeout = '30s';
SET LOCAL statement_timeout = '10min';
LOCK TABLE
    messenger_message_placements,
    messenger_user_message_states,
    m_workspace_messages,
    m_workspace_read_state_projects_v1,
    m_workspace_user_read_chunks_v1,
    m_workspace_user_topic_read_stats_v1,
    m_workspace_user_read_revisions_v1
IN SHARE ROW EXCLUSIVE MODE;
SET LOCAL lock_timeout = '0';

CREATE TEMP TABLE messenger_v2_compatibility_read_repair_targets (
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    ingest_sequence bigint NOT NULL,
    PRIMARY KEY (project_id, user_uuid, ingest_sequence)
) ON COMMIT DROP;

INSERT INTO messenger_v2_compatibility_read_repair_targets (
    project_id, user_uuid, topic_uuid, ingest_sequence
)
SELECT state.project_id, state.user_uuid, legacy.topic_uuid,
       legacy.ingest_sequence
FROM messenger_user_message_states AS state
JOIN messenger_message_placements AS placement
  ON placement.project_id = state.project_id
 AND placement.uuid = state.placement_uuid
JOIN m_workspace_messages AS legacy
  ON legacy.project_id = placement.project_id
 AND legacy.uuid = COALESCE(placement.legacy_public_uuid, placement.uuid)
JOIN m_workspace_read_state_projects_v1 AS read_project
  ON read_project.project_id = state.project_id
 AND read_project.mode IN ('compact', 'rollback')
LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
  ON chunk.user_uuid = state.user_uuid
 AND chunk.chunk_number = legacy.ingest_sequence / 4096
WHERE state.read_at IS NOT NULL
  AND COALESCE(
      get_bit(
          chunk.read_bits,
          (legacy.ingest_sequence % 4096)::integer
      ),
      0
  ) = 0
ON CONFLICT (project_id, user_uuid, ingest_sequence) DO NOTHING;

WITH masks AS (
    SELECT target.user_uuid,
           target.ingest_sequence / 4096 AS chunk_number,
           bit_or(
               set_bit(
                   B'0'::bit(4096),
                   (target.ingest_sequence % 4096)::integer,
                   1
               )
           ) AS read_bits
    FROM messenger_v2_compatibility_read_repair_targets AS target
    GROUP BY target.user_uuid, target.ingest_sequence / 4096
)
INSERT INTO m_workspace_user_read_chunks_v1 (
    user_uuid, chunk_number, read_bits, created_at, updated_at
)
SELECT user_uuid, chunk_number, read_bits, NOW(), NOW()
FROM masks
ON CONFLICT (user_uuid, chunk_number) DO UPDATE SET
    read_bits = m_workspace_user_read_chunks_v1.read_bits
                | EXCLUDED.read_bits,
    updated_at = NOW();

WITH scopes AS MATERIALIZED (
    SELECT DISTINCT project_id, user_uuid, topic_uuid
    FROM messenger_v2_compatibility_read_repair_targets
), snapshots AS MATERIALIZED (
    SELECT scope.project_id, scope.user_uuid, scope.topic_uuid,
           count(message.uuid) FILTER (
               WHERE COALESCE(
                   get_bit(
                       chunk.read_bits,
                       (message.ingest_sequence % 4096)::integer
                   ),
                   0
               ) = 1
           )::bigint AS read_count
    FROM scopes AS scope
    LEFT JOIN m_workspace_messages AS message
      ON message.project_id = scope.project_id
     AND message.topic_uuid = scope.topic_uuid
    LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
      ON chunk.user_uuid = scope.user_uuid
     AND chunk.chunk_number = message.ingest_sequence / 4096
    GROUP BY scope.project_id, scope.user_uuid, scope.topic_uuid
)
INSERT INTO m_workspace_user_topic_read_stats_v1 (
    project_id, user_uuid, topic_uuid, read_count, created_at, updated_at
)
SELECT project_id, user_uuid, topic_uuid, read_count, NOW(), NOW()
FROM snapshots
ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE SET
    read_count = EXCLUDED.read_count,
    updated_at = NOW();

INSERT INTO m_workspace_user_read_revisions_v1 (
    project_id, user_uuid, revision, created_at, updated_at
)
SELECT DISTINCT project_id, user_uuid, 1, NOW(), NOW()
FROM messenger_v2_compatibility_read_repair_targets
ON CONFLICT (project_id, user_uuid) DO UPDATE SET
    revision = m_workspace_user_read_revisions_v1.revision + 1,
    updated_at = NOW();

DO $compatibility_read_repair_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM messenger_v2_compatibility_read_repair_targets AS target
        LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
          ON chunk.user_uuid = target.user_uuid
         AND chunk.chunk_number = target.ingest_sequence / 4096
        WHERE COALESCE(
            get_bit(
                chunk.read_bits,
                (target.ingest_sequence % 4096)::integer
            ),
            0
        ) <> 1
    ) THEN
        RAISE EXCEPTION
            'Messenger v2 compatibility read-state repair is incomplete';
    END IF;
END;
$compatibility_read_repair_guard$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0165-repair-provider-participant-message-state-73514c.py"]

    @property
    def migration_id(self):
        return "95c09d2a-38f1-46ed-9115-cbe860d63fd6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
