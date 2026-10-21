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
        self._depends = ["0188-Skip-tombstoned-streams-in-binding-mirrors-fd5982.py"]

    @property
    def migration_id(self):
        return "140dd576-35e2-4655-919f-c861caf3cd00"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE messenger_push_outbox (
                uuid uuid PRIMARY KEY, project_id uuid NOT NULL,
                user_uuid uuid NOT NULL, message_uuid uuid NOT NULL,
                event jsonb NOT NULL, terminal boolean NOT NULL DEFAULT false,
                error_code varchar(64), created_at timestamptz NOT NULL DEFAULT NOW(),
                expires_at timestamptz NOT NULL DEFAULT NOW() + INTERVAL '1 hour',
                next_attempt_at timestamptz NOT NULL DEFAULT NOW()
            );
            CREATE INDEX messenger_push_outbox_due ON messenger_push_outbox (next_attempt_at) WHERE NOT terminal;
            CREATE INDEX messenger_push_outbox_owner ON messenger_push_outbox (project_id, user_uuid);
        """,
            (),
        )

    def downgrade(self, session):
        session.execute("DROP TABLE messenger_push_outbox", ())


migration_step = MigrationStep()
