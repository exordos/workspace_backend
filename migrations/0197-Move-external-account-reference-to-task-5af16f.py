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
        self._depends = ["0196-Remove-external-account-revision-fcddce.py"]

    @property
    def migration_id(self):
        return "5af16fb0-315a-4075-bcc5-dcd31c127973"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE bridge_zulip_v5_tasks
                RENAME COLUMN account_uuid TO external_account_uuid;
            ALTER TABLE bridge_zulip_v5_tasks
                ALTER COLUMN external_account_uuid DROP EXPRESSION,
                ALTER COLUMN external_account_uuid SET NOT NULL;
            UPDATE bridge_zulip_v5_tasks
                SET content = content - 'external_account_uuid';
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            UPDATE bridge_zulip_v5_tasks
                SET content = content || jsonb_build_object(
                    'external_account_uuid', external_account_uuid::text
                );
            ALTER TABLE bridge_zulip_v5_tasks DROP COLUMN external_account_uuid;
            ALTER TABLE bridge_zulip_v5_tasks ADD COLUMN account_uuid UUID
                GENERATED ALWAYS AS (
                    (content->>'external_account_uuid')::UUID
                ) STORED REFERENCES m_external_accounts_v2 (uuid) ON DELETE CASCADE;
            CREATE INDEX bridge_zulip_v5_tasks_account_idx
                ON bridge_zulip_v5_tasks (account_uuid);
            """
        )


migration_step = MigrationStep()
