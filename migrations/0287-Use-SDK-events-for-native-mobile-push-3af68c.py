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

from gcl_sdk import migrations as sdk_migrations
from gcl_sdk.common import utils as sdk_utils
from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0286-Replace-per-message-history-intents-with-bulk-import-batches-16952d.py"
        ]

    @property
    def migration_id(self):
        return "3af68c7d-5db5-4cc9-9ea6-e8c84532b12c"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        engine = sdk_utils.MigrationEngine._get_migration_engine(sdk_migrations)
        engine.apply_migration("0000-init-events-table-2cfd220e.py", session)
        session.execute("""
            CREATE INDEX IF NOT EXISTS gcl_sdk_events_status ON gcl_sdk_events (status);
            UPDATE gcl_sdk_events SET event_data=jsonb_build_object(
                'kind','WorkspaceTopicSignal','body',jsonb_build_object(
                    'project_id',event_data->'body'->>'project_id',
                    'user_uuid',event_data->'body'->'exchange'->>'user_uuid',
                    'stream_uuid',event_data->'body'->'event_params'->>'stream_uuid',
                    'topic_uuid',event_data->'body'->'event_params'->>'topic_uuid',
                    'source_uuid',event_data->'body'->'_topic_snapshot'->>'source_uuid',
                    'previous_active_count',event_data->'body'->'_topic_snapshot'->'previous_active_count'))
            WHERE event_data->>'kind'='WorkspacePush' AND event_data->'body' ? '_topic_snapshot';
        """)

    def downgrade(self, session):
        engine = sdk_utils.MigrationEngine._get_migration_engine(sdk_migrations)
        engine.rollback_migration("0000-init-events-table-2cfd220e.py", session)


migration_step = MigrationStep()
