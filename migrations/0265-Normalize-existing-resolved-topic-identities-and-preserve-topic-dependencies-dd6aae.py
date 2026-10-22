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


PAYLOAD_SQL = """
CREATE FUNCTION messenger_remap_topic_payload(value jsonb,p_old uuid,p_new uuid)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE STRICT AS $$
BEGIN
    IF jsonb_typeof(value)='object' THEN
        RETURN COALESCE((SELECT jsonb_object_agg(entry.key,
            CASE WHEN entry.key IN ('uuid','topic_uuid','_topic_uuid','default_topic_uuid','new_topic_uuid')
                      AND entry.value=to_jsonb(p_old::text) THEN to_jsonb(p_new::text)
                 ELSE messenger_remap_topic_payload(entry.value,p_old,p_new) END)
            FROM jsonb_each(value) AS entry),'{}'::jsonb);
    ELSIF jsonb_typeof(value)='array' THEN
        RETURN COALESCE((SELECT jsonb_agg(messenger_remap_topic_payload(entry.value,p_old,p_new))
                         FROM jsonb_array_elements(value) AS entry),'[]'::jsonb);
    END IF;
    RETURN value;
END;
$$;
"""


MOVE_TOPIC_SQL = """
ALTER TABLE messenger_topics ADD COLUMN redirect_topic_uuid uuid;
ALTER TABLE messenger_topics ADD CONSTRAINT messenger_topics_redirect_fkey
    FOREIGN KEY(project_id,redirect_topic_uuid) REFERENCES messenger_topics(project_id,uuid) ON DELETE SET NULL;
CREATE FUNCTION messenger_move_topic(p_project uuid,p_old uuid,p_new uuid,p_stream uuid,p_name text,p_done boolean)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE previous_topic jsonb;
BEGIN
    IF p_old=p_new THEN
        UPDATE messenger_topics SET name=p_name,is_done=p_done,version=version+1,updated_at=NOW()
        WHERE project_id=p_project AND uuid=p_old;
        RETURN;
    END IF;
    SELECT to_jsonb(topic) INTO previous_topic FROM messenger_topics AS topic
    WHERE project_id=p_project AND uuid=p_old FOR UPDATE;
    -- Create the destination before touching its FK graph. A merge retains the
    -- destination's identity and metadata; journals retain both summary histories.
    INSERT INTO messenger_topics
    SELECT (jsonb_populate_record(NULL::messenger_topics,to_jsonb(original_topic)||
        jsonb_build_object('uuid',p_new,'stream_uuid',p_stream,'name',p_name,
            'is_done',p_done,'deleted_at',NULL,'redirect_topic_uuid',NULL,'version',original_topic.version+1))).*
    FROM messenger_topics AS original_topic WHERE project_id=p_project AND uuid=p_old
    ON CONFLICT (project_id,uuid) DO UPDATE SET deleted_at=NULL,redirect_topic_uuid=NULL;

    INSERT INTO messenger_user_topic_bindings
    SELECT (jsonb_populate_record(NULL::messenger_user_topic_bindings,to_jsonb(source)||
        jsonb_build_object('uuid',messenger_uuid_v5(p_new,source.user_uuid::text),'topic_uuid',p_new))).*
    FROM messenger_user_topic_bindings AS source WHERE project_id=p_project AND topic_uuid=p_old
    ON CONFLICT (project_id,user_uuid,topic_uuid) DO UPDATE SET
        notification_mode=CASE WHEN EXCLUDED.updated_at>messenger_user_topic_bindings.updated_at
            THEN EXCLUDED.notification_mode ELSE messenger_user_topic_bindings.notification_mode END,
        summary_has_new_messages=messenger_user_topic_bindings.summary_has_new_messages OR EXCLUDED.summary_has_new_messages,
        updated_at=GREATEST(messenger_user_topic_bindings.updated_at,EXCLUDED.updated_at);
    INSERT INTO messenger_user_topic_bindings(uuid,project_id,user_uuid,topic_uuid)
    SELECT messenger_uuid_v5(p_new,user_uuid::text),project_id,user_uuid,p_new
    FROM messenger_stream_bindings WHERE project_id=p_project AND stream_uuid=p_stream AND active
    ON CONFLICT (project_id,user_uuid,topic_uuid) DO NOTHING;

    UPDATE m_workspace_drafts SET topic_uuid=p_new,stream_uuid=p_stream
    WHERE project_id=p_project AND topic_uuid=p_old;
    DELETE FROM m_workspace_topic_summary_jobs AS target
    USING m_workspace_topic_summary_jobs AS source
    WHERE target.project_id=p_project AND target.topic_uuid=p_new
      AND source.project_id=p_project AND source.topic_uuid=p_old AND source.updated_at>target.updated_at;
    UPDATE m_workspace_topic_summary_jobs AS source SET topic_uuid=p_new
    WHERE source.project_id=p_project AND source.topic_uuid=p_old
      AND NOT EXISTS (SELECT 1 FROM m_workspace_topic_summary_jobs AS target
                      WHERE target.project_id=p_project AND target.topic_uuid=p_new);
    UPDATE m_workspace_topic_summary_journal SET topic_uuid=p_new
    WHERE project_id=p_project AND topic_uuid=p_old;

    INSERT INTO messenger_message_counter_totals
    SELECT project_id,user_uuid,p_stream,p_new,membership_generation,membership_started_at,unread_count,mentioned_count
    FROM messenger_message_counter_totals WHERE project_id=p_project AND topic_uuid=p_old
    ON CONFLICT (project_id,user_uuid,stream_uuid,topic_uuid,membership_generation,membership_started_at)
    DO UPDATE SET unread_count=messenger_message_counter_totals.unread_count+EXCLUDED.unread_count,
                  mentioned_count=messenger_message_counter_totals.mentioned_count+EXCLUDED.mentioned_count;
    UPDATE messenger_message_counter_contributions SET topic_uuid=p_new,stream_uuid=p_stream
    WHERE project_id=p_project AND topic_uuid=p_old;
    UPDATE messenger_message_counter_changes SET topic_uuid=p_new,stream_uuid=p_stream
    WHERE project_id=p_project AND topic_uuid=p_old;
    -- Unsent native messages follow a full topic move. An uncertain send keeps
    -- its immutable route until its delivery identity is known.
    UPDATE bridge_zulip_v5_deliveries AS delivery SET stream_uuid=p_stream,
        topic_name=bridge_zulip_v5_topic_provider_name(p_new,p_stream,p_name,p_done),
        recipient=jsonb_build_object('to',(SELECT stream_id FROM bridge_zulip_v5_channels
                     WHERE stream_uuid=p_stream AND channel_type='channel' LIMIT 1))
    WHERE delivery.project_id=p_project AND NOT delivery.sending
      AND EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                  WHERE placement.project_id=delivery.project_id AND placement.message_uuid=delivery.message_uuid
                    AND placement.stream_uuid=delivery.stream_uuid AND placement.topic_uuid=p_old);
    UPDATE messenger_message_placements SET topic_uuid=p_new,stream_uuid=p_stream,updated_at=NOW()
    WHERE project_id=p_project AND topic_uuid=p_old;
    UPDATE messenger_streams SET default_topic_uuid=CASE WHEN uuid=p_stream THEN p_new ELSE NULL END
    WHERE project_id=p_project AND default_topic_uuid=p_old;
    UPDATE messenger_topics SET name=p_name,is_done=p_done,version=version+1,updated_at=NOW()
    WHERE project_id=p_project AND uuid=p_new;
    UPDATE messenger_domain_outbox_events SET scope_key=replace(scope_key,p_old::text,p_new::text),
        payload=messenger_remap_topic_payload(payload,p_old,p_new)
    WHERE project_id=p_project AND payload->>'source_kind' IS DISTINCT FROM 'topic.deleted'
      AND (scope_key LIKE '%'||p_old::text||'%' OR payload::text LIKE '%'||p_old::text||'%');
    UPDATE messenger_projection_tasks SET scope_key=replace(scope_key,p_old::text,p_new::text),
        payload=messenger_remap_topic_payload(payload,p_old,p_new)
    WHERE project_id=p_project AND status<>'completed' AND payload->>'source_kind' IS DISTINCT FROM 'topic.deleted'
      AND (scope_key LIKE '%'||p_old::text||'%' OR payload::text LIKE '%'||p_old::text||'%');
    UPDATE bridge_zulip_v5_tasks SET content=messenger_remap_topic_payload(content,p_old,p_new)
    WHERE project_id=p_project AND status IN ('new','waiting','retry') AND content::text LIKE '%'||p_old::text||'%';
    UPDATE messenger_mutation_commands SET payload=messenger_remap_topic_payload(payload,p_old,p_new)
    WHERE project_id=p_project AND status='pending' AND payload::text LIKE '%'||p_old::text||'%';
    UPDATE messenger_publication_outbox SET payload=messenger_remap_topic_payload(payload,p_old,p_new),
        recipient_payloads=messenger_remap_topic_payload(recipient_payloads,p_old,p_new)
    WHERE project_id=p_project AND kind<>'topic.deleted'
      AND (payload::text LIKE '%'||p_old::text||'%' OR recipient_payloads::text LIKE '%'||p_old::text||'%');
    UPDATE messenger_topics SET redirect_topic_uuid=p_new WHERE project_id=p_project AND redirect_topic_uuid=p_old;
    DELETE FROM messenger_topics WHERE project_id=p_project AND uuid=p_old;
    IF previous_topic IS NOT NULL THEN
        INSERT INTO messenger_topics SELECT (jsonb_populate_record(NULL::messenger_topics,previous_topic||
            jsonb_build_object('deleted_at',clock_timestamp(),'redirect_topic_uuid',p_new))).*;
    END IF;
END;
$$;
"""


