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
        self._depends = ["0076-drop-external-account-user-sync-is-synced-5793d3.py"]

    @property
    def migration_id(self):
        return "ffe9dc97-7dca-4a54-834a-af001ac65d1d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                DROP CONSTRAINT IF EXISTS
                    "external_accounts_account_type_check",
                DROP CONSTRAINT IF EXISTS
                    "m_external_accounts_account_type_check",
                ADD CONSTRAINT "m_external_accounts_account_type_check"
                    CHECK ("account_type" IN ('zulip', 'iam'));
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_account_user_syncs"
                DROP CONSTRAINT IF EXISTS
                    "m_external_account_user_syncs_account_type_check",
                ADD CONSTRAINT
                    "m_external_account_user_syncs_account_type_check"
                    CHECK ("account_type" IN ('zulip', 'iam'));
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_account_user_syncs"
                DROP CONSTRAINT IF EXISTS
                    "m_external_account_user_syncs_account_type_check",
                ADD CONSTRAINT
                    "m_external_account_user_syncs_account_type_check"
                    CHECK ("account_type" IN ('zulip'));
            """
        )
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                DROP CONSTRAINT IF EXISTS
                    "external_accounts_account_type_check",
                DROP CONSTRAINT IF EXISTS
                    "m_external_accounts_account_type_check",
                ADD CONSTRAINT "m_external_accounts_account_type_check"
                    CHECK ("account_type" IN ('zulip'));
            """
        )


migration_step = MigrationStep()
