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
ALTER TABLE messenger_user_topic_bindings ADD COLUMN notification_updated_at timestamp;
ALTER TABLE bridge_zulip_v5_channels ADD COLUMN notification_observed_at timestamp NOT NULL DEFAULT 'epoch';
ALTER TABLE bridge_zulip_v5_channels ADD COLUMN notification_event_id bigint NOT NULL DEFAULT -1;
ALTER TABLE bridge_zulip_v5_channels ADD COLUMN topic_notifications_observed_at timestamp NOT NULL DEFAULT 'epoch';
ALTER TABLE bridge_zulip_v5_topic_notifications ADD COLUMN observed_at timestamp NOT NULL DEFAULT 'epoch';
ALTER TABLE bridge_zulip_v5_topic_notifications ADD COLUMN event_id bigint NOT NULL DEFAULT -1;
CREATE INDEX bridge_zulip_v5_pending_topic_notifications_idx
ON bridge_zulip_v5_tasks (external_account_uuid, (content->>'channel_uuid'), (content->>'topic_uuid'))
WHERE content->>'kind'='update_notifications' AND status IN ('new','waiting','retry');
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_topic_notification_snapshot()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT topic.project_id, topic.uuid
    FROM m_external_accounts_v2 AS account
    JOIN messenger_topics AS topic
      ON topic.project_id = account.project_id AND topic.stream_uuid = NEW.stream_uuid
    WHERE account.uuid = NEW.external_account_uuid
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_topic_notification_owner()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT topic.project_id, topic.uuid
    FROM bridge_zulip_v5_channels AS channel
    JOIN messenger_topics AS topic ON topic.stream_uuid = channel.stream_uuid
    WHERE channel.external_account_uuid = NEW.uuid
      AND topic.project_id IN (NEW.project_id, OLD.project_id)
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE OR REPLACE FUNCTION messenger_track_zulip_topic_notification_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.notification_mode = OLD.notification_mode THEN
        RETURN NULL;
    END IF;
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT project_id, uuid FROM messenger_topics
    WHERE project_id = NEW.project_id AND uuid = NEW.topic_uuid
      AND stream_uuid IN (SELECT uuid FROM messenger_streams WHERE project_id=NEW.project_id AND source_name='zulip')
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
DROP VIEW bridge_zulip_v5_stream_binding_states;

CREATE VIEW bridge_zulip_v5_stream_binding_states AS
WITH zulip_bindings AS (
    SELECT DISTINCT account.project_id, channel.stream_uuid,
           account.owner_user_uuid AS user_uuid, channel.notification_mode,
           account.uuid AS account_uuid, channel.uuid AS channel_uuid, channel.notification_observed_at, channel.notification_event_id
    FROM bridge_zulip_v5_channels AS channel
    JOIN m_external_accounts_v2 AS account ON account.uuid = channel.external_account_uuid
    JOIN messenger_streams AS stream
      ON stream.project_id = account.project_id AND stream.uuid = channel.stream_uuid
    WHERE account.provider = 'zulip' AND stream.source_name = 'zulip' AND stream.deleted_at IS NULL
), workspace_bindings AS (
    SELECT binding.project_id, binding.stream_uuid, binding.user_uuid,
           binding.uuid, binding.active, binding.notification_mode,
           binding.notification_updated_at AT TIME ZONE 'UTC' AS notification_updated_at
    FROM messenger_stream_bindings AS binding
    JOIN messenger_streams AS stream
      ON stream.project_id = binding.project_id AND stream.uuid = binding.stream_uuid
    WHERE stream.source_name = 'zulip' AND stream.deleted_at IS NULL
)
SELECT COALESCE(zulip.project_id, workspace.project_id) AS project_id,
       COALESCE(zulip.stream_uuid, workspace.stream_uuid) AS stream_uuid,
       COALESCE(zulip.user_uuid, workspace.user_uuid) AS user_uuid,
       COALESCE(workspace.uuid, messenger_uuid_v5(zulip.project_id,
           'zulip-binding:' || zulip.stream_uuid::text || ':' || zulip.user_uuid::text)) AS binding_uuid,
       CASE WHEN zulip.user_uuid IS NULL THEN 'delete'
            WHEN workspace.uuid IS NULL OR NOT workspace.active THEN 'create'
            ELSE 'update' END AS action,
       zulip.notification_mode, zulip.notification_observed_at AS observed_at
