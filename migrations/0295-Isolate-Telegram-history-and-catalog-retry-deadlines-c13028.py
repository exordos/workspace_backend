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
            "0293-Rebuild-Telegram-channel-and-personal-folder-classification-4a7e78.py"
        ]

    @property
    def migration_id(self):
        return "c1302857-65dd-44d4-b7f7-a1963c659a01"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE telegram_account_state
                ADD COLUMN history_retry_at timestamptz,
                ADD COLUMN catalog_retry_at timestamptz,
                ADD COLUMN common_retry_at timestamptz,
                ADD COLUMN catalog_error_code text;
            ALTER TABLE telegram_peer_state ADD COLUMN receive_retry_at timestamptz,
                ADD COLUMN media_retry_at timestamptz,
                ADD COLUMN receive_error_code text,
                ADD COLUMN media_error_code text,
                ADD COLUMN history_retry_at timestamptz,
                ADD COLUMN history_error_code text;
            CREATE INDEX telegram_pending_peer_events ON telegram_incoming_events(external_account_uuid,(payload->>'peer'),id)
                WHERE processed_at IS NULL
        """)

    def downgrade(self, session):
        session.execute("""
            ALTER TABLE telegram_account_state
                DROP COLUMN catalog_retry_at,
                DROP COLUMN history_retry_at,DROP COLUMN common_retry_at,DROP COLUMN catalog_error_code;
            ALTER TABLE telegram_peer_state DROP COLUMN receive_retry_at,DROP COLUMN media_retry_at,
                DROP COLUMN history_retry_at,DROP COLUMN history_error_code,
                DROP COLUMN receive_error_code,DROP COLUMN media_error_code;
            DROP INDEX telegram_pending_peer_events
        """)


migration_step = MigrationStep()
