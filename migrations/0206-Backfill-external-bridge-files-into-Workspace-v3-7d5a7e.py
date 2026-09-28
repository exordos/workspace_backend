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
INSERT INTO workspace_v3.files (
    uuid, project_id, user_uuid, stream_uuid, acl_mode, name, description,
    content_type, size_bytes, hash, storage_type, storage_id,
    storage_object_id, created_at, updated_at
)
SELECT file.uuid, file.project_id, file.user_uuid, file.stream_uuid,
       file.acl_mode, file.name, file.description, file.content_type,
       file.size_bytes, file.hash, file.storage_type, file.storage_id,
       file.storage_object_id, file.created_at, file.updated_at
FROM m_workspace_files AS file
JOIN workspace_v3.users AS user_record ON user_record.uuid = file.user_uuid
JOIN workspace_v3.streams AS stream
  ON stream.project_id = file.project_id AND stream.uuid = file.stream_uuid
WHERE file.external_account_uuid IS NOT NULL
  AND file.storage_object_id LIKE 'external-content/sha256/%'
ON CONFLICT (project_id, uuid) DO NOTHING;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0205-Requeue-hierarchical-unread-counter-rollups-bf1934.py"]

    @property
    def migration_id(self):
        return "7d5a7e04-72df-421f-9fce-f48a68f15115"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
