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


PREVIOUS_SUMMARY_REFRESH_SQL = """
CREATE OR REPLACE FUNCTION messenger_refresh_topic_summary_state(
    target_project_id uuid, target_topic_uuid uuid, target_users uuid[] DEFAULT NULL
) RETURNS void LANGUAGE sql AS $$
UPDATE messenger_user_topic_bindings AS binding
SET summary_has_new_messages=CASE WHEN topic.summary IS NULL THEN NULL ELSE EXISTS (
    SELECT 1 FROM messenger_api_user_messages_v1 AS message
    WHERE message.project_id=binding.project_id AND message.topic_uuid=binding.topic_uuid
      AND message.user_uuid=binding.user_uuid AND message.visible
      AND message.payload->>'kind'='markdown'
      AND (boundary.uuid IS NULL OR (message.created_at,message.uuid) > (boundary.created_at,boundary.uuid))
) END
FROM messenger_topics AS topic
LEFT JOIN messenger_topic_messages_v1 AS boundary
  ON boundary.project_id=topic.project_id AND boundary.uuid=topic.summary_last_message_uuid
WHERE binding.project_id=target_project_id AND binding.topic_uuid=target_topic_uuid
  AND topic.project_id=binding.project_id AND topic.uuid=binding.topic_uuid
  AND (topic.summary IS NOT NULL OR binding.summary_has_new_messages IS NOT NULL)
  AND (target_users IS NULL OR binding.user_uuid=ANY(target_users));
$$;
"""

# The boundary view must be looked up by its unique placement key before it
# joins history. Otherwise stale project statistics can cause repeated history
# scans even for an empty topic, before the null-summary filter is evaluated.
# LIMIT 1 preserves cardinality: the boundary placement UUID is unique.
SUMMARY_REFRESH_SQL = PREVIOUS_SUMMARY_REFRESH_SQL.replace(
    """LEFT JOIN messenger_topic_messages_v1 AS boundary
  ON boundary.project_id=topic.project_id AND boundary.uuid=topic.summary_last_message_uuid""",
    """LEFT JOIN LATERAL (
    SELECT boundary.uuid,boundary.created_at
    FROM messenger_topic_messages_v1 AS boundary
    WHERE topic.summary IS NOT NULL
      AND boundary.project_id=topic.project_id
      AND boundary.uuid=topic.summary_last_message_uuid
    LIMIT 1
) AS boundary ON true""",
)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0292-Replan-transition-table-synchronization-for-each-insert-batch-b9410e.py"
        ]

    @property
    def migration_id(self):
        return "cebf1238-10c6-4536-b53b-24a8499ad5af"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(SUMMARY_REFRESH_SQL)

    def downgrade(self, session):
        session.execute(PREVIOUS_SUMMARY_REFRESH_SQL)


migration_step = MigrationStep()
