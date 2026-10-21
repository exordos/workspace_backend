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

from migrations import zulip_upgrade_data


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0246-Prefer-Zulip-realtime-queues-over-account-and-periodic-rescans-0060ed.py"
        ]

    @property
    def migration_id(self):
        return "a6c9f7a9-9442-47cf-8c62-c364fd390f51"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        zulip_upgrade_data.prepare(session)

    def downgrade(self, session):
        if session.execute(
            "SELECT to_regclass('public.workspace_zulip_v5_legacy_state') IS NOT NULL "
            "OR to_regclass('public.workspace_zulip_v5_legacy_archive') IS NOT NULL AS captured"
        ).fetchone()["captured"]:
            raise RuntimeError(
                "Restore the coordinated pre-upgrade checkpoint to undo the legacy handoff"
            )


migration_step = MigrationStep()
