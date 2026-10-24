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

from workspace.common import emoji


SCHEMA = """
CREATE TABLE zulip_outgoing_intents (
    id bigserial PRIMARY KEY,
    uuid uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    project_id uuid NOT NULL,
    entity_type text NOT NULL,
    entity_uuid uuid NOT NULL,
    ordering_key text NOT NULL,
    external_account_uuid uuid NOT NULL REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
    operation text NOT NULL,
    data jsonb NOT NULL,
    request jsonb,
    status text NOT NULL DEFAULT 'new' CHECK(status IN ('new','checking','done','error')),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE UNIQUE INDEX zulip_file_copy_request ON zulip_outgoing_intents(external_account_uuid,entity_uuid,(data->>'account_provider_uuid')) WHERE entity_type='file' AND operation='insert';
CREATE INDEX zulip_outgoing_ready ON zulip_outgoing_intents(next_attempt_at,id) WHERE status IN ('new','checking');
CREATE INDEX zulip_outgoing_entity ON zulip_outgoing_intents(entity_type,entity_uuid,id) WHERE status<>'done';
CREATE INDEX zulip_outgoing_order ON zulip_outgoing_intents(ordering_key,id) WHERE status<>'done';
-- Copies of native entities belong here, never in native provider_identity.
-- No entity FK: deletion intents and in-flight dependencies must survive deletion.
CREATE TABLE zulip_entity_links (
    project_id uuid NOT NULL,
    entity_type text NOT NULL,
    entity_uuid uuid NOT NULL,
    provider_uuid uuid NOT NULL REFERENCES messenger_providers(uuid),
    external_account_uuid uuid NOT NULL REFERENCES m_external_accounts_v2(uuid) ON DELETE CASCADE,
    provider_identity varchar(255) NOT NULL,
    data jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY(entity_type,entity_uuid,provider_uuid,external_account_uuid)
);
CREATE FUNCTION zulip_outgoing_wakeup() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('workspace_zulip_outgoing','');
    RETURN NULL;
END $$;
CREATE TRIGGER zulip_outgoing_wakeup AFTER INSERT ON zulip_outgoing_intents
FOR EACH STATEMENT EXECUTE FUNCTION zulip_outgoing_wakeup();
"""

