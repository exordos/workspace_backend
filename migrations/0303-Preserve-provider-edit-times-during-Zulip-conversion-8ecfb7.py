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


FUNCTION_SQL = "SELECT pg_get_functiondef('messenger_track_content_version()'::regprocedure) AS definition"
PREVIOUS_GUARD = """NEW.content_updated_at IS NULL
                OR NEW.content_updated_at IS NOT DISTINCT FROM OLD.content_updated_at"""
PROVIDER_GUARD = """NEW.content_updated_at IS NULL
                OR (NEW.content_updated_at IS NOT DISTINCT FROM OLD.content_updated_at
                    AND current_setting('workspace.zulip_incoming',true) IS DISTINCT FROM 'on')"""


def replace_guard(session, before, after):
    definition = session.execute(FUNCTION_SQL).fetchone()["definition"]
    if definition.count(before) != 1:
        raise RuntimeError("Unexpected message content version trigger definition")
    session.execute(definition.replace(before, after, 1))


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0302-Synchronize-compatible-Zulip-polls-40ec4a.py"
        ]

    @property
    def migration_id(self):
        return "8ecfb74c-6005-48f7-ac81-5ff731ec96c7"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        replace_guard(session, PREVIOUS_GUARD, PROVIDER_GUARD)

    def downgrade(self, session):
        replace_guard(session, PROVIDER_GUARD, PREVIOUS_GUARD)


migration_step = MigrationStep()
