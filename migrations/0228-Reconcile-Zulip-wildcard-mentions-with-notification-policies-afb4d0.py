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


# Reuse the same viewer/access contract when restoring the previous semantics.
USER_MESSAGE_STATES_SQL = """
CREATE OR REPLACE VIEW bridge_zulip_v5_user_message_states AS
WITH changes AS (
    SELECT project_id, message_uuid FROM bridge_zulip_v5_message_changes
    UNION ALL
    SELECT workspace.project_id, workspace.message_uuid FROM messenger_message_changes AS workspace
    WHERE NOT EXISTS (
        SELECT 1 FROM bridge_zulip_v5_message_changes AS zulip
        WHERE zulip.project_id = workspace.project_id AND zulip.message_uuid = workspace.message_uuid
    )
)
SELECT changes.project_id, changes.message_uuid, target.user_uuid, placement.uuid AS placement_uuid,
       COALESCE(binding.uuid, messenger_uuid_v5(placement.uuid, target.user_uuid::text)) AS binding_uuid,
       COALESCE(state.uuid, messenger_uuid_v5(placement.uuid, target.user_uuid::text)) AS state_uuid,
       target.source_uuid, target.flags_updated_at, target.membership_generation,
       CASE WHEN canonical.author_uuid = target.user_uuid THEN 'author' ELSE 'member' END AS relation_role,
       target.read, target.starred, target.mentioned,
       CASE WHEN target.source_uuid IS NULL THEN 'delete'
            WHEN binding.uuid IS NULL OR state.uuid IS NULL THEN 'create' ELSE 'update' END AS action
FROM changes
JOIN messenger_messages AS canonical
  ON canonical.project_id = changes.project_id AND canonical.uuid = changes.message_uuid AND canonical.source_name = 'zulip'
JOIN messenger_message_placements AS placement
  ON placement.project_id = changes.project_id AND placement.message_uuid = changes.message_uuid
 AND placement.uuid = messenger_uuid_v5(changes.message_uuid, 'zulip-placement')
CROSS JOIN LATERAL (
    WITH desired AS (
        SELECT DISTINCT ON (account.owner_user_uuid)
               account.owner_user_uuid AS user_uuid, observed.uuid AS source_uuid,
               observed.updated_at AT TIME ZONE 'UTC' AS flags_updated_at, membership.membership_generation,
               observed.read, observed.starred, {mentioned} AS mentioned
        FROM bridge_zulip_v5_messages AS observed
        JOIN m_external_accounts_v2 AS account ON account.uuid = observed.external_account_uuid AND account.project_id = observed.project_id
        JOIN bridge_zulip_v5_channels AS channel ON channel.uuid = observed.channel AND channel.stream_uuid = placement.stream_uuid
        JOIN messenger_stream_bindings AS membership
          ON membership.project_id = observed.project_id AND membership.stream_uuid = placement.stream_uuid
         AND membership.user_uuid = account.owner_user_uuid AND membership.active
        LEFT JOIN messenger_user_topic_bindings AS topic_binding
          ON topic_binding.project_id = observed.project_id
         AND topic_binding.topic_uuid = placement.topic_uuid
         AND topic_binding.user_uuid = account.owner_user_uuid
        WHERE observed.project_id = changes.project_id
          AND messenger_uuid_v5(observed.project_id, observed.message_uuid::text) = changes.message_uuid
          AND account.provider = 'zulip' AND canonical.deleted_at IS NULL
        ORDER BY account.owner_user_uuid, observed.updated_at DESC, observed.uuid
    ), actual AS (
        SELECT user_uuid FROM messenger_user_message_bindings
        WHERE project_id = changes.project_id AND placement_uuid = placement.uuid
        UNION
        SELECT user_uuid FROM messenger_user_message_states
        WHERE project_id = changes.project_id AND placement_uuid = placement.uuid
    )
    SELECT COALESCE(desired.user_uuid, actual.user_uuid) AS user_uuid,
           desired.source_uuid, desired.flags_updated_at, desired.membership_generation,
           desired.read, desired.starred, desired.mentioned
    FROM desired FULL JOIN actual ON actual.user_uuid = desired.user_uuid
) AS target
LEFT JOIN messenger_user_message_bindings AS binding
  ON binding.project_id = changes.project_id AND binding.placement_uuid = placement.uuid AND binding.user_uuid = target.user_uuid
LEFT JOIN messenger_user_message_states AS state
  ON state.project_id = changes.project_id AND state.placement_uuid = placement.uuid AND state.user_uuid = target.user_uuid
WHERE target.source_uuid IS NULL
   OR binding.uuid IS NULL OR state.uuid IS NULL
   OR binding.membership_generation <> target.membership_generation
   OR binding.relation_role IS DISTINCT FROM CASE WHEN canonical.author_uuid = target.user_uuid THEN 'author' ELSE 'member' END
   OR binding.visibility <> 'visible'
   OR binding.permissions <> '{{"read":true,"react":true,"star":true,"pin":true}}'::jsonb
   OR state.binding_uuid IS DISTINCT FROM binding.uuid
   OR state.membership_generation <> target.membership_generation
   OR state.zulip_source_uuid IS DISTINCT FROM target.source_uuid
   OR state.zulip_flags_updated_at IS NULL OR state.zulip_flags_updated_at < target.flags_updated_at
   {mention_changed};
"""

