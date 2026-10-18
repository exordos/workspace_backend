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


UPGRADE_SQL = """
LOCK TABLE bridge_zulip_v5_message_states IN ACCESS EXCLUSIVE MODE;
ALTER TABLE messenger_messages
    ADD COLUMN content_hash text,
    ADD COLUMN content_updated_at timestamp without time zone;
ALTER TABLE messenger_messages DISABLE TRIGGER messenger_track_message_changes;
ALTER TABLE messenger_messages DISABLE TRIGGER messenger_v2_mirror_message_update_to_legacy;
UPDATE messenger_messages
SET content_hash = CASE WHEN payload->>'kind' = 'markdown'
        THEN encode(digest(convert_to(payload->>'content', 'UTF8'), 'sha256'), 'hex')
    END,
    content_updated_at = updated_at;
ALTER TABLE messenger_messages ENABLE TRIGGER messenger_v2_mirror_message_update_to_legacy;
ALTER TABLE messenger_messages ENABLE TRIGGER messenger_track_message_changes;
ALTER TABLE messenger_messages ALTER COLUMN content_updated_at SET NOT NULL;

CREATE FUNCTION messenger_track_content_version()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.content_updated_at := COALESCE(NEW.content_updated_at, NEW.updated_at);
    ELSIF NEW.payload IS DISTINCT FROM OLD.payload AND (
        NEW.content_updated_at IS NULL
        OR NEW.content_updated_at IS NOT DISTINCT FROM OLD.content_updated_at
    ) THEN
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
CREATE TRIGGER messenger_track_content_version
BEFORE INSERT OR UPDATE ON messenger_messages
FOR EACH ROW EXECUTE FUNCTION messenger_track_content_version();

CREATE FUNCTION bridge_zulip_v5_hash_message_content()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.zulip_content IS DISTINCT FROM OLD.zulip_content THEN
        NEW.zulip_content_hash := encode(
            digest(convert_to(NEW.zulip_content, 'UTF8'), 'sha256'), 'hex'
        );
    ELSE
        NEW.zulip_content_hash := OLD.zulip_content_hash;
    END IF;
    IF TG_OP = 'INSERT' OR NEW.workspace_content IS DISTINCT FROM OLD.workspace_content THEN
        NEW.workspace_content_hash := encode(
            digest(convert_to(NEW.workspace_content, 'UTF8'), 'sha256'), 'hex'
        );
    ELSE
        NEW.workspace_content_hash := OLD.workspace_content_hash;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_hash_message_content
BEFORE INSERT OR UPDATE ON bridge_zulip_v5_message_contents
FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_hash_message_content();

CREATE OR REPLACE VIEW bridge_zulip_v5_message_states AS
WITH changes AS (
    SELECT project_id, message_uuid FROM bridge_zulip_v5_message_changes
    UNION ALL
    SELECT workspace.project_id, workspace.message_uuid
    FROM messenger_message_changes AS workspace
    WHERE NOT EXISTS (
        SELECT 1 FROM bridge_zulip_v5_message_changes AS zulip
        WHERE zulip.project_id = workspace.project_id
          AND zulip.message_uuid = workspace.message_uuid
    )
)
SELECT changes.project_id, changes.message_uuid, source.uuid AS source_uuid,
       CASE WHEN source.uuid IS NULL THEN 'delete'
            WHEN workspace.uuid IS NULL OR workspace.deleted_at IS NOT NULL
                THEN 'create'
            ELSE 'update' END AS action
FROM changes
LEFT JOIN LATERAL (
    SELECT message.uuid
    FROM bridge_zulip_v5_messages AS message
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = message.external_account_uuid
     AND account.project_id = message.project_id
    JOIN bridge_zulip_v5_message_contents AS content ON content.uuid = message.content
    WHERE message.project_id = changes.project_id
      AND messenger_uuid_v5(message.project_id, message.message_uuid::text)
          = changes.message_uuid
      AND account.provider = 'zulip'
    ORDER BY content.workspace_content_updated_at DESC, content.uuid, message.uuid
    LIMIT 1
) AS source ON true
LEFT JOIN messenger_messages AS workspace
  ON workspace.project_id = changes.project_id
 AND workspace.uuid = changes.message_uuid
WHERE (source.uuid IS NOT NULL AND workspace.uuid IS NULL)
   OR (workspace.source_name = 'zulip'
       AND (source.uuid IS NULL OR workspace.deleted_at IS NOT NULL
            OR (workspace.payload->>'kind' = 'markdown' AND EXISTS (
                SELECT 1
                FROM bridge_zulip_v5_messages AS observed
                JOIN m_external_accounts_v2 AS account
                  ON account.uuid = observed.external_account_uuid
                 AND account.project_id = observed.project_id
                JOIN bridge_zulip_v5_message_contents AS content
                  ON content.uuid = observed.content
                WHERE observed.project_id = changes.project_id
                  AND messenger_uuid_v5(observed.project_id, observed.message_uuid::text)
                      = changes.message_uuid
                  AND account.provider = 'zulip'
                  AND (content.workspace_content_hash IS DISTINCT FROM workspace.content_hash
                       OR content.workspace_content_updated_at
                           IS DISTINCT FROM workspace.content_updated_at)
            ))));

INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
SELECT DISTINCT message.project_id,
       messenger_uuid_v5(message.project_id, message.message_uuid::text)
FROM bridge_zulip_v5_messages AS message
JOIN m_external_accounts_v2 AS account
  ON account.uuid = message.external_account_uuid
 AND account.project_id = message.project_id
WHERE account.provider = 'zulip'
ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;

INSERT INTO messenger_message_changes (project_id, message_uuid)
SELECT project_id, uuid FROM messenger_messages WHERE source_name = 'zulip'
ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
"""


