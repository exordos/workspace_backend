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
        self._depends = ["0191-Add-external-account-status-6c9e6d.py"]

    @property
    def migration_id(self):
        return "d80af539-0a86-469d-921b-6d67dcefe19e"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE "bridge_zulip_v5_tasks" (
                "uuid" UUID PRIMARY KEY,
                "priority" BIGINT NOT NULL DEFAULT 0 CHECK (priority >= 0),
                "status" VARCHAR(16) NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'error', 'done')),
                "content" JSONB NOT NULL,
                "account_uuid" UUID GENERATED ALWAYS AS (
                    (content->>'external_account_uuid')::UUID
                ) STORED REFERENCES "m_external_accounts_v2" ("uuid") ON DELETE CASCADE,
                "error_description" TEXT,
                "lease_owner" UUID,
                "lease_expires_at" TIMESTAMP WITHOUT TIME ZONE,
                "created_at" TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                "updated_at" TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                CHECK (jsonb_typeof(content) = 'object' AND content ? 'kind'),
                CHECK (
                    content->>'kind' <> 'import_history' OR (
                        content ? 'first_message_id' AND content ? 'last_message_id' AND
                        (content->>'first_message_id')::BIGINT >= 1 AND
                        (content->>'last_message_id')::BIGINT
                            BETWEEN (content->>'first_message_id')::BIGINT
                            AND (content->>'first_message_id')::BIGINT + 4999
                    )
                )
            );
            CREATE INDEX "bridge_zulip_v5_tasks_ready_idx"
                ON "bridge_zulip_v5_tasks" (priority DESC, created_at, uuid)
                WHERE status = 'new';
            CREATE INDEX "bridge_zulip_v5_tasks_account_idx"
                ON "bridge_zulip_v5_tasks" (account_uuid);
            """
        )

    def downgrade(self, session):
        self._delete_table_if_exists(session, "bridge_zulip_v5_tasks")


migration_step = MigrationStep()
