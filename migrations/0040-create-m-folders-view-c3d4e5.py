#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
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
        self._depends = ["0039-rename-m-folders-created-view-b2c3d4.py"]

    @property
    def migration_id(self):
        return "e7f8a9b0-c1d2-4e3f-b4a5-c6d7e8f9a0b1"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_folders_view" AS
            SELECT * FROM "m_folder_all_view"
            UNION ALL
            SELECT * FROM "m_folder_channels_view"
            UNION ALL
            SELECT * FROM "m_folder_personal_view"
            UNION ALL
            SELECT * FROM "m_folder_created_view";
            """
        )

    def downgrade(self, session):
        session.execute('DROP VIEW IF EXISTS "m_folders_view";')


migration_step = MigrationStep()
