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


UPGRADE_SQL = r"""
CREATE TEMP TABLE messenger_v2_provider_group_streams (
    project_id uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    provider_managed_name boolean NOT NULL,
    PRIMARY KEY (project_id, stream_uuid)
) ON COMMIT DROP;

INSERT INTO messenger_v2_provider_group_streams (
    project_id, stream_uuid, provider_managed_name
)
SELECT stream.project_id, stream.uuid,
       bool_or(
           chat.display_name = stream.name
           OR COALESCE(
               stream.provider->>'default_display_name' = stream.name,
               FALSE
           )
           OR COALESCE(legacy_label.display_name = stream.name, FALSE)
       )
FROM messenger_streams AS stream
JOIN m_external_chats_v2 AS chat
  ON chat.project_id = stream.project_id
 AND chat.projection_stream_uuid = stream.uuid
 AND chat.selected
 AND chat.source->>'chat_type' = 'group'
 AND jsonb_array_length(
         COALESCE(chat.source->'participants', '[]'::jsonb)
     ) > 1
LEFT JOIN LATERAL (
    SELECT STRING_AGG(
               COALESCE(
                   NULLIF(
                       TRIM(
                           COALESCE(participant_user.first_name, '') || ' ' ||
                           COALESCE(participant_user.last_name, '')
                       ),
                       ''
                   ),
                   NULLIF(participant_user.username, ''),
                   participant.value->>'display_name'
               ),
               ', ' ORDER BY participant.position
           ) AS display_name
    FROM jsonb_array_elements(
        COALESCE(chat.source->'participants', '[]'::jsonb)
    ) WITH ORDINALITY AS participant(value, position)
    LEFT JOIN m_workspace_users AS participant_user
      ON participant_user.uuid =
            (participant.value->>'identity_uuid')::uuid
    WHERE participant.value->>'identity_uuid' <>
          chat.owner_user_uuid::text
) AS legacy_label ON TRUE
WHERE stream.deleted_at IS NULL
GROUP BY stream.project_id, stream.uuid;

-- Provider group DMs are membership-scoped channels in Workspace.  Preserve
-- an existing local rename, but remember provider-generated names so public
-- views can render the Zulip-style participant list for each viewer.
UPDATE messenger_streams AS stream
SET private = FALSE,
    invite_only = TRUE,
    direct_user_uuid = NULL,
    private_index = NULL,
    provider = CASE
        WHEN target.provider_managed_name THEN jsonb_set(
            COALESCE(stream.provider, '{}'::jsonb),
            '{default_display_name}',
            to_jsonb(stream.name),
            TRUE
        )
        ELSE COALESCE(stream.provider, '{}'::jsonb) - 'default_display_name'
    END,
    updated_at = NOW()
FROM messenger_v2_provider_group_streams AS target
WHERE stream.project_id = target.project_id
  AND stream.uuid = target.stream_uuid;

DELETE FROM messenger_folder_items AS item
USING messenger_v2_provider_group_streams AS target
WHERE item.project_id = target.project_id
  AND item.stream_uuid = target.stream_uuid
  AND item.automatic
  AND item.folder_uuid =
      '00000000-0000-0000-0000-000000000001'::uuid;

UPDATE messenger_folder_items AS item
SET chat_type = 'stream', updated_at = NOW()
FROM messenger_v2_provider_group_streams AS target
WHERE item.project_id = target.project_id
  AND item.stream_uuid = target.stream_uuid
  AND item.chat_type IS DISTINCT FROM 'stream';

INSERT INTO messenger_folder_items (
    uuid, project_id, user_uuid, folder_uuid, stream_uuid,
    chat_type, automatic, created_at, updated_at
)
SELECT ('22' || substr(target.stream_uuid::text, 3))::uuid,
       target.project_id, binding.user_uuid,
       '00000000-0000-0000-0000-000000000002'::uuid,
       target.stream_uuid, 'stream', TRUE, NOW(), NOW()
FROM messenger_v2_provider_group_streams AS target
JOIN messenger_stream_bindings AS binding
  ON binding.project_id = target.project_id
 AND binding.stream_uuid = target.stream_uuid
 AND binding.active
ON CONFLICT (project_id, user_uuid, folder_uuid, stream_uuid) DO UPDATE SET
    chat_type = 'stream', automatic = TRUE, updated_at = NOW();

WITH affected_users AS MATERIALIZED (
    SELECT DISTINCT target.project_id, binding.user_uuid
    FROM messenger_v2_provider_group_streams AS target
    JOIN messenger_stream_bindings AS binding
      ON binding.project_id = target.project_id
     AND binding.stream_uuid = target.stream_uuid
     AND binding.active
), snapshots AS MATERIALIZED (
    SELECT folder_binding.project_id, folder_binding.user_uuid,
           folder_binding.folder_uuid,
           COALESCE(sum(stream_binding.active_unread_count), 0)::integer
               AS unread_count,
           COALESCE(
               jsonb_agg(
                   jsonb_build_object(
                       'uuid', item.uuid,
                       CASE WHEN folder_binding.rule = 'custom'
                            THEN 'folder_uuid' ELSE 'folder' END,
                       item.folder_uuid,
                       'project_id', item.project_id,
                       'user_uuid', item.user_uuid,
                       'stream_uuid', item.stream_uuid,
                       'order_index', item.order_index,
                       'pinned_at', item.pinned_at::timestamp,
                       'chat_type', item.chat_type,
                       'unread_count', stream_binding.unread_count,
                       'active_unread_count',
                           stream_binding.active_unread_count,
                       'passive_unread_count',
                           stream_binding.passive_unread_count,
                       'created_at', item.created_at,
                       'updated_at', item.updated_at
                   )
                   ORDER BY item.pinned_at DESC NULLS LAST,
                            item.order_index ASC NULLS LAST,
                            item.created_at, item.uuid
               ) FILTER (
                   WHERE item.uuid IS NOT NULL
                     AND stream_binding.user_uuid IS NOT NULL
                     AND visible_stream.uuid IS NOT NULL
               ),
               '[]'::jsonb
           ) AS folder_items_snapshot
    FROM messenger_user_folder_bindings AS folder_binding
    JOIN affected_users AS affected
      ON affected.project_id = folder_binding.project_id
     AND affected.user_uuid = folder_binding.user_uuid
    LEFT JOIN messenger_folder_items AS item
      ON item.project_id = folder_binding.project_id
     AND item.user_uuid = folder_binding.user_uuid
     AND item.folder_uuid = folder_binding.folder_uuid
    LEFT JOIN messenger_stream_bindings AS stream_binding
      ON stream_binding.project_id = item.project_id
     AND stream_binding.user_uuid = item.user_uuid
     AND stream_binding.stream_uuid = item.stream_uuid
     AND stream_binding.active
    LEFT JOIN messenger_streams AS visible_stream
      ON visible_stream.project_id = item.project_id
     AND visible_stream.uuid = item.stream_uuid
     AND NOT visible_stream.is_archived
     AND visible_stream.deleted_at IS NULL
    GROUP BY folder_binding.project_id, folder_binding.user_uuid,
             folder_binding.folder_uuid, folder_binding.rule
)
UPDATE messenger_user_folder_bindings AS binding
SET unread_count = snapshot.unread_count,
    folder_items_snapshot = snapshot.folder_items_snapshot,
    snapshot_version = binding.snapshot_version + 1,
    snapshot_updated_at = NOW(), updated_at = NOW()
FROM snapshots AS snapshot
WHERE binding.project_id = snapshot.project_id
  AND binding.user_uuid = snapshot.user_uuid
  AND binding.folder_uuid = snapshot.folder_uuid;

CREATE OR REPLACE VIEW m_workspace_user_streams AS
SELECT
    stream.uuid,
    CASE
        WHEN external_label.chat_type = 'group'
             AND stream.provider_metadata ? 'default_display_name'
             AND stream.provider_metadata->>'default_display_name' = stream.name
            THEN COALESCE(external_label.display_name, stream.name)
        WHEN stream.private AND external_label.chat_type = 'personal' THEN
            COALESCE(external_label.display_name, stream.name)
        WHEN stream.private THEN COALESCE(
            NULLIF(
                TRIM(
                    COALESCE(peer.first_name, '') || ' ' ||
                    COALESCE(peer.last_name, '')
                ),
                ''
            ),
            peer.username,
            stream.name
        )
        ELSE stream.name
    END::varchar AS name,
    stream.description,
    stream.project_id,
    stream.source_name,
    stream.source,
    stream.user_uuid AS owner,
    binding.user_uuid,
    binding.role,
    COALESCE(unread.unread_count, 0) AS unread_count,
    stream.invite_only,
    stream.announce,
    stream.private,
    stream.created_at,
    stream.updated_at,
    CASE
        WHEN stream.private AND external_label.chat_type = 'personal'
            THEN external_label.peer_uuid
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
             AND stream.user_uuid = binding.user_uuid
            THEN stream.direct_user_uuid
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
            THEN stream.user_uuid
        ELSE NULL
    END AS direct_user_uuid,
    stream.private_index,
    stream.is_archived,
    binding.notification_mode,
    stream.color,
    last_message.uuid AS last_message_uuid,
    stream.default_topic_uuid,
    COALESCE(unread.active_unread_count, 0) AS active_unread_count,
    COALESCE(unread.passive_unread_count, 0) AS passive_unread_count
FROM m_workspace_streams AS stream
JOIN m_workspace_stream_bindings AS binding
  ON binding.stream_uuid = stream.uuid
 AND binding.project_id = stream.project_id
LEFT JOIN m_unread_user_messages AS unread
  ON unread.uuid = stream.uuid
 AND unread.user_uuid = binding.user_uuid
 AND unread.project_id = stream.project_id
LEFT JOIN LATERAL (
    SELECT message.uuid
    FROM m_workspace_messages AS message
    WHERE message.project_id = stream.project_id
      AND message.stream_uuid = stream.uuid
    ORDER BY message.created_at DESC, message.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN LATERAL (
    SELECT
        chat.source->>'chat_type' AS chat_type,
        CASE
            WHEN chat.source->>'chat_type' IN ('personal', 'direct')
                 AND names.peer_count = 1
                THEN names.peer_uuid
            ELSE NULL
        END AS peer_uuid,
        NULLIF(names.display_name, '') AS display_name
    FROM m_external_chats_v2 AS chat
    CROSS JOIN LATERAL (
        SELECT
            COUNT(*) AS peer_count,
            (ARRAY_AGG(
                (participant.value->>'identity_uuid')::uuid
                ORDER BY participant.position
            ))[1] AS peer_uuid,
            STRING_AGG(
                COALESCE(
                    NULLIF(participant.value->>'display_name', ''),
                    NULLIF(
                        TRIM(
                            COALESCE(participant_user.first_name, '') || ' ' ||
                            COALESCE(participant_user.last_name, '')
                        ),
                        ''
                    ),
                    NULLIF(participant_user.username, '')
                ),
                ', ' ORDER BY participant.position
            ) AS display_name
        FROM jsonb_array_elements(
            COALESCE(chat.source->'participants', '[]'::jsonb)
        ) WITH ORDINALITY AS participant(value, position)
        LEFT JOIN m_workspace_users AS participant_user
          ON participant_user.uuid =
                (participant.value->>'identity_uuid')::uuid
        WHERE participant.value->>'identity_uuid' <>
              binding.user_uuid::text
    ) AS names
    WHERE chat.project_id = stream.project_id
      AND chat.projection_stream_uuid = stream.uuid
      AND chat.selected
      AND (
          chat.owner_user_uuid = binding.user_uuid
          OR EXISTS (
              SELECT 1
              FROM jsonb_array_elements(
                  COALESCE(chat.source->'participants', '[]'::jsonb)
              ) AS viewer(value)
              WHERE viewer.value->>'identity_uuid' = binding.user_uuid::text
          )
      )
    ORDER BY
        (chat.owner_user_uuid = binding.user_uuid) DESC,
        chat.updated_at DESC,
        chat.uuid
    LIMIT 1
) AS external_label
  ON stream.source_name <> 'native'
LEFT JOIN m_workspace_users AS peer
  ON peer.uuid = CASE
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
             AND stream.user_uuid = binding.user_uuid
            THEN stream.direct_user_uuid
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
            THEN stream.user_uuid
        ELSE NULL
    END
LEFT JOIN m_confirmed_external_stream_access AS access
  ON access.project_id = stream.project_id
 AND access.user_uuid = binding.user_uuid
 AND access.stream_uuid = stream.uuid
WHERE stream.source_name = 'native' OR access.user_uuid IS NOT NULL;

CREATE OR REPLACE VIEW messenger_api_user_streams_v1 AS
SELECT
    stream.uuid,
    binding.user_uuid,
    CASE
        WHEN external_label.chat_type = 'group'
             AND stream.provider ? 'default_display_name'
             AND stream.provider->>'default_display_name' = stream.name
            THEN COALESCE(external_label.display_name, stream.name)
        WHEN stream.private AND external_label.chat_type = 'personal' THEN
            COALESCE(external_label.display_name, stream.name)
        WHEN stream.private THEN COALESCE(
            NULLIF(
                TRIM(
                    COALESCE(peer.first_name, '') || ' ' ||
                    COALESCE(peer.last_name, '')
                ),
                ''
            ),
            peer.username,
            stream.name
        )
        ELSE stream.name
    END::varchar(255) AS name,
    stream.description,
    stream.project_id,
    stream.created_at,
    stream.updated_at,
    stream.owner_uuid AS owner,
    binding.role,
    binding.notification_mode,
    binding.unread_count,
    binding.active_unread_count,
    binding.passive_unread_count,
    stream.source_name,
    stream.source,
    stream.invite_only,
    stream.announce,
    stream.private,
    stream.is_archived,
    CASE
        WHEN stream.private AND external_label.chat_type = 'personal'
            THEN external_label.peer_uuid
        WHEN stream.direct_user_uuid IS NULL THEN NULL
        WHEN binding.user_uuid = stream.owner_uuid THEN stream.direct_user_uuid
        ELSE stream.owner_uuid
    END AS direct_user_uuid,
    stream.private_index,
    stream.color,
    binding.last_message_uuid,
    stream.default_topic_uuid,
    stream.provider,
    stream.delivery,
    stream.deleted_at,
    stream.deleted_at IS NULL AS visible
FROM messenger_stream_bindings AS binding
JOIN messenger_streams AS stream
  ON stream.project_id = binding.project_id
 AND stream.uuid = binding.stream_uuid
LEFT JOIN LATERAL (
    SELECT
        chat.source->>'chat_type' AS chat_type,
        CASE
            WHEN chat.source->>'chat_type' IN ('personal', 'direct')
                 AND names.peer_count = 1
                THEN names.peer_uuid
            ELSE NULL
        END AS peer_uuid,
        NULLIF(names.display_name, '') AS display_name
    FROM m_external_chats_v2 AS chat
    CROSS JOIN LATERAL (
        SELECT
            COUNT(*) AS peer_count,
            (ARRAY_AGG(
                (participant.value->>'identity_uuid')::uuid
                ORDER BY participant.position
            ))[1] AS peer_uuid,
            STRING_AGG(
                COALESCE(
                    NULLIF(participant.value->>'display_name', ''),
                    NULLIF(
                        TRIM(
                            COALESCE(participant_user.first_name, '') || ' ' ||
                            COALESCE(participant_user.last_name, '')
                        ),
                        ''
                    ),
                    NULLIF(participant_user.username, '')
                ),
                ', ' ORDER BY participant.position
            ) AS display_name
        FROM jsonb_array_elements(
            COALESCE(chat.source->'participants', '[]'::jsonb)
        ) WITH ORDINALITY AS participant(value, position)
        LEFT JOIN m_workspace_users AS participant_user
          ON participant_user.uuid =
                (participant.value->>'identity_uuid')::uuid
        WHERE participant.value->>'identity_uuid' <>
              binding.user_uuid::text
    ) AS names
    WHERE chat.project_id = stream.project_id
      AND chat.projection_stream_uuid = stream.uuid
      AND chat.selected
      AND (
          chat.owner_user_uuid = binding.user_uuid
          OR EXISTS (
              SELECT 1
              FROM jsonb_array_elements(
                  COALESCE(chat.source->'participants', '[]'::jsonb)
              ) AS viewer(value)
              WHERE viewer.value->>'identity_uuid' = binding.user_uuid::text
          )
      )
    ORDER BY
        (chat.owner_user_uuid = binding.user_uuid) DESC,
        chat.updated_at DESC,
        chat.uuid
    LIMIT 1
) AS external_label
  ON stream.source_name <> 'native'
LEFT JOIN m_workspace_users AS peer
  ON peer.uuid = CASE
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
             AND stream.owner_uuid = binding.user_uuid
            THEN stream.direct_user_uuid
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
            THEN stream.owner_uuid
        ELSE NULL
    END
WHERE binding.active;
"""


