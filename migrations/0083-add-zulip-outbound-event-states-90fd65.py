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
        self._depends = ["0082-add-zulip-event-queue-states-d0e511.py"]

    @property
    def migration_id(self):
        return "90fd6531-0b91-4697-9287-fd570dd399bc"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_zulip_outbound_event_states" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "epoch_version" BIGINT NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "status" VARCHAR(32) NOT NULL DEFAULT 'pending',
                "attempts" INTEGER NOT NULL DEFAULT 0,
                "next_retry_at" TIMESTAMP WITH TIME ZONE,
                "last_error" TEXT,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_zulip_outbound_event_states_event_fkey"
                    FOREIGN KEY ("epoch_version")
                    REFERENCES "m_workspace_events" ("epoch_version")
                    ON DELETE CASCADE,
                CONSTRAINT "m_zulip_outbound_event_states_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON DELETE CASCADE
            );
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_zulip_outbound_event_states_epoch_idx"
                ON "m_zulip_outbound_event_states" ("epoch_version");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_zulip_outbound_event_states_pending_idx"
                ON "m_zulip_outbound_event_states"
                    ("status", "next_retry_at", "epoch_version");
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_zulip_outbound_event_states_account_idx"
                ON "m_zulip_outbound_event_states"
                    ("external_account_uuid");
            """
        )

    def downgrade(self, session):
        self._delete_table_if_exists(
            session,
            "m_zulip_outbound_event_states",
        )


migration_step = MigrationStep()
