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
        self._depends = [
            "0022-update-m-message-to-sync-view-e1b3d9.py"
        ]

    @property
    def migration_id(self):
        return "a8c4e5f1-2b3d-4a7e-9c1f-6d5b8e0a4f27"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_message_reactions" (
                "uuid" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL,
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL,
                "message_uuid" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "emoji_name" VARCHAR(128) NOT NULL,
                "status" VARCHAR(16) NOT NULL,
                PRIMARY KEY ("uuid"),
                UNIQUE ("message_uuid", "user_uuid", "emoji_name")
            );
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_message_reactions_message_uuid_idx"
                ON "m_workspace_message_reactions" ("message_uuid");
            """
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_user_messages"
                ADD COLUMN IF NOT EXISTS "is_mentioned" BOOLEAN NOT NULL DEFAULT FALSE;
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS "m_workspace_user_messages_is_mentioned_idx"
                ON "m_workspace_user_messages" ("is_mentioned");
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP TABLE IF EXISTS "m_workspace_message_reactions";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_user_messages"
                DROP COLUMN IF EXISTS "is_mentioned";
            """
        )


migration_step = MigrationStep()
