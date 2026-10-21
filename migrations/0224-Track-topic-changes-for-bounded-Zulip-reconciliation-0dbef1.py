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
CREATE TABLE bridge_zulip_v5_topic_changes (
    project_id uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    revision bigint NOT NULL DEFAULT nextval('messenger_message_change_revision_seq'),
    PRIMARY KEY (project_id, topic_uuid)
);
CREATE INDEX bridge_zulip_v5_messages_project_topic_channel_idx
ON bridge_zulip_v5_messages (project_id, topic, channel, external_account_uuid);

CREATE FUNCTION bridge_zulip_v5_mark_topic_changed(target_project_id uuid, target_topic_uuid uuid)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    VALUES (target_project_id, target_topic_uuid)
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
$$;

CREATE FUNCTION bridge_zulip_v5_track_message_topic_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.project_id, NEW.topic, NEW.channel, NEW.external_account_uuid)
        IS NOT DISTINCT FROM (OLD.project_id, OLD.topic, OLD.channel, OLD.external_account_uuid) THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        PERFORM bridge_zulip_v5_mark_topic_changed(OLD.project_id, OLD.topic);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        PERFORM bridge_zulip_v5_mark_topic_changed(NEW.project_id, NEW.topic);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_track_message_topic_changes
AFTER INSERT OR DELETE OR UPDATE OF project_id, topic, channel, external_account_uuid
ON bridge_zulip_v5_messages
FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_message_topic_changes();

CREATE FUNCTION bridge_zulip_v5_track_channel_topic_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT DISTINCT project_id, topic FROM bridge_zulip_v5_messages WHERE channel = NEW.uuid
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_track_channel_topic_changes
AFTER UPDATE OF stream_uuid, external_account_uuid ON bridge_zulip_v5_channels
FOR EACH ROW WHEN ((NEW.stream_uuid, NEW.external_account_uuid)
    IS DISTINCT FROM (OLD.stream_uuid, OLD.external_account_uuid))
EXECUTE FUNCTION bridge_zulip_v5_track_channel_topic_changes();

CREATE FUNCTION bridge_zulip_v5_track_account_topic_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT DISTINCT project_id, topic FROM bridge_zulip_v5_messages
    WHERE external_account_uuid = NEW.uuid
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_track_account_topic_changes
AFTER UPDATE OF provider ON m_external_accounts_v2
FOR EACH ROW WHEN (NEW.provider IS DISTINCT FROM OLD.provider)
EXECUTE FUNCTION bridge_zulip_v5_track_account_topic_changes();

CREATE FUNCTION messenger_mark_zulip_stream_topics_changed(target_project_id uuid, target_stream_uuid uuid)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT message.project_id, message.topic
    FROM bridge_zulip_v5_messages AS message
    JOIN bridge_zulip_v5_channels AS channel ON channel.uuid = message.channel
    WHERE message.project_id = target_project_id AND channel.stream_uuid = target_stream_uuid
    UNION
    SELECT project_id, uuid FROM messenger_topics
    WHERE project_id = target_project_id AND stream_uuid = target_stream_uuid AND source_name = 'zulip'
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
$$;
CREATE FUNCTION messenger_track_zulip_stream_topic_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.project_id, NEW.uuid, NEW.source_name, NEW.deleted_at)
        IS NOT DISTINCT FROM (OLD.project_id, OLD.uuid, OLD.source_name, OLD.deleted_at) THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        PERFORM messenger_mark_zulip_stream_topics_changed(OLD.project_id, OLD.uuid);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        PERFORM messenger_mark_zulip_stream_topics_changed(NEW.project_id, NEW.uuid);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_zulip_stream_topic_changes
AFTER INSERT OR DELETE OR UPDATE OF project_id, uuid, source_name, deleted_at ON messenger_streams
FOR EACH ROW EXECUTE FUNCTION messenger_track_zulip_stream_topic_changes();

CREATE FUNCTION messenger_track_zulip_topic_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.project_id, NEW.uuid, NEW.stream_uuid, NEW.source_name, NEW.deleted_at)
        IS NOT DISTINCT FROM (OLD.project_id, OLD.uuid, OLD.stream_uuid, OLD.source_name, OLD.deleted_at) THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' AND (OLD.source_name = 'zulip'
        OR EXISTS (SELECT 1 FROM bridge_zulip_v5_topics WHERE uuid = OLD.uuid)) THEN
        PERFORM bridge_zulip_v5_mark_topic_changed(OLD.project_id, OLD.uuid);
    END IF;
    IF TG_OP <> 'DELETE' AND (NEW.source_name = 'zulip'
        OR EXISTS (SELECT 1 FROM bridge_zulip_v5_topics WHERE uuid = NEW.uuid)) THEN
        PERFORM bridge_zulip_v5_mark_topic_changed(NEW.project_id, NEW.uuid);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_zulip_topic_changes
AFTER INSERT OR DELETE OR UPDATE OF project_id, uuid, stream_uuid, source_name, deleted_at ON messenger_topics
FOR EACH ROW EXECUTE FUNCTION messenger_track_zulip_topic_changes();

