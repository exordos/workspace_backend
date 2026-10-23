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
            "0265-Normalize-existing-resolved-topic-identities-and-preserve-topic-dependencies-dd6aae.py"
        ]

    @property
    def migration_id(self):
        return "11e12e51-3f0b-45a8-936d-f23dedec41c6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        definition = session.execute(
            "SELECT pg_get_viewdef('bridge_zulip_v5_topic_states'::regclass,true) AS definition"
        ).fetchone()["definition"]
        title = "CASE WHEN workspace.source_name='native' THEN workspace.name ELSE source.name END"
        definition = definition.replace("source.name,", "__topic_title__ AS name,", 1)
        definition = definition.replace("source.name", title).replace(
            "__topic_title__", title
        )
        definition = definition.replace(
            "delivery.project_id = changes.project_id",
            "delivery.project_id = changes.project_id AND EXISTS (SELECT 1 FROM messenger_streams AS native_stream "
            "WHERE native_stream.project_id=placement.project_id AND native_stream.uuid=placement.stream_uuid "
            "AND native_stream.source_name='zulip' AND native_stream.deleted_at IS NULL)",
        )
        session.execute(
            "CREATE OR REPLACE VIEW bridge_zulip_v5_topic_states AS " + definition
        )

    def downgrade(self, session):
        # The previous schema supports this ownership guard. Keep native data
        # protected while earlier migrations restore their original views.
        pass


migration_step = MigrationStep()
