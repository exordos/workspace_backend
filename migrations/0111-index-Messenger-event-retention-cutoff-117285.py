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
        self._depends = ["0110-add-Messenger-canonical-import-ledger-e8e1b2.py"]

    @property
    def migration_id(self):
        return "117285b9-5985-4f14-a194-a578c3787f69"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE INDEX "m_workspace_events_retention_cutoff_idx"
                ON "m_workspace_events" (
                    "created_at", "project_id", "user_uuid", "epoch_version"
                );
            """
        )

    def downgrade(self, session):
        session.execute(
            'DROP INDEX "m_workspace_events_retention_cutoff_idx";'
        )


migration_step = MigrationStep()
