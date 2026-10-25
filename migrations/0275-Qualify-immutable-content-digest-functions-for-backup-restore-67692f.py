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
            "0274-Add-resumable-bounded-Zulip-history-reconciliation-f75ecf.py"
        ]

    @property
    def migration_id(self):
        return "67692f82-050b-4ab9-8d35-50b3c921db02"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE OR REPLACE FUNCTION messenger_workspace_content_hash(p_workspace jsonb)
            RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
                SELECT encode(public.digest(convert_to(p_workspace::text,'UTF8'),'sha256'),'hex')
            $$;
            CREATE OR REPLACE FUNCTION messenger_provider_content_hash(p_provider text)
            RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
                SELECT encode(public.digest(convert_to(p_provider,'UTF8'),'sha256'),'hex')
            $$;
        """)

    def downgrade(self, session):
        # Explicit qualification preserves the same hashes on the older schema.
        pass


migration_step = MigrationStep()
