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
ALTER TABLE bridge_zulip_v5_channels
    ADD COLUMN topic_notifications_synced boolean NOT NULL DEFAULT false;

CREATE TABLE bridge_zulip_v5_topic_notifications (
    uuid uuid PRIMARY KEY,
    channel_uuid uuid NOT NULL REFERENCES bridge_zulip_v5_channels(uuid) ON DELETE CASCADE,
    topic_name text NOT NULL,
    visibility_policy smallint NOT NULL CHECK (visibility_policy BETWEEN 0 AND 3),
    last_updated bigint NOT NULL CHECK (last_updated >= 0),
    UNIQUE (channel_uuid, topic_name)
);

CREATE FUNCTION bridge_zulip_v5_track_topic_notification_changes()
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
CREATE TRIGGER bridge_zulip_v5_track_topic_notification_changes
AFTER INSERT OR DELETE OR UPDATE OF visibility_policy ON bridge_zulip_v5_topic_notifications
FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_topic_notification_changes();

CREATE FUNCTION bridge_zulip_v5_track_topic_notification_snapshot()
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
CREATE TRIGGER bridge_zulip_v5_track_topic_notification_snapshot
AFTER UPDATE OF topic_notifications_synced ON bridge_zulip_v5_channels
FOR EACH ROW WHEN (NEW.topic_notifications_synced AND NOT OLD.topic_notifications_synced)
EXECUTE FUNCTION bridge_zulip_v5_track_topic_notification_snapshot();

CREATE FUNCTION bridge_zulip_v5_track_topic_notification_owner()
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
CREATE TRIGGER bridge_zulip_v5_track_topic_notification_owner
AFTER UPDATE OF owner_user_uuid, project_id, provider ON m_external_accounts_v2
FOR EACH ROW WHEN ((NEW.owner_user_uuid, NEW.project_id, NEW.provider)
    IS DISTINCT FROM (OLD.owner_user_uuid, OLD.project_id, OLD.provider))
EXECUTE FUNCTION bridge_zulip_v5_track_topic_notification_owner();

CREATE FUNCTION messenger_track_zulip_topic_notification_binding()
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
CREATE TRIGGER messenger_track_zulip_topic_notification_binding
AFTER INSERT OR UPDATE OF notification_mode ON messenger_user_topic_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_track_zulip_topic_notification_binding();

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

UPDATE bridge_zulip_v5_topics AS topic SET name='Zulip', updated_at=NOW()
WHERE topic.uuid IN (
    SELECT message.topic FROM bridge_zulip_v5_messages AS message
    JOIN bridge_zulip_v5_channels AS channel ON channel.uuid = message.channel
    WHERE channel.channel_type IN ('private', 'group')
);

WITH renamed AS (
    UPDATE messenger_topics AS topic SET name='Zulip', updated_at=NOW()
    WHERE topic.source_name='zulip' AND topic.name IS DISTINCT FROM 'Zulip'
      AND EXISTS (
          SELECT 1 FROM bridge_zulip_v5_messages AS message
          JOIN bridge_zulip_v5_channels AS channel ON channel.uuid=message.channel
          WHERE message.project_id=topic.project_id AND message.topic=topic.uuid
            AND channel.channel_type IN ('private', 'group')
      )
    RETURNING topic.project_id, topic.uuid, topic.stream_uuid
)
INSERT INTO bridge_zulip_v5_message_counter_changes
    (project_id,user_uuid,scope_kind,scope_uuid,stream_uuid,topic_uuid)
SELECT DISTINCT topic.project_id,binding.user_uuid,scope.kind,scope.uuid,
       topic.stream_uuid,CASE WHEN scope.kind='user-topic' THEN topic.uuid END
FROM renamed AS topic
JOIN messenger_user_topic_bindings AS binding
  ON binding.project_id=topic.project_id AND binding.topic_uuid=topic.uuid
CROSS JOIN LATERAL (VALUES ('user-stream',topic.stream_uuid),('user-topic',topic.uuid)) AS scope(kind,uuid)
ON CONFLICT (project_id,user_uuid,scope_kind,scope_uuid) DO UPDATE SET revision=EXCLUDED.revision;

UPDATE bridge_zulip_v5_tasks SET status='new', not_before_at=NULL, next_retry_at=NULL
WHERE content->>'kind'='import_channels' AND status IN ('new', 'waiting', 'retry');
"""

DOWNGRADE_SQL = """
UPDATE bridge_zulip_v5_topics AS topic SET name='General Topic'
WHERE topic.name='Zulip' AND topic.uuid IN (
    SELECT message.topic FROM bridge_zulip_v5_messages AS message
    JOIN bridge_zulip_v5_channels AS channel ON channel.uuid=message.channel
    WHERE channel.channel_type IN ('private', 'group')
);
UPDATE messenger_topics AS topic SET name='General Topic'
WHERE topic.source_name='zulip' AND topic.name='Zulip' AND EXISTS (
    SELECT 1 FROM bridge_zulip_v5_messages AS message
    JOIN bridge_zulip_v5_channels AS channel ON channel.uuid=message.channel
    WHERE message.project_id=topic.project_id AND message.topic=topic.uuid
      AND channel.channel_type IN ('private', 'group')
);
DROP VIEW bridge_zulip_v5_topic_notification_states;
DROP TRIGGER messenger_track_zulip_topic_notification_binding ON messenger_user_topic_bindings;
DROP FUNCTION messenger_track_zulip_topic_notification_binding();
DROP TRIGGER bridge_zulip_v5_track_topic_notification_owner ON m_external_accounts_v2;
DROP FUNCTION bridge_zulip_v5_track_topic_notification_owner();
DROP TRIGGER bridge_zulip_v5_track_topic_notification_snapshot ON bridge_zulip_v5_channels;
DROP FUNCTION bridge_zulip_v5_track_topic_notification_snapshot();
DROP TABLE bridge_zulip_v5_topic_notifications;
DROP FUNCTION bridge_zulip_v5_track_topic_notification_changes();
ALTER TABLE bridge_zulip_v5_channels DROP COLUMN topic_notifications_synced;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0226-Import-Zulip-subscriptions-and-notification-preferences-e30387.py"
        ]

    @property
    def migration_id(self):
        return "4a77fdae-a542-409f-9a98-b0e639f4a65d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
