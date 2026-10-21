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
            "0247-Preserve-legacy-Zulip-identities-during-automatic-upgrades-a6c9f7.py"
        ]

    @property
    def migration_id(self):
        return "e378c736-4e2f-4400-b4a7-b58181841226"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE OR REPLACE FUNCTION bridge_zulip_v5_subscribe_workspace_events() RETURNS trigger AS $$
            BEGIN
                -- Capture acknowledged outgoing routes in the same transaction
                -- as DELETE, before projection workers purge the native root.
                UPDATE messenger_domain_outbox_events AS event
                SET payload = event.payload || jsonb_build_object('zulip_deletions', (
                    SELECT COALESCE(jsonb_agg(jsonb_build_object(
                        'delivery_uuid', delivery.uuid,
                        'external_account_uuid', delivery.external_account_uuid,
                        'content', jsonb_build_object(
                            'kind', 'delete_message',
                            'message_id', delivery.zulip_message_id,
                            'hostname', delivery.hostname,
                            'external_account_id', delivery.external_account_id,
                            'author_uuid', account.owner_user_uuid
                        )
                    )), '[]'::jsonb)
                    FROM bridge_zulip_v5_deliveries AS delivery
                    JOIN m_external_accounts_v2 AS account
                      ON account.uuid=delivery.external_account_uuid
                     AND account.project_id=delivery.project_id
                    WHERE delivery.project_id=event.project_id
                      AND delivery.message_uuid=(event.payload->>'canonical_message_uuid')::uuid
                      AND delivery.stream_uuid=(event.payload->'placement'->>'stream_uuid')::uuid
                      AND delivery.zulip_message_id IS NOT NULL
                      AND account.owner_user_uuid=(event.payload->>'author_uuid')::uuid
                      AND event.payload->>'source_name'='native'
                ))
                FROM new_outbox_events AS inserted
                WHERE event.uuid=inserted.uuid AND event.project_id=inserted.project_id
                  AND inserted.event_kind='delivery_snapshot_event'
                  AND inserted.payload->>'source_kind'='message.deleted';
                INSERT INTO bridge_zulip_v5_workspace_events(uuid, project_id)
                SELECT uuid, project_id FROM new_outbox_events
                WHERE (event_kind='fanout' AND payload->>'source_kind'='message.created')
                   OR (event_kind='content_mentions' AND payload->>'source_kind'='message.updated')
                   OR (event_kind='delivery_snapshot_event' AND payload->>'source_kind'='message.deleted');
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
        """)

    def downgrade(self, session):
        session.execute("""
            CREATE OR REPLACE FUNCTION bridge_zulip_v5_subscribe_workspace_events() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_workspace_events(uuid, project_id)
                SELECT uuid, project_id FROM new_outbox_events
                WHERE (event_kind='fanout' AND payload->>'source_kind'='message.created')
                   OR (event_kind='content_mentions' AND payload->>'source_kind'='message.updated');
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
        """)


migration_step = MigrationStep()
