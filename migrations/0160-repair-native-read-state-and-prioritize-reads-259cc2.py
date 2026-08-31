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


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0159-index-Messenger-v2-projection-claim-order-16837b.py"]

    @property
    def migration_id(self):
        return "259cc21a-d775-4d90-98e5-6fde45181e3f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            WITH expected_read AS MATERIALIZED (
                SELECT state.project_id, state.uuid AS state_uuid
                FROM messenger_user_message_states AS state
                JOIN messenger_message_placements AS placement
                  ON placement.project_id = state.project_id
                 AND placement.uuid = state.placement_uuid
                JOIN messenger_streams AS stream
                  ON stream.project_id = placement.project_id
                 AND stream.uuid = placement.stream_uuid
                 AND stream.source_name = 'native'
                JOIN m_workspace_messages AS legacy
                  ON legacy.project_id = placement.project_id
                 AND legacy.uuid = COALESCE(
                        placement.legacy_public_uuid,
                        placement.uuid
                     )
                LEFT JOIN m_workspace_read_state_projects_v1 AS read_project
                  ON read_project.project_id = state.project_id
                LEFT JOIN m_workspace_user_message_flags AS flag
                  ON flag.project_id = state.project_id
                 AND flag.user_uuid = state.user_uuid
                 AND flag.uuid = legacy.uuid
                LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
                  ON chunk.user_uuid = state.user_uuid
                 AND chunk.chunk_number = legacy.ingest_sequence / 4096
                WHERE state.read_at IS NULL
                  AND CASE
                      WHEN read_project.mode IN ('compact', 'rollback') THEN
                          COALESCE(
                              get_bit(
                                  chunk.read_bits,
                                  (legacy.ingest_sequence % 4096)::integer
                              ),
                              0
                          ) = 1
                      ELSE COALESCE(flag.read, FALSE)
                  END
            )
            UPDATE messenger_user_message_states AS state
            SET read_at = NOW(), updated_at = NOW()
            FROM expected_read AS expected
            WHERE state.project_id = expected.project_id
              AND state.uuid = expected.state_uuid;

            WITH snapshots AS MATERIALIZED (
                SELECT target.project_id,
                       target.user_uuid,
                       target.stream_uuid,
                       count(state.uuid) FILTER (
                           WHERE state.read_at IS NULL
                       )::integer AS unread_count,
                       count(state.uuid) FILTER (
                           WHERE state.read_at IS NULL AND CASE
                               WHEN topic_binding.notification_mode = 'mute'
                                   THEN FALSE
                               WHEN topic_binding.notification_mode = 'follow'
                                   THEN TRUE
                               WHEN topic_binding.notification_mode = 'unmute'
                                   THEN state.mentioned
                               WHEN target.notification_mode = 'all_messages'
                                   THEN TRUE
                               WHEN target.notification_mode = 'mentions_only'
                                   THEN state.mentioned
                               ELSE FALSE
                           END
                       )::integer AS active_unread_count,
                       (array_agg(
                           placement.uuid
                           ORDER BY message.created_at DESC,
                                    placement.uuid DESC
                       ))[1] AS last_message_uuid
                FROM messenger_stream_bindings AS target
                JOIN messenger_streams AS stream
                  ON stream.project_id = target.project_id
                 AND stream.uuid = target.stream_uuid
                 AND stream.source_name = 'native'
                LEFT JOIN messenger_message_placements AS placement
                  ON placement.project_id = target.project_id
                 AND placement.stream_uuid = target.stream_uuid
                 AND EXISTS (
                     SELECT 1 FROM messenger_messages AS visible_message
                     WHERE visible_message.project_id = placement.project_id
                       AND visible_message.uuid = placement.message_uuid
                       AND visible_message.deleted_at IS NULL
                 )
                 AND EXISTS (
                     SELECT 1 FROM messenger_topics AS visible_topic
                     WHERE visible_topic.project_id = placement.project_id
                       AND visible_topic.uuid = placement.topic_uuid
                       AND visible_topic.deleted_at IS NULL
                 )
                LEFT JOIN messenger_messages AS message
                  ON message.project_id = placement.project_id
                 AND message.uuid = placement.message_uuid
                 AND message.deleted_at IS NULL
                LEFT JOIN messenger_user_message_states AS state
                  ON state.project_id = placement.project_id
                 AND state.placement_uuid = placement.uuid
                 AND state.user_uuid = target.user_uuid
                 AND state.membership_generation = target.membership_generation
                LEFT JOIN messenger_user_topic_bindings AS topic_binding
                  ON topic_binding.project_id = placement.project_id
                 AND topic_binding.topic_uuid = placement.topic_uuid
                 AND topic_binding.user_uuid = target.user_uuid
                WHERE target.active
                GROUP BY target.project_id, target.user_uuid,
                         target.stream_uuid
            )
            UPDATE messenger_stream_bindings AS binding
            SET unread_count = snapshot.unread_count,
                active_unread_count = snapshot.active_unread_count,
                passive_unread_count =
                    snapshot.unread_count - snapshot.active_unread_count,
                last_message_uuid = snapshot.last_message_uuid,
                updated_at = NOW()
            FROM snapshots AS snapshot
            WHERE binding.project_id = snapshot.project_id
              AND binding.user_uuid = snapshot.user_uuid
              AND binding.stream_uuid = snapshot.stream_uuid;

            WITH snapshots AS MATERIALIZED (
                SELECT target.project_id,
                       target.user_uuid,
                       target.topic_uuid,
                       count(state.uuid) FILTER (
                           WHERE state.read_at IS NULL
                       )::integer AS unread_count,
                       count(state.uuid) FILTER (
                           WHERE state.read_at IS NULL AND CASE
                               WHEN target.notification_mode = 'mute' THEN FALSE
                               WHEN target.notification_mode = 'follow' THEN TRUE
                               WHEN target.notification_mode = 'unmute'
                                   THEN state.mentioned
                               WHEN stream_binding.notification_mode =
                                    'all_messages' THEN TRUE
                               WHEN stream_binding.notification_mode =
                                    'mentions_only' THEN state.mentioned
                               ELSE FALSE
                           END
                       )::integer AS active_unread_count,
                       (array_agg(
                           placement.uuid
                           ORDER BY message.created_at DESC,
                                    placement.uuid DESC
                       ))[1] AS last_message_uuid
                FROM messenger_user_topic_bindings AS target
                JOIN messenger_topics AS topic
                  ON topic.project_id = target.project_id
                 AND topic.uuid = target.topic_uuid
                 AND topic.deleted_at IS NULL
                JOIN messenger_streams AS stream
                  ON stream.project_id = topic.project_id
                 AND stream.uuid = topic.stream_uuid
                 AND stream.source_name = 'native'
                JOIN messenger_stream_bindings AS stream_binding
                  ON stream_binding.project_id = topic.project_id
                 AND stream_binding.stream_uuid = topic.stream_uuid
                 AND stream_binding.user_uuid = target.user_uuid
                 AND stream_binding.active
                LEFT JOIN messenger_message_placements AS placement
                  ON placement.project_id = target.project_id
                 AND placement.topic_uuid = target.topic_uuid
                 AND EXISTS (
                     SELECT 1 FROM messenger_messages AS visible_message
                     WHERE visible_message.project_id = placement.project_id
                       AND visible_message.uuid = placement.message_uuid
                       AND visible_message.deleted_at IS NULL
                 )
                LEFT JOIN messenger_messages AS message
                  ON message.project_id = placement.project_id
                 AND message.uuid = placement.message_uuid
                 AND message.deleted_at IS NULL
                LEFT JOIN messenger_user_message_states AS state
                  ON state.project_id = placement.project_id
                 AND state.placement_uuid = placement.uuid
                 AND state.user_uuid = target.user_uuid
                 AND state.membership_generation =
                     stream_binding.membership_generation
                GROUP BY target.project_id, target.user_uuid,
                         target.topic_uuid
            )
            UPDATE messenger_user_topic_bindings AS binding
            SET unread_count = snapshot.unread_count,
                active_unread_count = snapshot.active_unread_count,
                passive_unread_count =
                    snapshot.unread_count - snapshot.active_unread_count,
                last_message_uuid = snapshot.last_message_uuid,
                updated_at = NOW()
            FROM snapshots AS snapshot
            WHERE binding.project_id = snapshot.project_id
              AND binding.user_uuid = snapshot.user_uuid
              AND binding.topic_uuid = snapshot.topic_uuid;

            WITH snapshots AS MATERIALIZED (
                SELECT target.project_id, target.user_uuid, target.folder_uuid,
                       COALESCE(sum(
                           stream_binding.active_unread_count
                       ), 0)::integer AS unread_count,
                       COALESCE(
                           jsonb_agg(
                               jsonb_build_object(
                                   'uuid', item.uuid,
                                   CASE WHEN target.rule = 'custom'
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
                                        item.created_at,
                                        item.uuid
                           ) FILTER (
                               WHERE item.uuid IS NOT NULL
                                 AND stream_binding.user_uuid IS NOT NULL
                                 AND visible_stream.uuid IS NOT NULL
                           ),
                           '[]'::jsonb
                       ) AS folder_items_snapshot
                FROM messenger_user_folder_bindings AS target
                LEFT JOIN messenger_folder_items AS item
                  ON item.project_id = target.project_id
                 AND item.user_uuid = target.user_uuid
                 AND item.folder_uuid = target.folder_uuid
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
                GROUP BY target.project_id, target.user_uuid,
                         target.folder_uuid, target.rule
            )
            UPDATE messenger_user_folder_bindings AS binding
            SET unread_count = snapshot.unread_count,
                folder_items_snapshot = snapshot.folder_items_snapshot,
                snapshot_version = binding.snapshot_version + 1,
                snapshot_updated_at = NOW(),
                updated_at = NOW()
            FROM snapshots AS snapshot
            WHERE binding.project_id = snapshot.project_id
              AND binding.user_uuid = snapshot.user_uuid
              AND binding.folder_uuid = snapshot.folder_uuid;

            DO $native_read_guard$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM messenger_user_message_states AS state
                    JOIN messenger_message_placements AS placement
                      ON placement.project_id = state.project_id
                     AND placement.uuid = state.placement_uuid
                    JOIN messenger_streams AS stream
                      ON stream.project_id = placement.project_id
                     AND stream.uuid = placement.stream_uuid
                     AND stream.source_name = 'native'
                    JOIN m_workspace_messages AS legacy
                      ON legacy.project_id = placement.project_id
                     AND legacy.uuid = COALESCE(
                            placement.legacy_public_uuid,
                            placement.uuid
                         )
                    LEFT JOIN m_workspace_read_state_projects_v1 AS read_project
                      ON read_project.project_id = state.project_id
                    LEFT JOIN m_workspace_user_message_flags AS flag
                      ON flag.project_id = state.project_id
                     AND flag.user_uuid = state.user_uuid
                     AND flag.uuid = legacy.uuid
                    LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
                      ON chunk.user_uuid = state.user_uuid
                     AND chunk.chunk_number = legacy.ingest_sequence / 4096
                    WHERE state.read_at IS NULL
                      AND CASE
                          WHEN read_project.mode IN ('compact', 'rollback') THEN
                              COALESCE(
                                  get_bit(
                                      chunk.read_bits,
                                      (legacy.ingest_sequence % 4096)::integer
                                  ),
                                  0
                              ) = 1
                          ELSE COALESCE(flag.read, FALSE)
                      END
                ) THEN
                    RAISE EXCEPTION
                        'Native compact reads were not preserved in Messenger v2';
                END IF;
            END;
            $native_read_guard$;
            """
        )
        session.execute(
            """
            CREATE INDEX IF NOT EXISTS
                messenger_projection_tasks_interactive_read_idx
                ON messenger_projection_tasks (created_at, uuid)
                WHERE status NOT IN ('completed', 'dead_letter')
                  AND task_kind = 'read_counters'
                  AND payload->>'source_kind' IN (
                      'message.read', 'messages.read',
                      'stream.read', 'topic.read'
                  )
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS messenger_projection_tasks_interactive_read_idx
            """
        )


migration_step = MigrationStep()
