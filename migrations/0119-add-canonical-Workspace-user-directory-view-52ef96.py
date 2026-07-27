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
        self._depends = ["0118-add-push-devices-5c4ae0.py"]

    @property
    def migration_id(self):
        return "52ef9640-de45-456d-807e-4bb972bfcb33"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE VIEW "m_workspace_directory_users_v1" AS
            SELECT "user".*
            FROM "m_workspace_users" AS "user"
            WHERE "user"."source" != 'zulip'
               OR EXISTS (
                    SELECT 1
                    FROM "m_external_provider_identity_links_v1" AS "link"
                    WHERE "link"."workspace_user_uuid" = "user"."uuid"
               );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP VIEW "m_workspace_directory_users_v1";
            """
        )


migration_step = MigrationStep()
