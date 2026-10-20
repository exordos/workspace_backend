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


RETIRED_TABLES = (
    "m_folders",
    "m_folder_items",
    "m_workspace_messages",
    "m_workspace_message_reactions",
    "m_workspace_streams",
    "m_workspace_stream_bindings",
    "m_workspace_stream_topics",
    "m_workspace_user_message_flags",
    "m_workspace_user_topic_flags",
    "m_workspace_message_mentions_v1",
    "m_workspace_project_ingest_ranges_v2",
    "m_workspace_read_memberships_v1",
    "m_workspace_read_state_compaction_v1",
    "m_workspace_read_state_projects_v1",
    "m_workspace_topic_message_stats_v1",
    "m_workspace_user_read_chunks_v1",
    "m_workspace_user_read_revisions_v1",
    "m_workspace_user_topic_read_stats_v1",
)

PLACEMENT_MAP_SQL = r"""
CREATE TEMP TABLE messenger_cutover_placements ON COMMIT DROP AS
SELECT project_id, uuid, message_uuid, stream_uuid, topic_uuid,
       COALESCE(legacy_public_uuid, uuid) AS public_uuid
FROM messenger_message_placements;
CREATE INDEX ON messenger_cutover_placements(project_id, public_uuid);
ANALYZE messenger_cutover_placements;
"""

VERIFY_SQL = r"""

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM m_workspace_messages AS legacy
        LEFT JOIN messenger_cutover_placements AS placement
          ON placement.project_id=legacy.project_id
         AND placement.public_uuid=legacy.uuid
        LEFT JOIN messenger_messages AS message
          ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
        WHERE message.uuid IS NULL OR message.payload IS DISTINCT FROM legacy.payload
           OR message.author_uuid IS DISTINCT FROM legacy.user_uuid
           OR placement.stream_uuid IS DISTINCT FROM legacy.stream_uuid
           OR placement.topic_uuid IS DISTINCT FROM legacy.topic_uuid
    ) THEN
        RAISE EXCEPTION 'Cannot retire Messenger compatibility tables: unprojected messages remain';
    END IF;
    IF EXISTS (
        SELECT 1 FROM m_workspace_stream_bindings AS legacy
        WHERE NOT EXISTS (SELECT 1 FROM messenger_stream_bindings WHERE project_id=legacy.project_id AND stream_uuid=legacy.stream_uuid AND user_uuid=legacy.user_uuid)
    ) OR EXISTS (
        SELECT 1 FROM m_workspace_message_reactions AS legacy
        JOIN messenger_cutover_placements AS placement
          ON placement.project_id=legacy.project_id AND placement.public_uuid=legacy.message_uuid
        WHERE NOT EXISTS (SELECT 1 FROM messenger_message_reaction_facts WHERE project_id=legacy.project_id AND canonical_message_uuid=placement.message_uuid AND user_uuid=legacy.user_uuid AND emoji_name=legacy.emoji_name)
    ) OR EXISTS (
        SELECT 1 FROM m_workspace_streams AS legacy
        WHERE NOT EXISTS (SELECT 1 FROM messenger_streams WHERE project_id=legacy.project_id AND uuid=legacy.uuid)
    ) OR EXISTS (
        SELECT 1 FROM m_workspace_stream_topics AS legacy
        WHERE NOT EXISTS (SELECT 1 FROM messenger_topics WHERE project_id=legacy.project_id AND uuid=legacy.uuid)
    ) OR EXISTS (
        SELECT 1 FROM m_folders AS legacy
        WHERE NOT EXISTS (SELECT 1 FROM messenger_user_folder_bindings WHERE project_id=legacy.project_id AND user_uuid=legacy.user_uuid AND folder_uuid=legacy.uuid)
    ) OR EXISTS (
        SELECT 1 FROM m_folder_items AS legacy
        WHERE NOT EXISTS (SELECT 1 FROM messenger_folder_items WHERE project_id=legacy.project_id AND user_uuid=legacy.user_uuid AND uuid=legacy.uuid)
    ) THEN
        RAISE EXCEPTION 'Cannot retire Messenger compatibility tables: unprojected containers remain';
    END IF;
END;
$$;

"""

