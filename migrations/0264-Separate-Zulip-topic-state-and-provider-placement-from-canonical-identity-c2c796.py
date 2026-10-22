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


TOPIC_STATES_SQL = """
CREATE OR REPLACE VIEW bridge_zulip_v5_topic_states AS
SELECT changes.project_id, COALESCE(source.stream_uuid, workspace.stream_uuid) AS stream_uuid,
       changes.topic_uuid,
       CASE WHEN workspace.uuid IS NULL THEN 'create'
            WHEN source.stream_uuid IS NULL THEN 'delete' ELSE 'update' END AS action,
       source.name, source.is_done
FROM bridge_zulip_v5_topic_changes AS changes
LEFT JOIN LATERAL (
    SELECT observed.stream_uuid, min(observed.base_name) AS name,
           bool_and(observed.name LIKE '✔ %') AS is_done
    FROM (
        (SELECT DISTINCT ON (message.message_uuid)
               message.message_uuid, channel.stream_uuid, topic.name,
               COALESCE(NULLIF(topic.base_name, ''),
                   NULLIF(bridge_zulip_v5_topic_base_name(topic.name), ''), 'General Topic') AS base_name,
               COALESCE(topic.workspace_topic_uuid, topic.uuid) AS topic_uuid
        FROM bridge_zulip_v5_messages AS message
        JOIN bridge_zulip_v5_topics AS topic ON topic.uuid=message.topic
        JOIN bridge_zulip_v5_channels AS channel ON channel.uuid=message.channel
        JOIN m_external_accounts_v2 AS account ON account.uuid=message.external_account_uuid
        JOIN messenger_streams AS stream ON stream.project_id=message.project_id
          AND stream.uuid=channel.stream_uuid AND stream.source_name='zulip' AND stream.deleted_at IS NULL
        WHERE message.project_id=changes.project_id AND account.provider='zulip'
          AND COALESCE(topic.workspace_topic_uuid, topic.uuid)=changes.topic_uuid
          AND NOT EXISTS (
              SELECT 1 FROM bridge_zulip_v5_messages AS newer
              JOIN m_external_accounts_v2 AS observer ON observer.uuid=newer.external_account_uuid AND observer.provider='zulip'
              WHERE newer.project_id=message.project_id AND newer.message_uuid=message.message_uuid
                AND (newer.placement_updated_at,newer.placement_event_uuid IS NOT NULL,newer.uuid)
                    > (message.placement_updated_at,message.placement_event_uuid IS NOT NULL,message.uuid)
          )
        ORDER BY message.message_uuid,message.placement_updated_at DESC,
                 (message.placement_event_uuid IS NOT NULL) DESC,message.uuid)
        UNION ALL
        SELECT delivery.message_uuid,placement.stream_uuid,delivery.topic_name,topic.name,topic.uuid
        FROM bridge_zulip_v5_deliveries AS delivery
        JOIN messenger_messages AS message ON message.project_id=delivery.project_id
          AND message.uuid=delivery.message_uuid AND message.source_name='native' AND message.deleted_at IS NULL
        JOIN messenger_message_placements AS placement ON placement.project_id=delivery.project_id
          AND placement.message_uuid=delivery.message_uuid AND placement.stream_uuid=delivery.stream_uuid
        JOIN messenger_topics AS topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid
        WHERE delivery.project_id=changes.project_id AND topic.uuid=changes.topic_uuid AND delivery.topic_name IS NOT NULL
          AND delivery.zulip_message_id IS NOT NULL
    ) AS observed
    GROUP BY observed.stream_uuid
) AS source ON true
LEFT JOIN messenger_topics AS workspace
  ON workspace.project_id=changes.project_id AND workspace.uuid=changes.topic_uuid
 AND workspace.deleted_at IS NULL
WHERE ((source.stream_uuid IS NOT NULL AND (workspace.uuid IS NULL
        OR (workspace.name,workspace.is_done) IS DISTINCT FROM (source.name,source.is_done)))
   OR (source.stream_uuid IS NULL AND workspace.source_name='zulip'
       AND NOT EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                       WHERE placement.project_id=changes.project_id AND placement.topic_uuid=changes.topic_uuid)))
  AND NOT EXISTS (SELECT 1 FROM bridge_zulip_v5_tasks AS intent
      WHERE intent.project_id=changes.project_id AND intent.content->>'kind'='update_topic'
        AND intent.status IN ('new','waiting','retry')
        AND changes.topic_uuid::text IN (intent.content->>'topic_uuid',intent.content->>'new_topic_uuid'));
"""


