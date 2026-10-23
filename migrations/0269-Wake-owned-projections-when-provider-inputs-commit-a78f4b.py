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



TABLES = (
    "bridge_zulip_v5_message_changes",
    "messenger_message_changes",
    "bridge_zulip_v5_flag_changes",
    "bridge_zulip_v5_reaction_changes",
)


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0268-Avoid-redundant-owned-placement-ingress-topic-invalidations-db98da.py"]

    @property
    def migration_id(self):
        return "a78f4b56-e06e-43d1-b1a3-5db5da2469fc"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        for table in TABLES:
            session.execute(f"""CREATE TRIGGER {table}_wake_owned
                AFTER INSERT OR UPDATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION messenger_notify_mutation_command()""")

    def downgrade(self, session):
        for table in TABLES:
            session.execute(f"DROP TRIGGER {table}_wake_owned ON {table}")


migration_step = MigrationStep()