DETACH_MIRRORS_SQL = r"""
DROP FUNCTION messenger_v2_mirror_binding_to_legacy() CASCADE;
DROP FUNCTION messenger_v2_mirror_folder_binding_to_legacy() CASCADE;
DROP FUNCTION messenger_v2_mirror_folder_item_to_legacy() CASCADE;
DROP FUNCTION messenger_v2_mirror_message_update_to_legacy() CASCADE;
DROP FUNCTION messenger_v2_mirror_placement_to_legacy() CASCADE;
DROP FUNCTION messenger_v2_mirror_reaction_to_legacy() CASCADE;
DROP FUNCTION messenger_v2_mirror_state_to_legacy() CASCADE;
DROP FUNCTION messenger_v2_mirror_stream_to_legacy() CASCADE;
DROP FUNCTION messenger_v2_mirror_topic_binding_to_legacy() CASCADE;
DROP FUNCTION messenger_v2_mirror_topic_to_legacy() CASCADE;
"""

BACKFILL_SQL = r"""
CREATE TEMP TABLE messenger_cutover_user_states ON COMMIT DROP AS
WITH scopes AS (
    SELECT project_id, uuid AS message_uuid, user_uuid FROM m_workspace_user_message_flags
    UNION
    SELECT message.project_id, message.uuid, chunk.user_uuid
    FROM m_workspace_messages AS message
    JOIN m_workspace_read_state_projects_v1 AS project
      ON project.project_id=message.project_id AND project.mode IN ('compact','rollback')
    JOIN m_workspace_user_read_chunks_v1 AS chunk
      ON chunk.chunk_number=message.ingest_sequence/4096
    WHERE get_bit(chunk.read_bits, (message.ingest_sequence % 4096)::integer)=1
    UNION
    SELECT message.project_id, message.uuid, mention.user_uuid
    FROM m_workspace_message_mentions_v1 AS mention
    JOIN m_workspace_messages AS message ON message.uuid=mention.message_uuid
), missing_states AS MATERIALIZED (
    SELECT placement.*, scopes.user_uuid
    FROM scopes
    JOIN messenger_cutover_placements AS placement
      ON placement.project_id=scopes.project_id AND placement.public_uuid=scopes.message_uuid
    WHERE NOT EXISTS (
        SELECT 1 FROM messenger_user_message_states AS state
        WHERE state.project_id=placement.project_id AND state.placement_uuid=placement.uuid
          AND state.user_uuid=scopes.user_uuid
    )
)
SELECT placement.project_id, placement.uuid AS placement_uuid,
       placement.stream_uuid, placement.topic_uuid, placement.user_uuid,
       membership.membership_generation,
       CASE WHEN message.author_uuid=placement.user_uuid THEN 'author' ELSE 'member' END AS relation_role,
       CASE WHEN CASE WHEN project.mode IN ('compact','rollback') THEN
           COALESCE(get_bit(chunk.read_bits, (legacy.ingest_sequence % 4096)::integer)=1, false)
           ELSE COALESCE(flags.read, false) END
           THEN COALESCE(flags.updated_at, legacy.updated_at) AT TIME ZONE current_setting('TIMEZONE') END AS read_at,
       CASE WHEN project.mode IN ('compact','rollback') THEN mention.message_uuid IS NOT NULL
            ELSE strpos(lower(CASE WHEN message.payload->>'kind'='markdown' THEN message.payload->>'content' ELSE '' END),
                        '](urn:user:' || lower(placement.user_uuid::text) || ')') > 0 END AS mentioned,
       COALESCE(flags.pinned,false) AS pinned, COALESCE(flags.starred,false) AS starred,
       COALESCE(flags.created_at,legacy.created_at) AS created_at,
       COALESCE(flags.updated_at,legacy.updated_at) AS updated_at
FROM missing_states AS placement
JOIN m_workspace_messages AS legacy
  ON legacy.project_id=placement.project_id AND legacy.uuid=placement.public_uuid
JOIN messenger_messages AS message
  ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
JOIN messenger_stream_bindings AS membership
  ON membership.project_id=placement.project_id AND membership.stream_uuid=placement.stream_uuid
 AND membership.user_uuid=placement.user_uuid AND membership.active
LEFT JOIN m_workspace_user_message_flags AS flags
  ON flags.project_id=placement.project_id AND flags.uuid=placement.public_uuid AND flags.user_uuid=placement.user_uuid
LEFT JOIN m_workspace_read_state_projects_v1 AS project ON project.project_id=placement.project_id
LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
  ON chunk.user_uuid=placement.user_uuid AND chunk.chunk_number=legacy.ingest_sequence/4096
LEFT JOIN m_workspace_message_mentions_v1 AS mention
  ON mention.message_uuid=legacy.uuid AND mention.user_uuid=placement.user_uuid;

INSERT INTO messenger_user_message_bindings (
    uuid, project_id, placement_uuid, user_uuid, membership_generation,
    relation_role, visibility, permissions, created_at, updated_at
)
SELECT messenger_uuid_v5(placement_uuid, user_uuid::text), project_id,
       placement_uuid, user_uuid, membership_generation, relation_role,
       'visible', '{"read":true,"react":true,"star":true,"pin":true}'::jsonb, created_at, updated_at
FROM messenger_cutover_user_states
ON CONFLICT (project_id, user_uuid, placement_uuid) DO NOTHING;

INSERT INTO messenger_user_message_states (
    uuid, project_id, placement_uuid, user_uuid, membership_generation,
    read_at, mentioned, pinned, starred, created_at, updated_at
)
SELECT messenger_uuid_v5(placement_uuid, user_uuid::text), project_id,
       placement_uuid, user_uuid, membership_generation,
       read_at, mentioned, pinned, starred, created_at, updated_at
FROM messenger_cutover_user_states
ON CONFLICT (project_id, user_uuid, placement_uuid) DO NOTHING;

INSERT INTO messenger_domain_outbox_events
    (uuid,project_id,event_kind,scope_kind,scope_key,payload)
SELECT gen_random_uuid(), state.project_id, 'read_counters', scope.kind,
       state.project_id::text || ':' || state.user_uuid::text || ':' || scope.uuid::text,
       jsonb_build_object('source_kind','compatibility.retired','user_uuid',state.user_uuid,
                          'stream_uuid',state.stream_uuid,'topic_uuid',state.topic_uuid)
FROM (SELECT DISTINCT project_id,user_uuid,stream_uuid,topic_uuid FROM messenger_cutover_user_states) AS state
CROSS JOIN LATERAL (VALUES ('user-stream',state.stream_uuid),('user-topic',state.topic_uuid)) AS scope(kind,uuid);

INSERT INTO messenger_domain_outbox_events
    (uuid,project_id,event_kind,scope_kind,scope_key,payload)
SELECT gen_random_uuid(), folder.project_id, 'folder_projection', 'user-folder',
       folder.project_id::text || ':' || folder.user_uuid::text || ':' || folder.folder_uuid::text,
       jsonb_build_object('source_kind','compatibility.retired','user_uuid',folder.user_uuid,'folder_uuid',folder.folder_uuid)
FROM messenger_user_folder_bindings AS folder
WHERE EXISTS (SELECT 1 FROM messenger_cutover_user_states AS state
              WHERE state.project_id=folder.project_id AND state.user_uuid=folder.user_uuid);
"""

