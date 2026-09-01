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

import importlib.util
import pathlib

from restalchemy.storage.sql import migrations


def _provider_label_upgrade_sql() -> str:
    """Load the canonical private-chat repair without duplicating its views."""
    migration_path = pathlib.Path(__file__).with_name(
        "0164-stabilize-provider-private-chat-labels-f8bd03.py"
    )
    spec = importlib.util.spec_from_file_location(
        "workspace_provider_private_chat_labels_0164",
        migration_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load provider private-chat label repair")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.UPGRADE_SQL


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0168-reconcile-compact-topic-message-stats-7b45ba.py"]

    @property
    def migration_id(self):
        return "485d6246-c577-4564-83d7-da856015b8d2"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Replaying 0164 repairs existing installations and refreshes the two
        # public stream views after provider labels became authoritative. A
        # fresh full-chain migration can still hold 0164's ON COMMIT DROP
        # scratch table in the same transaction.
        session.execute("DROP TABLE IF EXISTS messenger_v2_provider_group_streams")
        session.execute(_provider_label_upgrade_sql())

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
