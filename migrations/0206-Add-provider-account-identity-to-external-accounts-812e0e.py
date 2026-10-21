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
        self._depends = [
            "0205-Store-Zulip-message-content-timestamps-as-UTC-for-ORM-ee88ee.py"
        ]

    @property
    def migration_id(self):
        return "812e0e55-cdb1-4773-b976-2bf7dd87cc96"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE m_external_accounts_v2
                ADD COLUMN external_account_id VARCHAR(255),
                ADD COLUMN disabled BOOLEAN NOT NULL DEFAULT FALSE,
                ADD CONSTRAINT m_external_accounts_v2_project_provider_external_id_key
                    UNIQUE (project_id, provider, external_account_id);
            CREATE INDEX m_external_accounts_v2_external_account_id_idx
                ON m_external_accounts_v2 (external_account_id);
            ALTER TABLE m_workspace_users
                ADD COLUMN disabled BOOLEAN NOT NULL DEFAULT FALSE;
            CREATE OR REPLACE VIEW m_workspace_directory_users_v1 AS
                SELECT uuid, created_at, updated_at, username, source, status,
                    first_name, last_name, email, last_ping_at, status_emoji,
                    status_text, avatar, disabled
                FROM m_workspace_users;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE m_external_accounts_v2
                DROP COLUMN disabled,
                DROP COLUMN external_account_id;
            DROP VIEW m_workspace_directory_users_v1;
            ALTER TABLE m_workspace_users DROP COLUMN disabled;
            CREATE VIEW m_workspace_directory_users_v1 AS
                SELECT uuid, created_at, updated_at, username, source, status,
                    first_name, last_name, email, last_ping_at, status_emoji,
                    status_text, avatar
                FROM m_workspace_users;
            """
        )


migration_step = MigrationStep()