UPGRADE_SQL = """
ALTER TABLE bridge_zulip_v5_topics ADD COLUMN IF NOT EXISTS workspace_topic_uuid uuid,
    ADD COLUMN IF NOT EXISTS base_name text;
CREATE INDEX bridge_zulip_v5_topics_workspace_topic_idx
    ON bridge_zulip_v5_topics (COALESCE(workspace_topic_uuid,uuid));
ALTER TABLE bridge_zulip_v5_messages ADD COLUMN placement_event_uuid uuid
    REFERENCES bridge_zulip_v5_events(uuid) ON DELETE SET NULL;
ALTER TABLE bridge_zulip_v5_deliveries ADD COLUMN placement_event_uuid uuid
    REFERENCES bridge_zulip_v5_events(uuid) ON DELETE SET NULL;
ALTER TABLE bridge_zulip_v5_deliveries ADD COLUMN placement_updated_at timestamp without time zone NOT NULL DEFAULT 'epoch';

CREATE FUNCTION bridge_zulip_v5_topic_base_name(name text)
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE WHEN left(name,2)='✔ ' THEN substr(name,3) ELSE name END;
$$;
CREATE FUNCTION bridge_zulip_v5_workspace_topic_uuid(topic_uuid uuid)
RETURNS uuid LANGUAGE sql STABLE STRICT AS $$
    SELECT COALESCE((SELECT workspace_topic_uuid FROM bridge_zulip_v5_topics WHERE uuid=topic_uuid), topic_uuid);
$$;
CREATE FUNCTION bridge_zulip_v5_topic_provider_name(topic_uuid uuid,stream_uuid uuid,name text,is_done boolean)
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE WHEN is_done THEN left('✔ '||base,60) ELSE base END
    FROM (SELECT CASE WHEN topic_uuid=messenger_uuid_v5(stream_uuid,'') THEN '' ELSE name END AS base) AS topic;
$$;
CREATE OR REPLACE FUNCTION bridge_zulip_v5_mark_topic_changed(target_project_id uuid, target_topic_uuid uuid)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO bridge_zulip_v5_topic_changes (project_id,topic_uuid)
    VALUES (target_project_id,bridge_zulip_v5_workspace_topic_uuid(target_topic_uuid))
    ON CONFLICT (project_id,topic_uuid) DO UPDATE SET revision=EXCLUDED.revision;
$$;

"""


DOWNGRADE_SQL = """
DROP VIEW bridge_zulip_v5_topic_states;
CREATE VIEW bridge_zulip_v5_topic_states AS
SELECT changes.project_id, COALESCE(source.stream_uuid, workspace.stream_uuid) AS stream_uuid,
       changes.topic_uuid, CASE WHEN workspace.uuid IS NULL THEN 'create' ELSE 'delete' END AS action
FROM bridge_zulip_v5_topic_changes AS changes
LEFT JOIN LATERAL (
    SELECT DISTINCT channel.stream_uuid
    FROM bridge_zulip_v5_channels AS channel
    JOIN m_external_accounts_v2 AS account ON account.uuid=channel.external_account_uuid
    JOIN messenger_streams AS stream ON stream.project_id=account.project_id
      AND stream.uuid=channel.stream_uuid AND stream.source_name='zulip' AND stream.deleted_at IS NULL
    WHERE account.project_id=changes.project_id AND account.provider='zulip'
      AND EXISTS (SELECT 1 FROM bridge_zulip_v5_messages AS message
                  WHERE message.project_id=changes.project_id AND message.topic=changes.topic_uuid
                    AND message.channel=channel.uuid AND message.external_account_uuid=account.uuid)
) AS source ON true
LEFT JOIN messenger_topics AS workspace
  ON workspace.project_id=changes.project_id AND workspace.uuid=changes.topic_uuid AND workspace.deleted_at IS NULL
WHERE (source.stream_uuid IS NOT NULL AND workspace.uuid IS NULL)
   OR (source.stream_uuid IS NULL AND workspace.source_name='zulip'
       AND NOT EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                       WHERE placement.project_id=changes.project_id AND placement.topic_uuid=changes.topic_uuid));

CREATE OR REPLACE FUNCTION bridge_zulip_v5_mark_topic_changed(target_project_id uuid, target_topic_uuid uuid)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO bridge_zulip_v5_topic_changes (project_id,topic_uuid)
    VALUES (target_project_id,target_topic_uuid)
    ON CONFLICT (project_id,topic_uuid) DO UPDATE SET revision=EXCLUDED.revision;
$$;
DROP FUNCTION bridge_zulip_v5_workspace_topic_uuid(uuid);
DROP FUNCTION bridge_zulip_v5_topic_base_name(text);
DROP INDEX bridge_zulip_v5_topics_workspace_topic_idx;
ALTER TABLE bridge_zulip_v5_messages DROP COLUMN placement_event_uuid;
ALTER TABLE bridge_zulip_v5_deliveries DROP COLUMN placement_event_uuid, DROP COLUMN placement_updated_at;
ALTER TABLE bridge_zulip_v5_topics DROP COLUMN workspace_topic_uuid, DROP COLUMN base_name;
"""


