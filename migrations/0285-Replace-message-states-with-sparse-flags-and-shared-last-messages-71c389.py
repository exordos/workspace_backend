# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from restalchemy.storage.sql import migrations


FLAGS = {
    "unreads": "read_at IS NULL",
    "stars": "starred",
    "pins": "pinned",
    "mentions": "mentioned",
}

SCHEMA = """
LOCK TABLE messenger_user_message_states, messenger_user_message_bindings IN SHARE ROW EXCLUSIVE MODE;
ALTER TABLE messenger_message_placements ADD CONSTRAINT messenger_placement_scope_key
    UNIQUE (project_id,uuid,stream_uuid,topic_uuid);
-- Active state and membership generation are the access-fencing key.
-- KEY SHARE protects this key while permitting cached counter updates.
CREATE UNIQUE INDEX messenger_stream_membership_access_key ON messenger_stream_bindings
    (project_id,user_uuid,stream_uuid,membership_generation,active);
CREATE SEQUENCE messenger_counter_update_revision;
CREATE TABLE messenger_counter_updates (
    project_id uuid NOT NULL, user_uuid uuid NOT NULL,
    stream_uuid uuid NOT NULL, topic_uuid uuid NOT NULL,
    revision bigint NOT NULL DEFAULT nextval('messenger_counter_update_revision'),
    next_retry_at timestamptz,
    publish_updates boolean NOT NULL DEFAULT (current_setting('workspace.zulip_history',true) IS DISTINCT FROM 'on'),
    PRIMARY KEY(project_id,user_uuid,topic_uuid)
);
CREATE INDEX messenger_counter_updates_revision ON messenger_counter_updates(revision);
CREATE FUNCTION messenger_queue_counters(p_project uuid,p_user uuid,p_stream uuid,p_topic uuid,p_publish boolean DEFAULT NULL)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid,publish_updates)
    VALUES(p_project,p_user,p_stream,p_topic,COALESCE(p_publish,current_setting('workspace.zulip_history',true) IS DISTINCT FROM 'on'))
    ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE
        SET stream_uuid=EXCLUDED.stream_uuid,revision=EXCLUDED.revision,publish_updates=messenger_counter_updates.publish_updates OR EXCLUDED.publish_updates,next_retry_at=NULL;
    SELECT pg_notify('workspace_counters','');
$$;
"""

MESSAGE_VIEW = """
CREATE OR REPLACE VIEW messenger_api_user_messages_v1 AS
SELECT binding.uuid AS binding_uuid,placement.uuid,message.uuid AS canonical_message_uuid,
       binding.user_uuid,placement.project_id,message.created_at,message.updated_at,
       placement.stream_uuid,placement.topic_uuid,message.payload,message.author_uuid,
       NOT EXISTS(SELECT 1 FROM messenger_user_message_unreads f WHERE
           (f.project_id,f.user_uuid,f.placement_uuid)=(binding.project_id,binding.user_uuid,placement.uuid)) AS read,
       EXISTS(SELECT 1 FROM messenger_user_message_pins f WHERE
           (f.project_id,f.user_uuid,f.placement_uuid)=(binding.project_id,binding.user_uuid,placement.uuid)) AS pinned,
       EXISTS(SELECT 1 FROM messenger_user_message_stars f WHERE
           (f.project_id,f.user_uuid,f.placement_uuid)=(binding.project_id,binding.user_uuid,placement.uuid)) AS starred,
       message.author_uuid=binding.user_uuid AS is_own,
       EXISTS(SELECT 1 FROM messenger_user_message_mentions f WHERE
           (f.project_id,f.user_uuid,f.placement_uuid)=(binding.project_id,binding.user_uuid,placement.uuid)) AS mentioned,
       message.reactions,message.reaction_users,message.source_name,message.provider_uuid,message.provider_identity,
       COALESCE(message.deleted_at,stream.deleted_at,topic.deleted_at) AS deleted_at,
       COALESCE(message.deleted_at,stream.deleted_at,topic.deleted_at) IS NULL AS visible,message.status,
       membership.membership_generation
FROM messenger_user_message_bindings binding
JOIN messenger_message_placements placement ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid
JOIN messenger_messages_with_content_v1 message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
JOIN messenger_stream_bindings membership ON membership.project_id=binding.project_id AND membership.user_uuid=binding.user_uuid
    AND membership.stream_uuid=placement.stream_uuid AND membership.active
JOIN messenger_streams stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid;
"""

