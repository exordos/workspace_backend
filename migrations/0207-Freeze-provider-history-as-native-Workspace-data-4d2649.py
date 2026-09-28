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


UPGRADE = r"""
WITH provider_user_uuids AS (
    SELECT stream.user_uuid AS uuid
    FROM m_workspace_streams AS stream
    WHERE stream.source_name <> 'native'
    UNION
    SELECT stream.direct_user_uuid
    FROM m_workspace_streams AS stream
    WHERE stream.source_name <> 'native'
      AND stream.direct_user_uuid IS NOT NULL
    UNION
    SELECT binding.user_uuid
    FROM m_workspace_stream_bindings AS binding
    JOIN m_workspace_streams AS stream ON stream.uuid = binding.stream_uuid
    WHERE stream.source_name <> 'native'
    UNION
    SELECT binding.who_uuid
    FROM m_workspace_stream_bindings AS binding
    JOIN m_workspace_streams AS stream ON stream.uuid = binding.stream_uuid
    WHERE stream.source_name <> 'native'
    UNION
    SELECT message.user_uuid
    FROM m_workspace_messages AS message
    JOIN m_workspace_streams AS stream ON stream.uuid = message.stream_uuid
    WHERE stream.source_name <> 'native'
    UNION
    SELECT reaction.user_uuid
    FROM m_workspace_message_reactions AS reaction
    JOIN m_workspace_messages AS message ON message.uuid = reaction.message_uuid
    JOIN m_workspace_streams AS stream ON stream.uuid = message.stream_uuid
    WHERE stream.source_name <> 'native'
    UNION
    SELECT file.user_uuid
    FROM m_workspace_files AS file
    JOIN m_workspace_streams AS stream ON stream.uuid = file.stream_uuid
    WHERE stream.source_name <> 'native'
)
INSERT INTO workspace_v3.users (
    uuid, created_at, updated_at, username, source, status,
    first_name, last_name, email, last_ping_at,
    status_emoji, status_text, avatar
)
SELECT legacy.uuid, legacy.created_at, legacy.updated_at,
       CASE
           WHEN EXISTS (
               SELECT 1 FROM workspace_v3.users AS existing
               WHERE existing.username = legacy.username
                 AND existing.uuid <> legacy.uuid
           ) THEN LEFT(legacy.username, 91) || '-' || legacy.uuid::text
           ELSE COALESCE(NULLIF(legacy.username, ''), legacy.uuid::text)
       END,
       'iam', legacy.status, legacy.first_name, legacy.last_name,
       NULL,
       legacy.last_ping_at, legacy.status_emoji,
       legacy.status_text,
       COALESCE(
           NULLIF(legacy.avatar, ''),
           'urn:gravatar:' || md5(lower(COALESCE(legacy.email, legacy.username)))
       )
FROM m_workspace_users AS legacy
JOIN provider_user_uuids AS selected ON selected.uuid = legacy.uuid
ON CONFLICT (uuid) DO NOTHING;

INSERT INTO workspace_v3.streams (
    uuid, project_id, name, description, owner_uuid, source_name,
    invite_only, announce, direct_user_uuid, private, is_archived,
    private_index, color, created_at, updated_at
)
SELECT legacy.uuid, legacy.project_id, legacy.name, legacy.description,
       legacy.user_uuid, 'native', legacy.invite_only,
       legacy.announce, legacy.direct_user_uuid, legacy.private,
       legacy.is_archived, legacy.private_index,
       COALESCE(
           legacy.color,
           mod(
               ('x' || substr(md5(legacy.uuid::text), 1, 8))::bit(32)::bigint,
               16777216
           )
       ),
       legacy.created_at AT TIME ZONE 'UTC',
       legacy.updated_at AT TIME ZONE 'UTC'
FROM m_workspace_streams AS legacy
WHERE legacy.source_name <> 'native'
ON CONFLICT (project_id, uuid) DO NOTHING;

INSERT INTO workspace_v3.topics (
    uuid, project_id, stream_uuid, name, color, source_name,
    summary, summary_last_message_uuid, summary_enabled,
    summary_system_prompt, summary_reasoning_effort, is_done,
    created_at, updated_at
)
SELECT legacy.uuid, legacy.project_id, legacy.stream_uuid, legacy.name,
       COALESCE(
           legacy.color,
           mod(
               ('x' || substr(md5(legacy.uuid::text), 1, 8))::bit(32)::bigint,
               16777216
           )
       ),
       'native', legacy.summary, legacy.summary_last_message_uuid,
       legacy.summary_enabled, legacy.summary_system_prompt,
       legacy.summary_reasoning_effort,
       EXISTS (
           SELECT 1 FROM m_workspace_user_topic_flags AS flag
           WHERE flag.uuid = legacy.uuid AND flag.is_done
       ),
       legacy.created_at AT TIME ZONE 'UTC',
       legacy.updated_at AT TIME ZONE 'UTC'
FROM m_workspace_stream_topics AS legacy
JOIN m_workspace_streams AS stream ON stream.uuid = legacy.stream_uuid
WHERE stream.source_name <> 'native'
ON CONFLICT (project_id, uuid) DO NOTHING;

UPDATE workspace_v3.streams AS target
SET default_topic_uuid = legacy.default_topic_uuid
FROM m_workspace_streams AS legacy
WHERE legacy.source_name <> 'native'
  AND target.project_id = legacy.project_id
  AND target.uuid = legacy.uuid
  AND target.default_topic_uuid IS DISTINCT FROM legacy.default_topic_uuid;

INSERT INTO workspace_v3.stream_bindings (
    uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
    notification_mode, notification_updated_at, last_message_uuid,
    created_at, updated_at
)
SELECT legacy.uuid, legacy.project_id, legacy.stream_uuid,
       legacy.user_uuid, legacy.who_uuid, legacy.role,
       legacy.notification_mode, legacy.notification_updated_at,
       (
           SELECT message.uuid
           FROM m_workspace_messages AS message
           WHERE message.stream_uuid = legacy.stream_uuid
           ORDER BY message.created_at DESC, message.uuid DESC
           LIMIT 1
       ),
       legacy.created_at AT TIME ZONE 'UTC',
       legacy.updated_at AT TIME ZONE 'UTC'
FROM m_workspace_stream_bindings AS legacy
JOIN m_workspace_streams AS stream ON stream.uuid = legacy.stream_uuid
WHERE stream.source_name <> 'native'
ON CONFLICT (project_id, stream_uuid, user_uuid) DO NOTHING;

INSERT INTO workspace_v3.topic_bindings (
    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
    notification_mode, notification_updated_at, last_message_uuid,
    created_at, updated_at
)
SELECT md5(topic.uuid::text || ':' || binding.user_uuid::text)::uuid,
       topic.project_id, topic.stream_uuid, topic.uuid, binding.user_uuid,
       COALESCE(flag.notification_mode, 'default'),
       COALESCE(flag.notification_updated_at, binding.notification_updated_at),
       (
           SELECT message.uuid
           FROM m_workspace_messages AS message
           WHERE message.topic_uuid = topic.uuid
           ORDER BY message.created_at DESC, message.uuid DESC
           LIMIT 1
       ),
       COALESCE(flag.created_at, binding.created_at) AT TIME ZONE 'UTC',
       COALESCE(flag.updated_at, binding.updated_at) AT TIME ZONE 'UTC'
FROM m_workspace_stream_topics AS topic
JOIN m_workspace_streams AS stream ON stream.uuid = topic.stream_uuid
JOIN m_workspace_stream_bindings AS binding
  ON binding.stream_uuid = topic.stream_uuid
LEFT JOIN m_workspace_user_topic_flags AS flag
  ON flag.uuid = topic.uuid AND flag.user_uuid = binding.user_uuid
WHERE stream.source_name <> 'native'
ON CONFLICT (project_id, topic_uuid, user_uuid) DO NOTHING;

INSERT INTO workspace_v3.messages (
    uuid, project_id, stream_uuid, topic_uuid, author_uuid, payload,
    source_name, reactions, reaction_users, created_at, updated_at
)
SELECT legacy.uuid, legacy.project_id, legacy.stream_uuid,
       legacy.topic_uuid, legacy.user_uuid, legacy.payload,
       'zulip', '{}'::jsonb,
       COALESCE(legacy.reaction_users, '{}'::jsonb),
       legacy.created_at AT TIME ZONE 'UTC',
       legacy.updated_at AT TIME ZONE 'UTC'
FROM m_workspace_messages AS legacy
JOIN m_workspace_streams AS stream ON stream.uuid = legacy.stream_uuid
WHERE stream.source_name <> 'native'
ON CONFLICT (project_id, uuid) DO NOTHING;

INSERT INTO workspace_v3.message_flags (
    uuid, project_id, stream_uuid, message_uuid, user_uuid,
    read, pinned, starred, mentioned, created_at, updated_at
)
SELECT md5(message.uuid::text || ':' || binding.user_uuid::text)::uuid,
       message.project_id, message.stream_uuid, message.uuid, binding.user_uuid,
       CASE
           WHEN read_project.mode IN ('compact', 'rollback') THEN
               COALESCE(
                   get_bit(
                       read_chunk.read_bits,
                       (message.ingest_sequence % 4096)::integer
                   ),
                   0
               ) = 1
           ELSE COALESCE(flag.read, FALSE)
       END,
       COALESCE(flag.pinned, FALSE), COALESCE(flag.starred, FALSE),
       CASE
           WHEN read_project.mode IN ('compact', 'rollback') THEN
               mention.message_uuid IS NOT NULL
           ELSE POSITION(
               '](urn:user:' || LOWER(binding.user_uuid::text) || ')'
               IN LOWER(COALESCE(message.payload ->> 'content', ''))
           ) > 0
       END,
       COALESCE(flag.created_at, message.created_at),
       COALESCE(flag.updated_at, message.updated_at)
FROM m_workspace_messages AS message
JOIN m_workspace_streams AS stream ON stream.uuid = message.stream_uuid
JOIN workspace_v3.stream_bindings AS binding
  ON binding.project_id = message.project_id
 AND binding.stream_uuid = message.stream_uuid
LEFT JOIN m_workspace_read_state_projects_v1 AS read_project
  ON read_project.project_id = message.project_id
LEFT JOIN m_workspace_user_message_flags AS flag
  ON flag.project_id = message.project_id
 AND flag.uuid = message.uuid
 AND flag.user_uuid = binding.user_uuid
LEFT JOIN m_workspace_user_read_chunks_v1 AS read_chunk
  ON read_chunk.user_uuid = binding.user_uuid
 AND read_chunk.chunk_number = message.ingest_sequence / 4096
LEFT JOIN m_workspace_message_mentions_v1 AS mention
  ON mention.message_uuid = message.uuid
 AND mention.user_uuid = binding.user_uuid
WHERE stream.source_name <> 'native'
ON CONFLICT (project_id, message_uuid, user_uuid) DO NOTHING;

INSERT INTO workspace_v3.message_reactions (
    uuid, project_id, message_uuid, user_uuid, emoji_name,
    source_name, created_at, updated_at
)
SELECT reaction.uuid, reaction.project_id, reaction.message_uuid,
       reaction.user_uuid, reaction.emoji_name, 'zulip',
       reaction.created_at, reaction.updated_at
FROM m_workspace_message_reactions AS reaction
JOIN m_workspace_messages AS message ON message.uuid = reaction.message_uuid
JOIN m_workspace_streams AS stream ON stream.uuid = message.stream_uuid
WHERE stream.source_name <> 'native'
ON CONFLICT (project_id, uuid) DO NOTHING;

INSERT INTO workspace_v3.folders (
    uuid, project_id, user_uuid, kind, title, background_color_value,
    created_at, updated_at
)
SELECT folder.uuid, folder.project_id, binding.user_uuid, 'custom',
       folder.title, folder.background_color_value,
       folder.created_at AT TIME ZONE 'UTC',
       folder.updated_at AT TIME ZONE 'UTC'
FROM messenger_folders AS folder
JOIN messenger_user_folder_bindings AS binding
  ON binding.project_id = folder.project_id
 AND binding.folder_uuid = folder.uuid
JOIN workspace_v3.users AS user_record ON user_record.uuid = binding.user_uuid
WHERE binding.rule = 'custom'
ON CONFLICT (project_id, user_uuid, uuid) DO NOTHING;

INSERT INTO workspace_v3.folder_items (
    uuid, project_id, user_uuid, folder_uuid, stream_uuid,
    order_index, pinned_at, chat_type, automatic, created_at, updated_at
)
SELECT item.uuid, item.project_id, item.user_uuid, item.folder_uuid,
       item.stream_uuid, item.order_index, item.pinned_at, item.chat_type,
       item.automatic, item.created_at, item.updated_at
FROM messenger_folder_items AS item
JOIN workspace_v3.folders AS folder
  ON folder.project_id = item.project_id
 AND folder.user_uuid = item.user_uuid
 AND folder.uuid = item.folder_uuid
JOIN workspace_v3.stream_bindings AS stream_binding
  ON stream_binding.project_id = item.project_id
 AND stream_binding.stream_uuid = item.stream_uuid
 AND stream_binding.user_uuid = item.user_uuid
ON CONFLICT (project_id, user_uuid, folder_uuid, stream_uuid) DO NOTHING;

INSERT INTO workspace_v3.drafts (
    uuid, project_id, user_uuid, stream_uuid, topic_uuid, payload,
    revision, created_at, updated_at
)
SELECT draft.uuid, draft.project_id, draft.user_uuid, draft.stream_uuid,
       draft.topic_uuid, draft.payload, draft.revision,
       draft.created_at, draft.updated_at
FROM m_workspace_drafts AS draft
JOIN workspace_v3.stream_bindings AS binding
  ON binding.project_id = draft.project_id
 AND binding.stream_uuid = draft.stream_uuid
 AND binding.user_uuid = draft.user_uuid
JOIN workspace_v3.topics AS topic
  ON topic.project_id = draft.project_id
 AND topic.stream_uuid = draft.stream_uuid
 AND topic.uuid = draft.topic_uuid
ON CONFLICT (project_id, user_uuid, uuid) DO NOTHING;

INSERT INTO workspace_v3.files (
    uuid, project_id, user_uuid, stream_uuid, acl_mode, name, description,
    content_type, size_bytes, hash, storage_type, storage_id,
    storage_object_id, created_at, updated_at
)
SELECT file.uuid, file.project_id, file.user_uuid, file.stream_uuid,
       file.acl_mode, file.name, file.description, file.content_type,
       file.size_bytes, file.hash, file.storage_type, file.storage_id,
       file.storage_object_id, file.created_at, file.updated_at
FROM m_workspace_files AS file
JOIN workspace_v3.users AS user_record ON user_record.uuid = file.user_uuid
LEFT JOIN workspace_v3.streams AS stream
  ON stream.project_id = file.project_id AND stream.uuid = file.stream_uuid
WHERE file.stream_uuid IS NULL OR stream.uuid IS NOT NULL
ON CONFLICT (project_id, uuid) DO NOTHING;

UPDATE workspace_v3.users
SET source = 'iam', email = NULL
WHERE source <> 'iam';
UPDATE workspace_v3.streams
SET source_name = 'native'
WHERE source_name <> 'native';
UPDATE workspace_v3.topics
SET source_name = 'native'
WHERE source_name <> 'native';
UPDATE workspace_v3.messages AS target
SET source_name = 'zulip'
FROM m_workspace_messages AS legacy
WHERE target.project_id = legacy.project_id
  AND target.uuid = legacy.uuid
  AND legacy.source_name <> 'native';

UPDATE m_workspace_users
SET source = 'iam', email = NULL, provider_uuid = NULL,
    external_account_uuid = NULL, provider_external_id = NULL
WHERE source <> 'iam'
   OR provider_uuid IS NOT NULL
   OR external_account_uuid IS NOT NULL
   OR provider_external_id IS NOT NULL;
UPDATE m_workspace_streams
SET source_name = 'native', source = '{"kind":"native"}'::jsonb,
    provider_uuid = NULL, external_account_uuid = NULL,
    provider_external_id = NULL, delivery_status = NULL,
    delivery_error = NULL, delivery_updated_at = NULL,
    provider_metadata = NULL, delivery_metadata = NULL
WHERE source_name <> 'native'
   OR provider_uuid IS NOT NULL
   OR external_account_uuid IS NOT NULL;
UPDATE m_workspace_stream_topics
SET source_name = 'native', source = '{"kind":"native"}'::jsonb,
    provider_uuid = NULL, external_account_uuid = NULL,
    provider_external_id = NULL, delivery_status = NULL,
    delivery_error = NULL, delivery_updated_at = NULL,
    provider_metadata = NULL, delivery_metadata = NULL
WHERE source_name <> 'native'
   OR provider_uuid IS NOT NULL
   OR external_account_uuid IS NOT NULL;
UPDATE m_workspace_messages
SET source_name = 'zulip', source = '{"kind":"zulip"}'::jsonb,
    provider_uuid = NULL, external_account_uuid = NULL,
    provider_external_id = NULL, delivery_status = NULL,
    delivery_error = NULL, delivery_updated_at = NULL,
    provider_metadata = NULL, delivery_metadata = NULL
WHERE source_name <> 'native'
   OR provider_uuid IS NOT NULL
   OR external_account_uuid IS NOT NULL;
UPDATE m_workspace_message_reactions
SET provider_uuid = NULL, external_account_uuid = NULL,
    provider_external_id = NULL, delivery_status = NULL,
    delivery_error = NULL, delivery_updated_at = NULL,
    provider_metadata = NULL, delivery_metadata = NULL
WHERE provider_uuid IS NOT NULL OR external_account_uuid IS NOT NULL;
UPDATE m_workspace_files
SET provider_uuid = NULL, external_account_uuid = NULL
WHERE provider_uuid IS NOT NULL OR external_account_uuid IS NOT NULL;

DELETE FROM m_external_provider_identity_links_v1;
DELETE FROM m_external_accounts_v2;
DELETE FROM m_external_accounts;
DELETE FROM m_external_provider_policies_v1;

DELETE FROM workspace_v3.event_recipient_payloads
WHERE consumer_type = 'provider';
DELETE FROM workspace_v3.event_cursors
WHERE consumer_type = 'provider';
DELETE FROM workspace_v3.event_audience_members
WHERE consumer_type = 'provider';
DELETE FROM workspace_v3.events AS event
WHERE NOT EXISTS (
    SELECT 1
    FROM workspace_v3.event_audience_members AS member
    WHERE member.project_id = event.project_id
      AND member.audience_snapshot_uuid = event.audience_snapshot_uuid
);
DELETE FROM workspace_v3.event_audience_snapshots AS snapshot
WHERE NOT EXISTS (
    SELECT 1
    FROM workspace_v3.event_audience_members AS member
    WHERE member.project_id = snapshot.project_id
      AND member.audience_snapshot_uuid = snapshot.uuid
);

DROP TRIGGER IF EXISTS message_reactions_provider_state_cleanup
    ON workspace_v3.message_reactions;
DROP TRIGGER IF EXISTS message_flags_provider_state_cleanup
    ON workspace_v3.message_flags;
DROP TRIGGER IF EXISTS messages_provider_state_cleanup
    ON workspace_v3.messages;
DROP TRIGGER IF EXISTS topic_bindings_provider_state_cleanup
    ON workspace_v3.topic_bindings;
DROP TRIGGER IF EXISTS topics_provider_state_cleanup
    ON workspace_v3.topics;
DROP TRIGGER IF EXISTS stream_bindings_provider_state_cleanup
    ON workspace_v3.stream_bindings;
DROP TRIGGER IF EXISTS streams_provider_state_cleanup
    ON workspace_v3.streams;
DROP TRIGGER IF EXISTS users_provider_state_cleanup
    ON workspace_v3.users;
DROP FUNCTION IF EXISTS workspace_v3.cleanup_provider_entity_state();
DROP TABLE workspace_v3.provider_entity_states;
DROP TABLE workspace_v3.provider_consumers;

ALTER TABLE workspace_v3.events
    DROP CONSTRAINT events_origin_consumer_pair_check,
    DROP CONSTRAINT events_origin_consumer_type_check,
    DROP COLUMN origin_consumer_uuid,
    DROP COLUMN origin_consumer_type;
ALTER TABLE workspace_v3.event_audience_members
    DROP CONSTRAINT event_audience_members_consumer_type_check,
    ADD CONSTRAINT event_audience_members_consumer_type_check
        CHECK (consumer_type = 'user');
ALTER TABLE workspace_v3.event_recipient_payloads
    DROP CONSTRAINT event_recipient_payloads_consumer_type_check,
    ADD CONSTRAINT event_recipient_payloads_consumer_type_check
        CHECK (consumer_type = 'user');
ALTER TABLE workspace_v3.event_cursors
    DROP CONSTRAINT event_cursors_consumer_type_check,
    ADD CONSTRAINT event_cursors_consumer_type_check
        CHECK (consumer_type = 'user');
"""


