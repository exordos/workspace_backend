# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations


TOPIC_NOTIFICATION_VIEW = """
CREATE OR REPLACE VIEW bridge_zulip_v5_topic_notification_states AS
SELECT topic.project_id, binding.user_uuid, topic.uuid AS topic_uuid, topic.stream_uuid,
       CASE COALESCE(preference.visibility_policy, 0)
           WHEN 1 THEN 'mute' WHEN 2 THEN 'unmute' WHEN 3 THEN 'follow'
           ELSE 'default' END AS notification_mode
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
          ELSE 'default' END;
"""


def _set_topic_scope(session, imported_only):
    # Keep the existing trigger bodies; only broaden their topic selection.
    for name, predicate in (
        (
            "bridge_zulip_v5_track_topic_notification_changes",
            "WHERE channel.uuid = target_channel",
        ),
        (
            "bridge_zulip_v5_track_topic_notification_snapshot",
            "WHERE account.uuid = NEW.external_account_uuid",
        ),
        (
            "bridge_zulip_v5_track_topic_notification_owner",
            "AND topic.project_id IN (NEW.project_id, OLD.project_id)",
        ),
    ):
        definition = session.execute(
            "SELECT pg_get_functiondef(%s::regprocedure) AS definition", (name + "()",)
        ).fetchone()["definition"]
        filtered = predicate + " AND topic.source_name = 'zulip'"
        session.execute(
            definition.replace(predicate, filtered)
            if imported_only
            else definition.replace(filtered, predicate)
        )
    name = "messenger_track_zulip_topic_notification_binding"
    definition = session.execute(
        "SELECT pg_get_functiondef(%s::regprocedure) AS definition", (name + "()",)
    ).fetchone()["definition"]
    old = "AND source_name = 'zulip'"
    new = "AND stream_uuid IN (SELECT uuid FROM messenger_streams WHERE project_id=NEW.project_id AND source_name='zulip')"
    session.execute(
        definition.replace(new, old) if imported_only else definition.replace(old, new)
    )
    view = TOPIC_NOTIFICATION_VIEW
    if imported_only:
        view = view.replace(
            "AND topic.deleted_at IS NULL",
            "AND topic.source_name = 'zulip' AND topic.deleted_at IS NULL",
        )
    session.execute(view)


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
        _set_topic_scope(session, imported_only=False)

    def downgrade(self, session):
        session.execute(
            "DELETE FROM bridge_zulip_v5_tasks WHERE content->>'kind'='update_notifications'"
        )
        _set_topic_scope(session, imported_only=True)


migration_step = MigrationStep()
