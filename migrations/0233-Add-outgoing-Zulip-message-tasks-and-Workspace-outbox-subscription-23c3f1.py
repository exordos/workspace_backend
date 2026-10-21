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
            "0232-Add-durable-Zulip-realtime-subscriptions-and-backfill-tracking-9ac2f0.py"
        ]

    @property
    def migration_id(self):
        return "23c3f186-0e3d-438e-88f2-df7352944a6b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TABLE bridge_zulip_v5_deliveries (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                message_uuid UUID NOT NULL,
                stream_uuid UUID NOT NULL,
                external_account_uuid UUID NOT NULL,
                hostname TEXT NOT NULL,
                external_account_id TEXT NOT NULL,
                recipient JSONB NOT NULL,
                topic_name TEXT,
                sending BOOLEAN NOT NULL DEFAULT FALSE,
                zulip_message_id BIGINT,
                delivered_at TIMESTAMP WITHOUT TIME ZONE,
                created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                UNIQUE (project_id, message_uuid, stream_uuid),
                UNIQUE (project_id, hostname, zulip_message_id),
                FOREIGN KEY (project_id, message_uuid)
                    REFERENCES messenger_messages(project_id, uuid) ON DELETE CASCADE,
                FOREIGN KEY (project_id, stream_uuid)
                    REFERENCES messenger_streams(project_id, uuid) ON DELETE CASCADE,
                FOREIGN KEY (external_account_uuid, project_id)
                    REFERENCES m_external_accounts_v2(uuid, project_id) ON DELETE CASCADE
            );
            CREATE INDEX bridge_zulip_v5_deliveries_remote_idx
                ON bridge_zulip_v5_deliveries(project_id, zulip_message_id);
            ALTER TABLE bridge_zulip_v5_tasks ADD COLUMN delivery_uuid UUID
                GENERATED ALWAYS AS (CASE WHEN content->>'kind' IN ('send_message','edit_message')
                    THEN (content->>'delivery_uuid')::uuid END) STORED
                REFERENCES bridge_zulip_v5_deliveries(uuid) ON DELETE CASCADE;
            CREATE TABLE bridge_zulip_v5_workspace_events (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                processed_at TIMESTAMP WITHOUT TIME ZONE,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TIMESTAMP WITHOUT TIME ZONE,
                error_description TEXT,
                created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                FOREIGN KEY (project_id, uuid)
                    REFERENCES messenger_domain_outbox_events(project_id, uuid) ON DELETE CASCADE
            );
            CREATE INDEX bridge_zulip_v5_workspace_events_pending_idx
                ON bridge_zulip_v5_workspace_events(created_at, uuid)
                WHERE processed_at IS NULL;
            CREATE FUNCTION bridge_zulip_v5_subscribe_workspace_events() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_workspace_events(uuid, project_id)
                SELECT uuid, project_id FROM new_outbox_events
                WHERE (event_kind='fanout' AND payload->>'source_kind'='message.created')
                   OR (event_kind='content_mentions' AND payload->>'source_kind'='message.updated');
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_subscribe_workspace_events
                AFTER INSERT ON messenger_domain_outbox_events
                REFERENCING NEW TABLE AS new_outbox_events
                FOR EACH STATEMENT EXECUTE FUNCTION bridge_zulip_v5_subscribe_workspace_events();
            UPDATE bridge_zulip_v5_tasks SET priority=450000
                WHERE content->>'kind'='import_channels' AND status IN ('new','waiting','retry');
        """)

    def downgrade(self, session):
        session.execute("""
            DROP TRIGGER bridge_zulip_v5_subscribe_workspace_events ON messenger_domain_outbox_events;
            DROP FUNCTION bridge_zulip_v5_subscribe_workspace_events();
            DELETE FROM bridge_zulip_v5_deliveries;
            ALTER TABLE bridge_zulip_v5_tasks DROP COLUMN delivery_uuid;
            DROP TABLE bridge_zulip_v5_workspace_events;
            DROP TABLE bridge_zulip_v5_deliveries;
            UPDATE bridge_zulip_v5_tasks SET priority=50000
                WHERE content->>'kind'='import_channels' AND status IN ('new','waiting','retry');
        """)


migration_step = MigrationStep()
