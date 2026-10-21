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


CONTENT_VERSION_SQL = """
CREATE OR REPLACE FUNCTION messenger_track_content_version()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.content_updated_at := COALESCE(NEW.content_updated_at, NEW.updated_at);
    ELSIF NEW.payload IS DISTINCT FROM OLD.payload AND (
        NEW.content_updated_at IS NULL
        OR NEW.content_updated_at IS NOT DISTINCT FROM OLD.content_updated_at
    ) %(projection_guard)s THEN
        NEW.content_updated_at := statement_timestamp() AT TIME ZONE 'UTC';
    ELSE
        NEW.content_updated_at := COALESCE(NEW.content_updated_at, OLD.content_updated_at);
    END IF;
    IF TG_OP = 'INSERT' OR NEW.payload IS DISTINCT FROM OLD.payload THEN
        NEW.content_hash := CASE WHEN NEW.payload->>'kind' = 'markdown'
            THEN encode(digest(convert_to(NEW.payload->>'content', 'UTF8'), 'sha256'), 'hex')
        END;
    ELSE
        NEW.content_hash := OLD.content_hash;
    END IF;
    RETURN NEW;
END;
$$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0240-Normalize-complete-Unicode-emoji-identities-and-refresh-Zulip-reactions-dffae9.py"
        ]

    @property
    def migration_id(self):
        return "c8ac7d61-f574-4d7f-a8aa-90a690308545"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            CONTENT_VERSION_SQL
            % {
                "projection_guard": "AND current_setting('workspace.projected_message_content', true) IS DISTINCT FROM (NEW.project_id::text || ':' || NEW.uuid::text)"
            }
        )
        session.execute("""
            ALTER TABLE bridge_zulip_v5_message_contents
                ADD COLUMN IF NOT EXISTS conversion_version INTEGER NOT NULL DEFAULT 0;
            CREATE INDEX bridge_zulip_v5_message_contents_conversion_idx
                ON bridge_zulip_v5_message_contents(uuid) WHERE conversion_version < 1;
            CREATE TABLE bridge_zulip_v5_file_links (
                uuid UUID PRIMARY KEY,
                project_id UUID NOT NULL,
                hostname TEXT NOT NULL,
                stream_uuid UUID NOT NULL,
                file_uuid UUID NOT NULL,
                zulip_url TEXT NOT NULL,
                UNIQUE (project_id, hostname, stream_uuid, zulip_url),
                UNIQUE (project_id, hostname, stream_uuid, file_uuid),
                FOREIGN KEY (project_id, stream_uuid)
                    REFERENCES messenger_streams(project_id, uuid) ON DELETE CASCADE,
                FOREIGN KEY (file_uuid) REFERENCES m_workspace_files(uuid) ON DELETE CASCADE
            );
            CREATE TABLE bridge_zulip_v5_content_snapshots (
                uuid UUID PRIMARY KEY,
                delivery_uuid UUID NOT NULL
                    REFERENCES bridge_zulip_v5_deliveries(uuid) ON DELETE CASCADE,
                workspace_content TEXT NOT NULL,
                zulip_content TEXT NOT NULL,
                created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')
            );
            CREATE INDEX bridge_zulip_v5_content_snapshots_delivery_idx
                ON bridge_zulip_v5_content_snapshots(delivery_uuid);
            ALTER TABLE bridge_zulip_v5_deliveries
                ADD COLUMN content_observed_at TIMESTAMP WITHOUT TIME ZONE,
                ADD COLUMN observed_zulip_content TEXT,
                ADD COLUMN observed_workspace_content TEXT;
            INSERT INTO bridge_zulip_v5_tasks(uuid, priority, content)
            SELECT gen_random_uuid(), 150000,
                '{"kind":"convert_message_content","batch_size":100}'::jsonb
            WHERE EXISTS (SELECT 1 FROM bridge_zulip_v5_message_contents AS content
                JOIN bridge_zulip_v5_messages AS observed ON observed.content=content.uuid
                WHERE content.conversion_version < 1);
        """)

    def downgrade(self, session):
        session.execute(CONTENT_VERSION_SQL % {"projection_guard": ""})
        session.execute("""
            DELETE FROM bridge_zulip_v5_tasks WHERE content->>'kind' = 'convert_message_content';
            ALTER TABLE bridge_zulip_v5_deliveries DROP COLUMN content_observed_at,
                DROP COLUMN observed_zulip_content, DROP COLUMN observed_workspace_content;
            DROP TABLE bridge_zulip_v5_content_snapshots;
            DROP TABLE bridge_zulip_v5_file_links;
            ALTER TABLE bridge_zulip_v5_message_contents DROP COLUMN conversion_version;
        """)


migration_step = MigrationStep()
