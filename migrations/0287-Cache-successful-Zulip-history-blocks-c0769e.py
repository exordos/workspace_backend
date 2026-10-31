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
            "0286-Replace-per-message-history-intents-with-bulk-import-batches-16952d.py"
        ]

    @property
    def migration_id(self):
        return "c0769eb4-fb05-4970-99a8-c85981c6a0da"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TABLE zulip_history_block_receipts (
                external_account_uuid uuid NOT NULL REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
                upper_id bigint NOT NULL,lower_id bigint NOT NULL,
                data_hash bytea NOT NULL CHECK(octet_length(data_hash)=32),
                state_hash bytea NOT NULL CHECK(octet_length(state_hash)=32),
                context_hash bytea NOT NULL CHECK(octet_length(context_hash)=32),
                PRIMARY KEY(external_account_uuid,upper_id),
                CHECK(lower_id>=0 AND lower_id<upper_id)
            );
            ALTER TABLE zulip_history_batches ADD COLUMN lower_id bigint;
            UPDATE zulip_history_batches SET lower_id=CASE WHEN finished THEN 0 ELSE next_before_id END
                WHERE messages IS NOT NULL;
        """)

    def downgrade(self, session):
        session.execute("""
            ALTER TABLE zulip_history_batches DROP COLUMN lower_id;
            DROP TABLE zulip_history_block_receipts;
        """)


migration_step = MigrationStep()
