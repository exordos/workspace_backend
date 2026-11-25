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
        self._depends = ["0308-Register-APNs-push-devices-71fa20.py"]

    @property
    def migration_id(self):
        return "09e2dd9f-5826-49ee-9cd9-1546afb8ccec"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TABLE m_mail_folders (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                owner_user_uuid UUID NOT NULL
                    REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
                external_account_uuid UUID
                    REFERENCES m_external_accounts_v2(uuid) ON DELETE SET NULL,
                name VARCHAR(256) NOT NULL CHECK (length(name) >= 1),
                kind TEXT NOT NULL DEFAULT 'custom'
                    CHECK (kind IN (
                        'inbox', 'sent', 'drafts', 'trash', 'spam', 'archive', 'custom'
                    )),
                unread_count INTEGER NOT NULL DEFAULT 0
                    CHECK (unread_count >= 0),
                source_name VARCHAR(16) NOT NULL DEFAULT 'native'
                    CHECK (source_name IN ('native', 'provider')),
                provider_identity VARCHAR(2048),
                deleted BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (
                    (source_name = 'native' AND provider_identity IS NULL)
                    OR source_name = 'provider'
                )
            );

            CREATE UNIQUE INDEX m_mail_folders_owner_name_idx
                ON m_mail_folders (
                    project_id,
                    owner_user_uuid,
                    COALESCE(
                        external_account_uuid,
                        '00000000-0000-0000-0000-000000000000'::uuid
                    ),
                    name
                )
                WHERE NOT deleted;
            CREATE UNIQUE INDEX m_mail_folders_owner_system_kind_idx
                ON m_mail_folders (
                    project_id, owner_user_uuid,
                    COALESCE(
                        external_account_uuid,
                        '00000000-0000-0000-0000-000000000000'::uuid
                    ),
                    kind
                )
                WHERE NOT deleted AND kind <> 'custom';
            CREATE INDEX m_mail_folders_owner_updated_idx
                ON m_mail_folders (
                    project_id, owner_user_uuid, updated_at DESC, uuid
                )
                WHERE NOT deleted;

            CREATE TABLE m_mail_messages (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                owner_user_uuid UUID NOT NULL
                    REFERENCES m_workspace_users(uuid) ON DELETE CASCADE,
                folder_uuid UUID NOT NULL
                    REFERENCES m_mail_folders(uuid) ON DELETE RESTRICT,
                external_account_uuid UUID
                    REFERENCES m_external_accounts_v2(uuid) ON DELETE SET NULL,
                from_address VARCHAR(2048) NOT NULL DEFAULT '',
                to_addresses JSONB NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(to_addresses) = 'array'),
                cc_addresses JSONB NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(cc_addresses) = 'array'),
                bcc_addresses JSONB NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(bcc_addresses) = 'array'),
                reply_to_uuid UUID,
                subject VARCHAR(2048) NOT NULL DEFAULT '',
                payload JSONB NOT NULL DEFAULT '{"kind":"multipart","parts":[]}'::jsonb,
                attachment_urns JSONB NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(attachment_urns) = 'array'),
                sent_at TIMESTAMPTZ,
                read BOOLEAN NOT NULL DEFAULT FALSE,
                starred BOOLEAN NOT NULL DEFAULT FALSE,
                trash_origin_uuid UUID,
                draft BOOLEAN NOT NULL DEFAULT TRUE,
                delivery_status VARCHAR(16) NOT NULL DEFAULT 'draft'
                    CHECK (delivery_status IN (
                        'draft', 'queued', 'sent', 'received', 'failed'
                    )),
                delivery_error TEXT,
                source_name VARCHAR(16) NOT NULL DEFAULT 'native'
                    CHECK (source_name IN ('native', 'provider')),
                provider_identity VARCHAR(2048),
                deleted BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (
                    (source_name = 'native' AND provider_identity IS NULL)
                    OR source_name = 'provider'
                ),
                CHECK (draft OR delivery_status <> 'draft')
            );

            CREATE INDEX m_mail_messages_owner_created_idx
                ON m_mail_messages (
                    project_id, owner_user_uuid, created_at DESC, uuid
                )
                WHERE NOT deleted;
            CREATE INDEX m_mail_messages_owner_folder_created_idx
                ON m_mail_messages (
                    project_id, owner_user_uuid, folder_uuid,
                    created_at DESC, uuid
                )
                WHERE NOT deleted;
            CREATE UNIQUE INDEX m_mail_messages_provider_identity_idx
                ON m_mail_messages (
                    external_account_uuid, provider_identity
                )
                WHERE provider_identity IS NOT NULL;

            CREATE FUNCTION mail_refresh_folder_unreads()
                RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE
                before_uuid UUID;
                after_uuid UUID;
            BEGIN
                IF TG_OP <> 'INSERT' AND NOT OLD.deleted AND NOT OLD.draft AND NOT OLD.read THEN
                    before_uuid := OLD.folder_uuid;
                END IF;
                IF TG_OP <> 'DELETE' AND NOT NEW.deleted AND NOT NEW.draft AND NOT NEW.read THEN
                    after_uuid := NEW.folder_uuid;
                END IF;
                IF before_uuid IS DISTINCT FROM after_uuid THEN
                    PERFORM uuid FROM m_mail_folders
                        WHERE uuid IN (before_uuid, after_uuid) ORDER BY uuid FOR UPDATE;
                    UPDATE m_mail_folders SET
                        unread_count = unread_count
                            + CASE WHEN uuid = after_uuid THEN 1 ELSE -1 END,
                        updated_at = now()
                        WHERE uuid IN (before_uuid, after_uuid);
                END IF;
                RETURN NULL;
            END $$;
            CREATE TRIGGER mail_refresh_folder_unreads
                AFTER INSERT OR UPDATE OR DELETE ON m_mail_messages
                FOR EACH ROW EXECUTE FUNCTION mail_refresh_folder_unreads();

            CREATE TABLE mail_outgoing_intents (
                id BIGSERIAL PRIMARY KEY,
                uuid UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
                project_id UUID NOT NULL,
                message_uuid UUID NOT NULL
                    REFERENCES m_mail_messages(uuid) ON DELETE CASCADE,
                external_account_uuid UUID NOT NULL
                    REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
                payload JSONB NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'checking', 'done', 'error')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                error TEXT,
                next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                completed_at TIMESTAMPTZ
            );

            CREATE UNIQUE INDEX mail_outgoing_active_message_idx
                ON mail_outgoing_intents(message_uuid)
                WHERE status IN ('new', 'checking');
            CREATE INDEX mail_outgoing_ready_idx
                ON mail_outgoing_intents(next_attempt_at, id)
                WHERE status IN ('new', 'checking');

            CREATE FUNCTION mail_outgoing_wakeup()
                RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM pg_notify('workspace_mail_outgoing', '');
                RETURN NULL;
            END $$;
            CREATE TRIGGER mail_outgoing_wakeup
                AFTER INSERT ON mail_outgoing_intents
                FOR EACH STATEMENT EXECUTE FUNCTION mail_outgoing_wakeup();

            ALTER TABLE m_workspace_events
                DROP CONSTRAINT m_workspace_events_object_type_check,
                ADD CONSTRAINT m_workspace_events_object_type_check
                    CHECK (object_type IN (
                        'message', 'message_reaction', 'stream',
                        'stream_binding', 'topic', 'user', 'folder',
                        'folder_item', 'file', 'external_account',
                        'external_chat', 'external_operation',
                        'mail_folder', 'mail_message', 'mail_draft'
                    ));
        """)

    def downgrade(self, session):
        session.execute("""
            DELETE FROM m_workspace_events
            WHERE object_type IN ('mail_folder', 'mail_message', 'mail_draft');
            ALTER TABLE m_workspace_events
                DROP CONSTRAINT m_workspace_events_object_type_check,
                ADD CONSTRAINT m_workspace_events_object_type_check
                    CHECK (object_type IN (
                        'message', 'message_reaction', 'stream',
                        'stream_binding', 'topic', 'user', 'folder',
                        'folder_item', 'file', 'external_account',
                        'external_chat', 'external_operation'
                    ));
            DROP TABLE mail_outgoing_intents;
            DROP FUNCTION mail_outgoing_wakeup();
            DROP TABLE m_mail_messages;
            DROP FUNCTION mail_refresh_folder_unreads();
            DROP TABLE m_mail_folders;
        """)


migration_step = MigrationStep()