FACTS = """
CREATE FUNCTION messenger_initialize_message_flags() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE placement record; message record;
BEGIN
    SELECT * INTO placement FROM messenger_message_placements WHERE project_id=NEW.project_id AND uuid=NEW.placement_uuid;
    SELECT author_uuid,canonical.created_at,contents.workspace_content AS payload INTO message
    FROM messenger_messages canonical JOIN messenger_message_contents contents ON contents.uuid=canonical.content_uuid
    WHERE canonical.project_id=placement.project_id AND canonical.uuid=placement.message_uuid;
    IF current_setting('workspace.zulip_incoming',true)='on' OR (message.author_uuid<>NEW.user_uuid AND message.created_at >= (
        SELECT membership_started_at FROM messenger_stream_bindings
        WHERE project_id=NEW.project_id AND user_uuid=NEW.user_uuid AND stream_uuid=placement.stream_uuid AND active
    )) THEN
        INSERT INTO messenger_user_message_unreads VALUES(NEW.project_id,NEW.user_uuid,NEW.placement_uuid,placement.stream_uuid,placement.topic_uuid)
        ON CONFLICT DO NOTHING;
    END IF;
    IF message.payload->>'kind'='markdown' AND position('](urn:user:'||NEW.user_uuid::text||')' IN lower(message.payload->>'content'))>0 THEN
        INSERT INTO messenger_user_message_mentions VALUES(NEW.project_id,NEW.user_uuid,NEW.placement_uuid,placement.stream_uuid,placement.topic_uuid)
        ON CONFLICT DO NOTHING;
    END IF;
    PERFORM messenger_queue_counters(NEW.project_id,NEW.user_uuid,placement.stream_uuid,placement.topic_uuid);
    INSERT INTO bridge_zulip_v5_flag_changes(project_id,state_uuid)
    SELECT NEW.project_id,NEW.uuid FROM messenger_streams
    WHERE project_id=NEW.project_id AND uuid=placement.stream_uuid AND source_name='zulip'
    ON CONFLICT(project_id,state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_initialize_message_flags AFTER INSERT ON messenger_user_message_bindings
    FOR EACH ROW EXECUTE FUNCTION messenger_initialize_message_flags();
CREATE FUNCTION messenger_track_sparse_counter_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN
        INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid)
        SELECT DISTINCT project_id,user_uuid,stream_uuid,topic_uuid FROM old_flags ORDER BY 1,2,4
        ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE SET stream_uuid=EXCLUDED.stream_uuid,revision=EXCLUDED.revision,publish_updates=messenger_counter_updates.publish_updates OR EXCLUDED.publish_updates,next_retry_at=NULL;
    END IF;
    IF TG_OP<>'DELETE' THEN
        INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid)
        SELECT DISTINCT project_id,user_uuid,stream_uuid,topic_uuid FROM new_flags ORDER BY 1,2,4
        ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE SET stream_uuid=EXCLUDED.stream_uuid,revision=EXCLUDED.revision,publish_updates=messenger_counter_updates.publish_updates OR EXCLUDED.publish_updates,next_retry_at=NULL;
    END IF;
    RETURN NULL;
END $$;
CREATE FUNCTION messenger_track_sparse_access_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid)
    SELECT DISTINCT binding.project_id,binding.user_uuid,placement.stream_uuid,placement.topic_uuid
    FROM old_bindings binding JOIN messenger_message_placements placement
      ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid ORDER BY 1,2,4
    ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE SET revision=EXCLUDED.revision,publish_updates=messenger_counter_updates.publish_updates OR EXCLUDED.publish_updates,next_retry_at=NULL;
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_track_sparse_access_change AFTER DELETE ON messenger_user_message_bindings
    REFERENCING OLD TABLE AS old_bindings FOR EACH STATEMENT EXECUTE FUNCTION messenger_track_sparse_access_change();
CREATE FUNCTION messenger_reset_message_membership() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.active AND NEW.membership_generation=OLD.membership_generation THEN RETURN NULL; END IF;
    DELETE FROM messenger_user_message_bindings binding USING messenger_message_placements placement
    WHERE binding.project_id=NEW.project_id AND binding.user_uuid=NEW.user_uuid
      AND placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid AND placement.stream_uuid=NEW.stream_uuid;
    DELETE FROM messenger_user_message_unreads WHERE project_id=NEW.project_id AND user_uuid=NEW.user_uuid AND stream_uuid=NEW.stream_uuid;
    DELETE FROM messenger_user_message_stars WHERE project_id=NEW.project_id AND user_uuid=NEW.user_uuid AND stream_uuid=NEW.stream_uuid;
    DELETE FROM messenger_user_message_pins WHERE project_id=NEW.project_id AND user_uuid=NEW.user_uuid AND stream_uuid=NEW.stream_uuid;
    DELETE FROM messenger_user_message_mentions WHERE project_id=NEW.project_id AND user_uuid=NEW.user_uuid AND stream_uuid=NEW.stream_uuid;
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_reset_message_membership AFTER UPDATE OF membership_generation,active ON messenger_stream_bindings
    FOR EACH ROW EXECUTE FUNCTION messenger_reset_message_membership();
"""