CAPTURE = """
CREATE FUNCTION zulip_capture_outgoing() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actor uuid := nullif(current_setting('workspace.outgoing_actor',true),'')::uuid;
    row_ jsonb := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    old_ jsonb := CASE WHEN TG_OP='INSERT' THEN '{}'::jsonb ELSE to_jsonb(OLD) END;
    kind_ text := TG_ARGV[0];
    operation_ text := lower(TG_OP);
    stream_ uuid;
    message_ uuid;
    entity_ uuid := (row_->>'uuid')::uuid;
    placement_ record;
    target_ record;
    data_ jsonb;
BEGIN
    IF actor IS NULL OR pg_trigger_depth()>1 THEN RETURN NULL; END IF;
    IF kind_='message' THEN
        IF TG_TABLE_NAME='messenger_message_placements' THEN
            message_ := (row_->>'message_uuid')::uuid;
            entity_ := message_;
            SELECT to_jsonb(m) INTO data_ FROM messenger_messages m WHERE m.project_id=(row_->>'project_id')::uuid AND m.uuid=message_;
            row_ := data_ || jsonb_build_object('placement_uuid',row_->>'uuid','stream_uuid',row_->>'stream_uuid','topic_uuid',row_->>'topic_uuid');
        ELSE
            IF row_->'payload' IS NOT DISTINCT FROM old_->'payload' AND row_->'deleted_at' IS NOT DISTINCT FROM old_->'deleted_at' THEN RETURN NULL; END IF;
            message_ := entity_;
            IF row_->>'deleted_at' IS NOT NULL THEN operation_ := 'delete'; END IF;
        END IF;
        IF row_->'payload'->>'kind'<>'markdown' THEN RETURN NULL; END IF;
    ELSIF kind_='reaction' THEN
        message_ := (row_->>'canonical_message_uuid')::uuid;
        IF TG_OP='UPDATE' AND row_->'emoji_name'=old_->'emoji_name' AND row_->'canonical_message_uuid'=old_->'canonical_message_uuid' THEN RETURN NULL; END IF;
    ELSIF kind_='message_state' THEN
        IF TG_OP='DELETE' THEN RETURN NULL; END IF;
        IF TG_OP='INSERT' AND NOT (row_->>'starred')::boolean THEN RETURN NULL; END IF;
        IF TG_OP='UPDATE' AND (row_->>'read_at' IS NOT NULL)=(old_->>'read_at' IS NOT NULL) AND row_->'starred'=old_->'starred' THEN RETURN NULL; END IF;
        SELECT message_uuid INTO message_ FROM messenger_message_placements WHERE project_id=(row_->>'project_id')::uuid AND uuid=(row_->>'placement_uuid')::uuid;
    ELSIF kind_='stream_binding' THEN
        IF TG_OP='UPDATE' AND row_->'active'=old_->'active' AND row_->'notification_mode'=old_->'notification_mode' THEN RETURN NULL; END IF;
    ELSIF kind_='topic_binding' THEN
        IF TG_OP='INSERT' AND row_->>'notification_mode'='default' THEN RETURN NULL; END IF;
        IF TG_OP='UPDATE' AND row_->'notification_mode'=old_->'notification_mode' THEN RETURN NULL; END IF;
        SELECT stream_uuid INTO stream_ FROM messenger_topics WHERE project_id=(row_->>'project_id')::uuid AND uuid=(row_->>'topic_uuid')::uuid;
    ELSIF kind_='stream' THEN
        stream_ := entity_;
        IF TG_OP='UPDATE' AND ROW(row_->'name',row_->'description',row_->'invite_only',row_->'is_archived',row_->'deleted_at') IS NOT DISTINCT FROM ROW(old_->'name',old_->'description',old_->'invite_only',old_->'is_archived',old_->'deleted_at') THEN RETURN NULL; END IF;
    ELSIF kind_='topic' THEN
        IF TG_OP='UPDATE' AND ROW(row_->'name',row_->'is_done',row_->'deleted_at') IS NOT DISTINCT FROM ROW(old_->'name',old_->'is_done',old_->'deleted_at') THEN RETURN NULL; END IF;
    ELSIF kind_='file' AND TG_OP='UPDATE' THEN
        -- File metadata is local; Zulip only supports uploading/removing content.
        RETURN NULL;
    END IF;
    stream_ := coalesce(stream_,(row_->>'stream_uuid')::uuid);
    IF message_ IS NOT NULL THEN
        SELECT stream_uuid,topic_uuid,uuid INTO placement_ FROM messenger_message_placements
        WHERE project_id=(row_->>'project_id')::uuid AND message_uuid=message_
        AND (stream_ IS NULL OR stream_uuid=stream_) ORDER BY uuid LIMIT 1;
        stream_ := coalesce(stream_,placement_.stream_uuid);
        row_ := row_ || jsonb_build_object('message_uuid',message_,'stream_uuid',stream_,'topic_uuid',placement_.topic_uuid);
    END IF;
    IF stream_ IS NULL THEN RETURN NULL; END IF;
    row_ := row_ || jsonb_build_object('stream_uuid',stream_);
    SELECT row_ || jsonb_build_object('stream_name',stream.name,'topic_name',coalesce(
        CASE WHEN topic.uuid=messenger_uuid_v5(stream_,'') THEN ''
             WHEN topic.is_done AND topic.name NOT LIKE '✔ %' THEN '✔ '||topic.name
             ELSE topic.name END,'')) INTO data_
    FROM messenger_streams stream LEFT JOIN messenger_topics topic
      ON topic.project_id=stream.project_id AND topic.uuid=(row_->>'topic_uuid')::uuid
    WHERE stream.project_id=(row_->>'project_id')::uuid AND stream.uuid=stream_;

    row_ := coalesce(data_,row_);
    IF row_->>'deleted_at' IS NOT NULL THEN operation_ := 'delete'; END IF;
    IF kind_='reaction' AND TG_OP='UPDATE' THEN
        operation_ := 'insert';
        INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
        SELECT (old_->>'project_id')::uuid,'reaction',entity_,
            'reaction:'||(old_->>'canonical_message_uuid')||':'||(old_->>'user_uuid')||':'||(old_->>'emoji_name'),
            account.uuid,'delete',old_||jsonb_build_object('message_uuid',old_->>'canonical_message_uuid','stream_uuid',placement.stream_uuid,
              'before','{}'::jsonb,'account_identity',account.external_account_id,'account_provider_uuid',account.provider_uuid,
              'actor_uuid',actor,'remote_message_identity',coalesce(message.provider_identity,
                (SELECT provider_identity FROM zulip_entity_links WHERE entity_type='message' AND entity_uuid=message.uuid AND external_account_uuid=account.uuid)))
        FROM messenger_message_placements placement JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
        JOIN bridge_zulip_v5_channels channel ON channel.stream_uuid=placement.stream_uuid
        JOIN m_external_accounts_v2 account ON account.uuid=channel.external_account_uuid AND account.project_id=placement.project_id
        WHERE placement.uuid=(old_->>'placement_uuid')::uuid AND placement.project_id=(old_->>'project_id')::uuid AND account.owner_user_uuid=actor AND NOT account.disabled;
    END IF;
    FOR target_ IN
        SELECT account.uuid, account.provider_uuid, account.external_account_id, channel.uuid AS channel_uuid, channel.stream_id, channel.channel_type, channel.direct_user_uuid
        FROM m_external_accounts_v2 account
        LEFT JOIN bridge_zulip_v5_channels channel ON channel.external_account_uuid=account.uuid AND channel.stream_uuid=stream_
        JOIN messenger_streams stream ON stream.project_id=account.project_id AND stream.uuid=stream_
        WHERE account.project_id=(row_->>'project_id')::uuid AND account.owner_user_uuid=actor AND account.provider='zulip'
        AND NOT account.disabled AND account.status<>'new' AND account.settings->>'api_key' IS NOT NULL
        AND ((stream.source_name='zulip' AND stream.provider_uuid=account.provider_uuid)
             OR EXISTS(SELECT 1 FROM zulip_entity_links link WHERE link.entity_type='stream' AND link.entity_uuid=stream_ AND link.external_account_uuid=account.uuid AND link.provider_uuid=account.provider_uuid))
    LOOP
        data_ := row_ || jsonb_build_object('before',old_,'actor_uuid',actor,'account_identity',target_.external_account_id,'account_provider_uuid',target_.provider_uuid,'stream_id',target_.stream_id,'channel_type',target_.channel_type,'direct_user_uuid',target_.direct_user_uuid);
        IF kind_='reaction' THEN
            SELECT data_ || jsonb_build_object('remote_message_identity',coalesce(
                (SELECT provider_identity FROM messenger_messages WHERE project_id=(row_->>'project_id')::uuid AND uuid=message_ AND provider_uuid=target_.provider_uuid),
                (SELECT link.provider_identity FROM zulip_entity_links link WHERE link.entity_type='message' AND link.entity_uuid=message_ AND link.provider_uuid=target_.provider_uuid LIMIT 1)),
                'emoji_observations',(SELECT coalesce(jsonb_agg(jsonb_build_object('emoji_name',r.emoji_name,'emoji_code',r.emoji_code,'reaction_type',r.reaction_type)),'[]'::jsonb)
                    FROM bridge_zulip_v5_reactions r JOIN bridge_zulip_v5_reaction_messages m ON m.uuid=r.reaction_message_uuid WHERE m.project_id=(row_->>'project_id')::uuid AND m.canonical_message_uuid=message_)) INTO data_;
        END IF;
        IF kind_='stream_binding' AND row_->'notification_mode' IS DISTINCT FROM old_->'notification_mode' THEN
            UPDATE bridge_zulip_v5_channels SET notification_mode=row_->>'notification_mode' WHERE uuid=target_.channel_uuid;
        ELSIF kind_='topic_binding' THEN
            INSERT INTO bridge_zulip_v5_topic_notifications(uuid,channel_uuid,topic_name,visibility_policy,last_updated)
            SELECT gen_random_uuid(),target_.channel_uuid,lower(row_->>'topic_name'),
                CASE row_->>'notification_mode' WHEN 'default' THEN 0 WHEN 'mute' THEN 1 WHEN 'unmute' THEN 2 WHEN 'follow' THEN 3 END,0
            WHERE target_.channel_uuid IS NOT NULL
            ON CONFLICT(channel_uuid,topic_name) DO UPDATE SET visibility_policy=EXCLUDED.visibility_policy,last_updated=0;
        END IF;
        -- Save known provider IDs now, so deletes never depend on a surviving row.
        SELECT data_ || jsonb_build_object('remote_identity',coalesce(
            (SELECT provider_identity FROM zulip_entity_links WHERE entity_type=kind_ AND entity_uuid=entity_ AND external_account_uuid=target_.uuid AND provider_uuid=target_.provider_uuid),
            row_->>'provider_identity')) INTO data_;
        INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
        VALUES((row_->>'project_id')::uuid,kind_,entity_,CASE WHEN kind_='reaction' THEN 'reaction:'||message_::text||':'||(row_->>'user_uuid')||':'||(row_->>'emoji_name') ELSE kind_||':'||entity_::text END,target_.uuid,operation_,data_);
    END LOOP;
    IF FOUND AND TG_OP<>'DELETE' AND row_->>'source_name'<>'native' THEN
        EXECUTE format('UPDATE %I SET status=''new'' WHERE project_id=$1 AND uuid=$2 AND status<>''new''',
            CASE WHEN TG_TABLE_NAME='messenger_message_placements' THEN 'messenger_messages' ELSE TG_TABLE_NAME END)
        USING (row_->>'project_id')::uuid,entity_;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER zulip_capture_message_create AFTER INSERT ON messenger_message_placements FOR EACH ROW EXECUTE FUNCTION zulip_capture_outgoing('message');
CREATE TRIGGER zulip_capture_message_update AFTER UPDATE ON messenger_messages FOR EACH ROW EXECUTE FUNCTION zulip_capture_outgoing('message');
"""
for _table, _kind in (
    ("messenger_streams", "stream"),
    ("messenger_topics", "topic"),
    ("messenger_stream_bindings", "stream_binding"),
    ("messenger_user_topic_bindings", "topic_binding"),
    ("messenger_user_message_states", "message_state"),
    ("messenger_message_reaction_facts", "reaction"),
    ("m_workspace_files", "file"),
):
    CAPTURE += f"CREATE TRIGGER zulip_capture_outgoing AFTER INSERT OR UPDATE OR DELETE ON {_table} FOR EACH ROW EXECUTE FUNCTION zulip_capture_outgoing('{_kind}');\n"

