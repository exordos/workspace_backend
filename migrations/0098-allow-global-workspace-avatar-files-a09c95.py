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
        self._depends = ["0097-preserve-messenger-provider-metadata-77beeb.py"]

    @property
    def migration_id(self):
        return "a09c955b-389f-4a72-a478-d0ced5725376"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_files"
                ALTER COLUMN "stream_uuid" DROP NOT NULL;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DELETE FROM "m_workspace_files"
            WHERE "stream_uuid" IS NULL;

            ALTER TABLE "m_workspace_files"
                ALTER COLUMN "stream_uuid" SET NOT NULL;
            """
        )


migration_step = MigrationStep()
