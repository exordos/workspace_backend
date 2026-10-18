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
            "0209-Add-Zulip-channel-types-and-direct-account-references-6bdbf2.py"
        ]

    @property
    def migration_id(self):
        return "751a1a52-c12e-4ff5-9962-fa63eafa2434"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW m_workspace_directory_users_v1 AS
                SELECT uuid, created_at, updated_at, username, source, status,
                    first_name, last_name, email, last_ping_at, status_emoji,
                    status_text, avatar, disabled
                FROM m_workspace_users
                WHERE source <> 'hidden';
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            UPDATE m_workspace_users SET source = 'zulip' WHERE source = 'hidden';
            CREATE OR REPLACE VIEW m_workspace_directory_users_v1 AS
                SELECT uuid, created_at, updated_at, username, source, status,
                    first_name, last_name, email, last_ping_at, status_emoji,
                    status_text, avatar, disabled
                FROM m_workspace_users;
            """
        )


migration_step = MigrationStep()
