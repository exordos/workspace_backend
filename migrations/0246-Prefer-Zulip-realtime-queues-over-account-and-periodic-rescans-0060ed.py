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

import hashlib
import json

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0245-Retry-Zulip-history-imports-stopped-by-database-deadlocks-cc62d6.py"
        ]

    @property
    def migration_id(self):
        return "0060ed94-8596-4229-bb01-cc773c93705e"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            DROP TRIGGER bridge_zulip_v5_track_account_flags ON m_external_accounts_v2;
            CREATE TRIGGER bridge_zulip_v5_track_account_flags
                AFTER UPDATE ON m_external_accounts_v2 FOR EACH ROW
                WHEN ((OLD.provider,OLD.owner_user_uuid,OLD.disabled,OLD.external_account_id,OLD.settings->>'server_url')
                    IS DISTINCT FROM
                      (NEW.provider,NEW.owner_user_uuid,NEW.disabled,NEW.external_account_id,NEW.settings->>'server_url'))
                EXECUTE FUNCTION bridge_zulip_v5_track_account_flags();
            DROP TRIGGER bridge_zulip_v5_requeue_reactions ON m_external_accounts_v2;
            CREATE TRIGGER bridge_zulip_v5_requeue_reactions
                AFTER UPDATE ON m_external_accounts_v2 FOR EACH ROW
                WHEN ((OLD.provider,OLD.owner_user_uuid,OLD.disabled,OLD.external_account_id,OLD.settings->>'server_url')
                    IS DISTINCT FROM
                      (NEW.provider,NEW.owner_user_uuid,NEW.disabled,NEW.external_account_id,NEW.settings->>'server_url'))
                EXECUTE FUNCTION bridge_zulip_v5_requeue_reactions();
            DROP TRIGGER bridge_zulip_v5_accounts_changed ON m_external_accounts_v2;
            CREATE FUNCTION bridge_zulip_v5_wake_changed_account() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' AND
                    (OLD.project_id,OLD.owner_user_uuid,OLD.provider,OLD.disabled,OLD.external_account_id,
                     OLD.settings->>'server_url',OLD.settings->>'email',OLD.settings->>'api_key',OLD.status='new')
                    IS NOT DISTINCT FROM
                    (NEW.project_id,NEW.owner_user_uuid,NEW.provider,NEW.disabled,NEW.external_account_id,
                     NEW.settings->>'server_url',NEW.settings->>'email',NEW.settings->>'api_key',NEW.status='new') THEN
                    RETURN NULL;
                END IF;
                PERFORM pg_notify('workspace_zulip_realtime','accounts');
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER bridge_zulip_v5_accounts_changed
                AFTER INSERT OR UPDATE OR DELETE ON m_external_accounts_v2
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_wake_changed_account();

            UPDATE bridge_zulip_v5_tasks
            SET status='cancel',next_retry_at=NULL,lease_owner=NULL,lease_expires_at=NULL,
                updated_at=now() AT TIME ZONE 'UTC'
            WHERE status IN ('new','waiting','retry') AND not_before_at IS NOT NULL
              AND backfill_uuid IS NULL AND realtime_event_uuid IS NULL
              AND content->>'kind' IN ('import_users','import_channels')
              AND content->>'periodic' IS DISTINCT FROM 'false';

            UPDATE bridge_zulip_v5_tasks
            SET status='retry',next_retry_at=now() AT TIME ZONE 'UTC',
                lease_owner=NULL,lease_expires_at=NULL,updated_at=now() AT TIME ZONE 'UTC'
            WHERE status='error' AND content->>'kind'='import_users'
              AND split_part(error_description,chr(10),1) IN (
                  'deadlock detected',
                  'Deadlock found when trying to get lock. Original message: deadlock detected'
              );
        """)
        accounts = session.execute("""
            SELECT uuid,project_id,owner_user_uuid,provider,external_account_id,settings
            FROM m_external_accounts_v2 AS account WHERE provider='zulip' AND settings ? 'server_url'
              AND (settings ? 'api_key' OR EXISTS (SELECT 1 FROM bridge_zulip_v5_subscriptions WHERE external_account_uuid=account.uuid))
        """).fetchall()
        for account in accounts:
            scope = [
                str(account["project_id"]),
                str(account["owner_user_uuid"]),
                account["provider"],
                account["external_account_id"],
            ]
            settings = account["settings"]
            old_revision = hashlib.sha256(
                json.dumps([*scope, settings], sort_keys=True).encode()
            ).hexdigest()
            connection = {
                name: settings.get(name) for name in ("server_url", "email", "api_key")
            }
            revision = hashlib.sha256(
                json.dumps([*scope, connection], sort_keys=True).encode()
            ).hexdigest()
            subscription = hashlib.sha256(
                json.dumps(
                    [*scope, settings["server_url"].rstrip("/")], sort_keys=True
                ).encode()
            ).hexdigest()
            session.execute(
                """
                UPDATE bridge_zulip_v5_subscriptions SET account_revision=%s
                WHERE external_account_uuid=%s AND project_id=%s AND account_revision=%s
            """,
                (subscription, account["uuid"], account["project_id"], old_revision),
            )
            session.execute(
                """
                UPDATE bridge_zulip_v5_tasks
                SET content=jsonb_set(content,'{account_revision}',to_jsonb(%s::text))
                WHERE external_account_uuid=%s AND project_id=%s
                  AND status IN ('new','waiting','retry') AND content->>'account_revision'=%s
            """,
                (revision, account["uuid"], account["project_id"], old_revision),
            )

    def downgrade(self, session):
        session.execute("""
            DROP TRIGGER bridge_zulip_v5_track_account_flags ON m_external_accounts_v2;
            CREATE TRIGGER bridge_zulip_v5_track_account_flags
                AFTER UPDATE OF provider,owner_user_uuid,disabled,external_account_id,settings
                ON m_external_accounts_v2 FOR EACH ROW
                EXECUTE FUNCTION bridge_zulip_v5_track_account_flags();
            DROP TRIGGER bridge_zulip_v5_requeue_reactions ON m_external_accounts_v2;
            CREATE TRIGGER bridge_zulip_v5_requeue_reactions
                AFTER UPDATE OF provider,owner_user_uuid,disabled,external_account_id,settings
                ON m_external_accounts_v2 FOR EACH ROW
                EXECUTE FUNCTION bridge_zulip_v5_requeue_reactions();
            DROP TRIGGER bridge_zulip_v5_accounts_changed ON m_external_accounts_v2;
            CREATE TRIGGER bridge_zulip_v5_accounts_changed
                AFTER INSERT OR UPDATE OR DELETE ON m_external_accounts_v2
                FOR EACH STATEMENT EXECUTE FUNCTION bridge_zulip_v5_wake_realtime('accounts');
            DROP FUNCTION bridge_zulip_v5_wake_changed_account();
        """)
        accounts = session.execute("""
            SELECT uuid,project_id,owner_user_uuid,provider,external_account_id,settings
            FROM m_external_accounts_v2 AS account WHERE provider='zulip' AND settings ? 'server_url'
              AND (settings ? 'api_key' OR EXISTS (SELECT 1 FROM bridge_zulip_v5_subscriptions WHERE external_account_uuid=account.uuid))
        """).fetchall()
        for account in accounts:
            scope = [
                str(account["project_id"]),
                str(account["owner_user_uuid"]),
                account["provider"],
                account["external_account_id"],
            ]
            settings = account["settings"]
            old_revision = hashlib.sha256(
                json.dumps([*scope, settings], sort_keys=True).encode()
            ).hexdigest()
            connection = {
                name: settings.get(name) for name in ("server_url", "email", "api_key")
            }
            revision = hashlib.sha256(
                json.dumps([*scope, connection], sort_keys=True).encode()
            ).hexdigest()
            subscription = hashlib.sha256(
                json.dumps(
                    [*scope, settings["server_url"].rstrip("/")], sort_keys=True
                ).encode()
            ).hexdigest()
            session.execute(
                """
                UPDATE bridge_zulip_v5_subscriptions SET account_revision=%s
                WHERE external_account_uuid=%s AND project_id=%s AND account_revision=%s
            """,
                (old_revision, account["uuid"], account["project_id"], subscription),
            )
            session.execute(
                """
                UPDATE bridge_zulip_v5_tasks
                SET content=jsonb_set(content,'{account_revision}',to_jsonb(%s::text))
                WHERE external_account_uuid=%s AND project_id=%s
                  AND status IN ('new','waiting','retry') AND content->>'account_revision'=%s
            """,
                (old_revision, account["uuid"], account["project_id"], revision),
            )


migration_step = MigrationStep()
