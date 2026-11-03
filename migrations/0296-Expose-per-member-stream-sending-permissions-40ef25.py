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
            "0295-Isolate-Telegram-history-and-catalog-retry-deadlines-c13028.py"
        ]

    @property
    def migration_id(self):
        return "40ef2533-e934-4165-a8a9-28ab4c13311c"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE messenger_stream_bindings ADD COLUMN can_send boolean NOT NULL DEFAULT true;
            ALTER TABLE messenger_streams ADD COLUMN announcement_only boolean NOT NULL DEFAULT false;
            ALTER TABLE telegram_peer_state ADD COLUMN broadcast boolean NOT NULL DEFAULT false;
            UPDATE messenger_stream_bindings binding SET can_send=peer.can_send
            FROM telegram_peer_state peer,m_external_accounts_v2 account
            WHERE account.uuid=peer.external_account_uuid
              AND binding.project_id=account.project_id AND binding.user_uuid=account.owner_user_uuid
              AND binding.stream_uuid=peer.stream_uuid AND binding.source_name='telegram'
        """)
        # Append a viewer capability while retaining the complete current projection.
        definition = (
            session.execute(
                "SELECT pg_get_viewdef('messenger_api_user_streams_v1'::regclass,true) AS definition"
            )
            .fetchone()["definition"]
            .rstrip()
            .rstrip(";")
        )
        session.execute(f"""
            CREATE OR REPLACE VIEW messenger_api_user_streams_v1 AS
            SELECT visible.*, stream.announcement_only, binding.can_send AND (
                visible.source_name <> 'native' OR NOT stream.announcement_only
                OR visible.role IN ('owner','administrator','moderator')
            ) AND (visible.source_name='native' OR EXISTS(
                SELECT 1 FROM m_external_accounts_v2 account
                LEFT JOIN telegram_account_state state ON state.external_account_uuid=account.uuid
                WHERE account.project_id=visible.project_id AND account.owner_user_uuid=visible.user_uuid
                  AND account.provider=visible.source_name AND account.provider_uuid=visible.provider_uuid
                  AND NOT account.disabled AND account.external_account_id IS NOT NULL
                  AND ((account.provider='telegram' AND state.auth_state='authorized' AND NOT state.disconnect_requested
                    AND EXISTS(SELECT 1 FROM telegram_peer_state peer WHERE peer.external_account_uuid=account.uuid
                        AND peer.stream_uuid=visible.uuid AND peer.selected AND peer.can_send))
                    OR (account.provider='zulip' AND coalesce(account.settings->>'api_key','')<>''))
            )) AS can_send
            FROM ({definition}) visible
            JOIN messenger_stream_bindings binding ON binding.project_id=visible.project_id
              AND binding.stream_uuid=visible.uuid AND binding.user_uuid=visible.user_uuid
            JOIN messenger_streams stream ON stream.project_id=visible.project_id AND stream.uuid=visible.uuid
        """)
        session.execute("""
            CREATE FUNCTION messenger_provider_capabilities_changed() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE account_id uuid; target_stream uuid;
            BEGIN
                account_id := CASE WHEN TG_TABLE_NAME IN ('telegram_account_state','telegram_peer_state')
                    THEN (to_jsonb(NEW)->>'external_account_uuid')::uuid ELSE (to_jsonb(NEW)->>'uuid')::uuid END;
                target_stream := CASE WHEN TG_TABLE_NAME='telegram_peer_state' THEN (to_jsonb(NEW)->>'stream_uuid')::uuid END;
                INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
                SELECT gen_random_uuid(),member.project_id,'delivery_snapshot_event','resource',
                    member.project_id::text || ':stream:' || member.stream_uuid::text,
                    jsonb_build_object('source_kind','stream.updated','resource_kind','stream',
                        'resource_uuid',member.stream_uuid,'stream_uuid',member.stream_uuid)
                FROM m_external_accounts_v2 account
                JOIN messenger_stream_bindings member ON member.project_id=account.project_id
                    AND member.user_uuid=account.owner_user_uuid AND member.active
                JOIN messenger_streams stream ON stream.project_id=member.project_id AND stream.uuid=member.stream_uuid
                    AND stream.source_name=account.provider AND stream.provider_uuid=account.provider_uuid
                WHERE account.uuid=account_id AND (target_stream IS NULL OR member.stream_uuid=target_stream);
                RETURN NULL;
            END $$;
            CREATE TRIGGER messenger_account_capabilities AFTER UPDATE OF disabled,settings,external_account_id ON m_external_accounts_v2
                FOR EACH ROW WHEN (OLD.disabled IS DISTINCT FROM NEW.disabled OR OLD.settings IS DISTINCT FROM NEW.settings
                    OR OLD.external_account_id IS DISTINCT FROM NEW.external_account_id)
                EXECUTE FUNCTION messenger_provider_capabilities_changed();
            CREATE TRIGGER messenger_telegram_peer_capabilities AFTER UPDATE OF selected,can_send ON telegram_peer_state
                FOR EACH ROW WHEN (NEW.stream_uuid IS NOT NULL AND (OLD.selected IS DISTINCT FROM NEW.selected
                    OR OLD.can_send IS DISTINCT FROM NEW.can_send))
                EXECUTE FUNCTION messenger_provider_capabilities_changed();
            CREATE TRIGGER messenger_telegram_capabilities AFTER UPDATE OF auth_state,disconnect_requested ON telegram_account_state
                FOR EACH ROW WHEN (OLD.auth_state IS DISTINCT FROM NEW.auth_state
                    OR OLD.disconnect_requested IS DISTINCT FROM NEW.disconnect_requested)
                EXECUTE FUNCTION messenger_provider_capabilities_changed();
        """)

    def downgrade(self, session):
        raise RuntimeError(
            "Restore a matching schema backup to remove stream capabilities"
        )


migration_step = MigrationStep()
