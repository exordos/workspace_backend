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
            "0242-Process-Zulip-message-repairs-from-newest-to-oldest-e9bd04.py"
        ]

    @property
    def migration_id(self):
        return "cc1987d0-894c-488f-8dff-4843ce8ed54f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE bridge_zulip_v5_tasks
                ADD COLUMN IF NOT EXISTS queue VARCHAR(16) NOT NULL DEFAULT 'background',
                ADD COLUMN IF NOT EXISTS concurrency_key TEXT NOT NULL DEFAULT '';
            ALTER TABLE bridge_zulip_v5_tasks ADD CONSTRAINT bridge_zulip_v5_tasks_queue_check
                CHECK (queue IN ('realtime','background'));
            CREATE FUNCTION bridge_zulip_v5_route_task() RETURNS TRIGGER LANGUAGE plpgsql AS $$
            DECLARE task_kind TEXT := NEW.content->>'kind';
            BEGIN
                IF NEW.backfill_uuid IS NOT NULL OR task_kind IN (
                    'full_backfill','actualize_backfill','refresh_message_flags',
                    'convert_message_content','reconciliation','refresh_message_counters'
                ) THEN
                    NEW.queue := 'background';
                ELSIF task_kind IN ('send_message','edit_message','update_message_flags',
                    'update_reaction','initialize_connection','reconcile_realtime') OR EXISTS (
                    SELECT 1 FROM bridge_zulip_v5_events
                    WHERE uuid=NEW.realtime_event_uuid AND source='zulip'
                ) THEN
                    NEW.queue := 'realtime';
                END IF;
                NEW.concurrency_key := CASE WHEN task_kind IN ('import_users','import_channels')
                    THEN NEW.project_id::text || ':catalog'
                    WHEN NEW.external_account_uuid IS NOT NULL
                    THEN NEW.project_id::text || ':' || NEW.external_account_uuid::text
                    ELSE task_kind END;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER bridge_zulip_v5_route_task
                BEFORE INSERT OR UPDATE OF content,queue,external_account_uuid,project_id,
                    backfill_uuid,realtime_event_uuid ON bridge_zulip_v5_tasks
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_route_task();
            UPDATE bridge_zulip_v5_tasks SET queue=queue
                WHERE status IN ('new','waiting','retry','error');
            CREATE INDEX bridge_zulip_v5_tasks_queue_ready_idx
                ON bridge_zulip_v5_tasks(queue,priority DESC,message_created_at DESC,
                    message_sequence DESC,created_at,uuid)
                WHERE status IN ('new','waiting','retry');
            CREATE INDEX bridge_zulip_v5_tasks_queue_leases_idx
                ON bridge_zulip_v5_tasks(queue,concurrency_key,lease_expires_at)
                WHERE lease_owner IS NOT NULL AND status IN ('new','waiting','retry');
        """)

    def downgrade(self, session):
        session.execute("""
            DROP TRIGGER bridge_zulip_v5_route_task ON bridge_zulip_v5_tasks;
            DROP FUNCTION bridge_zulip_v5_route_task();
            ALTER TABLE bridge_zulip_v5_tasks DROP COLUMN queue, DROP COLUMN concurrency_key;
        """)


migration_step = MigrationStep()
