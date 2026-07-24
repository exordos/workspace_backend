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
        self._depends = [
            "0114-scope-Messenger-email-uniqueness-to-IAM-1dbd2c.py"
        ]

    @property
    def migration_id(self):
        return "39cb26af-4a18-4e87-befd-e5e540271137"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_accounts_v2"
                ADD COLUMN "provider_realm_uuid" UUID,
                ADD COLUMN "provider_owner_user_id" TEXT,
                ADD CONSTRAINT "m_external_accounts_v2_provider_identity_pair_check"
                    CHECK (
                        ("provider_realm_uuid" IS NULL) =
                        ("provider_owner_user_id" IS NULL)
                    );

            CREATE UNIQUE INDEX
                "m_external_accounts_v2_provider_identity_key"
                ON "m_external_accounts_v2" (
                    "provider", "provider_realm_uuid",
                    "provider_owner_user_id"
                )
                WHERE "provider_realm_uuid" IS NOT NULL;

            CREATE TABLE "m_external_provider_identity_links_v1" (
                "provider" TEXT NOT NULL,
                "provider_realm_uuid" UUID NOT NULL,
                "provider_user_id" TEXT NOT NULL,
                "workspace_user_uuid" UUID NOT NULL,
                "link_kind" TEXT NOT NULL,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (
                    "provider", "provider_realm_uuid", "provider_user_id"
                ),
                CONSTRAINT "m_external_provider_identity_links_v1_provider_check"
                    CHECK ("provider" IN ('zulip')),
                CONSTRAINT "m_external_provider_identity_links_v1_kind_check"
                    CHECK ("link_kind" IN (
                        'verified_account_owner', 'provider_identity'
                    ))
            );

            CREATE INDEX
                "m_external_provider_identity_links_v1_workspace_user_idx"
                ON "m_external_provider_identity_links_v1" (
                    "workspace_user_uuid"
                );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP TABLE "m_external_provider_identity_links_v1";
            DROP INDEX "m_external_accounts_v2_provider_identity_key";
            ALTER TABLE "m_external_accounts_v2"
                DROP CONSTRAINT
                    "m_external_accounts_v2_provider_identity_pair_check",
                DROP COLUMN "provider_owner_user_id",
                DROP COLUMN "provider_realm_uuid";
            """
        )


migration_step = MigrationStep()
