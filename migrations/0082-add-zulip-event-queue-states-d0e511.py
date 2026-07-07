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
        self._depends = ["0081-add-source-fields-to-zulip-topics-and-messages-7295ec.py"]

    @property
    def migration_id(self):
        return "d0e51103-2c06-4b4f-a7c5-88195dbda335"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_zulip_event_queue_states" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "server_url" VARCHAR(2048) NOT NULL,
                "user_uuid" UUID NOT NULL,
                "queue_id" VARCHAR(256),
                "last_event_id" BIGINT NOT NULL DEFAULT -1,
                "last_message_id" BIGINT NOT NULL DEFAULT 0,
                "is_synced" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_zulip_event_queue_states_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON DELETE CASCADE
            );
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_zulip_event_queue_states_account_idx"
                ON "m_zulip_event_queue_states"
                    ("external_account_uuid");
            """
        )
        session.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                "m_zulip_event_queue_states_owner_idx"
                ON "m_zulip_event_queue_states"
                    ("project_id", "server_url", "user_uuid");
            """
        )

    def downgrade(self, session):
        self._delete_table_if_exists(
            session,
            "m_zulip_event_queue_states",
        )


migration_step = MigrationStep()