RETARGET_SQL = r"""

ALTER TABLE m_workspace_drafts DROP CONSTRAINT m_workspace_drafts_binding_fkey,
    DROP CONSTRAINT m_workspace_drafts_stream_uuid_fkey,
    DROP CONSTRAINT m_workspace_drafts_topic_uuid_fkey;
ALTER TABLE m_workspace_drafts
    ADD CONSTRAINT m_workspace_drafts_binding_fkey FOREIGN KEY (project_id, stream_uuid, user_uuid)
        REFERENCES messenger_stream_bindings(project_id, stream_uuid, user_uuid) ON DELETE CASCADE,
    ADD CONSTRAINT m_workspace_drafts_topic_uuid_fkey FOREIGN KEY (project_id, stream_uuid, topic_uuid)
        REFERENCES messenger_topics(project_id, stream_uuid, uuid) ON DELETE CASCADE;
ALTER TABLE m_workspace_files DROP CONSTRAINT m_workspace_files_stream_uuid_fkey;
ALTER TABLE m_workspace_files ADD CONSTRAINT m_workspace_files_stream_uuid_fkey
    FOREIGN KEY (project_id, stream_uuid) REFERENCES messenger_streams(project_id, uuid) ON DELETE CASCADE;
ALTER TABLE m_workspace_topic_summary_jobs DROP CONSTRAINT IF EXISTS m_workspace_topic_summary_job_boundary_fkey,
    DROP CONSTRAINT IF EXISTS m_workspace_topic_summary_job_topic_fkey;
UPDATE m_workspace_topic_summary_jobs AS job SET boundary_message_uuid=placement.uuid
FROM messenger_message_placements AS placement
WHERE placement.project_id=job.project_id AND placement.legacy_public_uuid=job.boundary_message_uuid;
UPDATE m_workspace_topic_summary_journal AS journal SET boundary_message_uuid=placement.uuid
FROM messenger_message_placements AS placement
WHERE placement.project_id=journal.project_id AND placement.legacy_public_uuid=journal.boundary_message_uuid;
ALTER TABLE m_workspace_topic_summary_jobs
    ADD CONSTRAINT m_workspace_topic_summary_job_boundary_fkey FOREIGN KEY (project_id, boundary_message_uuid)
        REFERENCES messenger_message_placements(project_id, uuid) ON DELETE SET NULL (boundary_message_uuid),
    ADD CONSTRAINT m_workspace_topic_summary_job_topic_fkey FOREIGN KEY (project_id, topic_uuid)
        REFERENCES messenger_topics(project_id, uuid) ON DELETE CASCADE;
ALTER TABLE m_workspace_topic_summary_journal DROP CONSTRAINT IF EXISTS m_workspace_topic_summary_journal_topic_fkey;
ALTER TABLE m_workspace_topic_summary_journal ADD CONSTRAINT m_workspace_topic_summary_journal_topic_fkey
    FOREIGN KEY (project_id, topic_uuid) REFERENCES messenger_topics(project_id, uuid) ON DELETE CASCADE;

CREATE VIEW messenger_topic_messages_v1 AS
SELECT placement.uuid, placement.project_id, placement.stream_uuid, placement.topic_uuid,
       message.author_uuid AS user_uuid, message.payload, message.created_at, message.updated_at
FROM messenger_message_placements AS placement
JOIN messenger_messages AS message
  ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
JOIN messenger_topics AS topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid
JOIN messenger_streams AS stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
WHERE message.deleted_at IS NULL AND topic.deleted_at IS NULL AND stream.deleted_at IS NULL;

CREATE VIEW messenger_api_user_drafts_v1 AS
SELECT draft.* FROM m_workspace_drafts AS draft
JOIN messenger_stream_bindings AS membership
  ON membership.project_id=draft.project_id AND membership.stream_uuid=draft.stream_uuid
 AND membership.user_uuid=draft.user_uuid AND membership.active
JOIN messenger_streams AS stream
  ON stream.project_id=draft.project_id AND stream.uuid=draft.stream_uuid AND stream.deleted_at IS NULL
JOIN messenger_topics AS topic
  ON topic.project_id=draft.project_id AND topic.uuid=draft.topic_uuid AND topic.deleted_at IS NULL;

CREATE FUNCTION messenger_cleanup_revoked_membership() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM m_workspace_drafts WHERE project_id=NEW.project_id
      AND stream_uuid=NEW.stream_uuid AND user_uuid=NEW.user_uuid;
    DELETE FROM m_workspace_file_accesses AS access
    USING m_workspace_files AS file
    WHERE access.file_uuid=file.uuid AND access.project_id=NEW.project_id
      AND access.user_uuid=NEW.user_uuid AND file.project_id=NEW.project_id
      AND file.stream_uuid=NEW.stream_uuid;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_cleanup_revoked_membership
AFTER UPDATE OF active ON messenger_stream_bindings FOR EACH ROW
WHEN (OLD.active AND NOT NEW.active) EXECUTE FUNCTION messenger_cleanup_revoked_membership();

CREATE FUNCTION messenger_cancel_disabled_summary_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE m_workspace_llm_endpoints AS endpoint
    SET claim_token=NULL,claim_expires_at=NULL,updated_at=NOW()
    FROM m_workspace_topic_summary_jobs AS job
    WHERE job.project_id=NEW.project_id AND job.topic_uuid=NEW.uuid
      AND endpoint.uuid=job.endpoint_uuid AND endpoint.claim_token=job.endpoint_claim_token;
    DELETE FROM m_workspace_topic_summary_jobs
    WHERE project_id=NEW.project_id AND topic_uuid=NEW.uuid;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_cancel_disabled_summary_job
AFTER UPDATE OF summary_enabled ON messenger_topics FOR EACH ROW
WHEN (NOT NEW.summary_enabled) EXECUTE FUNCTION messenger_cancel_disabled_summary_job();

CREATE FUNCTION messenger_refresh_topic_summary_state(
    target_project_id uuid, target_topic_uuid uuid, target_users uuid[] DEFAULT NULL
) RETURNS void LANGUAGE sql AS $$
UPDATE messenger_user_topic_bindings AS binding
SET summary_has_new_messages=CASE WHEN topic.summary IS NULL THEN NULL ELSE EXISTS (
    SELECT 1 FROM messenger_api_user_messages_v1 AS message
    WHERE message.project_id=binding.project_id AND message.topic_uuid=binding.topic_uuid
      AND message.user_uuid=binding.user_uuid AND message.visible
      AND message.payload->>'kind'='markdown'
      AND (boundary.uuid IS NULL OR (message.created_at,message.uuid) > (boundary.created_at,boundary.uuid))
) END
FROM messenger_topics AS topic
LEFT JOIN messenger_topic_messages_v1 AS boundary
  ON boundary.project_id=topic.project_id AND boundary.uuid=topic.summary_last_message_uuid
WHERE binding.project_id=target_project_id AND binding.topic_uuid=target_topic_uuid
  AND topic.project_id=binding.project_id AND topic.uuid=binding.topic_uuid
  AND (topic.summary IS NOT NULL OR binding.summary_has_new_messages IS NOT NULL)
  AND (target_users IS NULL OR binding.user_uuid=ANY(target_users));
$$;

CREATE FUNCTION messenger_refresh_summary_after_topic_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM messenger_refresh_topic_summary_state(NEW.project_id,NEW.uuid);
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_refresh_summary_after_topic_change
AFTER UPDATE OF summary,summary_last_message_uuid ON messenger_topics FOR EACH ROW
EXECUTE FUNCTION messenger_refresh_summary_after_topic_change();

CREATE FUNCTION messenger_refresh_summary_after_preview_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM messenger_refresh_topic_summary_state(NEW.project_id,NEW.topic_uuid,ARRAY[NEW.user_uuid]);
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_refresh_summary_after_preview_insert
AFTER INSERT ON messenger_user_topic_bindings FOR EACH ROW
EXECUTE FUNCTION messenger_refresh_summary_after_preview_change();
CREATE TRIGGER messenger_refresh_summary_after_preview_change
AFTER UPDATE OF last_message_uuid ON messenger_user_topic_bindings FOR EACH ROW
WHEN (OLD.last_message_uuid IS DISTINCT FROM NEW.last_message_uuid)
EXECUTE FUNCTION messenger_refresh_summary_after_preview_change();

CREATE FUNCTION messenger_refresh_summary_after_content_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_topic uuid;
BEGIN
    FOR target_topic IN SELECT DISTINCT topic_uuid FROM messenger_message_placements
                        WHERE project_id=NEW.project_id AND message_uuid=NEW.uuid LOOP
        PERFORM messenger_refresh_topic_summary_state(NEW.project_id,target_topic);
    END LOOP;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_refresh_summary_after_content_change
AFTER UPDATE OF payload,deleted_at ON messenger_messages FOR EACH ROW
WHEN (OLD.payload->>'kind' IS DISTINCT FROM NEW.payload->>'kind' OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
EXECUTE FUNCTION messenger_refresh_summary_after_content_change();

SELECT messenger_refresh_topic_summary_state(project_id,uuid)
FROM messenger_topics WHERE summary IS NOT NULL;



"""

