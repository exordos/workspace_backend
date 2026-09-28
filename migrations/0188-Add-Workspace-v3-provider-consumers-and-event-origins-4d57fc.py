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
CREATE TABLE workspace_v3.provider_consumers (
    uuid UUID NOT NULL,
    project_id UUID NOT NULL,
    name VARCHAR(32) NOT NULL,
    iam_user_uuid UUID NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, name),
    UNIQUE (project_id, iam_user_uuid),
    CONSTRAINT provider_consumers_name_check
        CHECK (name <> '' AND name <> 'native')
);

CREATE INDEX provider_consumers_auth_idx
    ON workspace_v3.provider_consumers (project_id, iam_user_uuid)
    WHERE enabled;

ALTER TABLE workspace_v3.events
    ADD COLUMN origin_consumer_type VARCHAR(16),
    ADD COLUMN origin_consumer_uuid UUID,
    ADD CONSTRAINT events_origin_consumer_type_check
        CHECK (origin_consumer_type IN ('user', 'provider')),
    ADD CONSTRAINT events_origin_consumer_pair_check
        CHECK (
            (origin_consumer_type IS NULL) = (origin_consumer_uuid IS NULL)
        );
"""

DOWNGRADE = """
ALTER TABLE workspace_v3.events
    DROP CONSTRAINT events_origin_consumer_pair_check,
    DROP CONSTRAINT events_origin_consumer_type_check,
    DROP COLUMN origin_consumer_uuid,
    DROP COLUMN origin_consumer_type;

DROP TABLE workspace_v3.provider_consumers;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0187-Add-restricted-stream-history-53f0d9.py"]

    @property
    def migration_id(self):
        return "4d57fccb-ca4c-4622-9a3d-f1fe864affe3"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
