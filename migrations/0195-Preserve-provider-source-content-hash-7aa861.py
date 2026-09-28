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
    ADD COLUMN source_content_hash BYTEA;

UPDATE workspace_v3.provider_entity_states
SET source_content_hash = content_hash;

ALTER TABLE workspace_v3.provider_entity_states
    ALTER COLUMN source_content_hash SET NOT NULL,
    ADD CONSTRAINT provider_entity_states_source_hash_check
        CHECK (octet_length(source_content_hash) = 32);
"""

DOWNGRADE = """
ALTER TABLE workspace_v3.provider_entity_states
    DROP CONSTRAINT provider_entity_states_source_hash_check,
    DROP COLUMN source_content_hash;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0194-Index-Workspace-v3-reaction-users-e429f1.py"]

    @property
    def migration_id(self):
        return "7aa8617f-b2f4-4d9e-bdad-95083561cfde"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
