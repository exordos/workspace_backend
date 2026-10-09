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
            "0184-Merge-sticker-catalog-and-messenger-migrations-76cc47.py"
        ]

    @property
    def migration_id(self):
        return "c67142e8-3c76-45c5-9421-095d9f2f4729"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE messenger_sticker_cleanup_tasks (
                sticker_uuid uuid PRIMARY KEY,
                storage_object_id varchar(1024) NOT NULL,
                status varchar(16) NOT NULL DEFAULT 'pending',
                attempts integer NOT NULL DEFAULT 0,
                safe_error text,
                lease_owner varchar(255),
                lease_expires_at timestamp with time zone,
                next_retry_at timestamp with time zone NOT NULL DEFAULT now(),
                created_at timestamp with time zone NOT NULL DEFAULT now(),
                updated_at timestamp with time zone NOT NULL DEFAULT now(),
                CHECK (status IN ('pending', 'running', 'completed', 'failed')),
                CHECK (attempts >= 0)
            );
            CREATE INDEX messenger_sticker_cleanup_ready_idx
                ON messenger_sticker_cleanup_tasks (
                    status, next_retry_at, lease_expires_at,
                    created_at, sticker_uuid
                ) WHERE status IN ('pending', 'running', 'failed');

            REVOKE ALL ON TABLE messenger_sticker_cleanup_tasks FROM PUBLIC;

            DO $migration$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'workspace') THEN
                    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE '
                        'messenger_sticker_cleanup_tasks TO workspace';
                END IF;
            END
            $migration$;
            """
        )

    def downgrade(self, session):
        session.execute(
            "LOCK TABLE messenger_sticker_cleanup_tasks IN ACCESS EXCLUSIVE MODE"
        )
        unfinished = session.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM messenger_sticker_cleanup_tasks
                WHERE status IN ('pending', 'running', 'failed')
            ) AS unfinished
            """
        ).fetchone()["unfinished"]
        if unfinished:
            raise RuntimeError(
                "Sticker cleanup tasks must complete before rolling back migration 0185"
            )
        session.execute(
            """
            DO $migration$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'workspace') THEN
                    EXECUTE 'REVOKE ALL ON TABLE '
                        'messenger_sticker_cleanup_tasks FROM workspace';
                END IF;
            END
            $migration$;

            DROP TABLE messenger_sticker_cleanup_tasks;
            """
        )


migration_step = MigrationStep()
