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
            "0273-Normalize-immutable-message-contents-and-preserve-per-message-revisions-2346b6.py"
        ]

    @property
    def migration_id(self):
        return "f75ecf58-2613-4adc-a82f-ad0ae2399db7"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TABLE zulip_history_jobs (
                external_account_uuid uuid PRIMARY KEY,
                project_id uuid NOT NULL,
                provider_uuid uuid NOT NULL REFERENCES messenger_providers(uuid),
                owner_user_uuid uuid NOT NULL,
                account_identity varchar(255) NOT NULL,
                server_url text NOT NULL,
                requested_at timestamptz NOT NULL DEFAULT now(),
                scanning_at timestamptz,
                completed_at timestamptz NOT NULL DEFAULT 'epoch',
                newest_id bigint NOT NULL DEFAULT 0,
                target_id bigint,
                before_id bigint,
                backfill_done boolean NOT NULL DEFAULT false,
                repair_before_id bigint,
                repair_after timestamptz NOT NULL DEFAULT now(),
                registration jsonb,
                subscribed_streams bigint[],
                registration_queue text,
                registration_started_at timestamptz,
                catalog_position integer NOT NULL DEFAULT 0,
                flag_after_id bigint NOT NULL DEFAULT 0,
                last_run_at timestamptz NOT NULL DEFAULT 'epoch',
                next_attempt_at timestamptz NOT NULL DEFAULT now(),
                attempts integer NOT NULL DEFAULT 0,
                error text,
                FOREIGN KEY (external_account_uuid, project_id)
                    REFERENCES m_external_accounts_v2(uuid, project_id) ON DELETE CASCADE
            );
            CREATE TABLE zulip_history_intents (
                id bigserial PRIMARY KEY,
                external_account_uuid uuid NOT NULL
                    REFERENCES zulip_history_jobs(external_account_uuid) ON DELETE CASCADE,
                entity_key text NOT NULL,
                payload jsonb NOT NULL,
                expected jsonb,
                identity jsonb NOT NULL,
                fetched_at timestamptz NOT NULL,
                next_attempt_at timestamptz NOT NULL DEFAULT now(),
                attempts integer NOT NULL DEFAULT 0,
                error text,
                failed boolean NOT NULL DEFAULT false,
                UNIQUE (external_account_uuid, entity_key)
            );
            CREATE INDEX zulip_history_ready ON zulip_history_intents(next_attempt_at,id)
                WHERE NOT failed;
            CREATE INDEX zulip_history_due ON zulip_history_jobs(next_attempt_at,last_run_at);
            CREATE INDEX zulip_history_message_ids
                ON messenger_messages(project_id,provider_uuid,(provider_identity::bigint))
                WHERE source_name='zulip' AND provider_identity ~ '^[0-9]+$';
            INSERT INTO zulip_history_jobs (
                external_account_uuid,project_id,provider_uuid,owner_user_uuid,
                account_identity,server_url
            ) SELECT account.uuid,account.project_id,account.provider_uuid,
                     account.owner_user_uuid,account.external_account_id,
                     account.settings->>'server_url'
              FROM m_external_accounts_v2 account
              WHERE account.provider='zulip' AND NOT account.disabled
                AND account.external_account_id IS NOT NULL
                AND COALESCE(account.settings->>'api_key','')<>'';
            ALTER TABLE messenger_publication_outbox
                ADD COLUMN priority smallint NOT NULL DEFAULT 0;
            CREATE INDEX messenger_publication_entity_order
                ON messenger_publication_outbox(project_id,entity_uuid,sequence);
            CREATE INDEX messenger_publication_priority
                ON messenger_publication_outbox(project_id,priority,sequence);
            CREATE INDEX zulip_incoming_message_ordering ON zulip_incoming_events
                USING gin(ordering_keys);
            CREATE INDEX zulip_incoming_recent ON zulip_incoming_events
                (project_id,provider_uuid,received_at);
            ALTER TABLE zulip_incoming_attachments
                ADD COLUMN owner_identity varchar(255),
                ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
            UPDATE zulip_incoming_attachments attachment
                SET owner_identity=account.external_account_id
            FROM zulip_entity_links link
            JOIN m_external_accounts_v2 account ON account.uuid=link.external_account_uuid
            WHERE link.entity_type='file' AND link.project_id=attachment.project_id
              AND link.provider_uuid=attachment.provider_uuid
              AND link.data->>'attachment_id'=attachment.attachment_id::text;
            CREATE FUNCTION zulip_history_wakeup() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM pg_notify('workspace_zulip_history','');
                RETURN NULL;
            END $$;
            CREATE TRIGGER zulip_history_requested AFTER INSERT OR UPDATE
                ON zulip_history_jobs FOR EACH STATEMENT
                EXECUTE FUNCTION zulip_history_wakeup();
        """)

    def downgrade(self, session):
        session.execute("""
            DROP TABLE zulip_history_intents;
            DROP TABLE zulip_history_jobs;
            DROP FUNCTION zulip_history_wakeup();
            DROP INDEX zulip_incoming_message_ordering;
            DROP INDEX zulip_incoming_recent;
            DROP INDEX zulip_history_message_ids;
            DROP INDEX messenger_publication_entity_order;
            DROP INDEX messenger_publication_priority;
            ALTER TABLE messenger_publication_outbox DROP COLUMN priority;
            ALTER TABLE zulip_incoming_attachments
                DROP COLUMN owner_identity,DROP COLUMN updated_at;
        """)


migration_step = MigrationStep()
