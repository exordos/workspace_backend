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
            "0278-Index-bounded-completed-service-history-collection-a04ac4.py"
        ]

    @property
    def migration_id(self):
        return "7f3b75d2-a3a0-4f59-aa0f-d6f3e56167ec"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            UPDATE zulip_incoming_events AS event
            SET ordering_keys=repaired.keys
            FROM (
                SELECT pending.id, CASE
                    WHEN pending.payload->>'type'='update_message_flags'
                         AND COALESCE((pending.payload->>'all')::boolean,false)
                        THEN ARRAY['*']::text[]
                    ELSE ARRAY(
                        SELECT DISTINCT 'message:' || identifier
                        FROM (
                            SELECT jsonb_array_elements_text(COALESCE(
                                pending.payload->'message_ids',
                                pending.payload->'messages', '[]'::jsonb
                            )) AS identifier
                            UNION ALL SELECT pending.payload->>'message_id'
                            UNION ALL SELECT pending.payload->'message'->>'id'
                        ) identifiers
                        WHERE identifier IS NOT NULL ORDER BY 1
                    ) END AS keys
                FROM zulip_incoming_events AS pending
                WHERE pending.processed_at IS NULL
                  AND pending.payload->>'type' IN (
                      'message','update_message','delete_message',
                      'reaction','update_message_flags'
                  )
            ) AS repaired
            WHERE event.id=repaired.id
              AND event.processed_at IS NULL
              AND event.ordering_keys IS DISTINCT FROM repaired.keys
        """)

    def downgrade(self, session):
        raise RuntimeError("Restored Zulip inbox ordering keys are irreversible")


migration_step = MigrationStep()
