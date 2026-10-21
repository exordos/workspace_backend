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
        self._depends = ["0190-add-Zulip-v5-bridge-models-1f232b.py"]

    @property
    def migration_id(self):
        return "6c9e6de2-bb58-40c5-8fda-aa48879a373b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_external_accounts_v2"
                ADD COLUMN "status" VARCHAR(16) NOT NULL DEFAULT 'new'
                    CHECK ("status" IN (
                        'new', 'backfill', 'fast_backfill', 'active', 'error'
                    )),
                ADD COLUMN "error_description" TEXT;
            """
        )

    def downgrade(self, session):
        session.execute(
            'ALTER TABLE "m_external_accounts_v2" '
            'DROP COLUMN "error_description", DROP COLUMN "status";'
        )


migration_step = MigrationStep()