MESSAGE_CHANGE_GUARD = """
    -- A pure provider placement observation of the same canonical topic does
    -- not invalidate content, flags, reactions or the canonical placement.
    IF TG_OP='UPDATE'
       AND (NEW.topic,NEW.placement_updated_at,NEW.placement_event_uuid)
           IS DISTINCT FROM (OLD.topic,OLD.placement_updated_at,OLD.placement_event_uuid)
       AND bridge_zulip_v5_workspace_topic_uuid(NEW.topic)=bridge_zulip_v5_workspace_topic_uuid(OLD.topic)
       AND (to_jsonb(NEW)-ARRAY['topic','placement_updated_at','placement_event_uuid','updated_at'])
           IS NOT DISTINCT FROM (to_jsonb(OLD)-ARRAY['topic','placement_updated_at','placement_event_uuid','updated_at']) THEN
        RETURN NULL;
    END IF;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0263-Index-Workspace-entity-references-in-canonical-message-content-4272f7.py"
        ]

    @property
    def migration_id(self):
        return "c2c7966c-d295-45c9-b16e-c2c9d8f87e29"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)
        definition = session.execute(
            "SELECT pg_get_functiondef('bridge_zulip_v5_track_message_changes()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        session.execute(definition.replace("BEGIN", "BEGIN" + MESSAGE_CHANGE_GUARD, 1))
        definition = session.execute(
            "SELECT pg_get_functiondef('bridge_zulip_v5_track_placement_version()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        session.execute(
            definition.replace(
                "ELSIF NEW.placement_updated_at < OLD.placement_updated_at THEN",
                "ELSIF NEW.placement_event_uuid IS NOT NULL AND NEW.placement_event_uuid IS DISTINCT FROM OLD.placement_event_uuid THEN\n"
                "        RETURN NEW;\n    ELSIF NEW.placement_updated_at < OLD.placement_updated_at THEN",
            )
        )
        definition = session.execute(
            "SELECT pg_get_viewdef('bridge_zulip_v5_message_placement_states'::regclass,true) AS definition"
        ).fetchone()["definition"]
        session.execute(
            "CREATE OR REPLACE VIEW bridge_zulip_v5_message_placement_states AS "
            + definition.replace(
                "message.topic AS topic_uuid",
                "bridge_zulip_v5_workspace_topic_uuid(message.topic) AS topic_uuid",
            ).replace(
                "message.placement_updated_at DESC, message.uuid",
                "message.placement_updated_at DESC, (message.placement_event_uuid IS NOT NULL) DESC, message.uuid",
            )
        )
        session.execute(TOPIC_STATES_SQL)
        definition = session.execute(
            "SELECT pg_get_viewdef('bridge_zulip_v5_topic_notification_states'::regclass,true) AS definition"
        ).fetchone()["definition"]
        original = "CASE\n            WHEN topic.uuid = messenger_uuid_v5(topic.stream_uuid, ''::text) THEN ''::text\n            ELSE lower(topic.name::text)\n        END"
        session.execute(
            "CREATE OR REPLACE VIEW bridge_zulip_v5_topic_notification_states AS "
            + definition.replace(
                original,
                "lower(bridge_zulip_v5_topic_provider_name(topic.uuid,topic.stream_uuid,topic.name,topic.is_done))",
            )
        )

    def downgrade(self, session):
        definition = session.execute(
            "SELECT pg_get_functiondef('bridge_zulip_v5_track_message_changes()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        session.execute(definition.replace(MESSAGE_CHANGE_GUARD, ""))
        definition = session.execute(
            "SELECT pg_get_viewdef('bridge_zulip_v5_topic_notification_states'::regclass,true) AS definition"
        ).fetchone()["definition"]
        session.execute(
            "CREATE OR REPLACE VIEW bridge_zulip_v5_topic_notification_states AS "
            + definition.replace(
                "lower(bridge_zulip_v5_topic_provider_name(topic.uuid, topic.stream_uuid, topic.name::text, topic.is_done))",
                "CASE WHEN topic.uuid=messenger_uuid_v5(topic.stream_uuid,'') THEN '' ELSE lower(topic.name) END",
            )
        )
        session.execute(
            "DROP FUNCTION bridge_zulip_v5_topic_provider_name(uuid,uuid,text,boolean)"
        )
        definition = session.execute(
            "SELECT pg_get_functiondef('bridge_zulip_v5_track_placement_version()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        session.execute(
            definition.replace(
                "ELSIF NEW.placement_event_uuid IS NOT NULL AND NEW.placement_event_uuid IS DISTINCT FROM OLD.placement_event_uuid THEN\n"
                "        RETURN NEW;\n    ",
                "",
            )
        )
        definition = session.execute(
            "SELECT pg_get_viewdef('bridge_zulip_v5_message_placement_states'::regclass,true) AS definition"
        ).fetchone()["definition"]
        session.execute(
            "CREATE OR REPLACE VIEW bridge_zulip_v5_message_placement_states AS "
            + definition.replace(
                "bridge_zulip_v5_workspace_topic_uuid(message.topic) AS topic_uuid",
                "message.topic AS topic_uuid",
            ).replace(", (message.placement_event_uuid IS NOT NULL) DESC", "")
        )
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
