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
        self._depends = ["0132-optimize-unread-folder-projections-938496.py"]

    @property
    def migration_id(self):
        return "52d0f82b-e692-4368-b004-f9263a1f3709"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_bindings"
                ADD COLUMN IF NOT EXISTS "notification_updated_at" TIMESTAMPTZ;
            UPDATE "m_workspace_stream_bindings"
            SET "notification_updated_at" = CASE
                WHEN "notification_mode" = 'all_messages'
                    THEN TIMESTAMPTZ 'epoch'
                ELSE "updated_at" AT TIME ZONE 'UTC'
            END
            WHERE "notification_updated_at" IS NULL;
            ALTER TABLE "m_workspace_stream_bindings"
                ALTER COLUMN "notification_updated_at"
                    SET DEFAULT TIMESTAMPTZ 'epoch',
                ALTER COLUMN "notification_updated_at" SET NOT NULL;

            ALTER TABLE "m_workspace_user_topic_flags"
                ADD COLUMN IF NOT EXISTS "notification_updated_at" TIMESTAMPTZ;
            UPDATE "m_workspace_user_topic_flags"
            SET "notification_updated_at" = CASE
                WHEN "notification_mode" = 'default'
                    THEN TIMESTAMPTZ 'epoch'
                ELSE "updated_at"
            END
            WHERE "notification_updated_at" IS NULL;
            ALTER TABLE "m_workspace_user_topic_flags"
                ALTER COLUMN "notification_updated_at"
                    SET DEFAULT TIMESTAMPTZ 'epoch',
                ALTER COLUMN "notification_updated_at" SET NOT NULL;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_user_topic_flags"
                DROP COLUMN IF EXISTS "notification_updated_at";
            ALTER TABLE "m_workspace_stream_bindings"
                DROP COLUMN IF EXISTS "notification_updated_at";
            """
        )


migration_step = MigrationStep()
