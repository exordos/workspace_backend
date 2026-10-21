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
            "0210-Hide-rebound-Zulip-profiles-from-the-user-directory-751a1a.py"
        ]

    @property
    def migration_id(self):
        return "237f1b00-ea60-4d38-8329-897681d67189"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE messenger_streams
                DROP CONSTRAINT messenger_streams_source_name_check;
            ALTER TABLE messenger_streams
                ADD CONSTRAINT messenger_streams_source_name_check
                CHECK (source_name IN ('native', 'zulip'));

            CREATE VIEW bridge_zulip_v5_stream_states AS
            WITH zulip_streams AS (
                SELECT DISTINCT account.project_id, channel.stream_uuid
                FROM bridge_zulip_v5_channels AS channel
                JOIN m_external_accounts_v2 AS account
                    ON account.uuid = channel.external_account_uuid
                WHERE account.provider = 'zulip'
            ), workspace_streams AS (
                SELECT project_id, uuid AS stream_uuid, source_name
                FROM messenger_streams
                WHERE deleted_at IS NULL
            )
            SELECT
                COALESCE(zulip.project_id, workspace.project_id) AS project_id,
                COALESCE(zulip.stream_uuid, workspace.stream_uuid) AS stream_uuid,
                CASE
                    WHEN workspace.stream_uuid IS NULL THEN 'create'
                    ELSE 'delete'
                END AS action
            FROM zulip_streams AS zulip
            FULL JOIN workspace_streams AS workspace
                ON workspace.project_id = zulip.project_id
               AND workspace.stream_uuid = zulip.stream_uuid
            WHERE workspace.stream_uuid IS NULL
               OR (
                   zulip.stream_uuid IS NULL
                   AND workspace.source_name = 'zulip'
               );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP VIEW bridge_zulip_v5_stream_states;
            UPDATE messenger_streams
                SET source_name = 'native', source = '{"kind":"native"}'::jsonb
                WHERE source_name = 'zulip';
            ALTER TABLE messenger_streams
                DROP CONSTRAINT messenger_streams_source_name_check;
            ALTER TABLE messenger_streams
                ADD CONSTRAINT messenger_streams_source_name_check
                CHECK (source_name = 'native');
            """
        )


migration_step = MigrationStep()
