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
            "0236-Avoid-self-generated-Zulip-message-reconciliation-changes-50c353.py"
        ]

    @property
    def migration_id(self):
        return "342a1cd6-e7c3-4aab-a342-2ff93056bd47"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Let in-flight task transactions finish before replacing their work.
        session.execute("LOCK TABLE bridge_zulip_v5_tasks IN ACCESS EXCLUSIVE MODE")
        session.execute("""
            UPDATE bridge_zulip_v5_tasks
            SET priority = 200000, updated_at = NOW() AT TIME ZONE 'UTC'
            WHERE content->>'kind' = 'reconciliation'
              AND status IN ('new', 'waiting', 'retry', 'error');

            INSERT INTO bridge_zulip_v5_tasks (
                uuid, project_id, external_account_uuid, priority, status, content,
                error_description, not_before_at, next_retry_at,
                backfill_uuid, realtime_event_uuid, created_at, updated_at
            )
            SELECT messenger_uuid_v5(original.uuid, 'history-batch:100:' || chunk.first_id::text),
                   original.project_id, original.external_account_uuid,
                   original.priority, original.status,
                   original.content || jsonb_build_object(
                       'first_message_id', chunk.first_id,
                       'last_message_id', LEAST(bounds.last_id, chunk.first_id + 99)
                   ),
                   original.error_description, original.not_before_at, original.next_retry_at,
                   original.backfill_uuid, original.realtime_event_uuid,
                   original.created_at + (
                       (bounds.last_id - bounds.first_id) / 100
                       - (chunk.first_id - bounds.first_id) / 100
                   ) * INTERVAL '1 microsecond',
                   NOW() AT TIME ZONE 'UTC'
            FROM bridge_zulip_v5_tasks AS original
            CROSS JOIN LATERAL (
                SELECT (original.content->>'first_message_id')::bigint AS first_id,
                       (original.content->>'last_message_id')::bigint AS last_id
            ) AS bounds
            CROSS JOIN LATERAL generate_series(bounds.first_id, bounds.last_id, 100) AS chunk(first_id)
            WHERE original.content->>'kind' = 'import_history'
              AND original.status IN ('new', 'waiting', 'retry', 'error')
              AND bounds.last_id - bounds.first_id + 1 > 100;

            UPDATE bridge_zulip_v5_tasks
            SET status = 'cancel', backfill_uuid = NULL, next_retry_at = NULL,
                lease_owner = NULL, lease_expires_at = NULL,
                error_description = concat_ws(E'\\n', error_description,
                    'Replaced by 100-ID history tasks (migration 0237).'),
                updated_at = NOW() AT TIME ZONE 'UTC'
            WHERE content->>'kind' = 'import_history'
              AND status IN ('new', 'waiting', 'retry', 'error')
              AND (content->>'last_message_id')::bigint
                  - (content->>'first_message_id')::bigint + 1 > 100;
        """)

    def downgrade(self, session):
        # Keep split tasks: some may already have completed. Older workers can
        # execute 100-ID ranges without rebuilding cancelled parent tasks.
        session.execute("""
            UPDATE bridge_zulip_v5_tasks
            SET priority = 400000, updated_at = NOW() AT TIME ZONE 'UTC'
            WHERE content->>'kind' = 'reconciliation'
              AND status IN ('new', 'waiting', 'retry', 'error')
              AND priority = 200000;
        """)


migration_step = MigrationStep()