FROM zulip_bindings AS zulip
FULL JOIN workspace_bindings AS workspace
  ON workspace.project_id = zulip.project_id AND workspace.stream_uuid = zulip.stream_uuid
 AND workspace.user_uuid = zulip.user_uuid
WHERE (zulip.user_uuid IS NOT NULL AND (workspace.uuid IS NULL OR NOT workspace.active
       OR (zulip.notification_mode IS NOT NULL AND zulip.notification_mode IS DISTINCT FROM workspace.notification_mode
           AND (workspace.notification_updated_at IS NULL OR zulip.notification_observed_at>=workspace.notification_updated_at)
           AND (zulip.notification_event_id>=0 OR NOT EXISTS (
               SELECT 1 FROM bridge_zulip_v5_tasks AS task WHERE task.external_account_uuid=zulip.account_uuid
                 AND task.content->>'kind'='update_notifications' AND task.content->>'topic_uuid' IS NULL
                 AND task.content->>'channel_uuid'=zulip.channel_uuid::text
                 AND task.status IN ('new','waiting','retry')
                 AND (task.content->>'changed_at')::timestamp=workspace.notification_updated_at
           )))))
   OR (zulip.user_uuid IS NULL AND workspace.active);

DROP VIEW bridge_zulip_v5_topic_notification_states;
DROP TRIGGER bridge_zulip_v5_track_topic_notification_changes ON bridge_zulip_v5_topic_notifications;
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_topic_notification_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_channel uuid;
    target_name text;
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.visibility_policy, NEW.observed_at, NEW.event_id)
        IS NOT DISTINCT FROM (OLD.visibility_policy, OLD.observed_at, OLD.event_id) THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' THEN
        target_channel := OLD.channel_uuid;
        target_name := OLD.topic_name;
    ELSE
        target_channel := NEW.channel_uuid;
        target_name := NEW.topic_name;
    END IF;
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT topic.project_id, topic.uuid
    FROM bridge_zulip_v5_channels AS channel
    JOIN m_external_accounts_v2 AS account ON account.uuid = channel.external_account_uuid
    JOIN messenger_topics AS topic
      ON topic.project_id = account.project_id AND topic.stream_uuid = channel.stream_uuid
    WHERE channel.uuid = target_channel
      AND CASE WHEN topic.uuid = messenger_uuid_v5(channel.stream_uuid, '') THEN ''
               ELSE lower(topic.name) END = target_name
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE TRIGGER bridge_zulip_v5_track_topic_notification_changes
AFTER INSERT OR DELETE OR UPDATE OF visibility_policy, observed_at, event_id ON bridge_zulip_v5_topic_notifications
FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_topic_notification_changes();
DROP TRIGGER bridge_zulip_v5_track_topic_notification_snapshot ON bridge_zulip_v5_channels;
CREATE TRIGGER bridge_zulip_v5_track_topic_notification_snapshot
AFTER UPDATE OF topic_notifications_synced, topic_notifications_observed_at ON bridge_zulip_v5_channels
FOR EACH ROW WHEN (NEW.topic_notifications_synced AND
    (NOT OLD.topic_notifications_synced OR NEW.topic_notifications_observed_at>OLD.topic_notifications_observed_at))