VIEWS_SQL = r"""
CREATE OR REPLACE VIEW m_workspace_visible_files_v1 AS  SELECT files.uuid,
    files.project_id,
    files.name,
    files.description,
    files.user_uuid,
    files.stream_uuid,
    files.content_type,
    files.size_bytes,
    files.hash,
    files.created_at,
    files.updated_at,
    files.storage_type,
    files.storage_id,
    files.storage_object_id,
    files.acl_mode,
    bindings.user_uuid AS viewer_user_uuid
   FROM (m_workspace_files files
     JOIN messenger_stream_bindings bindings ON (((bindings.project_id = files.project_id) AND (bindings.stream_uuid = files.stream_uuid))))
  JOIN messenger_streams AS stream ON stream.project_id=files.project_id AND stream.uuid=files.stream_uuid
WHERE files.acl_mode = 'stream' AND bindings.active AND stream.deleted_at IS NULL
UNION
 SELECT files.uuid,
    files.project_id,
    files.name,
    files.description,
    files.user_uuid,
    files.stream_uuid,
    files.content_type,
    files.size_bytes,
    files.hash,
    files.created_at,
    files.updated_at,
    files.storage_type,
    files.storage_id,
    files.storage_object_id,
    files.acl_mode,
    files.user_uuid AS viewer_user_uuid
   FROM m_workspace_files files
  WHERE (files.acl_mode = 'owner'::text)
UNION
 SELECT files.uuid,
    files.project_id,
    files.name,
    files.description,
    files.user_uuid,
    files.stream_uuid,
    files.content_type,
    files.size_bytes,
    files.hash,
    files.created_at,
    files.updated_at,
    files.storage_type,
    files.storage_id,
    files.storage_object_id,
    files.acl_mode,
    NULL::uuid AS viewer_user_uuid
   FROM m_workspace_files files
  WHERE (files.acl_mode = 'public'::text);;
CREATE OR REPLACE VIEW m_workspace_visible_events AS  WITH event_rows AS (
         SELECT e_1.epoch_version,
            e_1.uuid,
            e_1.project_id,
            e_1.user_uuid,
            e_1.payload,
            e_1.created_at,
            e_1.updated_at,
            e_1.schema_version,
            e_1.object_type,
            e_1.action
           FROM m_workspace_events e_1
        UNION ALL
         SELECT b.epoch_version,
            b.uuid,
            b.project_id,
            recipient.user_uuid,
            ((b.payload || COALESCE(override.payload, '{}'::jsonb)) ||
                CASE
                    WHEN (b.object_type = 'user'::text) THEN '{}'::jsonb
                    ELSE jsonb_build_object('user_uuid', recipient.user_uuid)
                END) AS payload,
            b.created_at,
            b.updated_at,
            b.schema_version,
            b.object_type,
            b.action
           FROM ((m_workspace_broadcast_message_events_v1 b
             JOIN m_workspace_event_audience_members_v1 recipient ON ((recipient.audience_snapshot_uuid = b.audience_snapshot_uuid)))
             LEFT JOIN m_workspace_event_recipient_payloads_v1 override ON (((override.event_uuid = b.uuid) AND (override.user_uuid = recipient.user_uuid))))
        )
 SELECT event.* FROM event_rows AS event
WHERE (event.object_type <> 'message' OR event.payload->>'stream_uuid' IS NULL OR EXISTS (
    SELECT 1 FROM messenger_stream_bindings AS binding
    WHERE binding.project_id=event.project_id AND binding.user_uuid=event.user_uuid
      AND binding.stream_uuid=(event.payload->>'stream_uuid')::uuid AND binding.active
)) AND (NOT EXISTS (
    SELECT 1 FROM messenger_event_membership_guards AS guard
    WHERE guard.event_uuid=event.uuid AND guard.user_uuid=event.user_uuid
) OR EXISTS (
    SELECT 1 FROM messenger_event_membership_guards AS guard
    LEFT JOIN messenger_stream_bindings AS binding
      ON binding.project_id=guard.project_id AND binding.stream_uuid=guard.stream_uuid
     AND binding.user_uuid=guard.user_uuid AND binding.active
     AND binding.membership_generation=guard.membership_generation
    WHERE guard.event_uuid=event.uuid AND guard.user_uuid=event.user_uuid
      AND (guard.control_effect OR binding.uuid IS NOT NULL)
));

"""

