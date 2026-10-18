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


UPGRADE_SQL = """
CREATE TABLE bridge_zulip_v5_message_counter_changes (
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL,
    scope_kind varchar(32) NOT NULL CHECK (scope_kind IN ('user-stream', 'user-topic')),
    scope_uuid uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    topic_uuid uuid,
    revision bigint NOT NULL DEFAULT nextval('messenger_message_change_revision_seq'),
    PRIMARY KEY (project_id, user_uuid, scope_kind, scope_uuid)
);
INSERT INTO bridge_zulip_v5_message_counter_changes
    (project_id, user_uuid, scope_kind, scope_uuid, stream_uuid, topic_uuid)
SELECT DISTINCT placement.project_id, binding.user_uuid, scope.kind, scope.uuid,
       placement.stream_uuid, scope.topic_uuid
FROM messenger_message_placements AS placement
JOIN messenger_messages AS message
  ON message.project_id = placement.project_id AND message.uuid = placement.message_uuid
JOIN messenger_user_message_bindings AS binding
  ON binding.project_id = placement.project_id AND binding.placement_uuid = placement.uuid
CROSS JOIN LATERAL (VALUES
    ('user-stream', placement.stream_uuid, NULL::uuid),
    ('user-topic', placement.topic_uuid, placement.topic_uuid)
) AS scope(kind, uuid, topic_uuid)
WHERE message.source_name = 'zulip';
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0224-Track-topic-changes-for-bounded-Zulip-reconciliation-0dbef1.py"
        ]

    @property
    def migration_id(self):
        return "09ca0ba3-a74a-43af-8586-3fb96f5bb2c5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute("DROP TABLE bridge_zulip_v5_message_counter_changes")


migration_step = MigrationStep()
