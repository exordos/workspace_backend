# Copyright 2016 Eugene Frolov <eugene@frolov.net.ru>
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License.
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
    m_workspace_messages,
    m_workspace_stream_topics,
    m_workspace_topic_message_stats_v1,
    m_workspace_read_state_projects_v1
IN SHARE ROW EXCLUSIVE MODE;
SET LOCAL lock_timeout = '0';

CREATE TEMP TABLE messenger_compact_topic_message_stat_snapshots (
    topic_uuid uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    message_count bigint NOT NULL,
    last_ingest_sequence bigint
) ON COMMIT DROP;

INSERT INTO messenger_compact_topic_message_stat_snapshots (
    topic_uuid, project_id, stream_uuid, message_count, last_ingest_sequence
)
SELECT topic.uuid, topic.project_id, topic.stream_uuid,
       count(message.uuid)::bigint,
       max(message.ingest_sequence)
FROM m_workspace_stream_topics AS topic
JOIN m_workspace_read_state_projects_v1 AS read_project
  ON read_project.project_id = topic.project_id
 AND read_project.mode IN ('compact', 'rollback')
LEFT JOIN m_workspace_messages AS message
  ON message.project_id = topic.project_id
 AND message.topic_uuid = topic.uuid
GROUP BY topic.uuid, topic.project_id, topic.stream_uuid;

INSERT INTO m_workspace_topic_message_stats_v1 (
    topic_uuid, project_id, stream_uuid, message_count,
    last_ingest_sequence, created_at, updated_at
)
SELECT topic_uuid, project_id, stream_uuid, message_count,
       last_ingest_sequence, NOW(), NOW()
FROM messenger_compact_topic_message_stat_snapshots
ON CONFLICT (topic_uuid) DO UPDATE SET
    project_id = EXCLUDED.project_id,
    stream_uuid = EXCLUDED.stream_uuid,
    message_count = EXCLUDED.message_count,
    last_ingest_sequence = EXCLUDED.last_ingest_sequence,
    updated_at = NOW();

UPDATE m_workspace_read_state_projects_v1 AS read_project
SET structure_revision = read_project.structure_revision + 1,
    updated_at = NOW()
WHERE read_project.mode IN ('compact', 'rollback')
  AND EXISTS (
      SELECT 1
      FROM messenger_compact_topic_message_stat_snapshots AS snapshot
      WHERE snapshot.project_id = read_project.project_id
  );

DO $compact_topic_message_stat_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM messenger_compact_topic_message_stat_snapshots AS snapshot
        LEFT JOIN m_workspace_topic_message_stats_v1 AS persisted
          ON persisted.topic_uuid = snapshot.topic_uuid
        WHERE persisted.project_id IS DISTINCT FROM snapshot.project_id
           OR persisted.stream_uuid IS DISTINCT FROM snapshot.stream_uuid
           OR persisted.message_count IS DISTINCT FROM snapshot.message_count
           OR persisted.last_ingest_sequence IS DISTINCT FROM
              snapshot.last_ingest_sequence
    ) THEN
        RAISE EXCEPTION
            'Compact topic message-stat reconciliation is incomplete';
    END IF;
END;
$compact_topic_message_stat_guard$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0167-reconcile-compact-topic-read-stats-303da9.py"]

    @property
    def migration_id(self):
        return "7b45baf3-8dc5-4fb2-b8bf-d73985af14c6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
