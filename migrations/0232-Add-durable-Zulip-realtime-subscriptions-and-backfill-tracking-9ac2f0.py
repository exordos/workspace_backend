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
            "0231-Retire-duplicate-Messenger-compatibility-tables-bc9678.py"
        ]

    @property
    def migration_id(self):
        return "9ac2f0da-c982-43e0-ba71-75747e3e741f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE m_external_accounts_v2
                ADD COLUMN has_completed_backfill BOOLEAN NOT NULL DEFAULT FALSE;
            UPDATE m_external_accounts_v2 SET has_completed_backfill = TRUE
                WHERE provider = 'zulip' AND status = 'backfill';
            CREATE TABLE bridge_zulip_v5_subscriptions (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                external_account_uuid UUID NOT NULL UNIQUE,
                generation UUID NOT NULL,
                queue_id TEXT NOT NULL,
                last_event_id BIGINT NOT NULL CHECK (last_event_id >= -1),
                account_revision TEXT NOT NULL,
                longpoll_timeout INTEGER NOT NULL CHECK (longpoll_timeout > 0),
                created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                FOREIGN KEY (external_account_uuid, project_id)
                    REFERENCES m_external_accounts_v2(uuid, project_id) ON DELETE CASCADE
            );
            CREATE TABLE bridge_zulip_v5_events (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                external_account_uuid UUID NOT NULL,
                generation UUID NOT NULL,
                queue_id TEXT NOT NULL,
                event_id BIGINT,
                source TEXT NOT NULL CHECK (source IN ('zulip', 'internal')),
                payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
                processed_at TIMESTAMP WITHOUT TIME ZONE,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TIMESTAMP WITHOUT TIME ZONE,
                error_description TEXT,
                created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                UNIQUE (external_account_uuid, generation, event_id),
                FOREIGN KEY (external_account_uuid, project_id)
                    REFERENCES m_external_accounts_v2(uuid, project_id) ON DELETE CASCADE
            );
            CREATE INDEX bridge_zulip_v5_events_pending_idx
                ON bridge_zulip_v5_events(created_at, uuid)
                WHERE processed_at IS NULL;
            ALTER TABLE bridge_zulip_v5_tasks ADD COLUMN backfill_uuid UUID
                REFERENCES bridge_zulip_v5_events(uuid) ON DELETE CASCADE;
            ALTER TABLE bridge_zulip_v5_tasks ADD COLUMN realtime_event_uuid UUID
                REFERENCES bridge_zulip_v5_events(uuid) ON DELETE CASCADE;
            CREATE INDEX bridge_zulip_v5_tasks_backfill_idx
                ON bridge_zulip_v5_tasks(backfill_uuid) WHERE backfill_uuid IS NOT NULL;
            UPDATE bridge_zulip_v5_tasks
                SET content = content || '{"last_message_id": 0}'::jsonb,
                    status = CASE WHEN status IN ('new', 'waiting', 'retry')
                        THEN 'cancel' ELSE status END
                WHERE content->>'kind' = 'full_backfill'
                    AND NOT (content ? 'last_message_id');
            CREATE FUNCTION bridge_zulip_v5_wake_realtime() RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify('workspace_zulip_realtime', TG_ARGV[0]);
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_accounts_changed
                AFTER INSERT OR UPDATE OR DELETE ON m_external_accounts_v2
                FOR EACH STATEMENT EXECUTE FUNCTION bridge_zulip_v5_wake_realtime('accounts');
            CREATE TRIGGER bridge_zulip_v5_events_enqueued
                AFTER INSERT ON bridge_zulip_v5_events
                FOR EACH STATEMENT EXECUTE FUNCTION bridge_zulip_v5_wake_realtime('events');
        """)

    def downgrade(self, session):
        session.execute("""
            DROP TRIGGER bridge_zulip_v5_accounts_changed ON m_external_accounts_v2;
            DROP TRIGGER bridge_zulip_v5_events_enqueued ON bridge_zulip_v5_events;
            DROP FUNCTION bridge_zulip_v5_wake_realtime();
            ALTER TABLE bridge_zulip_v5_tasks DROP COLUMN backfill_uuid,
                DROP COLUMN realtime_event_uuid;
            DROP TABLE bridge_zulip_v5_events;
            DROP TABLE bridge_zulip_v5_subscriptions;
            ALTER TABLE m_external_accounts_v2 DROP COLUMN has_completed_backfill;
        """)


migration_step = MigrationStep()
