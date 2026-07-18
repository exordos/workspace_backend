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
        self._depends = ["0108-add-external-provider-data-API-queues-f74573.py"]

    @property
    def migration_id(self):
        return "0ae35f4f-6d41-4625-b998-8cd7d1e751f4"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_files"
                ADD COLUMN "acl_mode" TEXT NOT NULL DEFAULT 'stream';
            UPDATE "m_workspace_files"
            SET "acl_mode" = 'public'
            WHERE "stream_uuid" IS NULL;
            ALTER TABLE "m_workspace_files"
                ADD CONSTRAINT "m_workspace_files_acl_mode_check"
                CHECK ("acl_mode" IN ('owner', 'stream', 'public'));

            CREATE VIEW "m_workspace_visible_files_v1" AS
            SELECT files.*, accesses."user_uuid" AS "viewer_user_uuid"
            FROM "m_workspace_files" AS files
            JOIN "m_workspace_file_accesses" AS accesses
              ON accesses."project_id" = files."project_id"
             AND accesses."file_uuid" = files."uuid"
            UNION
            SELECT files.*, NULL::UUID AS "viewer_user_uuid"
            FROM "m_workspace_files" AS files
            WHERE files."acl_mode" = 'public';

            CREATE VIEW "m_workspace_visible_message_reactions_v1" AS
            SELECT reactions.*, messages."user_uuid" AS "viewer_user_uuid"
            FROM "m_workspace_message_reactions" AS reactions
            JOIN "m_workspace_user_messages_view" AS messages
              ON messages."project_id" = reactions."project_id"
             AND messages."uuid" = reactions."message_uuid";
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP VIEW "m_workspace_visible_message_reactions_v1";
            DROP VIEW "m_workspace_visible_files_v1";
            ALTER TABLE "m_workspace_files"
                DROP CONSTRAINT "m_workspace_files_acl_mode_check",
                DROP COLUMN "acl_mode";
            """
        )


migration_step = MigrationStep()
