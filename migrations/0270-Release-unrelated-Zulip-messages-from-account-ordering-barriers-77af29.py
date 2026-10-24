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
            "0269-Retire-queued-Zulip-delivery-snapshots-after-direct-publication-a2ab09.py"
        ]

    @property
    def migration_id(self):
        return "77af29d3-ade6-4e72-8077-4e35b85bf450"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            UPDATE zulip_incoming_events event
            SET ordering_keys=CASE
                WHEN event.payload->>'type'='update_message_flags'
                     AND COALESCE((event.payload->>'all')::boolean,false)
                    THEN ARRAY['*']::text[]
                ELSE ARRAY(SELECT key FROM unnest(event.ordering_keys) AS key
                           WHERE key LIKE 'message:%' ORDER BY key)
                END
            WHERE event.processed_at IS NULL
              AND event.payload->>'type' IN
                  ('message','update_message','delete_message','reaction','update_message_flags');
        """)

    def downgrade(self, session):
        raise RuntimeError("Independent Zulip message ordering cutover is irreversible")


migration_step = MigrationStep()
