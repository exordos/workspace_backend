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
        self._depends = ["0117-scope-external-projections-by-account-4a9279.py"]

    @property
    def migration_id(self):
        return "5c4ae023-56c1-442c-b45a-8068c0c2fa68"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS "m_workspace_push_devices" (
                "uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "user_uuid" UUID NOT NULL,
                "transport" VARCHAR(16) NOT NULL,
                "platform" VARCHAR(16) NOT NULL,
                "registration_token" TEXT NOT NULL,
                "encryption" JSONB NOT NULL,
                "created_at" TIMESTAMP(6) WITH TIME ZONE
                    NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) WITH TIME ZONE
                    NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_push_devices_user_uuid_fkey"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_push_devices_transport_check"
                    CHECK ("transport" IN ('fcm')),
                CONSTRAINT "m_workspace_push_devices_platform_check"
                    CHECK ("platform" IN ('android', 'ios')),
                CONSTRAINT "m_workspace_push_devices_token_check"
                    CHECK (
                        LENGTH("registration_token") BETWEEN 1 AND 4096
                    ),
                CONSTRAINT "m_workspace_push_devices_encryption_check"
                    CHECK (
                        jsonb_typeof("encryption") = 'object'
                        AND "encryption"->>'kind' = 'HPKE'
                        AND "encryption"->>'algorithm' =
                            'HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM'
                        AND "encryption" ? 'key_uuid'
                        AND "encryption" ? 'public_key'
                        AND "encryption"->>'public_key'
                            ~ '^[A-Za-z0-9_-]{43}$'
                    )
            );
            """
        )

    def downgrade(self, session):
        self._delete_table_if_exists(
            session,
            "m_workspace_push_devices",
        )


migration_step = MigrationStep()
