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
LOCK TABLE bridge_zulip_v5_stream_states, bridge_zulip_v5_topic_states,
    bridge_zulip_v5_message_states IN ACCESS EXCLUSIVE MODE;
ALTER TABLE bridge_zulip_v5_messages
    ADD COLUMN placement_updated_at timestamp without time zone;
ALTER TABLE bridge_zulip_v5_messages DISABLE TRIGGER bridge_zulip_v5_track_message_changes;
UPDATE bridge_zulip_v5_messages AS message
SET placement_updated_at = content.zulip_content_updated_at
FROM bridge_zulip_v5_message_contents AS content WHERE content.uuid = message.content;
ALTER TABLE bridge_zulip_v5_messages ENABLE TRIGGER bridge_zulip_v5_track_message_changes;
ALTER TABLE bridge_zulip_v5_messages ALTER COLUMN placement_updated_at SET NOT NULL;

CREATE FUNCTION bridge_zulip_v5_track_placement_version()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.placement_updated_at := COALESCE(NEW.placement_updated_at, (
            SELECT zulip_content_updated_at FROM bridge_zulip_v5_message_contents
            WHERE uuid = NEW.content
        ));
    ELSIF NEW.placement_updated_at < OLD.placement_updated_at THEN
        NEW.channel := OLD.channel;
        NEW.topic := OLD.topic;
        NEW.placement_updated_at := OLD.placement_updated_at;
    ELSIF (NEW.channel, NEW.topic) IS DISTINCT FROM (OLD.channel, OLD.topic)
          AND (NEW.placement_updated_at IS NULL
               OR NEW.placement_updated_at IS NOT DISTINCT FROM OLD.placement_updated_at) THEN
        NEW.placement_updated_at := statement_timestamp() AT TIME ZONE 'UTC';
    ELSE
        NEW.placement_updated_at := COALESCE(NEW.placement_updated_at, OLD.placement_updated_at);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_track_placement_version
BEFORE INSERT OR UPDATE ON bridge_zulip_v5_messages
FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_placement_version();

CREATE INDEX bridge_zulip_v5_messages_workspace_placement_idx
ON bridge_zulip_v5_messages (
    project_id, messenger_uuid_v5(project_id, message_uuid::text),
    placement_updated_at DESC, uuid
) INCLUDE (channel, topic, external_account_uuid);

CREATE FUNCTION messenger_track_zulip_placement_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT'
       AND OLD.uuid = messenger_uuid_v5(OLD.message_uuid, 'zulip-placement')
       AND EXISTS (
           SELECT 1 FROM messenger_messages
           WHERE project_id = OLD.project_id AND uuid = OLD.message_uuid
             AND source_name = 'zulip'
       ) THEN
        PERFORM messenger_mark_message_changed(OLD.project_id, OLD.message_uuid);
    END IF;
    IF TG_OP <> 'DELETE'
       AND NEW.uuid = messenger_uuid_v5(NEW.message_uuid, 'zulip-placement')
       AND (TG_OP <> 'UPDATE' OR (NEW.project_id, NEW.message_uuid, NEW.uuid)
            IS DISTINCT FROM (OLD.project_id, OLD.message_uuid, OLD.uuid))
       AND EXISTS (
           SELECT 1 FROM messenger_messages
           WHERE project_id = NEW.project_id AND uuid = NEW.message_uuid
             AND source_name = 'zulip'
       ) THEN
        PERFORM messenger_mark_message_changed(NEW.project_id, NEW.message_uuid);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_zulip_placement_changes
AFTER INSERT OR UPDATE OR DELETE ON messenger_message_placements
FOR EACH ROW EXECUTE FUNCTION messenger_track_zulip_placement_changes();

CREATE FUNCTION bridge_zulip_v5_track_channel_placement_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
    SELECT DISTINCT project_id, messenger_uuid_v5(project_id, message_uuid::text)
    FROM bridge_zulip_v5_messages WHERE channel = NEW.uuid
    ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_track_channel_placement_changes
AFTER UPDATE OF stream_uuid ON bridge_zulip_v5_channels
FOR EACH ROW WHEN (OLD.stream_uuid IS DISTINCT FROM NEW.stream_uuid)
EXECUTE FUNCTION bridge_zulip_v5_track_channel_placement_changes();

