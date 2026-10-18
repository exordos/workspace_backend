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
        self._depends = ["0216-Add-Zulip-topic-reconciliation-view-140730.py"]

    @property
    def migration_id(self):
        return "5fa84f49-fbf9-4f15-9c77-b0d39a94527a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE VIEW bridge_zulip_v5_stream_binding_states AS
            WITH zulip_bindings AS (
                SELECT DISTINCT account.project_id, channel.stream_uuid,
                       account.owner_user_uuid AS user_uuid
                FROM bridge_zulip_v5_channels AS channel
                JOIN m_external_accounts_v2 AS account
                  ON account.uuid = channel.external_account_uuid
                JOIN messenger_streams AS stream
                  ON stream.project_id = account.project_id
                 AND stream.uuid = channel.stream_uuid
                WHERE account.provider = 'zulip'
                  AND stream.source_name = 'zulip'
                  AND stream.deleted_at IS NULL
            ), workspace_bindings AS (
                SELECT binding.project_id, binding.stream_uuid,
                       binding.user_uuid, binding.uuid, binding.active
                FROM messenger_stream_bindings AS binding
                JOIN messenger_streams AS stream
                  ON stream.project_id = binding.project_id
                 AND stream.uuid = binding.stream_uuid
                WHERE stream.source_name = 'zulip'
                  AND stream.deleted_at IS NULL
            )
            SELECT
                COALESCE(zulip.project_id, workspace.project_id) AS project_id,
                COALESCE(zulip.stream_uuid, workspace.stream_uuid) AS stream_uuid,
                COALESCE(zulip.user_uuid, workspace.user_uuid) AS user_uuid,
                COALESCE(
                    workspace.uuid,
                    messenger_uuid_v5(
                        zulip.project_id,
                        'zulip-binding:' || zulip.stream_uuid::text || ':' || zulip.user_uuid::text
                    )
                ) AS binding_uuid,
                CASE
                    WHEN workspace.uuid IS NULL OR NOT workspace.active THEN 'create'
                    ELSE 'delete'
                END AS action
            FROM zulip_bindings AS zulip
            FULL JOIN workspace_bindings AS workspace
              ON workspace.project_id = zulip.project_id
             AND workspace.stream_uuid = zulip.stream_uuid
             AND workspace.user_uuid = zulip.user_uuid
            WHERE (
                zulip.user_uuid IS NOT NULL
                AND (workspace.uuid IS NULL OR NOT workspace.active)
            ) OR (
                zulip.user_uuid IS NULL AND workspace.active
            );
            """
        )

    def downgrade(self, session):
        session.execute("DROP VIEW bridge_zulip_v5_stream_binding_states;")


migration_step = MigrationStep()
