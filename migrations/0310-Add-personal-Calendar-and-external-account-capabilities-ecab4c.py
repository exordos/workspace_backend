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
        self._depends = ["0309-Add-canonical-Mail-API-resources-09e2dd.py"]

    @property
    def migration_id(self):
        return "ecab4c2b-9ba0-4f40-95eb-5635ab912047"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE m_external_accounts_v2 ADD COLUMN capabilities TEXT[]
                GENERATED ALWAYS AS (
                    CASE provider
                        WHEN 'zulip' THEN ARRAY['messenger']
                        WHEN 'mail' THEN ARRAY['mail']
                        WHEN 'calendar' THEN ARRAY['calendar']
                        WHEN 'google' THEN ARRAY['mail', 'calendar']
                        WHEN 'microsoft' THEN ARRAY['mail', 'calendar']
                        ELSE ARRAY[]::text[]
                    END
                ) STORED;

            CREATE TABLE m_workspace_calendars (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                owner_user_uuid UUID NOT NULL
                    REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
                external_account_uuid UUID
                    REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
                name VARCHAR(256) NOT NULL CHECK (length(name) > 0),
                "primary" BOOLEAN NOT NULL DEFAULT FALSE,
                read_only BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (project_id, owner_user_uuid, uuid)
            );
            CREATE INDEX m_workspace_calendars_owner_idx
                ON m_workspace_calendars (project_id, owner_user_uuid, uuid);

            CREATE TABLE m_workspace_calendar_events (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                owner_user_uuid UUID NOT NULL,
                calendar_uuid UUID NOT NULL,
                payload JSONB NOT NULL,
                starts_at TIMESTAMPTZ NOT NULL,
                ends_at TIMESTAMPTZ NOT NULL CHECK (ends_at > starts_at),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                FOREIGN KEY (project_id, owner_user_uuid, calendar_uuid)
                    REFERENCES m_workspace_calendars (project_id, owner_user_uuid, uuid)
                    ON DELETE CASCADE
            );
            CREATE INDEX m_workspace_calendar_events_owner_start_idx
                ON m_workspace_calendar_events (
                    project_id, owner_user_uuid, starts_at, uuid
                );
            CREATE INDEX m_workspace_calendar_events_calendar_start_idx
                ON m_workspace_calendar_events (calendar_uuid, starts_at, uuid);

            ALTER TABLE m_workspace_events
                DROP CONSTRAINT m_workspace_events_object_type_check,
                ADD CONSTRAINT m_workspace_events_object_type_check CHECK (
                    object_type IN (
                        'message', 'message_reaction', 'stream', 'stream_binding',
                        'topic', 'user', 'folder', 'folder_item', 'file',
                        'external_account', 'external_chat', 'external_operation',
                        'mail_folder', 'mail_message', 'mail_draft',
                        'calendar', 'calendar_event'
                    )
                );
        """)

    def downgrade(self, session):
        session.execute("""
            DELETE FROM m_workspace_events
                WHERE object_type IN ('calendar', 'calendar_event');
            ALTER TABLE m_workspace_events
                DROP CONSTRAINT m_workspace_events_object_type_check,
                ADD CONSTRAINT m_workspace_events_object_type_check CHECK (
                    object_type IN (
                        'message', 'message_reaction', 'stream', 'stream_binding',
                        'topic', 'user', 'folder', 'folder_item', 'file',
                        'external_account', 'external_chat', 'external_operation',
                        'mail_folder', 'mail_message', 'mail_draft'
                    )
                );
            DROP TABLE m_workspace_calendar_events;
            DROP TABLE m_workspace_calendars;
            ALTER TABLE m_external_accounts_v2 DROP COLUMN capabilities;
        """)


migration_step = MigrationStep()
