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
        self._depends = ["0254-Add-durable-Messenger-mutation-journal-6d29df.py"]

    @property
    def migration_id(self):
        return "9aa15268-8a91-48cd-9f0c-c41961e05359"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE FUNCTION bridge_zulip_v5_probe_flag_states(
                target_project uuid, target_state uuid,
                target_account uuid, target_flag text
            ) RETURNS SETOF bridge_zulip_v5_flag_states
            LANGUAGE SQL STABLE SET join_collapse_limit=1 AS $$
                SELECT * FROM bridge_zulip_v5_flag_states
                WHERE project_id=target_project AND state_uuid=target_state
                  AND (target_account IS NULL OR external_account_uuid=target_account)
                  AND (target_flag IS NULL OR flag=target_flag)
                ORDER BY external_account_uuid,flag
            $$;
        """)

    def downgrade(self, session):
        session.execute(
            "DROP FUNCTION bridge_zulip_v5_probe_flag_states(uuid,uuid,uuid,text)"
        )


migration_step = MigrationStep()
