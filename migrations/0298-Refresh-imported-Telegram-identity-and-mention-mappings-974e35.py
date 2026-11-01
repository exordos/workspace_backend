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
            "0297-Synchronize-Telegram-stream-notification-preferences-bd22b4.py"
        ]

    @property
    def migration_id(self):
        return "974e3503-2ecd-407e-a329-79f0fa91b1ab"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            WITH identities AS (
                SELECT account.project_id,account.provider_uuid,split_part(peer.peer_key,':',2) AS remote
                FROM telegram_peer_state peer JOIN m_external_accounts_v2 account ON account.uuid=peer.external_account_uuid
                WHERE peer.peer_kind='user'
                UNION
                SELECT account.project_id,account.provider_uuid,event.payload->>'sender_id'
                FROM telegram_incoming_events event JOIN m_external_accounts_v2 account ON account.uuid=event.external_account_uuid
                WHERE event.payload->>'sender_id' IS NOT NULL
                UNION
                SELECT account.project_id,account.provider_uuid,actor->>'user_id'
                FROM telegram_incoming_events event JOIN m_external_accounts_v2 account ON account.uuid=event.external_account_uuid
                CROSS JOIN LATERAL jsonb_array_elements(coalesce(event.payload->'reaction_actors','[]')) actor
                WHERE actor->>'user_id' IS NOT NULL
            )
            INSERT INTO m_external_accounts_v2(uuid,project_id,owner_user_uuid,provider,provider_uuid,external_account_id,
                status,settings,disabled,created_at,updated_at)
            SELECT messenger_uuid_v5(identity.project_id,identity.provider_uuid::text||':'||identity.remote),
                identity.project_id,person.uuid,'telegram',identity.provider_uuid,identity.remote,
                'active','{}',person.disabled,now(),now()
            FROM identities identity JOIN m_workspace_users person
                ON person.uuid=messenger_uuid_v5(identity.provider_uuid,'user:'||identity.remote) AND person.source='telegram'
            ON CONFLICT DO NOTHING;
            DELETE FROM telegram_history_batches;
            UPDATE telegram_peer_state SET history_before_id=0,history_done=false,
                history_started_at=NULL,first_message_at=NULL,history_finished_at=NULL,
                imported_messages=0,imported_files=0,imported_bytes=0,
                fetch_seconds=0,merge_seconds=0,file_seconds=0,updated_at=now()
            WHERE history_before_id>0 OR imported_messages>0 OR history_done;
        """)

    def downgrade(self, session):
        # Keep canonical messages, files, and imported identity mappings.
        pass


migration_step = MigrationStep()
