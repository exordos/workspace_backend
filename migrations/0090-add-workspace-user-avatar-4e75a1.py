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
        self._depends = ["0089-filter-hidden-external-folder-events-68e898.py"]

    @property
    def migration_id(self):
        return "4e75a1c9-103c-4092-9f07-ad24ab183c64"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                ADD COLUMN IF NOT EXISTS "avatar" VARCHAR(2048);
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_users"
            SET "avatar" = 'urn:gavatar:' || "uuid"::text
            WHERE "avatar" IS NULL;
            """
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                ALTER COLUMN "avatar" SET NOT NULL;
            """
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_users_avatar_urn_check";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                ADD CONSTRAINT "m_workspace_users_avatar_urn_check"
                CHECK (
                    (
                        "avatar" LIKE 'urn:image:%'
                        AND length("avatar") = 46
                    )
                    OR (
                        "avatar" LIKE 'urn:gavatar:%'
                        AND length("avatar") = 48
                    )
                    OR "avatar" LIKE 'urn:url:http://%'
                    OR "avatar" LIKE 'urn:url:https://%'
                );
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_events"
            SET "payload" = jsonb_set(
                "payload",
                '{avatar}',
                to_jsonb('urn:gavatar:' || ("payload"->>'uuid')),
                true
            )
            WHERE "payload"->>'kind' = 'user.updated'
              AND NOT "payload" ? 'avatar';
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_users_avatar_urn_check";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                DROP COLUMN IF EXISTS "avatar";
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_events"
            SET "payload" = "payload" - 'avatar'
            WHERE "payload"->>'kind' = 'user.updated';
            """
        )


migration_step = MigrationStep()
