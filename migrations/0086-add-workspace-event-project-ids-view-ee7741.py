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
        self._depends = ["0085-allow-workspace-message-reaction-events-e371e2.py"]

    @property
    def migration_id(self):
        return "ee7741ae-30ec-4552-9f03-dc046fcd0e74"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW "m_workspace_projects_view" AS
            SELECT "project_id"
            FROM "m_workspace_stream_bindings"
            UNION
            SELECT "project_id"
            FROM "m_workspace_events";
            """
        )

    def downgrade(self, session):
        session.execute('DROP VIEW IF EXISTS "m_workspace_projects_view";')


migration_step = MigrationStep()
