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
        self._depends = ["0086-add-workspace-event-project-ids-view-ee7741.py"]

    @property
    def migration_id(self):
        return "d556e0bc-e207-4919-b4ef-2f7b45876246"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_zulip_history_sync_tasks" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "server_url" VARCHAR(2048) NOT NULL,
                "user_uuid" UUID NOT NULL,
                "from_message_id" BIGINT NOT NULL,
                "to_message_id" BIGINT NOT NULL,
                "status" VARCHAR(32) NOT NULL DEFAULT 'pending',
                "last_error" TEXT,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_zulip_history_sync_tasks_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_zulip_history_sync_tasks_range_check"
                    CHECK (
                        "from_message_id" >= 0
                        AND "to_message_id" >= "from_message_id"
                    )
            );
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_zulip_history_sync_tasks_pending_idx"
                ON "m_zulip_history_sync_tasks"
                    (
                        "status",
                        "to_message_id" DESC,
                        "from_message_id" DESC,
                        "created_at" ASC
                    );
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_zulip_history_sync_tasks_account_idx"
                ON "m_zulip_history_sync_tasks"
                    ("external_account_uuid", "status");
            """
        )

    def downgrade(self, session):
        self._delete_table_if_exists(
            session,
            "m_zulip_history_sync_tasks",
        )


migration_step = MigrationStep()