EXECUTE FUNCTION bridge_zulip_v5_track_topic_notification_snapshot();
CREATE VIEW bridge_zulip_v5_topic_notification_states AS
SELECT topic.project_id, binding.user_uuid, topic.uuid AS topic_uuid, topic.stream_uuid,
       CASE COALESCE(preference.visibility_policy, 0)
           WHEN 1 THEN 'mute' WHEN 2 THEN 'unmute' WHEN 3 THEN 'follow'
           ELSE 'default' END AS notification_mode,
       COALESCE(preference.observed_at, channel.topic_notifications_observed_at) AS observed_at
FROM bridge_zulip_v5_topic_changes AS pending
JOIN messenger_topics AS topic
  ON topic.project_id = pending.project_id AND topic.uuid = pending.topic_uuid
 AND topic.deleted_at IS NULL
JOIN messenger_streams AS stream
  ON stream.project_id = topic.project_id AND stream.uuid = topic.stream_uuid
 AND stream.source_name = 'zulip' AND stream.deleted_at IS NULL
JOIN messenger_user_topic_bindings AS binding
  ON binding.project_id = topic.project_id AND binding.topic_uuid = topic.uuid
JOIN messenger_stream_bindings AS membership
  ON membership.project_id = topic.project_id AND membership.stream_uuid = topic.stream_uuid
 AND membership.user_uuid = binding.user_uuid AND membership.active
JOIN m_external_accounts_v2 AS account
  ON account.project_id = topic.project_id AND account.owner_user_uuid = binding.user_uuid
 AND account.provider = 'zulip'
JOIN bridge_zulip_v5_channels AS channel
  ON channel.external_account_uuid = account.uuid AND channel.stream_uuid = topic.stream_uuid
 AND channel.channel_type = 'channel' AND channel.topic_notifications_synced
LEFT JOIN bridge_zulip_v5_topic_notifications AS preference
  ON preference.channel_uuid = channel.uuid
 AND preference.topic_name = CASE WHEN topic.uuid = messenger_uuid_v5(topic.stream_uuid, '') THEN ''
                                 ELSE lower(topic.name) END
WHERE binding.notification_mode IS DISTINCT FROM
      CASE COALESCE(preference.visibility_policy, 0)
          WHEN 1 THEN 'mute' WHEN 2 THEN 'unmute' WHEN 3 THEN 'follow'
          ELSE 'default' END
  AND (binding.notification_updated_at IS NULL
       OR binding.notification_updated_at <= COALESCE(preference.observed_at, channel.topic_notifications_observed_at))
  AND (COALESCE(preference.event_id, -1) >= 0 OR NOT EXISTS (
      SELECT 1 FROM bridge_zulip_v5_tasks AS task
      WHERE task.external_account_uuid=account.uuid
        AND task.content->>'kind'='update_notifications'
        AND task.content->>'topic_uuid'=topic.uuid::text
        AND task.status IN ('new', 'waiting', 'retry')
        AND (task.content->>'changed_at')::timestamp=binding.notification_updated_at
  ));
"""

DOWNGRADE_SQL = """
DROP VIEW bridge_zulip_v5_stream_binding_states;

CREATE VIEW bridge_zulip_v5_stream_binding_states AS
WITH zulip_bindings AS (
    SELECT DISTINCT account.project_id, channel.stream_uuid,
           account.owner_user_uuid AS user_uuid, channel.notification_mode
    FROM bridge_zulip_v5_channels AS channel
    JOIN m_external_accounts_v2 AS account ON account.uuid = channel.external_account_uuid
    JOIN messenger_streams AS stream
      ON stream.project_id = account.project_id AND stream.uuid = channel.stream_uuid
    WHERE account.provider = 'zulip' AND stream.source_name = 'zulip' AND stream.deleted_at IS NULL
), workspace_bindings AS (
    SELECT binding.project_id, binding.stream_uuid, binding.user_uuid,
           binding.uuid, binding.active, binding.notification_mode
    FROM messenger_stream_bindings AS binding
    JOIN messenger_streams AS stream
      ON stream.project_id = binding.project_id AND stream.uuid = binding.stream_uuid
    WHERE stream.source_name = 'zulip' AND stream.deleted_at IS NULL
)
SELECT COALESCE(zulip.project_id, workspace.project_id) AS project_id,
       COALESCE(zulip.stream_uuid, workspace.stream_uuid) AS stream_uuid,
       COALESCE(zulip.user_uuid, workspace.user_uuid) AS user_uuid,
       COALESCE(workspace.uuid, messenger_uuid_v5(zulip.project_id,
           'zulip-binding:' || zulip.stream_uuid::text || ':' || zulip.user_uuid::text)) AS binding_uuid,
       CASE WHEN zulip.user_uuid IS NULL THEN 'delete'
            WHEN workspace.uuid IS NULL OR NOT workspace.active THEN 'create'
            ELSE 'update' END AS action,
       zulip.notification_mode
