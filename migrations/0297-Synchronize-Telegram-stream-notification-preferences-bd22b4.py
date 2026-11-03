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
        self._depends = ["0296-Expose-per-member-stream-sending-permissions-40ef25.py"]

    @property
    def migration_id(self):
        return "bd22b4ab-8992-4f3d-87c7-a490d7d51462"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE telegram_account_state ADD COLUMN notification_defaults jsonb NOT NULL DEFAULT '{}',
                ADD COLUMN notification_defaults_at timestamptz;
            ALTER TABLE telegram_peer_state
                ADD COLUMN notification_muted boolean,
                ADD COLUMN notification_inherited boolean NOT NULL DEFAULT true,
                ADD COLUMN notification_mute_until timestamptz,
                ADD COLUMN notification_checked_at timestamptz;
            CREATE FUNCTION telegram_capture_notifications() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE actor uuid := nullif(current_setting('workspace.outgoing_actor',true),'')::uuid;
            BEGIN
                IF actor IS NULL OR actor<>NEW.user_uuid OR NOT NEW.active
                   OR NEW.notification_mode IS NOT DISTINCT FROM OLD.notification_mode THEN RETURN NULL; END IF;
                INSERT INTO telegram_outgoing_intents(project_id,external_account_uuid,peer_key,entity_type,entity_uuid,operation,data)
                SELECT NEW.project_id,account.uuid,peer.peer_key,'notifications',NEW.uuid,'update',
                    jsonb_build_object('notification_mode',NEW.notification_mode,'actor_uuid',actor)
                FROM telegram_peer_state peer JOIN m_external_accounts_v2 account ON account.uuid=peer.external_account_uuid
                WHERE peer.stream_uuid=NEW.stream_uuid AND peer.selected
                  AND account.project_id=NEW.project_id AND account.owner_user_uuid=actor
                  AND account.provider='telegram' AND NOT account.disabled;
                RETURN NULL;
            END $$;
            CREATE TRIGGER telegram_capture_notifications AFTER UPDATE OF notification_mode ON messenger_stream_bindings
                FOR EACH ROW EXECUTE FUNCTION telegram_capture_notifications();
        """)

    def downgrade(self, session):
        session.execute("""
            DROP TRIGGER telegram_capture_notifications ON messenger_stream_bindings;
            DROP FUNCTION telegram_capture_notifications();
            ALTER TABLE telegram_peer_state DROP COLUMN notification_checked_at,
                DROP COLUMN notification_mute_until,DROP COLUMN notification_inherited,DROP COLUMN notification_muted;
            ALTER TABLE telegram_account_state DROP COLUMN notification_defaults,DROP COLUMN notification_defaults_at;
        """)


migration_step = MigrationStep()