CREATE VIEW bridge_zulip_v5_message_placement_states AS
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
SELECT changes.project_id, changes.message_uuid,
       messenger_uuid_v5(changes.message_uuid, 'zulip-placement') AS placement_uuid,
       source.stream_uuid, source.topic_uuid,
       CASE WHEN source.uuid IS NULL OR canonical.deleted_at IS NOT NULL THEN 'delete'
            WHEN placement.uuid IS NULL THEN 'create' ELSE 'update' END AS action
FROM changes
JOIN messenger_messages AS canonical
  ON canonical.project_id = changes.project_id
 AND canonical.uuid = changes.message_uuid AND canonical.source_name = 'zulip'
LEFT JOIN LATERAL (
    SELECT message.uuid, channel.stream_uuid, message.topic AS topic_uuid
    FROM bridge_zulip_v5_messages AS message
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = message.external_account_uuid
     AND account.project_id = message.project_id
    JOIN bridge_zulip_v5_channels AS channel ON channel.uuid = message.channel
    WHERE message.project_id = changes.project_id
      AND messenger_uuid_v5(message.project_id, message.message_uuid::text) = changes.message_uuid
      AND account.provider = 'zulip'
    ORDER BY message.placement_updated_at DESC, message.uuid
    LIMIT 1
) AS source ON true
LEFT JOIN messenger_message_placements AS placement
  ON placement.project_id = changes.project_id
 AND placement.uuid = messenger_uuid_v5(changes.message_uuid, 'zulip-placement')
WHERE (placement.uuid IS NOT NULL
       AND (source.uuid IS NULL OR canonical.deleted_at IS NOT NULL))
   OR (source.uuid IS NOT NULL AND canonical.deleted_at IS NULL
       AND (placement.uuid IS NULL
            OR (placement.message_uuid, placement.stream_uuid, placement.topic_uuid)
               IS DISTINCT FROM (changes.message_uuid, source.stream_uuid, source.topic_uuid)));

ALTER TABLE m_workspace_messages ADD COLUMN canonical_message_uuid uuid;
UPDATE m_workspace_messages AS legacy SET canonical_message_uuid = placement.message_uuid
FROM messenger_message_placements AS placement
WHERE placement.project_id = legacy.project_id
  AND COALESCE(placement.legacy_public_uuid, placement.uuid) = legacy.uuid;
ALTER TABLE m_workspace_messages ADD CONSTRAINT m_workspace_messages_canonical_message_fkey
    FOREIGN KEY (project_id, canonical_message_uuid)
    REFERENCES messenger_messages(project_id, uuid) ON DELETE CASCADE;
CREATE INDEX m_workspace_messages_canonical_message_idx
    ON m_workspace_messages(project_id, canonical_message_uuid);

CREATE FUNCTION messenger_link_legacy_message_to_canonical()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT placement.message_uuid INTO NEW.canonical_message_uuid
    FROM messenger_message_placements AS placement
    WHERE placement.project_id = NEW.project_id
      AND (placement.legacy_public_uuid = NEW.uuid
           OR (placement.legacy_public_uuid IS NULL AND placement.uuid = NEW.uuid));
    RETURN NEW;
END;
$$;
CREATE TRIGGER messenger_link_legacy_message_to_canonical
BEFORE INSERT OR UPDATE ON m_workspace_messages
FOR EACH ROW EXECUTE FUNCTION messenger_link_legacy_message_to_canonical();

INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
SELECT DISTINCT message.project_id, messenger_uuid_v5(message.project_id, message.message_uuid::text)
FROM bridge_zulip_v5_messages AS message
JOIN m_external_accounts_v2 AS account
  ON account.uuid = message.external_account_uuid AND account.project_id = message.project_id
WHERE account.provider = 'zulip'
ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;