FROM zulip_bindings AS zulip
FULL JOIN workspace_bindings AS workspace
  ON workspace.project_id = zulip.project_id AND workspace.stream_uuid = zulip.stream_uuid
 AND workspace.user_uuid = zulip.user_uuid
WHERE (zulip.user_uuid IS NOT NULL AND (workspace.uuid IS NULL OR NOT workspace.active
       OR (zulip.notification_mode IS NOT NULL AND zulip.notification_mode IS DISTINCT FROM workspace.notification_mode)))
   OR (zulip.user_uuid IS NULL AND workspace.active);

DROP VIEW bridge_zulip_v5_topic_notification_states;
DROP INDEX bridge_zulip_v5_pending_topic_notifications_idx;
DROP TRIGGER bridge_zulip_v5_track_topic_notification_changes ON bridge_zulip_v5_topic_notifications;
DROP TRIGGER bridge_zulip_v5_track_topic_notification_snapshot ON bridge_zulip_v5_channels;
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_topic_notification_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_channel uuid;
    target_name text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.visibility_policy = OLD.visibility_policy THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' THEN
        target_channel := OLD.channel_uuid;
        target_name := OLD.topic_name;
    ELSE
        target_channel := NEW.channel_uuid;
        target_name := NEW.topic_name;
    END IF;
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT topic.project_id, topic.uuid
    FROM bridge_zulip_v5_channels AS channel
    JOIN m_external_accounts_v2 AS account ON account.uuid = channel.external_account_uuid
    JOIN messenger_topics AS topic
      ON topic.project_id = account.project_id AND topic.stream_uuid = channel.stream_uuid
    WHERE channel.uuid = target_channel AND topic.source_name = 'zulip'
      AND CASE WHEN topic.uuid = messenger_uuid_v5(channel.stream_uuid, '') THEN ''
               ELSE lower(topic.name) END = target_name
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_topic_notification_snapshot()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT topic.project_id, topic.uuid
    FROM m_external_accounts_v2 AS account
    JOIN messenger_topics AS topic
      ON topic.project_id = account.project_id AND topic.stream_uuid = NEW.stream_uuid
    WHERE account.uuid = NEW.external_account_uuid AND topic.source_name = 'zulip'
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_topic_notification_owner()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT topic.project_id, topic.uuid
    FROM bridge_zulip_v5_channels AS channel
    JOIN messenger_topics AS topic ON topic.stream_uuid = channel.stream_uuid
    WHERE channel.external_account_uuid = NEW.uuid
      AND topic.project_id IN (NEW.project_id, OLD.project_id) AND topic.source_name = 'zulip'
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE OR REPLACE FUNCTION messenger_track_zulip_topic_notification_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.notification_mode = OLD.notification_mode THEN
        RETURN NULL;
    END IF;
    INSERT INTO bridge_zulip_v5_topic_changes (project_id, topic_uuid)
    SELECT project_id, uuid FROM messenger_topics
    WHERE project_id = NEW.project_id AND uuid = NEW.topic_uuid AND source_name = 'zulip'
    ON CONFLICT (project_id, topic_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
ALTER TABLE messenger_user_topic_bindings DROP COLUMN notification_updated_at;
ALTER TABLE bridge_zulip_v5_channels DROP COLUMN notification_observed_at;
ALTER TABLE bridge_zulip_v5_channels DROP COLUMN notification_event_id;
ALTER TABLE bridge_zulip_v5_channels DROP COLUMN topic_notifications_observed_at;
ALTER TABLE bridge_zulip_v5_topic_notifications DROP COLUMN observed_at;
ALTER TABLE bridge_zulip_v5_topic_notifications DROP COLUMN event_id;
CREATE TRIGGER bridge_zulip_v5_track_topic_notification_changes
AFTER INSERT OR DELETE OR UPDATE OF visibility_policy ON bridge_zulip_v5_topic_notifications
FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_topic_notification_changes();
CREATE TRIGGER bridge_zulip_v5_track_topic_notification_snapshot
AFTER UPDATE OF topic_notifications_synced ON bridge_zulip_v5_channels
FOR EACH ROW WHEN (NEW.topic_notifications_synced AND NOT OLD.topic_notifications_synced)
EXECUTE FUNCTION bridge_zulip_v5_track_topic_notification_snapshot();
CREATE VIEW bridge_zulip_v5_topic_notification_states AS
SELECT topic.project_id, binding.user_uuid, topic.uuid AS topic_uuid, topic.stream_uuid,
       CASE COALESCE(preference.visibility_policy, 0)
           WHEN 1 THEN 'mute' WHEN 2 THEN 'unmute' WHEN 3 THEN 'follow'
           ELSE 'default' END AS notification_mode
FROM bridge_zulip_v5_topic_changes AS pending
JOIN messenger_topics AS topic
  ON topic.project_id = pending.project_id AND topic.uuid = pending.topic_uuid
 AND topic.source_name = 'zulip' AND topic.deleted_at IS NULL
JOIN messenger_streams AS stream
  ON stream.project_id = topic.project_id AND stream.uuid = topic.stream_uuid
 AND stream.source_name = 'zulip' AND stream.deleted_at IS NULL
JOIN messenger_user_topic_bindings AS binding
  ON binding.project_id = topic.project_id AND binding.topic_uuid = topic.uuid
JOIN messenger_stream_bindings AS membership
  ON membership.project_id = topic.project_id AND membership.stream_uuid = topic.stream_uuid
 AND membership.user_uuid = binding.user_uuid AND membership.active
JOIN m_external_accounts_v2 AS account
  ON account.project_id = topic.project_id AND account.owner_user_uuid = binding.user_uuid
 AND account.provider = 'zulip'
JOIN bridge_zulip_v5_channels AS channel
  ON channel.external_account_uuid = account.uuid AND channel.stream_uuid = topic.stream_uuid
 AND channel.channel_type = 'channel' AND channel.topic_notifications_synced
LEFT JOIN bridge_zulip_v5_topic_notifications AS preference
  ON preference.channel_uuid = channel.uuid
 AND preference.topic_name = CASE WHEN topic.uuid = messenger_uuid_v5(topic.stream_uuid, '') THEN ''
                                 ELSE lower(topic.name) END
WHERE binding.notification_mode IS DISTINCT FROM
      CASE COALESCE(preference.visibility_policy, 0)
          WHEN 1 THEN 'mute' WHEN 2 THEN 'unmute' WHEN 3 THEN 'follow'
          ELSE 'default' END;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0258-Retain-message-delete-tombstones-and-bound-mutation-receipts-92730c.py"
        ]

    @property
    def migration_id(self):
        return "0e2e4bc3-6a13-4db5-ac2d-ebda1e8bd6bd"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        if session.execute(
            "SELECT 1 FROM bridge_zulip_v5_tasks WHERE content->>'kind'='update_notifications' LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                "Topic notification task history requires the current schema; restore a pre-upgrade snapshot to roll back"
            )
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
