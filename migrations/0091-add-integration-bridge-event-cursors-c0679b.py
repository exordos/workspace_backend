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
        self._depends = ["0090-add-workspace-user-avatar-4e75a1.py"]

    @property
    def migration_id(self):
        return "c0679bde-42dc-4884-837c-a8782ec2dbb6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS
                "m_integration_bridge_event_cursors" (
                    "uuid" UUID PRIMARY KEY,
                    "name" VARCHAR(128) NOT NULL UNIQUE,
                    "epoch_version" BIGINT NOT NULL DEFAULT 0,
                    "created_at" TIMESTAMP WITH TIME ZONE NOT NULL
                        DEFAULT NOW(),
                    "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL
                        DEFAULT NOW()
                );
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_events_zulip_outbound_candidates_idx"
                ON "m_workspace_events" ("epoch_version")
                WHERE (
                    (
                        "object_type" = 'message'
                        AND "action" IN ('created', 'updated', 'deleted')
                        AND "payload"->>'source_name' = 'zulip'
                    )
                    OR (
                        "object_type" = 'message_reaction'
                        AND "action" IN ('created', 'updated', 'deleted')
                        AND "payload"->>'source_name' = 'zulip'
                    )
                    OR (
                        "object_type" = 'stream'
                        AND "action" = 'updated'
                        AND "payload"->>'source_name' = 'zulip'
                    )
                    OR (
                        "object_type" = 'topic'
                        AND "action" = 'updated'
                        AND "payload"->>'source_name' = 'zulip'
                    )
                );
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_events_previous_message_payload_idx"
                ON "m_workspace_events" (
                    "project_id",
                    ("payload"->>'uuid'),
                    "epoch_version" DESC
                )
                WHERE "object_type" = 'message'
                  AND "action" IN ('created', 'updated');
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_events_previous_stream_payload_idx"
                ON "m_workspace_events" (
                    "project_id",
                    "user_uuid",
                    ("payload"->>'uuid'),
                    "epoch_version" DESC
                )
                WHERE "object_type" = 'stream'
                  AND "action" IN ('created', 'updated');
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                "m_workspace_events_previous_topic_payload_idx"
                ON "m_workspace_events" (
                    "project_id",
                    "user_uuid",
                    ("payload"->>'uuid'),
                    "epoch_version" DESC
                )
                WHERE "object_type" = 'topic'
                  AND "action" IN ('created', 'updated');
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_workspace_events_previous_topic_payload_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_workspace_events_previous_stream_payload_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_workspace_events_previous_message_payload_idx";
            """
        )
        session.execute(
            """
            DROP INDEX IF EXISTS
                "m_workspace_events_zulip_outbound_candidates_idx";
            """
        )
        session.execute(
            """
            DROP TABLE IF EXISTS "m_integration_bridge_event_cursors";
            """
        )


migration_step = MigrationStep()
