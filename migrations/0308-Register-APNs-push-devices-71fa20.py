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
        self._depends = ["0307-Convert-legacy-mobile-push-device-arrays-5057f9.py"]

    @property
    def migration_id(self):
        return "71fa20f5-75d9-4351-bd60-e80da7538885"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE m_workspace_push_devices
                DROP CONSTRAINT m_workspace_push_devices_transport_check,
                ADD CONSTRAINT m_workspace_push_devices_transport_check
                    CHECK (transport IN ('fcm', 'apns', 'apns_sandbox'))
            """
        )

    def downgrade(self, session):
        # Older versions cannot read APNs registrations. Clients re-register on upgrade.
        session.execute(
            "DELETE FROM m_workspace_push_devices WHERE transport IN ('apns', 'apns_sandbox')"
        )
        session.execute(
            """
            ALTER TABLE m_workspace_push_devices
                DROP CONSTRAINT m_workspace_push_devices_transport_check,
                ADD CONSTRAINT m_workspace_push_devices_transport_check
                    CHECK (transport = 'fcm')
            """
        )


migration_step = MigrationStep()
