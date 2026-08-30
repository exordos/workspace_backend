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
DELETE FROM m_external_bridge_snapshots_v1
WHERE expires_at <= NOW();

CREATE TABLE m_external_bridge_snapshot_resources_v2 (
    snapshot_token text NOT NULL,
    ordinal bigint NOT NULL,
    resource jsonb NOT NULL,
    PRIMARY KEY (snapshot_token, ordinal),
    CONSTRAINT m_external_bridge_snapshot_resources_v2_snapshot_fkey
        FOREIGN KEY (snapshot_token)
        REFERENCES m_external_bridge_snapshots_v1 (snapshot_token)
        ON DELETE CASCADE,
    CONSTRAINT m_external_bridge_snapshot_resources_v2_ordinal_check
        CHECK (ordinal >= 0)
);

INSERT INTO m_external_bridge_snapshot_resources_v2 (
    snapshot_token, ordinal, resource
)
SELECT snapshot.snapshot_token, item.ordinality - 1, item.resource
FROM m_external_bridge_snapshots_v1 AS snapshot
CROSS JOIN LATERAL jsonb_array_elements(snapshot.resources)
    WITH ORDINALITY AS item(resource, ordinality);

UPDATE m_external_bridge_snapshots_v1
SET resources = '[]'::jsonb
WHERE resources <> '[]'::jsonb;
"""


DOWNGRADE_SQL = """
UPDATE m_external_bridge_snapshots_v1 AS snapshot
SET resources = COALESCE(
    (
        SELECT jsonb_agg(item.resource ORDER BY item.ordinal)
        FROM m_external_bridge_snapshot_resources_v2 AS item
        WHERE item.snapshot_token = snapshot.snapshot_token
    ),
    '[]'::jsonb
);

DROP TABLE m_external_bridge_snapshot_resources_v2;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0152-add-messenger-v2-canonical-model-b59d87.py"]

    @property
    def migration_id(self):
        return "75ad6f73-4ed6-43b5-9cb2-f853a82957da"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