CREATE FUNCTION messenger_mark_zulip_placement_topic_changed(target_project_id uuid, target_topic_uuid uuid)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT target_project_id, target_topic_uuid
    WHERE EXISTS (SELECT 1 FROM bridge_zulip_v5_topics WHERE uuid = target_topic_uuid)
       OR EXISTS (SELECT 1 FROM messenger_topics
                  WHERE project_id = target_project_id AND uuid = target_topic_uuid AND source_name = 'zulip')
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
$$;
CREATE FUNCTION messenger_track_zulip_placement_topic_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.project_id, NEW.topic_uuid)
        IS NOT DISTINCT FROM (OLD.project_id, OLD.topic_uuid) THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        PERFORM messenger_mark_zulip_placement_topic_changed(OLD.project_id, OLD.topic_uuid);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        PERFORM messenger_mark_zulip_placement_topic_changed(NEW.project_id, NEW.topic_uuid);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_zulip_placement_topic_changes
AFTER INSERT OR DELETE OR UPDATE OF project_id, topic_uuid ON messenger_message_placements
FOR EACH ROW EXECUTE FUNCTION messenger_track_zulip_placement_topic_changes();

CREATE OR REPLACE VIEW bridge_zulip_v5_topic_states AS
SELECT changes.project_id, COALESCE(source.stream_uuid, workspace.stream_uuid) AS stream_uuid,
       changes.topic_uuid,
       CASE WHEN workspace.uuid IS NULL THEN 'create' ELSE 'delete' END AS action
FROM bridge_zulip_v5_topic_changes AS changes
LEFT JOIN LATERAL (
    SELECT DISTINCT channel.stream_uuid
    FROM bridge_zulip_v5_channels AS channel
    JOIN m_external_accounts_v2 AS account ON account.uuid = channel.external_account_uuid
    JOIN messenger_streams AS stream
      ON stream.project_id = account.project_id AND stream.uuid = channel.stream_uuid
     AND stream.source_name = 'zulip' AND stream.deleted_at IS NULL
    WHERE account.project_id = changes.project_id AND account.provider = 'zulip'
      AND EXISTS (
          SELECT 1 FROM bridge_zulip_v5_messages AS message
          WHERE message.project_id = changes.project_id AND message.topic = changes.topic_uuid
            AND message.channel = channel.uuid AND message.external_account_uuid = account.uuid
      )
) AS source ON true
LEFT JOIN messenger_topics AS workspace
  ON workspace.project_id = changes.project_id AND workspace.uuid = changes.topic_uuid
 AND workspace.deleted_at IS NULL
WHERE (source.stream_uuid IS NOT NULL AND workspace.uuid IS NULL)
   OR (source.stream_uuid IS NULL AND workspace.source_name = 'zulip'
       AND NOT EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                       WHERE placement.project_id = changes.project_id AND placement.topic_uuid = changes.topic_uuid));

INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
SELECT project_id, topic FROM bridge_zulip_v5_messages
UNION SELECT project_id, uuid FROM messenger_topics WHERE source_name = 'zulip';
ANALYZE bridge_zulip_v5_topic_changes;
"""


DOWNGRADE_SQL = """
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
   OR (zulip.topic_uuid IS NULL AND workspace.source_name = 'zulip'
       AND NOT EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                       WHERE placement.project_id = workspace.project_id AND placement.topic_uuid = workspace.topic_uuid));

DROP TRIGGER messenger_track_zulip_placement_topic_changes ON messenger_message_placements;
DROP TRIGGER messenger_track_zulip_topic_changes ON messenger_topics;
DROP TRIGGER messenger_track_zulip_stream_topic_changes ON messenger_streams;
DROP TRIGGER bridge_zulip_v5_track_account_topic_changes ON m_external_accounts_v2;
DROP TRIGGER bridge_zulip_v5_track_channel_topic_changes ON bridge_zulip_v5_channels;
DROP TRIGGER bridge_zulip_v5_track_message_topic_changes ON bridge_zulip_v5_messages;
DROP FUNCTION messenger_track_zulip_placement_topic_changes();
DROP FUNCTION messenger_mark_zulip_placement_topic_changed(uuid, uuid);
DROP FUNCTION messenger_track_zulip_topic_changes();
DROP FUNCTION messenger_track_zulip_stream_topic_changes();
DROP FUNCTION messenger_mark_zulip_stream_topics_changed(uuid, uuid);
DROP FUNCTION bridge_zulip_v5_track_account_topic_changes();
DROP FUNCTION bridge_zulip_v5_track_channel_topic_changes();
DROP FUNCTION bridge_zulip_v5_track_message_topic_changes();
DROP FUNCTION bridge_zulip_v5_mark_topic_changed(uuid, uuid);
DROP INDEX bridge_zulip_v5_messages_project_topic_channel_idx;
DROP TABLE bridge_zulip_v5_topic_changes;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0223-Reconcile-Zulip-user-message-bindings-and-states-6430fc.py"
        ]

    @property
    def migration_id(self):
        return "0dbef174-4896-4c56-b14c-164757d53ec1"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
