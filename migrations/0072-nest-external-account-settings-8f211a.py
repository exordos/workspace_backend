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
        self._depends = ["0071-default-next-sync-at-for-unsynced-user-syncs-b4547e.py"]

    @property
    def migration_id(self):
        return "8f211a8b-cfa3-48c3-a77d-90c23bb0ed59"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            UPDATE "m_external_accounts"
                SET "account_settings" = jsonb_build_object(
                    'kind', "account_settings"->>'kind',
                    'credentials', jsonb_build_object(
                        'kind', "account_settings"->>'kind',
                        'login', "account_settings"->>'login',
                        'server_url', "account_settings"->>'server_url',
                        'token', "account_settings"->>'token'
                    ),
                    'user_info', NULL
                )
                WHERE "account_type" = 'zulip'
                  AND "account_settings" ? 'login';
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            UPDATE "m_external_accounts"
                SET "account_settings" = jsonb_build_object(
                    'kind', "account_settings"->>'kind',
                    'login', "account_settings"->'credentials'->>'login',
                    'server_url',
                        "account_settings"->'credentials'->>'server_url',
                    'token', "account_settings"->'credentials'->>'token'
                )
                WHERE "account_type" = 'zulip'
                  AND "account_settings" ? 'credentials';
            """
        )


migration_step = MigrationStep()
