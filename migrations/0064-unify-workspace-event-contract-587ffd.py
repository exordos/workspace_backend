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
        self._depends = ["0063-add-workspace-file-storage-backend-fields-8998ae.py"]

    @property
    def migration_id(self):
        return "587ffd24-cabb-4a39-b4bc-6cc7c75206a9"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_events"
                ADD COLUMN IF NOT EXISTS "schema_version" INTEGER,
                ADD COLUMN IF NOT EXISTS "object_type" VARCHAR(32),
                ADD COLUMN IF NOT EXISTS "action" VARCHAR(32);

            UPDATE "m_workspace_events"
            SET "schema_version" = 1
            WHERE "schema_version" IS NULL;

            UPDATE "m_workspace_events"
            SET
                "object_type" = CASE
                    WHEN "payload"->>'kind' IN (
                        'message.created',
                        'message.updated',
                        'message.deleted',
                        'messages.read',
                        'message.read'
                    ) THEN 'message'
                    WHEN "payload"->>'kind' IN (
                        'stream.created',
                        'stream.updated',
                        'stream.deleted',
                        'stream.read'
                    ) THEN 'stream'
                    WHEN "payload"->>'kind' = 'stream_bindings.created'
                        THEN 'stream_binding'
                    WHEN "payload"->>'kind' IN (
                        'topic.created',
                        'topic.updated',
                        'topic.deleted',
                        'topic.read'
                    ) THEN 'topic'
                    WHEN "payload"->>'kind' = 'user.updated'
                        THEN 'user'
                    WHEN "payload"->>'kind' IN (
                        'folder.created',
                        'folder.updated',
                        'folder.deleted'
                    ) THEN 'folder'
                    WHEN "payload"->>'kind' = 'folder_item.deleted'
                        THEN 'folder_item'
                    ELSE "object_type"
                END,
                "action" = CASE
                    WHEN "payload"->>'kind' LIKE '%.created'
                        THEN 'created'
                    WHEN "payload"->>'kind' LIKE '%.updated'
                        THEN 'updated'
                    WHEN "payload"->>'kind' LIKE '%.deleted'
                        THEN 'deleted'
                    WHEN "payload"->>'kind' IN (
                        'messages.read',
                        'message.read',
                        'stream.read',
                        'topic.read'
                    ) THEN 'read'
                    ELSE "action"
                END;

            UPDATE "m_workspace_events"
            SET "payload" = (
                "payload" - 'stream_bindings' - 'stream_uuid' - 'project_id'
            ) || jsonb_build_object(
                'uuid', "payload"->>'stream_uuid',
                'items', "payload"->'stream_bindings'
            )
            WHERE "payload"->>'kind' = 'stream_bindings.created'
              AND "payload" ? 'stream_bindings'
              AND NOT "payload" ? 'items';

            UPDATE "m_workspace_events"
            SET "payload" = jsonb_set(
                "payload",
                '{created_at}',
                to_jsonb(to_char(
                    ("payload"->>'created_at')::timestamp,
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                ))
            )
            WHERE "payload" ? 'created_at';

            UPDATE "m_workspace_events"
            SET "payload" = jsonb_set(
                "payload",
                '{updated_at}',
                to_jsonb(to_char(
                    ("payload"->>'updated_at')::timestamp,
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                ))
            )
            WHERE "payload" ? 'updated_at';

            UPDATE "m_workspace_events"
            SET "payload" = jsonb_set(
                "payload",
                '{last_ping_at}',
                to_jsonb(to_char(
                    ("payload"->>'last_ping_at')::timestamp,
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                ))
            )
            WHERE "payload" ? 'last_ping_at';

            ALTER TABLE "m_workspace_events"
                ALTER COLUMN "schema_version" SET DEFAULT 1,
                ALTER COLUMN "schema_version" SET NOT NULL,
                ALTER COLUMN "object_type" SET NOT NULL,
                ALTER COLUMN "action" SET NOT NULL;

            ALTER TABLE "m_workspace_events"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_schema_version_check",
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_object_type_check",
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_action_check",
                ADD CONSTRAINT "m_workspace_events_schema_version_check"
                    CHECK ("schema_version" = 1),
                ADD CONSTRAINT "m_workspace_events_object_type_check"
                    CHECK ("object_type" IN (
                        'message',
                        'stream',
                        'stream_binding',
                        'topic',
                        'user',
                        'folder',
                        'folder_item'
                    )),
                ADD CONSTRAINT "m_workspace_events_action_check"
                    CHECK ("action" IN (
                        'created',
                        'updated',
                        'deleted',
                        'read'
                    ));
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            UPDATE "m_workspace_events"
            SET "payload" = (
                "payload" - 'items' - 'uuid'
            ) || jsonb_build_object(
                'project_id', "project_id"::text,
                'stream_uuid', "payload"->>'uuid',
                'stream_bindings', "payload"->'items'
            )
            WHERE "payload"->>'kind' = 'stream_bindings.created'
              AND "payload" ? 'items'
              AND NOT "payload" ? 'stream_bindings';

            ALTER TABLE "m_workspace_events"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_action_check",
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_object_type_check",
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_events_schema_version_check",
                DROP COLUMN IF EXISTS "action",
                DROP COLUMN IF EXISTS "object_type",
                DROP COLUMN IF EXISTS "schema_version";
            """
        )


migration_step = MigrationStep()
