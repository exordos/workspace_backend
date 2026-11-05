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
            "0294-Delete-Messenger-roots-with-durable-snapshots-and-prepared-unreads-77d94b.py"
        ]

    @property
    def migration_id(self):
        return "6ce18008-6f43-4d20-9ed8-28cf73d4d838"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE INDEX zulip_incoming_local_receipt
            ON zulip_incoming_events(external_account_uuid,(payload->>'local_message_id'),id)
            WHERE payload ? 'local_message_id';

            WITH retried AS (
                UPDATE zulip_outgoing_intents
                SET status='new',request=NULL,next_attempt_at=clock_timestamp()+interval '1 second'
                WHERE status='error' AND entity_type='message' AND operation='insert'
                  AND request->>'kind'='send'
                  AND (error='Zulip HTTP 400: Bad event queue id'
                       OR error ~ '^Zulip HTTP 400: An event newer than [0-9]+ has already been pruned!$')
                RETURNING project_id,entity_uuid
            )
            UPDATE messenger_messages message SET status='new'
            FROM retried
            WHERE message.project_id=retried.project_id AND message.uuid=retried.entity_uuid
              AND message.status='error' AND message.provider_identity IS NULL;
        """)

    def downgrade(self, session):
        session.execute("DROP INDEX zulip_incoming_local_receipt")


migration_step = MigrationStep()
