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
        self._depends = ["0310-Add-personal-Calendar-resources-ecab4c.py"]

    @property
    def migration_id(self):
        return "7330e17a-02ed-49be-8ff7-029579ea944b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TABLE groupware_account_credentials (
                external_account_uuid UUID NOT NULL
                    REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
                service VARCHAR(8) NOT NULL CHECK (service IN ('mail', 'calendar')),
                encrypted_password BYTEA NOT NULL,
                next_sync_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (external_account_uuid, service)
            );
            CREATE INDEX groupware_credentials_due_idx
                ON groupware_account_credentials (next_sync_at, external_account_uuid);
            CREATE TABLE groupware_mailbox_progress (
                folder_uuid UUID PRIMARY KEY REFERENCES m_mail_folders(uuid) ON DELETE CASCADE,
                uid_validity BIGINT NOT NULL,
                last_uid BIGINT NOT NULL
            );
            CREATE TABLE groupware_calendar_bindings (
                calendar_uuid UUID PRIMARY KEY REFERENCES m_workspace_calendars(uuid) ON DELETE CASCADE,
                remote_path TEXT NOT NULL
            );
            CREATE TABLE groupware_calendar_event_bindings (
                event_uuid UUID PRIMARY KEY REFERENCES m_workspace_calendar_events(uuid) ON DELETE CASCADE,
                calendar_uuid UUID NOT NULL REFERENCES groupware_calendar_bindings(calendar_uuid) ON DELETE CASCADE,
                remote_path TEXT NOT NULL,
                etag TEXT NOT NULL,
                data TEXT NOT NULL,
                UNIQUE (calendar_uuid, remote_path)
            );
        """)

    def downgrade(self, session):
        session.execute("""
            DROP TABLE groupware_calendar_event_bindings;
            DROP TABLE groupware_calendar_bindings;
            DROP TABLE groupware_mailbox_progress;
            DROP TABLE groupware_account_credentials;
        """)


migration_step = MigrationStep()
