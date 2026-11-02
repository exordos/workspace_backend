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

import re

from restalchemy.storage.sql import migrations


DELETION_SQL = """
-- These locks fence an import page against deletion, including an unknown
-- provider message. Lock before touching the root, always in UUID order.
CREATE FUNCTION messenger_lock_message(p_project uuid,p_message uuid)
RETURNS void LANGUAGE sql AS $$
    SELECT pg_advisory_xact_lock(hashtextextended(p_project::text||':'||p_message::text,294))
$$;
CREATE FUNCTION messenger_delete_message(p_project uuid,p_message uuid)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM messenger_lock_message(p_project,p_message);
    DELETE FROM messenger_messages WHERE project_id=p_project AND uuid=p_message;
END $$;

CREATE FUNCTION messenger_reject_deleted_message() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM messenger_message_delete_tombstones
        WHERE project_id=NEW.project_id AND canonical_message_uuid=NEW.uuid) THEN
        RAISE EXCEPTION 'Deleted message UUID cannot be reused' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER messenger_reject_deleted_message BEFORE INSERT ON messenger_messages
    FOR EACH ROW EXECUTE FUNCTION messenger_reject_deleted_message();

CREATE FUNCTION messenger_delete_audience(p_project uuid,p_stream uuid,p_placement uuid DEFAULT NULL,p_after uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE sql AS $$
    SELECT jsonb_build_object(
        'recipients',coalesce(jsonb_agg(member.user_uuid ORDER BY member.user_uuid),'[]'),
        'membership_generations',coalesce(jsonb_object_agg(member.user_uuid,member.membership_generation),'{}'))
    FROM messenger_stream_bindings member
    WHERE member.project_id=p_project AND member.stream_uuid=p_stream AND member.active
      AND (p_after IS NULL OR member.user_uuid>p_after)
      AND (p_placement IS NULL OR EXISTS (
          SELECT 1 FROM messenger_user_message_bindings access
          WHERE access.project_id=p_project AND access.user_uuid=member.user_uuid
            AND access.placement_uuid=p_placement))
$$;

CREATE FUNCTION messenger_snapshot_delete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    placement record;
    payload jsonb;
    stream_ uuid;
    kind_ text := TG_ARGV[0];
BEGIN
    IF kind_='message' THEN
        INSERT INTO messenger_message_delete_tombstones(project_id,canonical_message_uuid,ingest_sequence)
        VALUES(OLD.project_id,OLD.uuid,OLD.ingest_sequence) ON CONFLICT DO NOTHING;
        -- Parent deletion already delivers its own control event. The compact
        -- marker still prevents a stale import from recreating the child.
        IF pg_trigger_depth()>1 OR current_setting('workspace.delete_cutover',true)='on' THEN RETURN OLD; END IF;
        FOR placement IN SELECT uuid,stream_uuid,topic_uuid FROM messenger_message_placements
            WHERE project_id=OLD.project_id AND message_uuid=OLD.uuid ORDER BY uuid
        LOOP
            -- Pointer locks precede dirty counter rows, matching placement
            -- cleanup. Read recipients still need summary/cache invalidation.
            PERFORM messenger_repair_last_message(OLD.project_id,placement.stream_uuid,placement.topic_uuid,placement.uuid);
            PERFORM messenger_queue_counters(OLD.project_id,access.user_uuid,placement.stream_uuid,placement.topic_uuid)
            FROM messenger_user_message_bindings access
            WHERE access.project_id=OLD.project_id AND access.placement_uuid=placement.uuid
            ORDER BY access.user_uuid;
            payload := jsonb_build_object('source_kind','message.deleted' ,'placement',to_jsonb(placement),
                'canonical_message_uuid',OLD.uuid,'message_created_at',OLD.created_at,'author_uuid',OLD.author_uuid,
                'source_name',OLD.source_name,'provider_uuid',OLD.provider_uuid,'provider_identity',OLD.provider_identity)
                || messenger_delete_audience(OLD.project_id,placement.stream_uuid,placement.uuid);
            INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
            VALUES(gen_random_uuid(),OLD.project_id,'delivery_snapshot_event','message',OLD.project_id::text||':'||OLD.uuid::text,payload);
        END LOOP;
    ELSE
        stream_ := CASE WHEN kind_='stream' THEN OLD.uuid ELSE (to_jsonb(OLD)->>'stream_uuid')::uuid END;
        IF pg_trigger_depth()=1 AND current_setting('workspace.delete_cutover',true) IS DISTINCT FROM 'on' THEN
            payload := jsonb_build_object('source_kind',kind_||'.deleted','stream_uuid',stream_,
                'source_name',OLD.source_name,'provider_uuid',OLD.provider_uuid,'provider_identity',OLD.provider_identity)
                || messenger_delete_audience(OLD.project_id,stream_);
            IF kind_='topic' THEN payload:=payload||jsonb_build_object('topic_uuid',OLD.uuid); END IF;
            INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
            VALUES(gen_random_uuid(),OLD.project_id,'delivery_snapshot_event',kind_,OLD.project_id::text||':'||OLD.uuid::text,payload);
        END IF;
        IF kind_='stream' AND pg_trigger_depth()=1 THEN
            -- Custom links disappear in the cascade. Queue their final folder
            -- snapshots now, including streams with no remaining topic scopes.
            WITH audience AS (
                SELECT user_uuid FROM messenger_stream_bindings
                WHERE project_id=OLD.project_id AND stream_uuid=OLD.uuid AND active
            ), folders AS (
                SELECT user_uuid,unnest(ARRAY['00000000-0000-0000-0000-000000000000'::uuid,
                    CASE WHEN OLD.private THEN '00000000-0000-0000-0000-000000000001'::uuid
                         ELSE '00000000-0000-0000-0000-000000000002'::uuid END]) AS folder_uuid
                FROM audience
                UNION
                SELECT item.user_uuid,item.folder_uuid FROM messenger_folder_items item
                JOIN audience ON audience.user_uuid=item.user_uuid
                WHERE item.project_id=OLD.project_id AND item.stream_uuid=OLD.uuid
            )
            INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
            SELECT gen_random_uuid(),OLD.project_id,'folder_projection','user-folder',
                OLD.project_id::text||':'||user_uuid::text||':'||folder_uuid::text,
                jsonb_build_object('source_kind','stream.deleted','user_uuid',user_uuid,
                    'stream_uuid',OLD.uuid,'folder_uuid',folder_uuid)
            FROM folders ORDER BY user_uuid,folder_uuid;
        END IF;
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER messenger_snapshot_delete BEFORE DELETE ON messenger_messages
    FOR EACH ROW EXECUTE FUNCTION messenger_snapshot_delete('message');
CREATE TRIGGER messenger_snapshot_delete BEFORE DELETE ON messenger_topics
    FOR EACH ROW EXECUTE FUNCTION messenger_snapshot_delete('topic');
CREATE TRIGGER messenger_snapshot_delete BEFORE DELETE ON messenger_streams
    FOR EACH ROW EXECUTE FUNCTION messenger_snapshot_delete('stream');

-- Clean only the canonical root affected by a placement cascade, after its
-- parent scope is gone. Deleting a child in a parent BEFORE trigger would
-- update that same parent through its summary/default-pointer foreign keys.
CREATE FUNCTION messenger_delete_scope_orphan() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM messenger_topics WHERE project_id=OLD.project_id AND uuid=OLD.topic_uuid)
       AND EXISTS(SELECT 1 FROM messenger_streams WHERE project_id=OLD.project_id AND uuid=OLD.stream_uuid) THEN
        RETURN NULL;
    END IF;
    PERFORM 1 FROM messenger_messages WHERE project_id=OLD.project_id AND uuid=OLD.message_uuid FOR UPDATE;
    DELETE FROM messenger_messages WHERE project_id=OLD.project_id AND uuid=OLD.message_uuid
      AND NOT EXISTS(SELECT 1 FROM messenger_message_placements
                     WHERE project_id=OLD.project_id AND message_uuid=OLD.message_uuid);
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_delete_scope_orphan AFTER DELETE ON messenger_message_placements
    FOR EACH ROW EXECUTE FUNCTION messenger_delete_scope_orphan();
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0293-Bound-topic-summary-boundary-lookups-cebf12.py"]

    @property
    def migration_id(self):
        return "77d94ba6-e4ab-433e-b040-f24b82a6fbc9"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('workspace-read-state-schema-v1',0))"
        )
        session.execute(DELETION_SQL)
        # Upgrade queued legacy deletes before removing their audience rows.
        # Preserve the keyset cursor: already delivered recipients stay done.
        session.execute("""
            UPDATE messenger_projection_tasks task SET payload=task.payload ||
                messenger_delete_audience(task.project_id,
                    coalesce((task.payload->>'stream_uuid')::uuid,(task.payload#>>'{placement,stream_uuid}')::uuid),
                    (task.payload#>>'{placement,uuid}')::uuid,task.progress_uuid),
                processed_count=0,progress_uuid=NULL
            WHERE task.task_kind='delivery_snapshot_event'
              AND task.payload->>'source_kind' IN ('message.deleted','topic.deleted','stream.deleted')
              AND NOT task.payload ? 'recipients' AND task.status<>'completed';
            UPDATE messenger_domain_outbox_events event SET payload=task.payload
            FROM messenger_projection_tasks task
            WHERE task.outbox_event_uuid=event.uuid AND task.project_id=event.project_id
              AND task.payload->>'source_kind' IN ('message.deleted','topic.deleted','stream.deleted');
            SELECT set_config('workspace.delete_cutover','on',true);
            DELETE FROM messenger_streams WHERE deleted_at IS NOT NULL;
            DELETE FROM messenger_topics WHERE deleted_at IS NOT NULL;
            DELETE FROM messenger_messages WHERE deleted_at IS NOT NULL;
            SELECT set_config('workspace.delete_cutover','off',true);
            DELETE FROM messenger_user_message_unreads unread WHERE NOT EXISTS (
                SELECT 1 FROM messenger_user_message_bindings access
                WHERE (access.project_id,access.user_uuid,access.placement_uuid)=
                      (unread.project_id,unread.user_uuid,unread.placement_uuid));
            ALTER TABLE messenger_user_message_unreads ADD CONSTRAINT messenger_unread_access_fk
                FOREIGN KEY(project_id,user_uuid,placement_uuid)
                REFERENCES messenger_user_message_bindings(project_id,user_uuid,placement_uuid)
                ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE;
        """)
        # Provider deletes must capture routing/content before FK cascades.
        definition = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        definition = definition.replace(
            "IF row_->'payload' IS NOT DISTINCT FROM old_->'payload' AND row_->'deleted_at' IS NOT DISTINCT FROM old_->'deleted_at' THEN RETURN NULL; END IF;",
            "IF TG_OP='UPDATE' AND row_->'payload' IS NOT DISTINCT FROM old_->'payload' THEN RETURN NULL; END IF;",
        )
        definition = definition.replace(
            "            IF row_->>'deleted_at' IS NOT NULL THEN operation_ := 'delete'; END IF;",
            "",
        )
        definition = definition.replace(
            "    IF row_->>'deleted_at' IS NOT NULL THEN operation_ := 'delete'; END IF;",
            "",
        )
        definition = definition.replace(",row_->'deleted_at'", "").replace(
            ",old_->'deleted_at'", ""
        )
        definition = definition.replace(
            "RETURN NULL;", "IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NULL;"
        )
        session.execute(definition)
        session.execute("""
            CREATE TRIGGER zulip_capture_message_delete BEFORE DELETE ON messenger_messages
                FOR EACH ROW EXECUTE FUNCTION zulip_capture_outgoing('message');
            DROP TRIGGER zulip_capture_outgoing ON messenger_streams;
            DROP TRIGGER zulip_capture_outgoing ON messenger_topics;
            CREATE TRIGGER zulip_capture_outgoing AFTER INSERT OR UPDATE ON messenger_streams
                FOR EACH ROW EXECUTE FUNCTION zulip_capture_outgoing('stream');
            CREATE TRIGGER zulip_capture_outgoing_delete BEFORE DELETE ON messenger_streams
                FOR EACH ROW EXECUTE FUNCTION zulip_capture_outgoing('stream');
            CREATE TRIGGER zulip_capture_outgoing AFTER INSERT OR UPDATE ON messenger_topics
                FOR EACH ROW EXECUTE FUNCTION zulip_capture_outgoing('topic');
            CREATE TRIGGER zulip_capture_outgoing_delete BEFORE DELETE ON messenger_topics
                FOR EACH ROW EXECUTE FUNCTION zulip_capture_outgoing('topic');
        """)

        # deleted_at remains a null compatibility field in public projections.
        # No working row or read query uses a soft-deletion state any longer.
        for table in ("messenger_messages", "messenger_topics", "messenger_streams"):
            session.execute(f"ALTER TABLE {table} ADD CHECK(deleted_at IS NULL)")
        for view in session.execute(
            "SELECT viewname,definition FROM pg_views WHERE schemaname='public' "
            "AND (viewname LIKE 'messenger_%' OR viewname LIKE 'bridge_zulip_v5_%' "
            "OR viewname='m_workspace_visible_files_v1') AND definition LIKE '%deleted_at IS NULL%'"
        ).fetchall():
            definition = re.sub(
                r"[a-z_]+\.deleted_at IS NULL", "true", view["definition"]
            )
            session.execute(
                f"CREATE OR REPLACE VIEW {view['viewname']} AS {definition}"
            )
        for name in (
            "messenger_set_last_message",
            "messenger_repair_last_message",
            "messenger_track_shared_last_message",
            "messenger_track_zulip_stream_topic_changes",
            "messenger_track_zulip_topic_changes",
        ):
            definition = session.execute(
                "SELECT pg_get_functiondef(oid) AS definition FROM pg_proc "
                "WHERE pronamespace='public'::regnamespace AND proname=%s",
                (name,),
            ).fetchone()["definition"]
            definition = definition.replace(", NEW.deleted_at", "").replace(
                ", OLD.deleted_at", ""
            )
            session.execute(
                re.sub(r" AND (?:[a-z_]+\.)?deleted_at IS NULL", "", definition)
            )
        session.execute("""
            DROP TRIGGER messenger_track_last_message_stream ON messenger_streams;
            DROP TRIGGER messenger_track_last_message_topic ON messenger_topics;
            DROP TRIGGER messenger_track_last_message_root ON messenger_messages;
            CREATE OR REPLACE FUNCTION messenger_track_last_message_root() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE placement record;
            BEGIN
                FOR placement IN SELECT * FROM messenger_message_placements
                    WHERE project_id=NEW.project_id AND message_uuid=NEW.uuid ORDER BY uuid
                LOOP
                    PERFORM messenger_repair_last_message(placement.project_id,placement.stream_uuid,placement.topic_uuid,placement.uuid);
                    PERFORM messenger_set_last_message(placement.project_id,placement.stream_uuid,placement.topic_uuid,placement.uuid,NEW.created_at);
                END LOOP;
                RETURN NULL;
            END $$;
            CREATE TRIGGER messenger_track_last_message_root AFTER UPDATE OF created_at ON messenger_messages
                FOR EACH ROW WHEN(OLD.created_at IS DISTINCT FROM NEW.created_at)
                EXECUTE FUNCTION messenger_track_last_message_root();
            DROP TRIGGER messenger_refresh_summary_after_content_change ON messenger_messages;
            CREATE TRIGGER messenger_refresh_summary_after_content_change AFTER UPDATE OF content_uuid ON messenger_messages
                FOR EACH ROW WHEN(OLD.content_uuid IS DISTINCT FROM NEW.content_uuid)
                EXECUTE FUNCTION messenger_refresh_summary_after_content_change();
        """)
        definition = session.execute(
            "SELECT pg_get_functiondef('messenger_refresh_summary_after_content_change()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        session.execute(
            definition.replace(
                "OLD.deleted_at IS NOT DISTINCT FROM NEW.deleted_at AND (", "("
            )
        )

        # An empty summary needs no message-view expansion. Keep this guard in
        # PL/pgSQL so PostgreSQL does not plan the large history UPDATE first.
        definition = session.execute(
            "SELECT pg_get_functiondef('messenger_refresh_topic_summary_state(uuid,uuid,uuid[])'::regprocedure) AS definition"
        ).fetchone()["definition"]
        guard = """BEGIN
            IF NOT EXISTS(SELECT 1 FROM messenger_topics
                WHERE project_id=target_project_id AND uuid=target_topic_uuid AND summary IS NOT NULL) THEN
                UPDATE messenger_user_topic_bindings SET summary_has_new_messages=NULL
                WHERE project_id=target_project_id AND topic_uuid=target_topic_uuid
                  AND summary_has_new_messages IS NOT NULL
                  AND (target_users IS NULL OR user_uuid=ANY(target_users))
                  AND NOT EXISTS(SELECT 1 FROM messenger_topics
                      WHERE project_id=target_project_id AND uuid=target_topic_uuid AND summary IS NOT NULL);
                RETURN;
            END IF;
        """
        session.execute(
            definition.replace("LANGUAGE sql", "LANGUAGE plpgsql")
            .replace("AS $function$", "AS $function$\n" + guard)
            .replace("\n$function$", "\nEND;\n$function$")
        )

    def downgrade(self, session):
        raise RuntimeError(
            "Physical deletion requires a database backup to restore removed roots"
        )


migration_step = MigrationStep()
