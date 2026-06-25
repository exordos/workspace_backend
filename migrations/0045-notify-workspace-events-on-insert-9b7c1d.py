#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
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
        self._depends = ["0044-unique-user-stream-bindings-4d8b2a.py"]

    @property
    def migration_id(self):
        return "9b7c1d2e-4f6a-4c8b-9d10-2e3f4a5b6c7d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE OR REPLACE FUNCTION notify_workspace_event_created()
            RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify('workspace_events', NEW.epoch_version::text);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """,
            """
            DROP TRIGGER IF EXISTS m_workspace_events_notify_created
                ON "m_workspace_events";
            """,
            """
            CREATE TRIGGER m_workspace_events_notify_created
                AFTER INSERT ON "m_workspace_events"
                FOR EACH ROW
                EXECUTE FUNCTION notify_workspace_event_created();
            """,
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            """
            DROP TRIGGER IF EXISTS m_workspace_events_notify_created
                ON "m_workspace_events";
            """,
            """
            DROP FUNCTION IF EXISTS notify_workspace_event_created();
            """,
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
