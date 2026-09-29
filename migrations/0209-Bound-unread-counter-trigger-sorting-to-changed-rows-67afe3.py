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


# The baseline disables sorts to keep its UUID cursor pages on their indexes.
# Its trigger queries must instead sort the finite changed-row join. Inheriting
# enable_sort=off makes PostgreSQL scan all parent rows in index order and join
# each one against the transition table, even when that transition is empty.
# Retain the same deterministic lock order, with ordinary bounded sorts.
FUNCTIONS = ("publish_topic_unread_state",) + tuple(
    f"{table}_unread_delta_{operation}"
    for table in ("unread_contributions", "topic_bindings", "folder_items")
    for operation in ("insert", "update", "delete")
)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0208-Keep-unread-baseline-snapshots-out-of-the-legacy-projection-queue-ec0e37.py"
        ]

    @property
    def migration_id(self):
        return "67afe3ba-7d19-4ada-aa96-b453531acadc"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        for function in FUNCTIONS:
            session.execute(
                f"ALTER FUNCTION workspace_v3.{function}() SET enable_sort=on"
            )

    def downgrade(self, session):
        for function in FUNCTIONS:
            session.execute(
                f"ALTER FUNCTION workspace_v3.{function}() RESET enable_sort"
            )


migration_step = MigrationStep()
