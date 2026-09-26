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


UPGRADE = """
ALTER TABLE workspace_v3.provider_entity_states
    ADD COLUMN source_updated_at TIMESTAMP WITH TIME ZONE;

UPDATE workspace_v3.provider_entity_states
SET source_updated_at = updated_at;

ALTER TABLE workspace_v3.provider_entity_states
    ALTER COLUMN source_updated_at SET NOT NULL;
"""

DOWNGRADE = """
ALTER TABLE workspace_v3.provider_entity_states
    DROP COLUMN source_updated_at;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0189-Add-Workspace-v3-provider-entity-states-d72d97.py"]

    @property
    def migration_id(self):
        return "443f26f9-4e95-4f66-a5b3-7f51f5ef518e"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
