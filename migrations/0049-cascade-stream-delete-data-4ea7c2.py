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
        self._depends = ["0048-exclude-archived-streams-from-folder-views-b55400.py"]

    @property
    def migration_id(self):
        return "4ea7c29e-f2b8-415a-a7cd-25b64aa86726"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            DELETE FROM "m_workspace_message_reactions" AS r
            WHERE NOT EXISTS (
                SELECT 1
                FROM "m_workspace_messages" AS m
                WHERE m."uuid" = r."message_uuid"
            );
            """,
            """
            DELETE FROM "m_workspace_user_message_flags" AS f
            WHERE NOT EXISTS (
                SELECT 1
                FROM "m_workspace_messages" AS m
                WHERE m."uuid" = f."uuid"
            );
            """,
            """
            DELETE FROM "m_folder_items" AS fi
            WHERE NOT EXISTS (
                SELECT 1
                FROM "m_workspace_streams" AS s
                WHERE s."uuid" = fi."stream_uuid"
            );
            """,
            """
            ALTER TABLE "m_workspace_message_reactions"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_message_reactions_message_uuid_fkey",
                ADD CONSTRAINT "m_workspace_message_reactions_message_uuid_fkey"
                    FOREIGN KEY ("message_uuid")
                    REFERENCES "m_workspace_messages" ("uuid")
                    ON DELETE CASCADE;
            """,
            """
            ALTER TABLE "m_workspace_user_message_flags"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_user_message_flags_uuid_fkey",
                ADD CONSTRAINT "m_workspace_user_message_flags_uuid_fkey"
                    FOREIGN KEY ("uuid")
                    REFERENCES "m_workspace_messages" ("uuid")
                    ON DELETE CASCADE;
            """,
            """
            ALTER TABLE "m_folder_items"
                DROP CONSTRAINT IF EXISTS "m_folder_items_stream_uuid_fkey",
                ADD CONSTRAINT "m_folder_items_stream_uuid_fkey"
                    FOREIGN KEY ("stream_uuid")
                    REFERENCES "m_workspace_streams" ("uuid")
                    ON DELETE CASCADE;
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            ALTER TABLE "m_folder_items"
                DROP CONSTRAINT IF EXISTS "m_folder_items_stream_uuid_fkey";
            """,
            """
            ALTER TABLE "m_workspace_user_message_flags"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_user_message_flags_uuid_fkey";
            """,
            """
            ALTER TABLE "m_workspace_message_reactions"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_message_reactions_message_uuid_fkey";
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
