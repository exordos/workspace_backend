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
ALTER FUNCTION messenger_uuid_v5(uuid, text) SET search_path = pg_catalog, public;

CREATE SEQUENCE messenger_message_change_revision_seq AS bigint;

CREATE TABLE messenger_message_changes (
    project_id uuid NOT NULL,
    message_uuid uuid NOT NULL,
    revision bigint NOT NULL DEFAULT nextval('messenger_message_change_revision_seq'),
    PRIMARY KEY (project_id, message_uuid)
);
CREATE TABLE bridge_zulip_v5_message_changes (
    project_id uuid NOT NULL,
    message_uuid uuid NOT NULL,
    revision bigint NOT NULL DEFAULT nextval('messenger_message_change_revision_seq'),
    PRIMARY KEY (project_id, message_uuid)
);

CREATE INDEX bridge_zulip_v5_messages_workspace_identity_idx
ON bridge_zulip_v5_messages (
    project_id, messenger_uuid_v5(project_id, message_uuid::text),
    updated_at DESC, uuid
) INCLUDE (external_account_uuid);

CREATE FUNCTION bridge_zulip_v5_mark_message_changed(
    target_project_id uuid, source_message_uuid uuid
) RETURNS void LANGUAGE sql AS $$
    INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
    VALUES (
        target_project_id,
        messenger_uuid_v5(target_project_id, source_message_uuid::text)
    )
    ON CONFLICT (project_id, message_uuid)
    DO UPDATE SET revision = EXCLUDED.revision;
$$;

CREATE FUNCTION bridge_zulip_v5_track_message_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        PERFORM bridge_zulip_v5_mark_message_changed(OLD.project_id, OLD.message_uuid);
    END IF;
    IF TG_OP <> 'DELETE' AND (
        TG_OP <> 'UPDATE'
        OR (NEW.project_id, NEW.message_uuid) IS DISTINCT FROM
           (OLD.project_id, OLD.message_uuid)
    ) THEN
        PERFORM bridge_zulip_v5_mark_message_changed(NEW.project_id, NEW.message_uuid);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_track_message_changes
AFTER INSERT OR UPDATE OR DELETE ON bridge_zulip_v5_messages
FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_message_changes();

CREATE FUNCTION bridge_zulip_v5_track_message_content_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
    SELECT DISTINCT message.project_id,
           messenger_uuid_v5(message.project_id, message.message_uuid::text)
    FROM bridge_zulip_v5_messages AS message
    WHERE message.content = NEW.uuid
    ON CONFLICT (project_id, message_uuid)
    DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_track_message_content_changes
AFTER UPDATE ON bridge_zulip_v5_message_contents
FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_message_content_changes();

CREATE FUNCTION bridge_zulip_v5_track_account_message_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
    SELECT DISTINCT message.project_id,
           messenger_uuid_v5(message.project_id, message.message_uuid::text)
    FROM bridge_zulip_v5_messages AS message
    WHERE message.external_account_uuid = NEW.uuid
    ON CONFLICT (project_id, message_uuid)
    DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_track_account_message_changes
AFTER UPDATE OF provider ON m_external_accounts_v2
FOR EACH ROW WHEN (OLD.provider IS DISTINCT FROM NEW.provider)
EXECUTE FUNCTION bridge_zulip_v5_track_account_message_changes();

CREATE FUNCTION messenger_mark_message_changed(
    target_project_id uuid, target_message_uuid uuid
) RETURNS void LANGUAGE sql AS $$
    INSERT INTO messenger_message_changes (project_id, message_uuid)
    VALUES (target_project_id, target_message_uuid)
    ON CONFLICT (project_id, message_uuid)
    DO UPDATE SET revision = EXCLUDED.revision;
$$;

CREATE FUNCTION messenger_track_message_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' AND OLD.source_name = 'zulip' THEN
        PERFORM messenger_mark_message_changed(OLD.project_id, OLD.uuid);
    END IF;
    IF TG_OP <> 'DELETE' AND NEW.source_name = 'zulip' AND (
        TG_OP <> 'UPDATE'
        OR OLD.source_name <> 'zulip'
        OR (NEW.project_id, NEW.uuid) IS DISTINCT FROM (OLD.project_id, OLD.uuid)
    ) THEN
        PERFORM messenger_mark_message_changed(NEW.project_id, NEW.uuid);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_message_changes
AFTER INSERT OR UPDATE OR DELETE ON messenger_messages
FOR EACH ROW EXECUTE FUNCTION messenger_track_message_changes();

INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
SELECT DISTINCT message.project_id,
       messenger_uuid_v5(message.project_id, message.message_uuid::text)
FROM bridge_zulip_v5_messages AS message
JOIN m_external_accounts_v2 AS account
  ON account.uuid = message.external_account_uuid
 AND account.project_id = message.project_id
WHERE account.provider = 'zulip';

INSERT INTO messenger_message_changes (project_id, message_uuid)
SELECT project_id, uuid FROM messenger_messages WHERE source_name = 'zulip';

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
"""


DOWNGRADE_SQL = """
CREATE OR REPLACE VIEW bridge_zulip_v5_message_states AS
WITH zulip_messages AS (
    SELECT DISTINCT ON (message.project_id, message.message_uuid)
           message.project_id,
           messenger_uuid_v5(message.project_id, message.message_uuid::text)
               AS message_uuid,
           message.uuid AS source_uuid
    FROM bridge_zulip_v5_messages AS message
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = message.external_account_uuid
     AND account.project_id = message.project_id
    WHERE account.provider = 'zulip'
    ORDER BY message.project_id, message.message_uuid,
             message.updated_at DESC, message.uuid
)
SELECT COALESCE(zulip.project_id, workspace.project_id) AS project_id,
       COALESCE(zulip.message_uuid, workspace.uuid) AS message_uuid,
       zulip.source_uuid,
       CASE WHEN zulip.source_uuid IS NULL THEN 'delete' ELSE 'create' END AS action
FROM zulip_messages AS zulip
FULL JOIN messenger_messages AS workspace
  ON workspace.project_id = zulip.project_id AND workspace.uuid = zulip.message_uuid
WHERE workspace.uuid IS NULL
   OR (workspace.source_name = 'zulip'
       AND (zulip.source_uuid IS NULL OR workspace.deleted_at IS NOT NULL));

DROP TRIGGER messenger_track_message_changes ON messenger_messages;
DROP TRIGGER bridge_zulip_v5_track_account_message_changes ON m_external_accounts_v2;
DROP TRIGGER bridge_zulip_v5_track_message_content_changes
    ON bridge_zulip_v5_message_contents;
DROP TRIGGER bridge_zulip_v5_track_message_changes ON bridge_zulip_v5_messages;
DROP FUNCTION messenger_track_message_changes();
DROP FUNCTION messenger_mark_message_changed(uuid, uuid);
DROP FUNCTION bridge_zulip_v5_track_account_message_changes();
DROP FUNCTION bridge_zulip_v5_track_message_content_changes();
DROP FUNCTION bridge_zulip_v5_track_message_changes();
DROP FUNCTION bridge_zulip_v5_mark_message_changed(uuid, uuid);
DROP INDEX bridge_zulip_v5_messages_workspace_identity_idx;
ALTER FUNCTION messenger_uuid_v5(uuid, text) RESET search_path;
DROP TABLE bridge_zulip_v5_message_changes, messenger_message_changes;
DROP SEQUENCE messenger_message_change_revision_seq;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0219-Add-Zulip-message-reconciliation-view-004ba4.py"]

    @property
    def migration_id(self):
        return "42f87634-6d99-4d21-b17c-750af63d8323"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