LAST_MESSAGES = """
CREATE FUNCTION messenger_set_last_message(p_project uuid,p_stream uuid,p_topic uuid,p_placement uuid,p_created timestamp)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM messenger_message_placements placement
        JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid AND message.deleted_at IS NULL
        JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid AND topic.deleted_at IS NULL
        JOIN messenger_streams stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid AND stream.deleted_at IS NULL
        WHERE placement.project_id=p_project AND placement.uuid=p_placement
    ) THEN RETURN; END IF;
    INSERT INTO messenger_stream_last_messages VALUES(p_project,p_stream,p_placement,p_created)
    ON CONFLICT(project_id,stream_uuid) DO UPDATE SET placement_uuid=EXCLUDED.placement_uuid,created_at=EXCLUDED.created_at
    WHERE (EXCLUDED.created_at,EXCLUDED.placement_uuid)>(messenger_stream_last_messages.created_at,messenger_stream_last_messages.placement_uuid);
    INSERT INTO messenger_topic_last_messages VALUES(p_project,p_topic,p_placement,p_created)
    ON CONFLICT(project_id,topic_uuid) DO UPDATE SET placement_uuid=EXCLUDED.placement_uuid,created_at=EXCLUDED.created_at
    WHERE (EXCLUDED.created_at,EXCLUDED.placement_uuid)>(messenger_topic_last_messages.created_at,messenger_topic_last_messages.placement_uuid);
END $$;
CREATE FUNCTION messenger_repair_last_message(p_project uuid,p_stream uuid,p_topic uuid,p_placement uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE previous record;
BEGIN
    -- Serialize the two shared pointers in the same order as UPSERT. Lock
    -- even for a non-last deletion: it may become last during this wait.
    PERFORM 1 FROM messenger_stream_last_messages WHERE project_id=p_project AND stream_uuid=p_stream FOR UPDATE;
    PERFORM 1 FROM messenger_topic_last_messages WHERE project_id=p_project AND topic_uuid=p_topic FOR UPDATE;
    IF EXISTS(SELECT 1 FROM messenger_stream_last_messages WHERE project_id=p_project AND stream_uuid=p_stream AND placement_uuid=p_placement) THEN
        SELECT placement.uuid,message.created_at INTO previous FROM messenger_message_placements placement
        JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid AND message.deleted_at IS NULL
        JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid AND topic.deleted_at IS NULL
        JOIN messenger_streams stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid AND stream.deleted_at IS NULL
        WHERE placement.project_id=p_project AND placement.stream_uuid=p_stream AND placement.uuid<>p_placement
        ORDER BY message.created_at DESC,placement.uuid DESC LIMIT 1;
        IF FOUND THEN
            UPDATE messenger_stream_last_messages SET placement_uuid=previous.uuid,created_at=previous.created_at
            WHERE project_id=p_project AND stream_uuid=p_stream AND placement_uuid=p_placement;
        ELSE DELETE FROM messenger_stream_last_messages WHERE project_id=p_project AND stream_uuid=p_stream AND placement_uuid=p_placement;
        END IF;
    END IF;    IF EXISTS(SELECT 1 FROM messenger_topic_last_messages WHERE project_id=p_project AND topic_uuid=p_topic AND placement_uuid=p_placement) THEN
        SELECT placement.uuid,message.created_at INTO previous FROM messenger_message_placements placement
        JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid AND message.deleted_at IS NULL
        JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid AND topic.deleted_at IS NULL
        JOIN messenger_streams stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid AND stream.deleted_at IS NULL
        WHERE placement.project_id=p_project AND placement.topic_uuid=p_topic AND placement.uuid<>p_placement
        ORDER BY message.created_at DESC,placement.uuid DESC LIMIT 1;
        IF FOUND THEN
            UPDATE messenger_topic_last_messages SET placement_uuid=previous.uuid,created_at=previous.created_at
            WHERE project_id=p_project AND topic_uuid=p_topic AND placement_uuid=p_placement;
        ELSE DELETE FROM messenger_topic_last_messages WHERE project_id=p_project AND topic_uuid=p_topic AND placement_uuid=p_placement;
        END IF;
    END IF;

END $$;
CREATE FUNCTION messenger_track_shared_last_message() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE message record;
BEGIN
    IF TG_OP<>'INSERT' THEN
        PERFORM messenger_repair_last_message(OLD.project_id,OLD.stream_uuid,OLD.topic_uuid,OLD.uuid);
    END IF;
    IF TG_OP<>'DELETE' THEN
        SELECT created_at INTO message FROM messenger_messages WHERE project_id=NEW.project_id AND uuid=NEW.message_uuid AND deleted_at IS NULL;
        IF FOUND THEN PERFORM messenger_set_last_message(NEW.project_id,NEW.stream_uuid,NEW.topic_uuid,NEW.uuid,message.created_at); END IF;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_track_shared_last_message AFTER INSERT OR DELETE OR UPDATE OF stream_uuid,topic_uuid,message_uuid ON messenger_message_placements
    FOR EACH ROW EXECUTE FUNCTION messenger_track_shared_last_message();
CREATE FUNCTION messenger_track_last_message_root() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE placement record;
BEGIN
    FOR placement IN SELECT * FROM messenger_message_placements WHERE project_id=NEW.project_id
      AND ((TG_TABLE_NAME='messenger_messages' AND message_uuid=NEW.uuid) OR (TG_TABLE_NAME='messenger_topics' AND topic_uuid=NEW.uuid) OR (TG_TABLE_NAME='messenger_streams' AND stream_uuid=NEW.uuid)) LOOP
        PERFORM messenger_repair_last_message(placement.project_id,placement.stream_uuid,placement.topic_uuid,placement.uuid);
        IF NEW.deleted_at IS NULL THEN
            PERFORM messenger_set_last_message(placement.project_id,placement.stream_uuid,placement.topic_uuid,placement.uuid,
                (SELECT created_at FROM messenger_messages WHERE project_id=placement.project_id AND uuid=placement.message_uuid));
        END IF;
        INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid)
        SELECT placement.project_id,binding.user_uuid,placement.stream_uuid,placement.topic_uuid FROM messenger_user_message_bindings binding
        WHERE binding.project_id=placement.project_id AND binding.placement_uuid=placement.uuid
        ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE SET revision=EXCLUDED.revision,publish_updates=messenger_counter_updates.publish_updates OR EXCLUDED.publish_updates,next_retry_at=NULL;
    END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_track_last_message_root AFTER UPDATE OF created_at,deleted_at ON messenger_messages
    FOR EACH ROW WHEN ((OLD.created_at,OLD.deleted_at) IS DISTINCT FROM (NEW.created_at,NEW.deleted_at)) EXECUTE FUNCTION messenger_track_last_message_root();
CREATE TRIGGER messenger_track_last_message_stream AFTER UPDATE OF deleted_at ON messenger_streams
    FOR EACH ROW WHEN (OLD.deleted_at IS DISTINCT FROM NEW.deleted_at) EXECUTE FUNCTION messenger_track_last_message_root();
CREATE TRIGGER messenger_track_last_message_topic AFTER UPDATE OF deleted_at ON messenger_topics
    FOR EACH ROW WHEN (OLD.deleted_at IS DISTINCT FROM NEW.deleted_at) EXECUTE FUNCTION messenger_track_last_message_root();
"""