DOWNGRADE_SQL = r"""
CREATE OR REPLACE VIEW m_workspace_user_streams AS
SELECT
    stream.uuid,
    CASE WHEN stream.private THEN
        COALESCE(
            NULLIF(
                TRIM(
                    COALESCE(peer.first_name, '') || ' ' ||
                    COALESCE(peer.last_name, '')
                ),
                ''
            ),
            peer.username,
            stream.name
        )
    ELSE stream.name END AS name,
    stream.description,
    stream.project_id,
    stream.source_name,
    stream.source,
    stream.user_uuid AS owner,
    binding.user_uuid,
    binding.role,
    COALESCE(unread.unread_count, 0) AS unread_count,
    stream.invite_only,
    stream.announce,
    stream.private,
    stream.created_at,
    stream.updated_at,
    CASE
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
             AND stream.user_uuid = binding.user_uuid
            THEN stream.direct_user_uuid
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
            THEN stream.user_uuid
        ELSE NULL
    END AS direct_user_uuid,
    stream.private_index,
    stream.is_archived,
    binding.notification_mode,
    stream.color,
    last_message.uuid AS last_message_uuid,
    stream.default_topic_uuid,
    COALESCE(unread.active_unread_count, 0) AS active_unread_count,
    COALESCE(unread.passive_unread_count, 0) AS passive_unread_count
FROM m_workspace_streams AS stream
JOIN m_workspace_stream_bindings AS binding
  ON binding.stream_uuid = stream.uuid
 AND binding.project_id = stream.project_id
LEFT JOIN m_unread_user_messages AS unread
  ON unread.uuid = stream.uuid
 AND unread.user_uuid = binding.user_uuid
 AND unread.project_id = stream.project_id
LEFT JOIN LATERAL (
    SELECT message.uuid
    FROM m_workspace_messages AS message
    WHERE message.project_id = stream.project_id
      AND message.stream_uuid = stream.uuid
    ORDER BY message.created_at DESC, message.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN m_workspace_users AS peer
  ON peer.uuid = CASE
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
             AND stream.user_uuid = binding.user_uuid
            THEN stream.direct_user_uuid
        WHEN stream.private AND stream.direct_user_uuid IS NOT NULL
            THEN stream.user_uuid
        WHEN stream.private AND stream.user_uuid <> binding.user_uuid
            THEN stream.user_uuid
        ELSE NULL
    END
LEFT JOIN m_confirmed_external_stream_access AS access
  ON access.project_id = stream.project_id
 AND access.user_uuid = binding.user_uuid
 AND access.stream_uuid = stream.uuid
WHERE stream.source_name = 'native' OR access.user_uuid IS NOT NULL;

CREATE OR REPLACE VIEW messenger_api_user_streams_v1 AS
SELECT
    stream.uuid,
    binding.user_uuid,
    stream.name,
    stream.description,
    stream.project_id,
    stream.created_at,
    stream.updated_at,
    stream.owner_uuid AS owner,
    binding.role,
    binding.notification_mode,
    binding.unread_count,
    binding.active_unread_count,
    binding.passive_unread_count,
    stream.source_name,
    stream.source,
    stream.invite_only,
    stream.announce,
    stream.private,
    stream.is_archived,
    CASE
        WHEN stream.direct_user_uuid IS NULL THEN NULL
        WHEN binding.user_uuid = stream.owner_uuid THEN stream.direct_user_uuid
        ELSE stream.owner_uuid
    END AS direct_user_uuid,
    stream.private_index,
    stream.color,
    binding.last_message_uuid,
    stream.default_topic_uuid,
    stream.provider,
    stream.delivery,
    stream.deleted_at,
    stream.deleted_at IS NULL AS visible
FROM messenger_stream_bindings AS binding
JOIN messenger_streams AS stream
  ON stream.project_id = binding.project_id
 AND stream.uuid = binding.stream_uuid
WHERE binding.active;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0163-repair-duplicate-provider-projections-867945.py"]

    @property
    def migration_id(self):
        return "f8bd035a-0e32-46ee-aa57-7f9ff1599640"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
