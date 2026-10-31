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


FUNCTION_SQL = "SELECT pg_get_functiondef('messenger_initialize_message_flags()'::regprocedure) AS definition"
BODY_START = "\nBEGIN\n"
HISTORY_GUARD = """    -- History merges its complete, prepared sparse flags and queues counters
    -- in the same transaction. Do not create temporary defaults or provider
    -- repairs which only repeat that work against the growing message store.
    IF current_setting('workspace.zulip_history',true)='on' THEN
        RETURN NULL;
    END IF;
"""


def replace_guard(session, before, after):
    definition = session.execute(FUNCTION_SQL).fetchone()["definition"]
    if definition.count(before) != 1:
        raise RuntimeError("Unexpected message flag initialization definition")
    session.execute(definition.replace(before, after, 1))


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0287-Cache-successful-Zulip-history-blocks-c0769e.py"]

    @property
    def migration_id(self):
        return "d89bb343-2f29-48ca-bada-16a887e7562b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        replace_guard(session, BODY_START, BODY_START + HISTORY_GUARD)

    def downgrade(self, session):
        replace_guard(session, BODY_START + HISTORY_GUARD, BODY_START)


migration_step = MigrationStep()
