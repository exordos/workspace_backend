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
        self._depends = ["0299-Expand-stream-names-to-2048-characters-400671.py"]

    @property
    def migration_id(self):
        return "82877f27-8e61-4e08-93d9-50db1de509d3"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TABLE zulip_history_recovery_accounts
                (LIKE zulip_history_accounts INCLUDING ALL);
            ALTER TABLE zulip_history_recovery_accounts ADD FOREIGN KEY (external_account_uuid)
                REFERENCES zulip_history_accounts(external_account_uuid) ON DELETE CASCADE;
            CREATE TABLE zulip_history_recovery_batches
                (LIKE zulip_history_batches INCLUDING ALL);
            ALTER TABLE zulip_history_recovery_batches ADD FOREIGN KEY (external_account_uuid)
                REFERENCES zulip_history_recovery_accounts(external_account_uuid) ON DELETE CASCADE;
        """)

    def downgrade(self, session):
        session.execute("DROP TABLE zulip_history_recovery_batches")
        session.execute("DROP TABLE zulip_history_recovery_accounts")


migration_step = MigrationStep()