CLEANUP_SQL = r"""
DROP FUNCTION m_workspace_assign_ingest_sequence_v1() CASCADE;
DROP FUNCTION m_workspace_fence_legacy_gap_cutover_v1() CASCADE;
DROP FUNCTION m_workspace_hold_legacy_gap_progress_v1() CASCADE;
DROP FUNCTION m_workspace_register_read_state_project_v1() CASCADE;
DROP FUNCTION messenger_link_legacy_message_to_canonical() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_binding() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_folder() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_folder_item() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_message() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_message_flag_inserts() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_message_flags() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_message_inserts() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_reaction() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_stream() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_topic() CASCADE;
DROP FUNCTION messenger_v2_import_legacy_topic_flags() CASCADE;
DROP TABLE m_folders,
    m_folder_items,
    m_workspace_messages,
    m_workspace_message_reactions,
    m_workspace_streams,
    m_workspace_stream_bindings,
    m_workspace_stream_topics,
    m_workspace_user_message_flags,
    m_workspace_user_topic_flags,
    m_workspace_message_mentions_v1,
    m_workspace_project_ingest_ranges_v2,
    m_workspace_read_memberships_v1,
    m_workspace_read_state_compaction_v1,
    m_workspace_read_state_projects_v1,
    m_workspace_topic_message_stats_v1,
    m_workspace_user_read_chunks_v1,
    m_workspace_user_read_revisions_v1,
    m_workspace_user_topic_read_stats_v1 CASCADE;

CREATE OR REPLACE VIEW m_workspace_projects_view AS
SELECT project_id FROM messenger_project_users
UNION SELECT project_id FROM m_workspace_events;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0230-Avoid-redundant-project-user-writes-and-bound-Zulip-reconciliation-batches-fc01aa.py"
        ]

    @property
    def migration_id(self):
        return "bc967804-430e-4cb0-b6e6-63c273140d15"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # This is a coordinated cutover: old API/worker processes must be stopped.
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('workspace-read-state-schema-v1', 0))"
        )
        session.execute(
            "LOCK TABLE " + ", ".join(RETIRED_TABLES) + " IN ACCESS EXCLUSIVE MODE"
        )
        session.execute(PLACEMENT_MAP_SQL)
        session.execute(VERIFY_SQL)
        session.execute(DETACH_MIRRORS_SQL)
        session.execute(BACKFILL_SQL)
        session.execute(RETARGET_SQL)
        session.execute(VIEWS_SQL)
        session.execute(CLEANUP_SQL)

    def downgrade(self, session):
        raise RuntimeError(
            "Retired Messenger tables require restoring the pre-cutover database backup"
        )


migration_step = MigrationStep()