DOWNGRADE = r"""
ALTER TABLE workspace_v3.event_audience_members
    DROP CONSTRAINT event_audience_members_consumer_type_check,
    ADD CONSTRAINT event_audience_members_consumer_type_check
        CHECK (consumer_type IN ('user', 'provider'));
ALTER TABLE workspace_v3.event_recipient_payloads
    DROP CONSTRAINT event_recipient_payloads_consumer_type_check,
    ADD CONSTRAINT event_recipient_payloads_consumer_type_check
        CHECK (consumer_type IN ('user', 'provider'));
ALTER TABLE workspace_v3.event_cursors
    DROP CONSTRAINT event_cursors_consumer_type_check,
    ADD CONSTRAINT event_cursors_consumer_type_check
        CHECK (consumer_type IN ('user', 'provider'));
ALTER TABLE workspace_v3.events
    ADD COLUMN origin_consumer_type VARCHAR(16),
    ADD COLUMN origin_consumer_uuid UUID,
    ADD CONSTRAINT events_origin_consumer_type_check
        CHECK (origin_consumer_type IN ('user', 'provider')),
    ADD CONSTRAINT events_origin_consumer_pair_check
        CHECK (
            (origin_consumer_type IS NULL) = (origin_consumer_uuid IS NULL)
        );

CREATE TABLE workspace_v3.provider_consumers (
    uuid UUID NOT NULL,
    project_id UUID NOT NULL,
    name VARCHAR(32) NOT NULL,
    iam_user_uuid UUID NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, uuid),
    UNIQUE (uuid),
    UNIQUE (project_id, name),
    UNIQUE (project_id, iam_user_uuid),
    CONSTRAINT provider_consumers_name_check
        CHECK (name <> '' AND name <> 'native')
);
CREATE INDEX provider_consumers_auth_idx
    ON workspace_v3.provider_consumers (project_id, iam_user_uuid)
    WHERE enabled;

CREATE TABLE workspace_v3.provider_entity_states (
    project_id UUID NOT NULL,
    provider_uuid UUID NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    entity_uuid UUID NOT NULL,
    content_hash BYTEA NOT NULL,
    source_updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source_content_hash BYTEA NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, provider_uuid, entity_type, entity_uuid),
    UNIQUE (project_id, entity_type, entity_uuid),
    CONSTRAINT provider_entity_states_provider_fkey
        FOREIGN KEY (project_id, provider_uuid)
        REFERENCES workspace_v3.provider_consumers (project_id, uuid)
        ON DELETE CASCADE,
    CONSTRAINT provider_entity_states_type_check
        CHECK (entity_type IN (
            'user', 'stream', 'stream_binding', 'topic', 'topic_binding',
            'message', 'message_flag', 'message_reaction'
        )),
    CONSTRAINT provider_entity_states_hash_check
        CHECK (octet_length(content_hash) = 32),
    CONSTRAINT provider_entity_states_source_hash_check
        CHECK (octet_length(source_content_hash) = 32)
);
CREATE INDEX provider_entity_states_list_idx
    ON workspace_v3.provider_entity_states (
        project_id, provider_uuid, entity_type, updated_at, entity_uuid
    );
CREATE INDEX provider_entity_states_entity_projects_idx
    ON workspace_v3.provider_entity_states (
        entity_type, entity_uuid, project_id
    );

CREATE FUNCTION workspace_v3.cleanup_provider_entity_state()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_ARGV[1] = 'global' THEN
        DELETE FROM workspace_v3.provider_entity_states
        WHERE entity_type = TG_ARGV[0] AND entity_uuid = OLD.uuid;
    ELSE
        DELETE FROM workspace_v3.provider_entity_states
        WHERE project_id = OLD.project_id
          AND entity_type = TG_ARGV[0]
          AND entity_uuid = OLD.uuid;
    END IF;
    RETURN OLD;
END
$function$;
CREATE TRIGGER users_provider_state_cleanup
    AFTER DELETE ON workspace_v3.users
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'user', 'global'
    );
CREATE TRIGGER streams_provider_state_cleanup
    AFTER DELETE ON workspace_v3.streams
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'stream', 'project'
    );
CREATE TRIGGER stream_bindings_provider_state_cleanup
    AFTER DELETE ON workspace_v3.stream_bindings
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'stream_binding', 'project'
    );
CREATE TRIGGER topics_provider_state_cleanup
    AFTER DELETE ON workspace_v3.topics
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'topic', 'project'
    );
CREATE TRIGGER topic_bindings_provider_state_cleanup
    AFTER DELETE ON workspace_v3.topic_bindings
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'topic_binding', 'project'
    );
CREATE TRIGGER messages_provider_state_cleanup
    AFTER DELETE ON workspace_v3.messages
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'message', 'project'
    );
CREATE TRIGGER message_flags_provider_state_cleanup
    AFTER DELETE ON workspace_v3.message_flags
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'message_flag', 'project'
    );
CREATE TRIGGER message_reactions_provider_state_cleanup
    AFTER DELETE ON workspace_v3.message_reactions
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'message_reaction', 'project'
    );

DELETE FROM workspace_v3.streams AS target
USING m_workspace_streams AS legacy
WHERE legacy.source_name <> 'native'
  AND target.project_id = legacy.project_id
  AND target.uuid = legacy.uuid;
DELETE FROM workspace_v3.users AS target
USING m_workspace_users AS legacy
WHERE target.uuid = legacy.uuid
  AND legacy.source <> 'iam'
  AND NOT EXISTS (
      SELECT 1 FROM workspace_v3.streams AS stream
      WHERE stream.owner_uuid = target.uuid OR stream.direct_user_uuid = target.uuid
  )
  AND NOT EXISTS (
      SELECT 1 FROM workspace_v3.stream_bindings AS binding
      WHERE binding.user_uuid = target.uuid OR binding.who_uuid = target.uuid
  )
  AND NOT EXISTS (
      SELECT 1 FROM workspace_v3.messages AS message
      WHERE message.author_uuid = target.uuid
  );
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0206-Backfill-external-bridge-files-into-Workspace-v3-7d5a7e.py"
        ]

    @property
    def migration_id(self):
        return "4d2649b6-1abd-4eab-95d4-0691c70553f0"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
