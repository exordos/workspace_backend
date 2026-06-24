#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
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
        self._depends = ["0041-add-m-workspace-user-topic-flags-e9d8c7.py"]

    @property
    def migration_id(self):
        return "2f6a9c81-4d7e-4a3b-95c6-0f1e2d3c4b5a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_events" (
                "epoch_version" BIGSERIAL PRIMARY KEY,
                "uuid" UUID NOT NULL UNIQUE,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "payload" JSONB NOT NULL DEFAULT '{}'::jsonb,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_events_project_epoch_idx"
                ON "m_workspace_events" ("project_id", "epoch_version");
            """,
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_events_user_epoch_idx"
                ON "m_workspace_events" (
                    "project_id", "user_uuid", "epoch_version"
                );
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "m_workspace_events")


migration_step = MigrationStep()