NORMALIZE_SQL = """
UPDATE bridge_zulip_v5_topics AS topic SET
    workspace_topic_uuid=messenger_uuid_v5(observed.stream_uuid,observed.base_name),
    base_name=observed.base_name
FROM (
    SELECT DISTINCT message.topic,channel.stream_uuid,
        CASE WHEN message.topic=messenger_uuid_v5(channel.stream_uuid,'') THEN ''
             ELSE bridge_zulip_v5_topic_base_name(topic.name) END AS base_name
    FROM bridge_zulip_v5_messages AS message
    JOIN bridge_zulip_v5_topics AS topic ON topic.uuid=message.topic
    JOIN bridge_zulip_v5_channels AS channel ON channel.uuid=message.channel AND channel.channel_type='channel'
    JOIN m_external_accounts_v2 AS account ON account.uuid=message.external_account_uuid AND account.provider='zulip'
    UNION
    SELECT topic.uuid,topic.stream_uuid,
        CASE WHEN topic.uuid=messenger_uuid_v5(topic.stream_uuid,'') THEN ''
             ELSE bridge_zulip_v5_topic_base_name(raw.name) END
    FROM messenger_topics AS topic JOIN bridge_zulip_v5_topics AS raw ON raw.uuid=topic.uuid
    WHERE EXISTS (SELECT 1 FROM messenger_streams AS stream
                  WHERE stream.project_id=topic.project_id AND stream.uuid=topic.stream_uuid AND stream.source_name='zulip')
      AND EXISTS (SELECT 1 FROM bridge_zulip_v5_channels AS channel JOIN m_external_accounts_v2 AS account
                  ON account.uuid=channel.external_account_uuid WHERE account.project_id=topic.project_id
                    AND channel.stream_uuid=topic.stream_uuid AND channel.channel_type='channel')
) AS observed WHERE topic.uuid=observed.topic;

DO $$
DECLARE target record;
BEGIN
    FOR target IN
        SELECT DISTINCT topic.project_id,topic.uuid AS old_uuid,topic.stream_uuid,
            COALESCE(raw.workspace_topic_uuid,messenger_uuid_v5(topic.stream_uuid,bridge_zulip_v5_topic_base_name(topic.name))) AS new_uuid,
            COALESCE(raw.base_name,bridge_zulip_v5_topic_base_name(topic.name)) AS base_name,
            COALESCE((SELECT bool_and(variant.name LIKE '✔ %')
             FROM bridge_zulip_v5_messages AS message
             JOIN bridge_zulip_v5_topics AS variant ON variant.uuid=message.topic
             WHERE message.project_id=topic.project_id
               AND COALESCE(variant.workspace_topic_uuid,variant.uuid)=raw.workspace_topic_uuid),topic.name LIKE '✔ %') AS is_done
        FROM messenger_topics AS topic
        LEFT JOIN bridge_zulip_v5_topics AS raw ON raw.uuid=topic.uuid
        WHERE topic.source_name='zulip'
          AND topic.deleted_at IS NULL
          AND (topic.uuid<>raw.workspace_topic_uuid OR (raw.uuid IS NULL AND topic.name LIKE '✔ %'
               AND topic.uuid=messenger_uuid_v5(topic.stream_uuid,topic.name)))
        UNION ALL
        SELECT topic.project_id,topic.uuid,topic.stream_uuid,
            messenger_uuid_v5(topic.stream_uuid,bridge_zulip_v5_topic_base_name(topic.name)),
            bridge_zulip_v5_topic_base_name(topic.name),
            COALESCE((SELECT bool_and(variant.name LIKE '✔ %') FROM bridge_zulip_v5_messages AS message
                      JOIN bridge_zulip_v5_topics AS variant ON variant.uuid=message.topic
                      WHERE message.project_id=topic.project_id AND variant.workspace_topic_uuid=
                            messenger_uuid_v5(topic.stream_uuid,bridge_zulip_v5_topic_base_name(topic.name))),
                     topic.is_done OR topic.name LIKE '✔ %')
        FROM messenger_topics AS topic
        JOIN messenger_streams AS stream ON stream.project_id=topic.project_id AND stream.uuid=topic.stream_uuid
        WHERE topic.source_name='native' AND stream.source_name='zulip'
          AND topic.deleted_at IS NULL
          AND topic.uuid<>messenger_uuid_v5(topic.stream_uuid,'')
          AND topic.uuid<>messenger_uuid_v5(topic.stream_uuid,bridge_zulip_v5_topic_base_name(topic.name))
          AND EXISTS (SELECT 1 FROM bridge_zulip_v5_channels AS channel JOIN m_external_accounts_v2 AS account
                      ON account.uuid=channel.external_account_uuid WHERE account.project_id=topic.project_id
                        AND channel.stream_uuid=topic.stream_uuid AND channel.channel_type='channel')
    LOOP
        PERFORM messenger_move_topic(target.project_id,target.old_uuid,target.new_uuid,target.stream_uuid,
            COALESCE(NULLIF(target.base_name,''),'General Topic'),target.is_done);
        INSERT INTO bridge_zulip_v5_tasks(uuid,project_id,content,queue,concurrency_key,priority,status)
        VALUES (gen_random_uuid(),target.project_id,jsonb_build_object('kind','rewrite_topic_references',
            'old_topic_uuid',target.old_uuid,'new_topic_uuid',target.new_uuid),
            'realtime',target.project_id::text||':topic-references',250000,'new');
    END LOOP;
END;
$$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0264-Separate-Zulip-topic-state-and-provider-placement-from-canonical-identity-c2c796.py"
        ]

    @property
    def migration_id(self):
        return "dd6aae4d-c8ec-4c80-afad-983091c0c910"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(PAYLOAD_SQL)
        session.execute(MOVE_TOPIC_SQL)
        session.execute(NORMALIZE_SQL)

    def downgrade(self, session):
        # Canonical IDs and merged user data remain valid after schema rollback.
        session.execute(
            "DROP FUNCTION messenger_move_topic(uuid,uuid,uuid,uuid,text,boolean)"
        )
        session.execute("DROP FUNCTION messenger_remap_topic_payload(jsonb,uuid,uuid)")
        session.execute("ALTER TABLE messenger_topics DROP COLUMN redirect_topic_uuid")


migration_step = MigrationStep()