INSERT INTO messenger_message_changes (project_id, message_uuid)
SELECT project_id, uuid FROM messenger_messages WHERE source_name = 'zulip'
ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
"""


DOWNGRADE_SQL = """
DROP VIEW bridge_zulip_v5_message_placement_states;
DROP TRIGGER messenger_link_legacy_message_to_canonical ON m_workspace_messages;
DROP FUNCTION messenger_link_legacy_message_to_canonical();
ALTER TABLE m_workspace_messages DROP COLUMN canonical_message_uuid;
DROP TRIGGER bridge_zulip_v5_track_channel_placement_changes ON bridge_zulip_v5_channels;
DROP FUNCTION bridge_zulip_v5_track_channel_placement_changes();
DROP TRIGGER messenger_track_zulip_placement_changes ON messenger_message_placements;
DROP FUNCTION messenger_track_zulip_placement_changes();
DROP INDEX bridge_zulip_v5_messages_workspace_placement_idx;
DROP TRIGGER bridge_zulip_v5_track_placement_version ON bridge_zulip_v5_messages;
DROP FUNCTION bridge_zulip_v5_track_placement_version();
ALTER TABLE bridge_zulip_v5_messages DROP COLUMN placement_updated_at;
"""


PARENT_VIEWS_SQL = """
CREATE OR REPLACE VIEW bridge_zulip_v5_stream_states AS
WITH zulip_streams AS (
    SELECT DISTINCT account.project_id, channel.stream_uuid
    FROM bridge_zulip_v5_channels AS channel
    JOIN m_external_accounts_v2 AS account ON account.uuid = channel.external_account_uuid
    WHERE account.provider = 'zulip'
), workspace_streams AS (
    SELECT project_id, uuid AS stream_uuid, source_name FROM messenger_streams
    WHERE deleted_at IS NULL
)
SELECT COALESCE(zulip.project_id, workspace.project_id) AS project_id,
       COALESCE(zulip.stream_uuid, workspace.stream_uuid) AS stream_uuid,
       CASE WHEN workspace.stream_uuid IS NULL THEN 'create' ELSE 'delete' END AS action
FROM zulip_streams AS zulip FULL JOIN workspace_streams AS workspace
  ON workspace.project_id = zulip.project_id AND workspace.stream_uuid = zulip.stream_uuid
WHERE workspace.stream_uuid IS NULL
   OR (zulip.stream_uuid IS NULL AND workspace.source_name = 'zulip' {stream_guard});

CREATE OR REPLACE VIEW bridge_zulip_v5_topic_states AS
WITH zulip_topics AS (
    SELECT DISTINCT account.project_id, channel.stream_uuid, message.topic AS topic_uuid
    FROM bridge_zulip_v5_messages AS message
    JOIN bridge_zulip_v5_channels AS channel
      ON channel.uuid = message.channel AND channel.external_account_uuid = message.external_account_uuid
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = channel.external_account_uuid AND account.project_id = message.project_id
    JOIN messenger_streams AS stream
      ON stream.project_id = account.project_id AND stream.uuid = channel.stream_uuid
     AND stream.source_name = 'zulip' AND stream.deleted_at IS NULL
    WHERE account.provider = 'zulip'
), workspace_topics AS (
    SELECT project_id, stream_uuid, uuid AS topic_uuid, source_name FROM messenger_topics
    WHERE deleted_at IS NULL
)
SELECT COALESCE(zulip.project_id, workspace.project_id) AS project_id,
       COALESCE(zulip.stream_uuid, workspace.stream_uuid) AS stream_uuid,
       COALESCE(zulip.topic_uuid, workspace.topic_uuid) AS topic_uuid,
       CASE WHEN workspace.topic_uuid IS NULL THEN 'create' ELSE 'delete' END AS action
FROM zulip_topics AS zulip FULL JOIN workspace_topics AS workspace
  ON workspace.project_id = zulip.project_id AND workspace.topic_uuid = zulip.topic_uuid
WHERE workspace.topic_uuid IS NULL
   OR (zulip.topic_uuid IS NULL AND workspace.source_name = 'zulip' {topic_guard});
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0221-Reconcile-Zulip-message-content-by-hash-and-edit-time-68dbbd.py"
        ]

    @property
    def migration_id(self):
        return "1f12c460-1055-49ea-9e97-e704c61419df"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            UPGRADE_SQL
            + PARENT_VIEWS_SQL.format(
                stream_guard="AND NOT EXISTS (SELECT 1 FROM messenger_message_placements AS placement "
                "WHERE placement.project_id = workspace.project_id "
                "AND placement.stream_uuid = workspace.stream_uuid)",
                topic_guard="AND NOT EXISTS (SELECT 1 FROM messenger_message_placements AS placement "
                "WHERE placement.project_id = workspace.project_id "
                "AND placement.topic_uuid = workspace.topic_uuid)",
            )
        )

    def downgrade(self, session):
        session.execute(
            PARENT_VIEWS_SQL.format(stream_guard="", topic_guard="") + DOWNGRADE_SQL
        )


migration_step = MigrationStep()