BRIDGE = """
CREATE VIEW bridge_zulip_v5_flag_targets AS
SELECT binding.project_id,binding.uuid AS state_uuid,binding.user_uuid,binding.placement_uuid,
       placement.message_uuid,placement.stream_uuid,placement.topic_uuid,
       account.uuid AS external_account_uuid,account.external_account_id,
       COALESCE(observed.message_id,delivery.zulip_message_id) AS message_id
FROM messenger_user_message_bindings binding
JOIN messenger_message_placements placement ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid
JOIN messenger_messages canonical ON canonical.project_id=placement.project_id AND canonical.uuid=placement.message_uuid AND canonical.deleted_at IS NULL
JOIN messenger_stream_bindings membership ON membership.project_id=binding.project_id AND membership.user_uuid=binding.user_uuid
    AND membership.stream_uuid=placement.stream_uuid AND membership.active
JOIN m_external_accounts_v2 account ON account.project_id=binding.project_id AND account.owner_user_uuid=binding.user_uuid
    AND account.provider='zulip' AND NOT account.disabled AND account.external_account_id IS NOT NULL
JOIN bridge_zulip_v5_channels channel ON channel.external_account_uuid=account.uuid AND channel.stream_uuid=placement.stream_uuid
LEFT JOIN messenger_providers provider ON provider.uuid=account.provider_uuid
LEFT JOIN bridge_zulip_v5_messages observed ON observed.external_account_uuid=account.uuid AND observed.project_id=binding.project_id
    AND messenger_uuid_v5(observed.project_id,observed.message_uuid::text)=placement.message_uuid
LEFT JOIN bridge_zulip_v5_deliveries delivery ON delivery.project_id=binding.project_id AND delivery.message_uuid=placement.message_uuid
    AND delivery.stream_uuid=placement.stream_uuid AND delivery.hostname=provider.hostname
WHERE COALESCE(observed.message_id,delivery.zulip_message_id) IS NOT NULL;
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_remote_flags() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('workspace.batch_flags',true)='on' THEN RETURN NULL; END IF;
    INSERT INTO bridge_zulip_v5_flag_changes(project_id,state_uuid)
    SELECT DISTINCT target.project_id,target.state_uuid FROM bridge_zulip_v5_flag_targets target
    WHERE target.external_account_uuid=NEW.external_account_uuid AND (NEW.message_id=0 OR target.message_id=NEW.message_id)
    ON CONFLICT(project_id,state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
    RETURN NULL;
END $$;
DROP FUNCTION bridge_zulip_v5_track_local_flags() CASCADE;
CREATE FUNCTION messenger_capture_sparse_flags() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    fact record;
    binding record;
    actor uuid := nullif(current_setting('workspace.outgoing_actor',true),'')::uuid;
    data_ jsonb;
BEGIN
    IF current_setting('workspace.batch_flags',true)='on' THEN RETURN NULL; END IF;
    IF TG_OP='DELETE' THEN fact:=OLD; ELSE fact:=NEW; END IF;
    SELECT * INTO binding FROM messenger_user_message_bindings
    WHERE project_id=fact.project_id AND user_uuid=fact.user_uuid AND placement_uuid=fact.placement_uuid;
    IF NOT FOUND THEN RETURN NULL; END IF;
    INSERT INTO bridge_zulip_v5_flag_changes(project_id,state_uuid)
    SELECT binding.project_id,binding.uuid FROM messenger_streams
    WHERE project_id=fact.project_id AND uuid=fact.stream_uuid AND source_name='zulip'
    ON CONFLICT(project_id,state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
    IF actor IS NULL OR actor<>fact.user_uuid OR pg_trigger_depth()>1 THEN RETURN NULL; END IF;
    SELECT jsonb_build_object('project_id',fact.project_id,'uuid',binding.uuid,'user_uuid',fact.user_uuid,
        'placement_uuid',fact.placement_uuid,'message_uuid',message.uuid,'stream_uuid',fact.stream_uuid,'topic_uuid',fact.topic_uuid,
        'source_name',message.source_name,'provider_uuid',message.provider_uuid,'actor_uuid',actor,
        'account_identity',account.external_account_id,'account_provider_uuid',account.provider_uuid,
        'remote_message_identity',message.provider_identity,'stream_name',stream.name,'topic_name',topic.name,
        'read_at',CASE WHEN NOT EXISTS(SELECT 1 FROM messenger_user_message_unreads f WHERE
            (f.project_id,f.user_uuid,f.placement_uuid)=(fact.project_id,fact.user_uuid,fact.placement_uuid)) THEN NOW() END,
        'starred',EXISTS(SELECT 1 FROM messenger_user_message_stars f WHERE
            (f.project_id,f.user_uuid,f.placement_uuid)=(fact.project_id,fact.user_uuid,fact.placement_uuid)),
        'flags',CASE WHEN TG_TABLE_NAME='messenger_user_message_unreads' THEN
            jsonb_build_object('read',TG_OP='DELETE') ELSE jsonb_build_object('starred',TG_OP='INSERT') END,
        'before',jsonb_build_object('read_at',CASE WHEN TG_TABLE_NAME='messenger_user_message_unreads' AND TG_OP='INSERT' THEN NOW() END,
            'starred',TG_TABLE_NAME='messenger_user_message_stars' AND TG_OP='DELETE'),
        'external_account_uuid',account.uuid)
    INTO data_
    FROM messenger_message_placements placement JOIN messenger_messages message
      ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
    JOIN messenger_streams stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
    JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid
    JOIN m_external_accounts_v2 account ON account.project_id=fact.project_id AND account.owner_user_uuid=actor
        AND account.provider='zulip' AND NOT account.disabled AND account.status<>'new'
        AND account.settings->>'api_key' IS NOT NULL AND account.provider_uuid=stream.provider_uuid
    WHERE placement.project_id=fact.project_id AND placement.uuid=fact.placement_uuid
        AND stream.source_name='zulip' LIMIT 1;
    IF data_ IS NOT NULL THEN
        INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
        VALUES(fact.project_id,'message_state',binding.uuid,'message_state:'||binding.uuid::text,
            (data_->>'external_account_uuid')::uuid,'update',data_);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_capture_sparse_flags AFTER INSERT OR DELETE ON messenger_user_message_unreads
    FOR EACH ROW EXECUTE FUNCTION messenger_capture_sparse_flags();
CREATE TRIGGER messenger_capture_sparse_flags AFTER INSERT OR DELETE ON messenger_user_message_stars
    FOR EACH ROW EXECUTE FUNCTION messenger_capture_sparse_flags();
"""

