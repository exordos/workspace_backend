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
    messenger_stream_bindings,
    m_workspace_messages,
    m_workspace_read_state_projects_v1,
    m_workspace_user_read_chunks_v1,
    m_workspace_user_topic_read_stats_v1,
    m_workspace_user_read_revisions_v1
IN SHARE ROW EXCLUSIVE MODE;
SET LOCAL lock_timeout = '0';

CREATE TEMP TABLE messenger_compact_topic_read_stat_scopes (
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    PRIMARY KEY (project_id, user_uuid, topic_uuid)
) ON COMMIT DROP;

INSERT INTO messenger_compact_topic_read_stat_scopes (
    project_id, user_uuid, topic_uuid
)
SELECT DISTINCT binding.project_id, binding.user_uuid, message.topic_uuid
FROM messenger_stream_bindings AS binding
JOIN m_workspace_read_state_projects_v1 AS read_project
  ON read_project.project_id = binding.project_id
 AND read_project.mode IN ('compact', 'rollback')
JOIN m_workspace_messages AS message
  ON message.project_id = binding.project_id
 AND message.stream_uuid = binding.stream_uuid
WHERE binding.active
UNION
SELECT stats.project_id, stats.user_uuid, stats.topic_uuid
FROM m_workspace_user_topic_read_stats_v1 AS stats
JOIN m_workspace_read_state_projects_v1 AS read_project
  ON read_project.project_id = stats.project_id
 AND read_project.mode IN ('compact', 'rollback')
ON CONFLICT (project_id, user_uuid, topic_uuid) DO NOTHING;

CREATE TEMP TABLE messenger_compact_topic_read_stat_snapshots (
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    read_count bigint NOT NULL,
    PRIMARY KEY (project_id, user_uuid, topic_uuid)
) ON COMMIT DROP;

INSERT INTO messenger_compact_topic_read_stat_snapshots (
    project_id, user_uuid, topic_uuid, read_count
)
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
FROM messenger_compact_topic_read_stat_scopes AS scope
LEFT JOIN m_workspace_messages AS message
  ON message.project_id = scope.project_id
 AND message.topic_uuid = scope.topic_uuid
LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
  ON chunk.user_uuid = scope.user_uuid
 AND chunk.chunk_number = message.ingest_sequence / 4096
GROUP BY scope.project_id, scope.user_uuid, scope.topic_uuid;

INSERT INTO m_workspace_user_topic_read_stats_v1 (
    project_id, user_uuid, topic_uuid, read_count, created_at, updated_at
)
SELECT project_id, user_uuid, topic_uuid, read_count, NOW(), NOW()
FROM messenger_compact_topic_read_stat_snapshots
ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE SET
    read_count = EXCLUDED.read_count,
    updated_at = NOW();

INSERT INTO m_workspace_user_read_revisions_v1 (
    project_id, user_uuid, revision, created_at, updated_at
)
SELECT DISTINCT project_id, user_uuid, 1, NOW(), NOW()
FROM messenger_compact_topic_read_stat_snapshots
ON CONFLICT (project_id, user_uuid) DO UPDATE SET
    revision = m_workspace_user_read_revisions_v1.revision + 1,
    updated_at = NOW();

DO $compact_topic_read_stat_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM messenger_compact_topic_read_stat_snapshots AS snapshot
        LEFT JOIN m_workspace_user_topic_read_stats_v1 AS persisted
          ON persisted.project_id = snapshot.project_id
         AND persisted.user_uuid = snapshot.user_uuid
         AND persisted.topic_uuid = snapshot.topic_uuid
        WHERE persisted.read_count IS DISTINCT FROM snapshot.read_count
    ) THEN
        RAISE EXCEPTION
            'Compact topic read-stat reconciliation is incomplete';
    END IF;
END;
$compact_topic_read_stat_guard$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0166-repair-v2-compatibility-read-state-95c09d.py"]

    @property
    def migration_id(self):
        return "303da967-7711-4781-9d38-8aefef13c82d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
