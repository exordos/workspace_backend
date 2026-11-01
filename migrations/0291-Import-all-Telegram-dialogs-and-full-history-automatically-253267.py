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
            "0290-Preserve-distinct-IAM-identities-with-equal-emails-f619c0.py"
        ]

    @property
    def migration_id(self):
        return "253267d8-5f75-4177-be6c-35e7b242b718"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            UPDATE m_external_accounts_v2 SET settings='{}' WHERE provider='telegram';
            UPDATE telegram_peer_state SET selected=true,history_done=false,history_before_id=0,
                history_finished_at=NULL,updated_at=now();
            CREATE TABLE telegram_history_batches (
                external_account_uuid uuid NOT NULL,
                peer_key text NOT NULL,
                before_id bigint NOT NULL,
                prepared jsonb NOT NULL,
                info jsonb NOT NULL,
                PRIMARY KEY(external_account_uuid,peer_key),
                FOREIGN KEY(external_account_uuid,peer_key)
                    REFERENCES telegram_peer_state(external_account_uuid,peer_key) ON DELETE CASCADE
            );
            CREATE INDEX telegram_pending_incoming
                ON telegram_incoming_events(external_account_uuid,received_at)
                WHERE processed_at IS NULL;
        """)

    def downgrade(self, session):
        # Do not discard already imported full history or restore old exclusions.
        session.execute(
            "DROP TABLE IF EXISTS telegram_history_batches; DROP INDEX IF EXISTS telegram_pending_incoming"
        )


migration_step = MigrationStep()
