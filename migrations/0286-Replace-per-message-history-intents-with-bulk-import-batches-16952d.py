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
            "0285-Replace-message-states-with-sparse-flags-and-shared-last-messages-71c389.py"
        ]

    @property
    def migration_id(self):
        return "16952d1f-2c86-44f2-8075-3a671aea265b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            DROP TABLE zulip_history_intents;
            DROP TABLE zulip_history_jobs;
            DROP FUNCTION zulip_history_wakeup();
            CREATE TABLE zulip_history_accounts (
                external_account_uuid uuid PRIMARY KEY REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
                project_id uuid NOT NULL,provider_uuid uuid NOT NULL,
                cutoff_at timestamptz NOT NULL,cutoff_id bigint NOT NULL,
                before_id bigint NOT NULL,finished boolean NOT NULL DEFAULT false,
                registration jsonb,subscribed_streams bigint[],catalog_position integer NOT NULL DEFAULT 0,
                next_attempt_at timestamptz NOT NULL DEFAULT now(),error text,
                last_run_at timestamptz NOT NULL DEFAULT 'epoch'
            );
            CREATE INDEX zulip_history_account_frontier ON zulip_history_accounts
                (project_id,provider_uuid,before_id DESC) WHERE NOT finished;
            CREATE TABLE zulip_history_batches (
                id bigserial PRIMARY KEY,
                external_account_uuid uuid NOT NULL UNIQUE REFERENCES zulip_history_accounts(external_account_uuid) ON DELETE CASCADE,
                before_id bigint NOT NULL,
                next_before_id bigint,
                messages jsonb,
                finished boolean NOT NULL DEFAULT false
            );
            CREATE INDEX workspace_file_project_content ON m_workspace_files(project_id,hash,size_bytes)
                INCLUDE(storage_type,storage_id,storage_object_id);
            -- A fresh queue registration supplies the immutable history boundary.
            -- Existing Workspace data stays intact; the replacement importer starts
            -- from the new snapshot instead of translating retired intent payloads.
            DELETE FROM zulip_incoming_subscriptions;
        """)

    def downgrade(self, session):
        raise RuntimeError(
            "Bulk history cutover is forward-only; restore the pre-upgrade backup"
        )


migration_step = MigrationStep()
