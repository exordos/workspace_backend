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
        self._depends = ["0217-Add-Zulip-stream-binding-reconciliation-view-5fa84f.py"]

    @property
    def migration_id(self):
        return "e8045524-db1f-43c8-a2e1-1b8850b9262d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE VIEW bridge_zulip_v5_project_user_states AS
            SELECT DISTINCT account.project_id,
                   account.owner_user_uuid AS user_uuid
            FROM m_external_accounts_v2 AS account
            WHERE account.provider = 'zulip'
              AND NOT EXISTS (
                  SELECT 1
                  FROM messenger_project_users AS project_user
                  WHERE project_user.project_id = account.project_id
                    AND project_user.user_uuid = account.owner_user_uuid
              );
            """
        )

    def downgrade(self, session):
        session.execute("DROP VIEW bridge_zulip_v5_project_user_states;")


migration_step = MigrationStep()
