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
            "0259-Preserve-bidirectional-Zulip-topic-notification-changes-0e2e4b.py"
        ]

    @property
    def migration_id(self):
        return "be705e82-dd69-4935-bfe8-a220ead24a1b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Reuse the fenced, message-scoped worker instead of rebuilding the
        # full reaction history inside the schema transaction or a GET request.
        session.execute("""
            INSERT INTO messenger_domain_outbox_events
                (uuid, project_id, event_kind, scope_kind, scope_key, payload)
            SELECT messenger_uuid_v5(message.uuid, 'bounded-reaction-users-be705e'),
                   message.project_id, 'reaction_snapshot', 'message',
                   message.project_id::text || ':' || message.uuid::text,
                   jsonb_build_object(
                       'source_kind', 'reaction_users.rebuild',
                       'placement_uuid', placement.uuid,
                       'emit_reaction_event', false
                   )
            FROM messenger_messages AS message
            JOIN LATERAL (
                SELECT candidate.uuid
                FROM messenger_message_placements AS candidate
                WHERE candidate.project_id = message.project_id
                  AND candidate.message_uuid = message.uuid
                ORDER BY candidate.created_at, candidate.uuid
                LIMIT 1
            ) AS placement ON TRUE
            WHERE message.deleted_at IS NULL
              AND (
                  message.reactions <> '{}'::jsonb
                  OR message.reaction_users <> '{}'::jsonb
                  OR EXISTS (
                      SELECT 1 FROM messenger_message_reaction_facts AS fact
                      WHERE fact.project_id = message.project_id
                        AND fact.canonical_message_uuid = message.uuid
                  )
              )
            ON CONFLICT (project_id, uuid) DO NOTHING
        """)

    def downgrade(self, session):
        session.execute("""
            DELETE FROM messenger_domain_outbox_events
            WHERE event_kind = 'reaction_snapshot'
              AND payload->>'source_kind' = 'reaction_users.rebuild'
        """)


migration_step = MigrationStep()
