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


RENAME_SQL = """
    UPDATE messenger_topics topic SET name=%s,updated_at=now()
    FROM messenger_streams stream
    WHERE topic.project_id=stream.project_id AND topic.stream_uuid=stream.uuid
      AND topic.source_name='zulip' AND stream.source_name='zulip'
      AND topic.provider_uuid=stream.provider_uuid AND NOT stream.private
      AND (topic.provider_identity IN (stream.provider_identity || ':',stream.provider_identity || ':✔ ')
           OR (topic.uuid=stream.default_topic_uuid AND topic.provider_identity IS NULL))
      AND topic.name=%s
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0297-Bound-shared-last-message-repair-during-topic-moves-5fe49e.py"
        ]

    @property
    def migration_id(self):
        return "141e3a03-5ff2-432e-88e5-315fa1601288"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("SELECT set_config('workspace.zulip_incoming','on',true)")
        session.execute(
            "CREATE TABLE workspace_zulip_label_repairs_0298 AS "
            "SELECT project_id,uuid,updated_at FROM messenger_topics WITH NO DATA"
        )
        session.execute(
            "WITH renamed AS ("
            + RENAME_SQL
            + " RETURNING topic.project_id,topic.uuid,topic.updated_at) "
            "INSERT INTO workspace_zulip_label_repairs_0298 SELECT * FROM renamed",
            ("General", "zulip"),
        )

    def downgrade(self, session):
        session.execute("SELECT set_config('workspace.zulip_incoming','on',true)")
        session.execute(
            "UPDATE messenger_topics topic SET name='zulip',updated_at=now() "
            "FROM workspace_zulip_label_repairs_0298 repaired "
            "WHERE topic.project_id=repaired.project_id AND topic.uuid=repaired.uuid "
            "AND topic.updated_at=repaired.updated_at AND topic.name='General'"
        )
        session.execute("DROP TABLE workspace_zulip_label_repairs_0298")


migration_step = MigrationStep()
