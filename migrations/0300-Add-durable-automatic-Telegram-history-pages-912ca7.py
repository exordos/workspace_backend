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
            "0299-Join-Telegram-provider-and-history-flag-optimization-migrations-480e21.py"
        ]

    @property
    def migration_id(self):
        return "912ca7eb-b2b8-4bd7-8330-06de4ca522e0"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            UPDATE m_external_accounts_v2 SET settings='{}' WHERE provider='telegram';
            UPDATE telegram_peer_state SET selected=true,updated_at=now() WHERE NOT selected;
            CREATE TABLE IF NOT EXISTS telegram_history_batches (
                external_account_uuid uuid NOT NULL,
                peer_key text NOT NULL,
                before_id bigint NOT NULL,
                prepared jsonb NOT NULL,
                info jsonb NOT NULL,
                PRIMARY KEY(external_account_uuid,peer_key),
                FOREIGN KEY(external_account_uuid,peer_key)
                    REFERENCES telegram_peer_state(external_account_uuid,peer_key) ON DELETE CASCADE
            );
        """)

    def downgrade(self, session):
        # Retain canonical messages/files and peer checkpoints when backfill is disabled.
        session.execute("DROP TABLE IF EXISTS telegram_history_batches")


migration_step = MigrationStep()
