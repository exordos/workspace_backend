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
        self._depends = ["0114-grant-SMTP-ingress-writer-gate-attester-access-c990ad.py"]

    @property
    def migration_id(self):
        return "c73e3516-9240-4c3e-803a-97deedad2721"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            DO $migration$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_roles
                    WHERE rolname = 'workspace_mail_gate'
                ) THEN
                    EXECUTE 'GRANT SELECT ON '
                        '"m_messenger_writer_gates_v1", '
                        '"m_messenger_writer_instances_v1", '
                        '"m_messenger_writer_gate_expected_v1", '
                        '"m_messenger_writer_gate_acks_v1" '
                        'TO "workspace_mail_gate"';
                    EXECUTE 'GRANT INSERT, UPDATE ON '
                        '"m_messenger_writer_instances_v1", '
                        '"m_messenger_writer_gate_acks_v1" '
                        'TO "workspace_mail_gate"';
                    EXECUTE format(
                        'GRANT CONNECT ON DATABASE %I TO "workspace_mail_gate"',
                        current_database()
                    );
                END IF;
            END
            $migration$;
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DO $migration$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_roles
                    WHERE rolname = 'workspace_mail_gate'
                ) THEN
                    EXECUTE format(
                        'REVOKE CONNECT ON DATABASE %I FROM "workspace_mail_gate"',
                        current_database()
                    );
                END IF;
            END
            $migration$;
            """
        )


migration_step = MigrationStep()
