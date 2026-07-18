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
        self._depends = ["0106-preserve-external-chat-catalog-capabilities-e29207.py"]

    @property
    def migration_id(self):
        return "d06fed62-22ac-4e80-a64b-d63b94f4a0a9"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE "m_workspace_event_cursors" (
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "epoch_generation" UUID NOT NULL DEFAULT gen_random_uuid(),
                "current_epoch_version" BIGINT NOT NULL DEFAULT 0,
                "pruned_through_epoch_version" BIGINT NOT NULL DEFAULT 0,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                PRIMARY KEY ("project_id", "user_uuid"),
                CHECK (
                    "current_epoch_version" >= 0
                    AND "pruned_through_epoch_version" >= 0
                    AND "pruned_through_epoch_version"
                        <= "current_epoch_version"
                )
            );

            INSERT INTO "m_workspace_event_cursors" (
                "project_id", "user_uuid", "current_epoch_version"
            )
            SELECT
                "project_id", "user_uuid", MAX("epoch_version")
            FROM "m_workspace_events"
            GROUP BY "project_id", "user_uuid";
            """
        )

    def downgrade(self, session):
        self._delete_table_if_exists(session, "m_workspace_event_cursors")


migration_step = MigrationStep()
