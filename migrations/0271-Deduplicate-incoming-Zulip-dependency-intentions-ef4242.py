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
            "0270-Release-unrelated-Zulip-messages-from-account-ordering-barriers-77af29.py"
        ]

    @property
    def migration_id(self):
        return "ef4242dd-fdb6-4545-9bad-4a80c129f9af"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE UNIQUE INDEX zulip_incoming_active_dependency
            ON zulip_incoming_events(project_id,provider_uuid,(payload->>'dependency_key'))
            WHERE payload->>'type'='ensure_dependency' AND processed_at IS NULL
        """)

    def downgrade(self, session):
        raise RuntimeError("Incoming dependency intention cutover is irreversible")


migration_step = MigrationStep()