DOWNGRADE_SQL = """
CREATE OR REPLACE VIEW bridge_zulip_v5_message_states AS
WITH changes AS (
    SELECT project_id, message_uuid FROM bridge_zulip_v5_message_changes
    UNION ALL
    SELECT workspace.project_id, workspace.message_uuid
    FROM messenger_message_changes AS workspace
    WHERE NOT EXISTS (
        SELECT 1 FROM bridge_zulip_v5_message_changes AS zulip
        WHERE zulip.project_id = workspace.project_id
          AND zulip.message_uuid = workspace.message_uuid
    )
)
SELECT changes.project_id, changes.message_uuid, source.uuid AS source_uuid,
       CASE WHEN source.uuid IS NULL THEN 'delete' ELSE 'create' END AS action
FROM changes
LEFT JOIN LATERAL (
    SELECT message.uuid
    FROM bridge_zulip_v5_messages AS message
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = message.external_account_uuid
     AND account.project_id = message.project_id
    WHERE message.project_id = changes.project_id
      AND messenger_uuid_v5(message.project_id, message.message_uuid::text)
          = changes.message_uuid
      AND account.provider = 'zulip'
    ORDER BY message.updated_at DESC, message.uuid
    LIMIT 1
) AS source ON true
LEFT JOIN messenger_messages AS workspace
  ON workspace.project_id = changes.project_id
 AND workspace.uuid = changes.message_uuid
WHERE (source.uuid IS NOT NULL AND workspace.uuid IS NULL)
   OR (workspace.source_name = 'zulip'
       AND (source.uuid IS NULL OR workspace.deleted_at IS NOT NULL));

DROP TRIGGER bridge_zulip_v5_hash_message_content ON bridge_zulip_v5_message_contents;
DROP FUNCTION bridge_zulip_v5_hash_message_content();
DROP TRIGGER messenger_track_content_version ON messenger_messages;
DROP FUNCTION messenger_track_content_version();
ALTER TABLE messenger_messages DROP COLUMN content_hash, DROP COLUMN content_updated_at;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0220-Track-message-changes-for-bounded-Zulip-reconciliation-42f876.py"
        ]

    @property
    def migration_id(self):
        return "68dbbd54-36d7-4de3-be26-99928bf8de9f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
