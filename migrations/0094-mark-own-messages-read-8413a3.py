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
        self._depends = ["0093-normalize-external-account-server-urls-2386dc.py"]

    @property
    def migration_id(self):
        return "8413a349-5ed2-485a-842e-8f15b3341dd8"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            UPDATE "m_workspace_user_message_flags" AS flags
            SET
                "read" = TRUE,
                "updated_at" = NOW()
            FROM "m_workspace_messages" AS messages
            WHERE flags."uuid" = messages."uuid"
                AND flags."project_id" = messages."project_id"
                AND flags."user_uuid" = messages."user_uuid"
                AND flags."read" = FALSE;
            """
        )

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
