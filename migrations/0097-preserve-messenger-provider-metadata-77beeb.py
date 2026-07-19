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
        self._depends = ["0096-move-default-topic-reference-to-workspace-streams-675f18.py"]

    @property
    def migration_id(self):
        return "77beeb27-699b-4672-9457-647e5f5fc026"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_accounts"
                ADD COLUMN "provider_uuid" UUID;

            ALTER TABLE "m_workspace_users"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "external_account_uuid" UUID,
                ADD COLUMN "provider_external_id" VARCHAR(2048);

            ALTER TABLE "m_workspace_files"
                ADD COLUMN "provider_uuid" UUID,
                ADD COLUMN "external_account_uuid" UUID;
            """
        )
        for table in (
            "m_workspace_streams",
            "m_workspace_stream_topics",
            "m_workspace_messages",
            "m_workspace_message_reactions",
        ):
            session.execute(
                f"""
                ALTER TABLE "{table}"
                    ADD COLUMN "provider_uuid" UUID,
                    ADD COLUMN "external_account_uuid" UUID,
                    ADD COLUMN "provider_external_id" VARCHAR(2048),
                    ADD COLUMN "delivery_status" VARCHAR(32),
                    ADD COLUMN "delivery_error" TEXT,
                    ADD COLUMN "delivery_updated_at" TIMESTAMPTZ,
                    ADD CONSTRAINT "{table}_delivery_status_check"
                        CHECK (
                            "delivery_status" IS NULL OR
                            "delivery_status" IN (
                                'pending', 'delivered', 'failed'
                            )
                        );
                """
            )

    def downgrade(self, session):
        for table in (
            "m_workspace_message_reactions",
            "m_workspace_messages",
            "m_workspace_stream_topics",
            "m_workspace_streams",
        ):
            session.execute(
                f"""
                ALTER TABLE "{table}"
                    DROP COLUMN "delivery_updated_at",
                    DROP COLUMN "delivery_error",
                    DROP COLUMN "delivery_status",
                    DROP COLUMN "provider_external_id",
                    DROP COLUMN "external_account_uuid",
                    DROP COLUMN "provider_uuid";
                """
            )
        session.execute(
            """
            ALTER TABLE "m_workspace_files"
                DROP COLUMN "external_account_uuid",
                DROP COLUMN "provider_uuid";

            ALTER TABLE "m_workspace_users"
                DROP COLUMN "provider_external_id",
                DROP COLUMN "external_account_uuid",
                DROP COLUMN "provider_uuid";

            ALTER TABLE "m_external_accounts"
                DROP COLUMN "provider_uuid";
            """
        )


migration_step = MigrationStep()