PREFERENCES = """
CREATE FUNCTION messenger_queue_root_counter_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME='messenger_topics' THEN
        INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid)
        SELECT OLD.project_id,personal.user_uuid,OLD.stream_uuid,OLD.uuid FROM messenger_user_topic_bindings personal
        WHERE personal.project_id=OLD.project_id AND personal.topic_uuid=OLD.uuid
        ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE SET revision=EXCLUDED.revision,publish_updates=messenger_counter_updates.publish_updates OR EXCLUDED.publish_updates,next_retry_at=NULL;
    ELSE
        INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid)
        SELECT OLD.project_id,personal.user_uuid,OLD.uuid,topic.uuid
        FROM messenger_topics topic JOIN messenger_user_topic_bindings personal
          ON personal.project_id=topic.project_id AND personal.topic_uuid=topic.uuid
        WHERE topic.project_id=OLD.project_id AND topic.stream_uuid=OLD.uuid
        ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE SET revision=EXCLUDED.revision,publish_updates=messenger_counter_updates.publish_updates OR EXCLUDED.publish_updates,next_retry_at=NULL;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER messenger_queue_topic_delete BEFORE DELETE ON messenger_topics
    FOR EACH ROW EXECUTE FUNCTION messenger_queue_root_counter_change();
CREATE TRIGGER messenger_queue_stream_delete BEFORE DELETE ON messenger_streams
    FOR EACH ROW EXECUTE FUNCTION messenger_queue_root_counter_change();
CREATE TRIGGER messenger_queue_stream_archive AFTER UPDATE OF is_archived ON messenger_streams
    FOR EACH ROW WHEN(OLD.is_archived IS DISTINCT FROM NEW.is_archived) EXECUTE FUNCTION messenger_queue_root_counter_change();
CREATE FUNCTION messenger_queue_preference_counters() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME='messenger_stream_bindings' THEN
        INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid)
        SELECT NEW.project_id,NEW.user_uuid,NEW.stream_uuid,topic.uuid FROM messenger_topics topic
        WHERE topic.project_id=NEW.project_id AND topic.stream_uuid=NEW.stream_uuid
        ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE SET revision=EXCLUDED.revision,publish_updates=messenger_counter_updates.publish_updates OR EXCLUDED.publish_updates,next_retry_at=NULL;
    ELSE
        PERFORM messenger_queue_counters(NEW.project_id,NEW.user_uuid,
            (SELECT stream_uuid FROM messenger_topics WHERE project_id=NEW.project_id AND uuid=NEW.topic_uuid),NEW.topic_uuid);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_queue_preference_counters AFTER UPDATE OF active,notification_mode ON messenger_stream_bindings
    FOR EACH ROW WHEN ((OLD.active,OLD.notification_mode) IS DISTINCT FROM (NEW.active,NEW.notification_mode))
    EXECUTE FUNCTION messenger_queue_preference_counters();
CREATE TRIGGER messenger_queue_preference_counters AFTER INSERT OR UPDATE OF notification_mode ON messenger_user_topic_bindings
    FOR EACH ROW EXECUTE FUNCTION messenger_queue_preference_counters();
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0284-Authorize-files-through-Workspace-membership-59b5ff.py"]

    @property
    def migration_id(self):
        return "71c38932-41fa-4cc3-9b89-97965f1840e1"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(SCHEMA)
        for suffix, predicate in FLAGS.items():
            session.execute(f"""
                CREATE TABLE messenger_user_message_{suffix} (
                    project_id uuid NOT NULL,user_uuid uuid NOT NULL,placement_uuid uuid NOT NULL,
                    stream_uuid uuid NOT NULL,topic_uuid uuid NOT NULL,
                    PRIMARY KEY(project_id,user_uuid,placement_uuid),
                    FOREIGN KEY(project_id,user_uuid) REFERENCES messenger_project_users(project_id,user_uuid) ON DELETE CASCADE,
                    FOREIGN KEY(project_id,placement_uuid,stream_uuid,topic_uuid)
                        REFERENCES messenger_message_placements(project_id,uuid,stream_uuid,topic_uuid)
                        ON DELETE CASCADE ON UPDATE CASCADE
                );
                CREATE INDEX messenger_message_{suffix}_topic ON messenger_user_message_{suffix}(project_id,user_uuid,topic_uuid,placement_uuid);
                CREATE INDEX messenger_message_{suffix}_placement ON messenger_user_message_{suffix}(project_id,placement_uuid,user_uuid);
                INSERT INTO messenger_user_message_{suffix}
                SELECT state.project_id,state.user_uuid,state.placement_uuid,placement.stream_uuid,placement.topic_uuid
                FROM messenger_user_message_states state
                JOIN messenger_message_placements placement ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
                JOIN messenger_stream_bindings membership ON membership.project_id=state.project_id AND membership.user_uuid=state.user_uuid
                    AND membership.stream_uuid=placement.stream_uuid AND membership.membership_generation=state.membership_generation
                WHERE {predicate};
            """)
        for scope in ("stream", "topic"):
            session.execute(f"""
                CREATE TABLE messenger_{scope}_last_messages (
                    project_id uuid NOT NULL,{scope}_uuid uuid NOT NULL,placement_uuid uuid NOT NULL,created_at timestamp NOT NULL,
                    PRIMARY KEY(project_id,{scope}_uuid),
                    FOREIGN KEY(project_id,{scope}_uuid) REFERENCES messenger_{"streams" if scope == "stream" else "topics"}(project_id,uuid) ON DELETE CASCADE
                );
                INSERT INTO messenger_{scope}_last_messages
                SELECT DISTINCT ON (placement.project_id,placement.{scope}_uuid)
                    placement.project_id,placement.{scope}_uuid,placement.uuid,message.created_at
                FROM messenger_message_placements placement
                JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid AND message.deleted_at IS NULL
                JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid AND topic.deleted_at IS NULL
                ORDER BY placement.project_id,placement.{scope}_uuid,message.created_at DESC,placement.uuid DESC;
            """)
            view = (
                f"messenger_api_user_{'streams' if scope == 'stream' else 'topics'}_v1"
            )
            definition = session.execute(
                "SELECT pg_get_viewdef(%s::regclass) AS definition", (view,)
            ).fetchone()["definition"]
            definition = definition.replace(
                "binding.last_message_uuid",
                f"(SELECT placement_uuid FROM messenger_{scope}_last_messages last_message WHERE last_message.project_id={scope}.project_id AND last_message.{scope}_uuid={scope}.uuid) AS last_message_uuid",
            )
            session.execute(f"CREATE OR REPLACE VIEW {view} AS {definition}")
        session.execute(MESSAGE_VIEW)
        definition = session.execute(
            "SELECT pg_get_viewdef('messenger_api_message_reactions_v1'::regclass) AS definition"
        ).fetchone()["definition"]
        definition = definition.replace(
            " AND (membership.membership_generation = binding.membership_generation)",
            "",
        )
        definition = definition.replace(
            "WHERE ((binding.visibility)::text = 'visible'::text)", ""
        )
        session.execute(
            f"CREATE OR REPLACE VIEW messenger_api_message_reactions_v1 AS {definition}"
        )
        # Keep pending provider flag work, replacing the retired state identity
        # with the identity of the corresponding visibility binding.
        session.execute("""
            ALTER TABLE bridge_zulip_v5_flag_changes DROP CONSTRAINT bridge_zulip_v5_flag_changes_project_id_state_uuid_fkey;
            DROP TRIGGER newest_message_first ON bridge_zulip_v5_flag_changes;
            CREATE TEMP TABLE pending_sparse_flags ON COMMIT DROP AS
            SELECT change.project_id,binding.uuid AS state_uuid,max(change.revision) AS revision,
                   max(change.message_created_at) AS message_created_at,max(change.message_sequence) AS message_sequence
            FROM bridge_zulip_v5_flag_changes change
            JOIN messenger_user_message_states state ON state.project_id=change.project_id AND state.uuid=change.state_uuid
            JOIN messenger_user_message_bindings binding ON binding.project_id=state.project_id
                AND binding.user_uuid=state.user_uuid AND binding.placement_uuid=state.placement_uuid
            GROUP BY change.project_id,binding.uuid;
            UPDATE zulip_outgoing_intents intent SET entity_uuid=binding.uuid,
                ordering_key='message_state:'||binding.uuid::text,
                data=jsonb_set(intent.data,'{uuid}',to_jsonb(binding.uuid::text))
            FROM messenger_user_message_states state JOIN messenger_user_message_bindings binding
              ON binding.project_id=state.project_id AND binding.user_uuid=state.user_uuid AND binding.placement_uuid=state.placement_uuid
            WHERE intent.project_id=state.project_id AND intent.entity_type='message_state' AND intent.entity_uuid=state.uuid;
            DELETE FROM bridge_zulip_v5_flag_changes;
            INSERT INTO bridge_zulip_v5_flag_changes SELECT * FROM pending_sparse_flags;
        """)
        definition = session.execute(
            "SELECT pg_get_functiondef('bridge_zulip_v5_order_message_change()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        session.execute(
            definition.replace(
                "messenger_user_message_states state",
                "messenger_user_message_bindings state",
            )
        )
        session.execute(
            "CREATE TRIGGER newest_message_first BEFORE INSERT OR UPDATE ON bridge_zulip_v5_flag_changes FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_order_message_change()"
        )
        session.execute("""
            DROP VIEW bridge_zulip_v5_user_message_states;
            
            DROP VIEW bridge_zulip_v5_flag_targets;
            DROP VIEW messenger_topic_counter_totals;
            DROP VIEW messenger_current_message_counter_contributions;
            DROP FUNCTION messenger_apply_message_counter_changes(uuid,uuid,uuid,integer);
            DROP FUNCTION messenger_track_counter_preferences() CASCADE;
            DROP FUNCTION messenger_track_root_counter_changes() CASCADE;
            DROP FUNCTION messenger_track_placement_counter_changes() CASCADE;
            DROP FUNCTION messenger_track_viewer_counter_changes() CASCADE;
            DROP FUNCTION messenger_mark_counter_message(uuid,uuid,uuid,uuid,uuid);
            DROP FUNCTION messenger_mark_counter_scope(uuid,uuid,uuid,uuid);
            DROP TABLE messenger_message_counter_changes,messenger_message_counter_totals,messenger_message_counter_contributions;
            DROP TABLE messenger_mutation_projection_state;
            DROP FUNCTION messenger_set_personal_preview_scope() CASCADE;
            DROP FUNCTION messenger_move_personal_preview_scope() CASCADE;
            DROP FUNCTION messenger_date_personal_preview_scope() CASCADE;
            DELETE FROM messenger_user_message_bindings binding USING messenger_message_placements placement,messenger_stream_bindings membership
            WHERE placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid
              AND membership.project_id=binding.project_id AND membership.user_uuid=binding.user_uuid AND membership.stream_uuid=placement.stream_uuid
              AND (binding.visibility<>'visible' OR NOT membership.active OR binding.membership_generation<>membership.membership_generation);
            ALTER TABLE messenger_user_message_bindings DROP COLUMN membership_generation,DROP COLUMN relation_role,DROP COLUMN visibility,DROP COLUMN permissions;
            DROP TABLE messenger_user_message_states CASCADE;
            DROP FUNCTION messenger_stamp_user_message_flags();
            DROP FUNCTION messenger_v2_move_canonical_message_project(uuid,uuid,uuid,uuid,uuid);
            DROP FUNCTION messenger_v2_move_canonical_stream_project(uuid,uuid,uuid);
            ALTER TABLE messenger_stream_bindings DROP COLUMN last_message_uuid;
            DROP TRIGGER messenger_refresh_summary_after_preview_change ON messenger_user_topic_bindings;
            ALTER TABLE messenger_user_topic_bindings DROP COLUMN last_message_uuid;
        """)
        session.execute(
            "DELETE FROM bridge_zulip_v5_flag_changes change WHERE NOT EXISTS(SELECT 1 FROM messenger_user_message_bindings binding WHERE binding.project_id=change.project_id AND binding.uuid=change.state_uuid)"
        )
        session.execute(
            "ALTER TABLE bridge_zulip_v5_flag_changes ADD FOREIGN KEY(project_id,state_uuid) REFERENCES messenger_user_message_bindings(project_id,uuid) ON DELETE CASCADE"
        )
        session.execute(BRIDGE)
        session.execute(FACTS)
        session.execute(PREFERENCES)
        definition = session.execute(
            "SELECT pg_get_functiondef('messenger_assign_provider_origin()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        start = definition.index(
            "    ELSIF TG_TABLE_NAME='messenger_user_message_states'"
        )
        end = definition.index("    ELSE", start)
        session.execute(definition[:start] + definition[end:])
        definition = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        start = definition.index("    ELSIF kind_='message_state'")
        end = definition.index("    ELSIF kind_='stream_binding'", start)
        session.execute(definition[:start] + definition[end:])
        session.execute(LAST_MESSAGES)
        for suffix in ("unreads", "mentions"):
            for operation, transition in (
                ("INSERT", "NEW TABLE AS new_flags"),
                ("DELETE", "OLD TABLE AS old_flags"),
                ("UPDATE", "OLD TABLE AS old_flags NEW TABLE AS new_flags"),
            ):
                session.execute(f"""CREATE TRIGGER messenger_track_sparse_counter_{operation.lower()}
                    AFTER {operation} ON messenger_user_message_{suffix}
                    REFERENCING {transition} FOR EACH STATEMENT
                    EXECUTE FUNCTION messenger_track_sparse_counter_change()""")
        session.execute("""
            INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid)
            SELECT personal.project_id,personal.user_uuid,topic.stream_uuid,topic.uuid
            FROM messenger_user_topic_bindings personal JOIN messenger_topics topic
                ON topic.project_id=personal.project_id AND topic.uuid=personal.topic_uuid;
        """)

    def downgrade(self, session):
        raise RuntimeError(
            "Sparse message flags require a database backup to restore the previous schema"
        )


migration_step = MigrationStep()