CUTOVER = """
UPDATE m_workspace_files file SET provider_identity=link.zulip_url
FROM bridge_zulip_v5_file_links link JOIN messenger_providers provider ON provider.hostname=link.hostname
WHERE file.uuid=link.file_uuid AND file.project_id=link.project_id AND file.provider_uuid=provider.uuid AND length(link.zulip_url)<=255;
DROP TRIGGER bridge_zulip_v5_subscribe_workspace_events ON messenger_domain_outbox_events;
DROP FUNCTION bridge_zulip_v5_subscribe_workspace_events();
-- Keep immutable delivery/content observations for inbound local-echo matching.
INSERT INTO zulip_entity_links(project_id,entity_type,entity_uuid,provider_uuid,external_account_uuid,provider_identity)
SELECT delivery.project_id,'message',delivery.message_uuid,provider.uuid,delivery.external_account_uuid,delivery.zulip_message_id::text
FROM bridge_zulip_v5_deliveries delivery JOIN messenger_providers provider ON provider.hostname=delivery.hostname WHERE delivery.zulip_message_id IS NOT NULL ON CONFLICT DO NOTHING;
INSERT INTO zulip_outgoing_intents(uuid,project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data,status,request)
SELECT task.uuid,task.project_id,'message',delivery.message_uuid,'message:'||delivery.message_uuid::text,task.external_account_uuid,
       CASE task.content->>'kind' WHEN 'send_message' THEN 'insert' ELSE 'update' END,
       to_jsonb(message) || jsonb_build_object('stream_uuid',delivery.stream_uuid,'topic_name',delivery.topic_name,'before','{}'::jsonb,'payload',jsonb_build_object('kind','markdown','content',task.content->>'content')),
       CASE WHEN task.content->>'kind'='send_message' AND delivery.sending AND delivery.zulip_message_id IS NULL THEN 'checking' ELSE 'new' END,
       CASE WHEN task.content->>'kind'='send_message' AND delivery.sending AND delivery.zulip_message_id IS NULL THEN jsonb_build_object('kind','send','method','POST','path','/messages','delivery_uuid',delivery.uuid,'last_event_id',coalesce(subscription.last_event_id,-1),'data',jsonb_build_object('queue_id',subscription.queue_id,'local_id',delivery.uuid::text)) END
FROM bridge_zulip_v5_tasks task JOIN bridge_zulip_v5_deliveries delivery ON delivery.uuid=(task.content->>'delivery_uuid')::uuid
JOIN messenger_messages message ON message.uuid=delivery.message_uuid AND message.project_id=task.project_id
LEFT JOIN bridge_zulip_v5_subscriptions subscription ON subscription.external_account_uuid=task.external_account_uuid
WHERE task.content->>'kind' IN ('send_message','edit_message') AND task.status IN ('new','waiting','retry','error') ORDER BY task.created_at,task.uuid;
INSERT INTO zulip_outgoing_intents(uuid,project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
SELECT uuid,project_id,'message',uuid,'message:'||uuid::text,external_account_uuid,'delete',content || jsonb_build_object('remote_identity',content->>'message_id')
FROM bridge_zulip_v5_tasks WHERE content->>'kind'='delete_message' AND status IN ('new','waiting','retry','error');
-- Preserve the remaining queued operation snapshots before removing old kinds.
INSERT INTO zulip_outgoing_intents(uuid,project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
SELECT task.uuid,task.project_id,'message_state',state.uuid,'message_state:'||state.uuid::text,task.external_account_uuid,'update',
       to_jsonb(state)||jsonb_build_object('message_uuid',placement.message_uuid,'before',jsonb_build_object(
           'read_at',CASE WHEN task.content->>'flag'='read' AND NOT (task.content->>'value')::boolean THEN now() ELSE NULL END,
           'starred',CASE WHEN task.content->>'flag'='starred' THEN NOT (task.content->>'value')::boolean ELSE state.starred END))
FROM bridge_zulip_v5_tasks task JOIN messenger_user_message_states state ON state.uuid=(task.content->>'state_uuid')::uuid AND state.project_id=task.project_id
JOIN messenger_message_placements placement ON placement.uuid=state.placement_uuid AND placement.project_id=state.project_id
WHERE task.content->>'kind'='update_message_flags' AND task.status IN ('new','waiting','retry','error');
INSERT INTO zulip_outgoing_intents(uuid,project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
SELECT task.uuid,task.project_id,'reaction',choice.uuid,'reaction:'||choice.canonical_message_uuid::text||':'||choice.user_uuid::text||':'||choice.emoji_name,
       task.external_account_uuid,CASE WHEN choice.present THEN 'insert' ELSE 'delete' END,
       to_jsonb(choice)||jsonb_build_object('message_uuid',choice.canonical_message_uuid,'before','{}'::jsonb)
FROM bridge_zulip_v5_tasks task JOIN bridge_zulip_v5_reaction_intents choice ON choice.uuid=(task.content->>'intent_uuid')::uuid
WHERE task.content->>'kind'='update_reaction' AND task.status IN ('new','waiting','retry','error');
INSERT INTO zulip_outgoing_intents(uuid,project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
SELECT task.uuid,task.project_id,CASE WHEN task.content->>'topic_name' IS NULL THEN 'stream_binding' ELSE 'topic_binding' END,
       task.uuid,'notifications:'||channel.stream_uuid::text||':'||task.external_account_uuid::text||coalesce(task.content->>'topic_name',''),
       task.external_account_uuid,'update',task.content||jsonb_build_object('stream_uuid',channel.stream_uuid,'stream_id',channel.stream_id,'channel_type','channel',
         'user_uuid',account.owner_user_uuid,'active',true,'before',jsonb_build_object('active',true))
FROM bridge_zulip_v5_tasks task JOIN bridge_zulip_v5_channels channel ON channel.uuid=(task.content->>'channel_uuid')::uuid
JOIN m_external_accounts_v2 account ON account.uuid=task.external_account_uuid
WHERE task.content->>'kind'='update_notifications' AND task.status IN ('new','waiting','retry','error');
-- API choices that have not reached the former task scheduler are durable too.
INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
SELECT state.project_id,'message_state',state.uuid,'message_state:'||state.uuid::text,changes.external_account_uuid,'update',
    to_jsonb(state)||jsonb_build_object('message_uuid',changes.message_uuid,'remote_message_identity',changes.message_id::text,
      'before',jsonb_build_object('read_at',CASE WHEN changes.read_changed THEN CASE WHEN state.read_at IS NULL THEN to_jsonb(now()) ELSE 'null'::jsonb END ELSE to_jsonb(state)->'read_at' END,
        'starred',CASE WHEN changes.star_changed THEN NOT state.starred ELSE state.starred END))
FROM (SELECT project_id,state_uuid,external_account_uuid,message_uuid,message_id,bool_or(flag='read') AS read_changed,bool_or(flag='starred') AS star_changed
    FROM bridge_zulip_v5_flag_states f WHERE f.local_updated_at IS NOT NULL AND f.local_value IS DISTINCT FROM f.remote_value
      AND (f.remote_updated_at IS NULL OR f.local_updated_at>f.remote_updated_at)
      AND NOT EXISTS(SELECT 1 FROM bridge_zulip_v5_tasks task WHERE task.content->>'kind'='update_message_flags'
        AND task.content->>'state_uuid'=f.state_uuid::text AND task.external_account_uuid=f.external_account_uuid
        AND task.content->>'flag'=f.flag AND (task.content->>'value')::boolean=f.local_value AND task.status IN ('new','waiting','retry','error'))
    GROUP BY project_id,state_uuid,external_account_uuid,message_uuid,message_id) changes
JOIN messenger_user_message_states state ON state.project_id=changes.project_id AND state.uuid=changes.state_uuid;
INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
SELECT choice.project_id,'reaction',coalesce(fact.uuid,choice.uuid),
    'reaction:'||choice.canonical_message_uuid::text||':'||choice.user_uuid::text||':'||choice.emoji_name,account.uuid,
    CASE WHEN choice.present THEN 'insert' ELSE 'delete' END,
    to_jsonb(choice)||jsonb_build_object('message_uuid',choice.canonical_message_uuid,'before','{}'::jsonb,
      'remote_message_identity',coalesce(message.provider_identity,delivery.zulip_message_id::text),
      'emoji_observations',(SELECT coalesce(jsonb_agg(jsonb_build_object('emoji_name',r.emoji_name,'emoji_code',r.emoji_code,'reaction_type',r.reaction_type)),'[]'::jsonb)
        FROM bridge_zulip_v5_reactions r JOIN bridge_zulip_v5_reaction_messages m ON m.uuid=r.reaction_message_uuid WHERE m.project_id=choice.project_id AND m.canonical_message_uuid=choice.canonical_message_uuid))
FROM bridge_zulip_v5_reaction_intents choice JOIN messenger_messages message ON message.project_id=choice.project_id AND message.uuid=choice.canonical_message_uuid
JOIN m_external_accounts_v2 account ON account.project_id=choice.project_id AND account.owner_user_uuid=choice.user_uuid AND account.provider='zulip' AND NOT account.disabled
LEFT JOIN messenger_message_reaction_facts fact ON fact.project_id=choice.project_id AND fact.canonical_message_uuid=choice.canonical_message_uuid AND fact.user_uuid=choice.user_uuid AND fact.emoji_name=choice.emoji_name
LEFT JOIN bridge_zulip_v5_deliveries delivery ON delivery.project_id=choice.project_id AND delivery.message_uuid=choice.canonical_message_uuid AND delivery.external_account_uuid=account.uuid
WHERE (message.provider_uuid=account.provider_uuid OR delivery.uuid IS NOT NULL)
 AND NOT EXISTS(SELECT 1 FROM zulip_outgoing_intents intent WHERE intent.entity_type='reaction' AND intent.data->>'canonical_message_uuid'=choice.canonical_message_uuid::text AND intent.data->>'user_uuid'=choice.user_uuid::text AND intent.data->>'emoji_name'=choice.emoji_name)
 AND NOT EXISTS(SELECT 1 FROM bridge_zulip_v5_tasks task WHERE task.content->>'kind'='update_reaction' AND task.content->>'intent_uuid'=choice.uuid::text AND task.status='done' AND (task.content->>'changed_at')::timestamp=choice.changed_at)
 AND NOT EXISTS(SELECT 1 FROM bridge_zulip_v5_reaction_messages m JOIN messenger_providers p ON p.hostname=m.hostname
    WHERE m.project_id=choice.project_id AND m.canonical_message_uuid=choice.canonical_message_uuid AND p.uuid=account.provider_uuid AND m.snapshot_at>=choice.changed_at)
 AND NOT EXISTS(SELECT 1 FROM bridge_zulip_v5_reactions r JOIN bridge_zulip_v5_reaction_messages m ON m.uuid=r.reaction_message_uuid
    JOIN zulip_choice_codes code ON code.code=r.emoji_code AND code.name=choice.emoji_name
    WHERE m.project_id=choice.project_id AND m.canonical_message_uuid=choice.canonical_message_uuid AND r.user_id::text=account.external_account_id AND r.observed_at>=choice.changed_at);
INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
SELECT event.project_id,'message',message.uuid,'message:'||message.uuid::text,account.uuid,
       CASE WHEN outbox.payload->>'source_kind'='message.created' THEN 'insert' ELSE 'update' END,
       to_jsonb(message)||jsonb_build_object('stream_uuid',placement.stream_uuid,'topic_name',topic.name,'before','{}'::jsonb)
FROM bridge_zulip_v5_workspace_events event JOIN messenger_domain_outbox_events outbox ON outbox.uuid=event.uuid
JOIN messenger_message_placements placement ON placement.uuid=(outbox.payload->>'placement_uuid')::uuid AND placement.project_id=event.project_id
JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid
JOIN m_external_accounts_v2 account ON account.project_id=event.project_id AND account.owner_user_uuid=message.author_uuid AND account.provider='zulip'
JOIN bridge_zulip_v5_channels channel ON channel.stream_uuid=placement.stream_uuid AND channel.external_account_uuid=account.uuid
WHERE event.processed_at IS NULL AND message.deleted_at IS NULL
  AND outbox.payload->>'source_kind' IN ('message.created','message.updated') ORDER BY event.created_at,event.uuid;
INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
SELECT event.project_id,'message',(deletion->>'delivery_uuid')::uuid,'message:'||(deletion->>'delivery_uuid'),
       (deletion->>'external_account_uuid')::uuid,'delete',deletion->'content'||jsonb_build_object('remote_identity',deletion->'content'->>'message_id')
FROM bridge_zulip_v5_workspace_events event JOIN messenger_domain_outbox_events outbox ON outbox.uuid=event.uuid
CROSS JOIN LATERAL jsonb_array_elements(outbox.payload->'zulip_deletions') deletion
WHERE event.processed_at IS NULL;
DELETE FROM bridge_zulip_v5_tasks WHERE content->>'kind' IN ('send_message','edit_message','delete_message','update_message_flags','update_reaction','update_notifications');
-- Copy receipts are operation data; they must outlive a deleted local root.
DO $$ DECLARE fk record; BEGIN
    FOR fk IN SELECT conname FROM pg_constraint WHERE conrelid='bridge_zulip_v5_deliveries'::regclass AND confrelid IN ('messenger_messages'::regclass,'messenger_streams'::regclass) LOOP
        EXECUTE format('ALTER TABLE bridge_zulip_v5_deliveries DROP CONSTRAINT %I',fk.conname);
    END LOOP;
END $$;
UPDATE zulip_outgoing_intents intent SET data=intent.data||jsonb_build_object(
    'actor_uuid',coalesce(intent.data->>'actor_uuid',intent.data->>'author_uuid',intent.data->>'user_uuid',account.owner_user_uuid::text),
    'account_identity',coalesce(intent.data->>'external_account_id',account.external_account_id),
    'account_provider_uuid',coalesce((SELECT uuid::text FROM messenger_providers WHERE hostname=intent.data->>'hostname'),account.provider_uuid::text))
FROM m_external_accounts_v2 account WHERE account.uuid=intent.external_account_uuid;
DROP TABLE bridge_zulip_v5_workspace_events;
DROP TRIGGER bridge_zulip_v5_track_local_reactions ON messenger_message_reaction_facts;
DROP FUNCTION bridge_zulip_v5_track_local_reactions();
ALTER TABLE bridge_zulip_v5_tasks DROP COLUMN reaction_intent_uuid, DROP COLUMN reaction_message_uuid, DROP COLUMN delivery_uuid, DROP COLUMN flag_state_uuid;
CREATE OR REPLACE FUNCTION bridge_zulip_v5_requeue_reactions() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_reaction_changes(project_id,canonical_message_uuid)
    SELECT message.project_id,message.canonical_message_uuid
    FROM bridge_zulip_v5_reactions reaction JOIN bridge_zulip_v5_reaction_messages message ON message.uuid=reaction.reaction_message_uuid
    WHERE message.project_id=NEW.project_id AND message.canonical_message_uuid IS NOT NULL
      AND OLD.provider_uuid=(SELECT uuid FROM messenger_providers WHERE hostname=message.hostname)
      AND OLD.external_account_id=reaction.user_id::text AND OLD.owner_user_uuid IS DISTINCT FROM NEW.owner_user_uuid
    ON CONFLICT(project_id,canonical_message_uuid) DO UPDATE SET revision=EXCLUDED.revision,next_retry_at=NULL;
    RETURN NULL;
END $$;
DROP TRIGGER newest_message_first ON bridge_zulip_v5_tasks;
CREATE OR REPLACE FUNCTION bridge_zulip_v5_order_message_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_message_uuid uuid;
BEGIN
    IF TG_TABLE_NAME='bridge_zulip_v5_flag_changes' THEN
        SELECT placement.message_uuid INTO target_message_uuid FROM messenger_user_message_states state
        JOIN messenger_message_placements placement ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
        WHERE state.project_id=NEW.project_id AND state.uuid=NEW.state_uuid;
    ELSIF TG_TABLE_NAME='bridge_zulip_v5_reaction_changes' THEN
        target_message_uuid := NEW.canonical_message_uuid;
    ELSE
        target_message_uuid := NEW.message_uuid;
    END IF;
    NEW.message_created_at := coalesce(bridge_zulip_v5_message_created_at(NEW.project_id,target_message_uuid),NEW.message_created_at,'epoch'::timestamp);
    NEW.message_sequence := coalesce(
        (SELECT max(observed.message_id) FROM bridge_zulip_v5_messages observed WHERE observed.project_id=NEW.project_id AND messenger_uuid_v5(observed.project_id,observed.message_uuid::text)=target_message_uuid),
        (SELECT max(delivery.zulip_message_id) FROM bridge_zulip_v5_deliveries delivery WHERE delivery.project_id=NEW.project_id AND delivery.message_uuid=target_message_uuid),NEW.message_sequence,0);
    RETURN NEW;
END $$;
DROP TABLE bridge_zulip_v5_reaction_intents CASCADE;
DROP FUNCTION bridge_zulip_v5_track_reaction_intents();
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0264-Add-synchronization-status-to-provider-entities-ca9efd.py"
        ]

    @property
    def migration_id(self):
        return "9cafda11-243a-408d-b553-7ba8112f6b56"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(SCHEMA)
        session.execute(CAPTURE)
        # Match canonical Workspace names to exact persisted provider codes.
        codes = session.execute(
            "SELECT DISTINCT emoji_code,emoji_name FROM bridge_zulip_v5_reactions WHERE reaction_type='unicode_emoji'"
        ).fetchall()
        pairs = {
            (
                emoji.name_for_code(row["emoji_code"], row["emoji_name"]),
                row["emoji_code"],
            )
            for row in codes
        }
        session.execute(
            "CREATE TEMP TABLE zulip_choice_codes(name text,code text) ON COMMIT DROP"
        )
        session.execute(
            "INSERT INTO zulip_choice_codes SELECT * FROM unnest(%s::text[],%s::text[])",
            ([name for name, code in pairs], [code for name, code in pairs]),
        )
        session.execute(CUTOVER)

    def downgrade(self, session):
        raise NotImplementedError(
            "Outbound task removal requires restoring the previous release and database backup"
        )


migration_step = MigrationStep()
