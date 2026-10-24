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


COUNTER_SCOPE_SQL = """
CREATE OR REPLACE FUNCTION messenger_mark_counter_scope(
    p_project uuid, p_user uuid, p_stream uuid, p_topic uuid
) RETURNS void LANGUAGE sql AS $$
    INSERT INTO bridge_zulip_v5_message_counter_changes
        (project_id, user_uuid, scope_kind, scope_uuid, stream_uuid, topic_uuid)
    SELECT p_project, p_user, scope.kind, scope.uuid, p_stream, scope.topic_uuid
    FROM (VALUES ('user-stream', p_stream, NULL::uuid),
                 ('user-topic', p_topic, p_topic)) AS scope(kind, uuid, topic_uuid)
    WHERE scope.uuid IS NOT NULL
      AND current_setting('workspace.owned_mutations', true) IS DISTINCT FROM 'on'
      {incoming_guard}
    ON CONFLICT (project_id, user_uuid, scope_kind, scope_uuid)
    DO UPDATE SET revision = EXCLUDED.revision;
$$;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0271-Deduplicate-incoming-Zulip-dependency-intentions-ef4242.py"]

    @property
    def migration_id(self):
        return "c313d14d-2cb5-4375-8890-653c8f5ac8fe"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Incoming application already queues authoritative counter work. Keep
        # its per-message deltas, but avoid the duplicate shared scope write.
        session.execute(
            COUNTER_SCOPE_SQL.format(
                incoming_guard="AND current_setting('workspace.zulip_incoming', true) IS DISTINCT FROM 'on'"
            )
        )

    def downgrade(self, session):
        session.execute(COUNTER_SCOPE_SQL.format(incoming_guard=""))


migration_step = MigrationStep()
