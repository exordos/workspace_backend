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


UPGRADE = """
WITH native_user_uuids AS (
    SELECT stream.user_uuid AS uuid
    FROM m_workspace_streams AS stream
    WHERE stream.source_name = 'native'
    UNION
    SELECT stream.direct_user_uuid
    FROM m_workspace_streams AS stream
    WHERE stream.source_name = 'native'
      AND stream.direct_user_uuid IS NOT NULL
    UNION
    SELECT binding.user_uuid
    FROM m_workspace_stream_bindings AS binding
    JOIN m_workspace_streams AS stream ON stream.uuid = binding.stream_uuid
    WHERE stream.source_name = 'native'
    UNION
    SELECT binding.who_uuid
    FROM m_workspace_stream_bindings AS binding
    JOIN m_workspace_streams AS stream ON stream.uuid = binding.stream_uuid
    WHERE stream.source_name = 'native'
    UNION
    SELECT message.user_uuid
    FROM m_workspace_messages AS message
    JOIN m_workspace_streams AS stream ON stream.uuid = message.stream_uuid
    WHERE stream.source_name = 'native'
    UNION
    SELECT reaction.user_uuid
    FROM m_workspace_message_reactions AS reaction
    JOIN m_workspace_messages AS message ON message.uuid = reaction.message_uuid
    WHERE message.source_name = 'native'
    UNION
    SELECT flag.user_uuid
    FROM m_workspace_user_message_flags AS flag
    JOIN m_workspace_messages AS message ON message.uuid = flag.uuid
    WHERE message.source_name = 'native'
    UNION
    SELECT binding.user_uuid
    FROM messenger_user_folder_bindings AS binding
    WHERE binding.rule = 'custom'
      AND EXISTS (
          SELECT 1 FROM m_workspace_streams AS stream
          WHERE stream.project_id = binding.project_id
            AND stream.source_name = 'native'
      )
    UNION
    SELECT file.user_uuid
    FROM m_workspace_files AS file
    LEFT JOIN m_workspace_streams AS stream ON stream.uuid = file.stream_uuid
    WHERE file.stream_uuid IS NULL OR stream.source_name = 'native'
)
INSERT INTO workspace_v3.users (
    uuid, created_at, updated_at, username, source, status,
    first_name, last_name, email, last_ping_at,
    status_emoji, status_text, avatar
)
SELECT legacy.uuid, legacy.created_at, legacy.updated_at,
       COALESCE(NULLIF(legacy.username, ''), legacy.uuid::text),
       legacy.source, legacy.status, legacy.first_name, legacy.last_name,
       legacy.email, legacy.last_ping_at, legacy.status_emoji,
       legacy.status_text,
       COALESCE(
           NULLIF(legacy.avatar, ''),
           'urn:gravatar:' || md5(lower(COALESCE(legacy.email, legacy.username)))
       )
FROM m_workspace_users AS legacy
JOIN native_user_uuids AS selected ON selected.uuid = legacy.uuid
ON CONFLICT (uuid) DO NOTHING;

INSERT INTO workspace_v3.streams (
    uuid, project_id, name, description, owner_uuid, source_name,
    invite_only, announce, direct_user_uuid, private, is_archived,
    private_index, color, created_at, updated_at
)
SELECT legacy.uuid, legacy.project_id, legacy.name, legacy.description,
       legacy.user_uuid, 'native', legacy.invite_only, legacy.announce,
       legacy.direct_user_uuid, legacy.private, legacy.is_archived,
       legacy.private_index,
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
WHERE legacy.source_name = 'native'
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
WHERE stream.source_name = 'native'
ON CONFLICT (project_id, uuid) DO NOTHING;

UPDATE workspace_v3.streams AS target
SET default_topic_uuid = legacy.default_topic_uuid
FROM m_workspace_streams AS legacy
WHERE legacy.source_name = 'native'
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
             AND message.source_name = 'native'
           ORDER BY message.created_at DESC, message.uuid DESC
           LIMIT 1
       ),
       legacy.created_at AT TIME ZONE 'UTC',
       legacy.updated_at AT TIME ZONE 'UTC'
FROM m_workspace_stream_bindings AS legacy
JOIN m_workspace_streams AS stream ON stream.uuid = legacy.stream_uuid
WHERE stream.source_name = 'native'
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
             AND message.source_name = 'native'
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
WHERE stream.source_name = 'native'
ON CONFLICT (project_id, topic_uuid, user_uuid) DO NOTHING;

INSERT INTO workspace_v3.messages (
    uuid, project_id, stream_uuid, topic_uuid, author_uuid, payload,
    source_name, reactions, reaction_users, created_at, updated_at
)
SELECT legacy.uuid, legacy.project_id, legacy.stream_uuid,
       legacy.topic_uuid, legacy.user_uuid, legacy.payload,
       'native', '{}'::jsonb,
       COALESCE(legacy.reaction_users, '{}'::jsonb),
       legacy.created_at AT TIME ZONE 'UTC',
       legacy.updated_at AT TIME ZONE 'UTC'
FROM m_workspace_messages AS legacy
JOIN m_workspace_streams AS stream ON stream.uuid = legacy.stream_uuid
WHERE stream.source_name = 'native'
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
WHERE message.source_name = 'native'
ON CONFLICT (project_id, message_uuid, user_uuid) DO NOTHING;

INSERT INTO workspace_v3.message_reactions (
    uuid, project_id, message_uuid, user_uuid, emoji_name,
    source_name, created_at, updated_at
)
SELECT reaction.uuid, reaction.project_id, reaction.message_uuid,
       reaction.user_uuid, reaction.emoji_name, 'native',
       reaction.created_at, reaction.updated_at
FROM m_workspace_message_reactions AS reaction
JOIN m_workspace_messages AS message ON message.uuid = reaction.message_uuid
JOIN m_workspace_streams AS stream ON stream.uuid = message.stream_uuid
WHERE stream.source_name = 'native'
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
       item.automatic,
       item.created_at AT TIME ZONE 'UTC',
       item.updated_at AT TIME ZONE 'UTC'
FROM messenger_folder_items AS item
JOIN messenger_user_folder_bindings AS binding
  ON binding.project_id = item.project_id
 AND binding.user_uuid = item.user_uuid
 AND binding.folder_uuid = item.folder_uuid
JOIN workspace_v3.stream_bindings AS stream_binding
  ON stream_binding.project_id = item.project_id
 AND stream_binding.stream_uuid = item.stream_uuid
 AND stream_binding.user_uuid = item.user_uuid
WHERE binding.rule = 'custom'
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
WHERE (file.stream_uuid IS NULL OR stream.uuid IS NOT NULL)
ON CONFLICT (project_id, uuid) DO NOTHING;
"""


DOWNGRADE = """
DELETE FROM workspace_v3.files AS target
USING m_workspace_files AS legacy
WHERE target.project_id = legacy.project_id
  AND target.uuid = legacy.uuid;

DELETE FROM workspace_v3.streams AS target
USING m_workspace_streams AS legacy
WHERE legacy.source_name = 'native'
  AND target.project_id = legacy.project_id
  AND target.uuid = legacy.uuid;

DELETE FROM workspace_v3.users AS target
USING m_workspace_users AS legacy
WHERE target.uuid = legacy.uuid
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
        self._depends = ["0192-Index-Workspace-v3-user-message-visibility-6b3971.py"]

    @property
    def migration_id(self):
        return "2ee19938-4e75-4119-8477-e669b058a728"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