MENTIONED_SQL = """observed.mentioned OR (
                   (observed.stream_wildcard_mentioned OR observed.topic_wildcard_mentioned)
                   AND (channel.channel_type <> 'channel' OR CASE topic_binding.notification_mode
                       WHEN 'mute' THEN false
                       WHEN 'unmute' THEN true
                       WHEN 'follow' THEN true
                       ELSE membership.notification_mode = 'all_messages'
                   END)
               )"""

REQUEUE_SQL = """
INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
SELECT DISTINCT observed.project_id,
       messenger_uuid_v5(observed.project_id, observed.message_uuid::text)
FROM bridge_zulip_v5_messages AS observed
JOIN m_external_accounts_v2 AS account
  ON account.uuid = observed.external_account_uuid AND account.project_id = observed.project_id
WHERE account.provider = 'zulip'
  AND (observed.stream_wildcard_mentioned OR observed.topic_wildcard_mentioned)
ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
"""

UPGRADE_SQL = """
CREATE FUNCTION messenger_track_zulip_wildcard_mention_preferences()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_stream uuid;
    target_topic uuid;
BEGIN
    IF NEW.notification_mode IS NOT DISTINCT FROM OLD.notification_mode THEN
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'messenger_stream_bindings' THEN
        target_stream := NEW.stream_uuid;
    ELSE
        target_topic := NEW.topic_uuid;
    END IF;
    INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
    SELECT DISTINCT observed.project_id,
           messenger_uuid_v5(observed.project_id, observed.message_uuid::text)
    FROM bridge_zulip_v5_messages AS observed
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = observed.external_account_uuid AND account.project_id = observed.project_id
    JOIN bridge_zulip_v5_channels AS channel ON channel.uuid = observed.channel
    WHERE observed.project_id = NEW.project_id AND account.owner_user_uuid = NEW.user_uuid
      AND account.provider = 'zulip'
      AND (observed.stream_wildcard_mentioned OR observed.topic_wildcard_mentioned)
      AND (target_stream IS NULL OR channel.stream_uuid = target_stream)
      AND (target_topic IS NULL OR observed.topic = target_topic)
    ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_zulip_stream_wildcard_mention_preferences
AFTER UPDATE OF notification_mode ON messenger_stream_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_track_zulip_wildcard_mention_preferences();
CREATE TRIGGER messenger_track_zulip_topic_wildcard_mention_preferences
AFTER UPDATE OF notification_mode ON messenger_user_topic_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_track_zulip_wildcard_mention_preferences();
"""

DOWNGRADE_SQL = """
DROP TRIGGER messenger_track_zulip_topic_wildcard_mention_preferences ON messenger_user_topic_bindings;
DROP TRIGGER messenger_track_zulip_stream_wildcard_mention_preferences ON messenger_stream_bindings;
DROP FUNCTION messenger_track_zulip_wildcard_mention_preferences();
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0227-Reconcile-Zulip-topic-notification-preferences-4a77fd.py"
        ]

    @property
    def migration_id(self):
        return "afb4d07e-5200-47a7-8a90-97d265aa6d48"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            USER_MESSAGE_STATES_SQL.format(
                mentioned=MENTIONED_SQL,
                mention_changed="OR state.mentioned IS DISTINCT FROM target.mentioned",
            )
        )
        session.execute(UPGRADE_SQL)
        session.execute(REQUEUE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)
        session.execute(
            USER_MESSAGE_STATES_SQL.format(
                mentioned="observed.mentioned", mention_changed=""
            )
        )
        session.execute(REQUEUE_SQL)


migration_step = MigrationStep()
