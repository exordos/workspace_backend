# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Batch projections for the clean Workspace v3 storage model.

Writers only mutate normalized facts. Database triggers append durable tasks,
and this worker turns those facts into read-optimized reaction snapshots,
per-user unread counters, and immutable event rows in one transaction.
"""

import collections.abc
import datetime
import hashlib
import json
import logging
import time
import typing
import uuid as sys_uuid

from workspace.messenger_api.api import v3_store
from workspace.workspace_v3 import constants

LOG = logging.getLogger(__name__)
DEFAULT_BATCH_SIZE = 1000
DEFAULT_LEASE_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_REACTION_USER_LIMIT = 100
DEFAULT_EVENT_RETENTION_HOURS = 72
PROVIDER_BACKFILL_QUIET_SECONDS = 300
AUDIENCE_NAMESPACE = sys_uuid.UUID("4a72ce87-e8a3-4f58-9bd7-b8c1d69c19b2")
ALL_CHATS_FOLDER_UUID = constants.ALL_CHATS_FOLDER_UUID
DIRECT_FOLDER_UUID = constants.DIRECT_FOLDER_UUID
STREAMS_FOLDER_UUID = constants.STREAMS_FOLDER_UUID
SYSTEM_FOLDERS = constants.SYSTEM_FOLDERS

TASK_COLUMNS = (
    "uuid",
    "project_id",
    "task_type",
    "scope_type",
    "scope_uuid",
    "user_uuid",
    "payload",
    "attempts",
    "created_at",
)


def _mapping(row: typing.Any, columns: tuple[str, ...]) -> dict[str, typing.Any]:
    if isinstance(row, collections.abc.Mapping):
        return {column: row[column] for column in columns}
    return dict(zip(columns, row, strict=True))


def _mappings(
    rows: typing.Iterable[typing.Any], columns: tuple[str, ...]
) -> list[dict[str, typing.Any]]:
    return [_mapping(row, columns) for row in rows]


def _uuid(value: object) -> sys_uuid.UUID:
    return sys_uuid.UUID(str(value))


def _payload(value: object) -> dict[str, typing.Any]:
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise TypeError("Projection task payload must be an object")
        return parsed
    if isinstance(value, dict):
        return value
    return dict(typing.cast(collections.abc.Mapping[str, typing.Any], value))


def _operations(payload: dict[str, typing.Any]) -> list[dict[str, typing.Any]]:
    values = payload.get("operations")
    if values is None:
        return [payload]
    if not isinstance(values, list):
        raise TypeError("Projection task operations must be a list")
    return [
        dict(typing.cast(collections.abc.Mapping[str, typing.Any], value))
        for value in values
    ]


def _claim_tasks(
    session: typing.Any,
    worker_id: str,
    batch_size: int,
    lease_seconds: int,
    max_attempts: int,
) -> list[dict[str, typing.Any]]:
    session.execute(
        """
        UPDATE workspace_v3.projection_tasks
        SET status = 'dead_letter', lease_owner = NULL,
            lease_expires_at = NULL, updated_at = clock_timestamp()
        WHERE attempts >= %s
          AND (
                status = 'failed'
                OR (
                    status = 'running'
                    AND lease_expires_at <= clock_timestamp()
                )
              )
        """,
        (max_attempts,),
    )
    result = session.execute(
        """
        WITH candidates AS MATERIALIZED (
            SELECT task.project_id, task.uuid
            FROM workspace_v3.projection_tasks AS task
            WHERE task.attempts < %s
              AND (
                    task.status IN ('pending', 'failed')
                    OR (
                        task.status = 'running'
                        AND task.lease_expires_at <= clock_timestamp()
                    )
                  )
              AND (
                    task.next_retry_at IS NULL
                    OR task.next_retry_at <= clock_timestamp()
                  )
              AND NOT (
                    task.task_type = 'read_counters'
                    AND task.payload IN (
                        '{"emit_message_events": false}'::jsonb,
                        '{"emit_message_event": false}'::jsonb
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM workspace_v3.provider_consumers AS provider
                        WHERE provider.project_id = task.project_id
                          AND provider.updated_at > clock_timestamp()
                              - make_interval(secs => %s)
                    )
                  )
            ORDER BY task.created_at, task.user_uuid NULLS FIRST,
                     task.scope_uuid, task.uuid
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE workspace_v3.projection_tasks AS task
        SET status = 'running', lease_owner = %s,
            lease_expires_at = clock_timestamp() + make_interval(secs => %s),
            attempts = task.attempts + 1,
            updated_at = clock_timestamp()
        FROM candidates
        WHERE task.project_id = candidates.project_id
          AND task.uuid = candidates.uuid
        RETURNING task.uuid, task.project_id, task.task_type,
                  task.scope_type, task.scope_uuid, task.user_uuid,
                  task.payload, task.attempts, task.created_at
        """,
        (
            max_attempts,
            PROVIDER_BACKFILL_QUIET_SECONDS,
            batch_size,
            worker_id,
            lease_seconds,
        ),
    )
    return _mappings(result.fetchall(), TASK_COLUMNS)


def _unique_scopes(
    tasks: typing.Iterable[dict[str, typing.Any]],
    task_type: str,
    scope_type: str,
) -> list[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID | None]]:
    values = {
        (
            _uuid(task["project_id"]),
            _uuid(task["scope_uuid"]),
            None if task["user_uuid"] is None else _uuid(task["user_uuid"]),
        )
        for task in tasks
        if task["task_type"] == task_type and task["scope_type"] == scope_type
    }
    return sorted(values, key=lambda value: tuple(str(item) for item in value))


def _update_reaction_snapshots(
    session: typing.Any,
    scopes: list[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID | None]],
    reaction_user_limit: int,
) -> list[dict[str, typing.Any]]:
    if not scopes:
        return []
    result = session.execute(
        """
        WITH targets(project_id, message_uuid) AS MATERIALIZED (
            SELECT * FROM unnest(%s::uuid[], %s::uuid[])
        ),
        grouped AS (
            SELECT reaction.project_id, reaction.message_uuid,
                   reaction.emoji_name, count(*)::integer AS reaction_count,
                   CASE WHEN count(*) <= %s THEN
                       jsonb_agg(
                           reaction.user_uuid::text
                           ORDER BY reaction.created_at, reaction.uuid
                       )
                   END AS reaction_users
            FROM workspace_v3.message_reactions AS reaction
            JOIN targets AS target
              ON target.project_id = reaction.project_id
             AND target.message_uuid = reaction.message_uuid
            GROUP BY reaction.project_id, reaction.message_uuid,
                     reaction.emoji_name
        ),
        snapshots AS (
            SELECT target.project_id, target.message_uuid,
                   COALESCE(
                       jsonb_object_agg(
                           grouped.emoji_name, grouped.reaction_count
                       ) FILTER (WHERE grouped.emoji_name IS NOT NULL),
                       '{}'::jsonb
                   ) AS reactions,
                   COALESCE(
                       jsonb_object_agg(
                           grouped.emoji_name, grouped.reaction_users
                       ) FILTER (
                           WHERE grouped.emoji_name IS NOT NULL
                             AND grouped.reaction_users IS NOT NULL
                       ),
                       '{}'::jsonb
                   ) AS reaction_users
            FROM targets AS target
            LEFT JOIN grouped
              ON grouped.project_id = target.project_id
             AND grouped.message_uuid = target.message_uuid
            GROUP BY target.project_id, target.message_uuid
        )
        UPDATE workspace_v3.messages AS message
        SET reactions = snapshot.reactions,
            reaction_users = snapshot.reaction_users,
            updated_at = clock_timestamp()
        FROM snapshots AS snapshot
        WHERE message.project_id = snapshot.project_id
          AND message.uuid = snapshot.message_uuid
          AND (
                message.reactions IS DISTINCT FROM snapshot.reactions
                OR message.reaction_users IS DISTINCT FROM snapshot.reaction_users
              )
        RETURNING message.project_id, message.uuid, message.stream_uuid,
                  message.topic_uuid, message.author_uuid, message.payload,
                  message.source_name, message.reactions,
                  message.reaction_users, message.created_at, message.updated_at
        """,
        (
            [scope[0] for scope in scopes],
            [scope[1] for scope in scopes],
            reaction_user_limit,
        ),
    )
    columns = (
        "project_id",
        "uuid",
        "stream_uuid",
        "topic_uuid",
        "author_uuid",
        "payload",
        "source_name",
        "reactions",
        "reaction_users",
        "created_at",
        "updated_at",
    )
    return _mappings(result.fetchall(), columns)


def _update_stream_counters(
    session: typing.Any,
    scopes: list[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID | None]],
) -> list[dict[str, typing.Any]]:
    if not scopes:
        return []
    result = session.execute(
        """
        WITH targets(project_id, stream_uuid, user_uuid) AS MATERIALIZED (
            SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[])
        ),
        snapshots AS (
            SELECT target.project_id, target.stream_uuid, target.user_uuid,
                   count(message.uuid)::integer AS unread_count,
                   count(message.uuid) FILTER (
                       WHERE CASE
                           WHEN topic_binding.notification_mode = 'mute'
                               THEN false
                           WHEN topic_binding.notification_mode = 'follow'
                               THEN true
                           WHEN topic_binding.notification_mode = 'unmute'
                               THEN flag.mentioned
                           WHEN stream_binding.notification_mode = 'all_messages'
                               THEN true
                           WHEN stream_binding.notification_mode = 'mentions_only'
                               THEN flag.mentioned
                           ELSE false
                       END
                   )::integer AS active_unread_count,
                   (
                       SELECT candidate.uuid
                       FROM workspace_v3.messages AS candidate
                       JOIN workspace_v3.message_flags AS candidate_flag
                         ON candidate_flag.project_id = candidate.project_id
                        AND candidate_flag.message_uuid = candidate.uuid
                        AND candidate_flag.user_uuid = target.user_uuid
                        AND candidate_flag.stream_uuid = target.stream_uuid
                       WHERE candidate.project_id = target.project_id
                         AND candidate.stream_uuid = target.stream_uuid
                       ORDER BY candidate.created_at DESC, candidate.uuid DESC
                       LIMIT 1
                   ) AS last_message_uuid
            FROM targets AS target
            JOIN workspace_v3.stream_bindings AS stream_binding
              ON stream_binding.project_id = target.project_id
             AND stream_binding.stream_uuid = target.stream_uuid
             AND stream_binding.user_uuid = target.user_uuid
            LEFT JOIN workspace_v3.message_flags AS flag
              ON flag.project_id = target.project_id
             AND flag.user_uuid = target.user_uuid
             AND flag.stream_uuid = target.stream_uuid
             AND NOT flag.read
            LEFT JOIN workspace_v3.messages AS message
              ON message.project_id = flag.project_id
             AND message.uuid = flag.message_uuid
             AND message.stream_uuid = target.stream_uuid
            LEFT JOIN workspace_v3.topic_bindings AS topic_binding
              ON topic_binding.project_id = message.project_id
             AND topic_binding.topic_uuid = message.topic_uuid
             AND topic_binding.user_uuid = target.user_uuid
            GROUP BY target.project_id, target.stream_uuid, target.user_uuid
        )
        UPDATE workspace_v3.stream_bindings AS binding
        SET unread_count = snapshot.unread_count,
            active_unread_count = snapshot.active_unread_count,
            passive_unread_count =
                snapshot.unread_count - snapshot.active_unread_count,
            last_message_uuid = snapshot.last_message_uuid,
            updated_at = clock_timestamp()
        FROM snapshots AS snapshot
        WHERE binding.project_id = snapshot.project_id
          AND binding.stream_uuid = snapshot.stream_uuid
          AND binding.user_uuid = snapshot.user_uuid
          AND (
                binding.unread_count,
                binding.active_unread_count,
                binding.passive_unread_count,
                binding.last_message_uuid
              ) IS DISTINCT FROM (
                snapshot.unread_count,
                snapshot.active_unread_count,
                snapshot.unread_count - snapshot.active_unread_count,
                snapshot.last_message_uuid
              )
        RETURNING binding.project_id, binding.uuid, binding.stream_uuid,
                  binding.user_uuid, binding.role, binding.notification_mode,
                  binding.unread_count, binding.active_unread_count,
                  binding.passive_unread_count, binding.last_message_uuid,
                  binding.created_at, binding.updated_at
        """,
        (
            [scope[0] for scope in scopes],
            [scope[1] for scope in scopes],
            [scope[2] for scope in scopes],
        ),
    )
    columns = (
        "project_id",
        "uuid",
        "stream_uuid",
        "user_uuid",
        "role",
        "notification_mode",
        "unread_count",
        "active_unread_count",
        "passive_unread_count",
        "last_message_uuid",
        "created_at",
        "updated_at",
    )
    return _mappings(result.fetchall(), columns)


def _update_topic_counters(
    session: typing.Any,
    scopes: list[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID | None]],
) -> list[dict[str, typing.Any]]:
    if not scopes:
        return []
    result = session.execute(
        """
        WITH targets(project_id, topic_uuid, user_uuid) AS MATERIALIZED (
            SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[])
        ),
        snapshots AS (
            SELECT target.project_id, target.topic_uuid, target.user_uuid,
                   count(message.uuid)::integer AS unread_count,
                   count(message.uuid) FILTER (
                       WHERE CASE
                           WHEN topic_binding.notification_mode = 'mute'
                               THEN false
                           WHEN topic_binding.notification_mode = 'follow'
                               THEN true
                           WHEN topic_binding.notification_mode = 'unmute'
                               THEN flag.mentioned
                           WHEN stream_binding.notification_mode = 'all_messages'
                               THEN true
                           WHEN stream_binding.notification_mode = 'mentions_only'
                               THEN flag.mentioned
                           ELSE false
                       END
                   )::integer AS active_unread_count,
                   (
                       SELECT candidate.uuid
                       FROM workspace_v3.messages AS candidate
                       JOIN workspace_v3.message_flags AS candidate_flag
                         ON candidate_flag.project_id = candidate.project_id
                        AND candidate_flag.message_uuid = candidate.uuid
                        AND candidate_flag.user_uuid = target.user_uuid
                       WHERE candidate.project_id = target.project_id
                         AND candidate.topic_uuid = target.topic_uuid
                       ORDER BY candidate.created_at DESC, candidate.uuid DESC
                       LIMIT 1
                   ) AS last_message_uuid
            FROM targets AS target
            JOIN workspace_v3.topic_bindings AS topic_binding
              ON topic_binding.project_id = target.project_id
             AND topic_binding.topic_uuid = target.topic_uuid
             AND topic_binding.user_uuid = target.user_uuid
            JOIN workspace_v3.topics AS topic
              ON topic.project_id = target.project_id
             AND topic.uuid = target.topic_uuid
            JOIN workspace_v3.stream_bindings AS stream_binding
              ON stream_binding.project_id = topic.project_id
             AND stream_binding.stream_uuid = topic.stream_uuid
             AND stream_binding.user_uuid = target.user_uuid
            LEFT JOIN workspace_v3.message_flags AS flag
              ON flag.project_id = target.project_id
             AND flag.user_uuid = target.user_uuid
             AND flag.stream_uuid = topic.stream_uuid
             AND NOT flag.read
            LEFT JOIN workspace_v3.messages AS message
              ON message.project_id = flag.project_id
             AND message.uuid = flag.message_uuid
             AND message.topic_uuid = target.topic_uuid
            GROUP BY target.project_id, target.topic_uuid, target.user_uuid
        )
        UPDATE workspace_v3.topic_bindings AS binding
        SET unread_count = snapshot.unread_count,
            active_unread_count = snapshot.active_unread_count,
            passive_unread_count =
                snapshot.unread_count - snapshot.active_unread_count,
            last_message_uuid = snapshot.last_message_uuid,
            updated_at = clock_timestamp()
        FROM snapshots AS snapshot
        WHERE binding.project_id = snapshot.project_id
          AND binding.topic_uuid = snapshot.topic_uuid
          AND binding.user_uuid = snapshot.user_uuid
          AND (
                binding.unread_count,
                binding.active_unread_count,
                binding.passive_unread_count,
                binding.last_message_uuid
              ) IS DISTINCT FROM (
                snapshot.unread_count,
                snapshot.active_unread_count,
                snapshot.unread_count - snapshot.active_unread_count,
                snapshot.last_message_uuid
              )
        RETURNING binding.project_id, binding.uuid, binding.topic_uuid,
                  binding.user_uuid, binding.notification_mode,
                  binding.unread_count, binding.active_unread_count,
                  binding.passive_unread_count, binding.last_message_uuid,
                  binding.summary_has_new_messages,
                  binding.created_at, binding.updated_at
        """,
        (
            [scope[0] for scope in scopes],
            [scope[1] for scope in scopes],
            [scope[2] for scope in scopes],
        ),
    )
    columns = (
        "project_id",
        "uuid",
        "topic_uuid",
        "user_uuid",
        "notification_mode",
        "unread_count",
        "active_unread_count",
        "passive_unread_count",
        "last_message_uuid",
        "summary_has_new_messages",
        "created_at",
        "updated_at",
    )
    return _mappings(result.fetchall(), columns)


def _folder_item_uuid(prefix: str, stream_uuid: sys_uuid.UUID) -> sys_uuid.UUID:
    return sys_uuid.UUID(prefix + str(stream_uuid)[2:])


def _active_folder_memberships(
    session: typing.Any,
    scopes: list[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID | None]],
) -> list[dict[str, typing.Any]]:
    if not scopes:
        return []
    result = session.execute(
        """
        WITH targets(project_id, stream_uuid, user_uuid) AS MATERIALIZED (
            SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[])
        )
        SELECT target.project_id, target.stream_uuid, target.user_uuid,
               stream.private
        FROM targets AS target
        JOIN workspace_v3.stream_bindings AS binding
          ON binding.project_id = target.project_id
         AND binding.stream_uuid = target.stream_uuid
         AND binding.user_uuid = target.user_uuid
        JOIN workspace_v3.streams AS stream
          ON stream.project_id = binding.project_id
         AND stream.uuid = binding.stream_uuid
        WHERE NOT stream.is_archived
        ORDER BY target.project_id, target.user_uuid, target.stream_uuid
        """,
        (
            [scope[0] for scope in scopes],
            [scope[1] for scope in scopes],
            [scope[2] for scope in scopes],
        ),
    )
    return _mappings(
        result.fetchall(),
        ("project_id", "stream_uuid", "user_uuid", "private"),
    )


def _ensure_system_folders(
    session: typing.Any,
    memberships: list[dict[str, typing.Any]],
) -> set[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]]:
    users = sorted(
        {(_uuid(row["project_id"]), _uuid(row["user_uuid"])) for row in memberships},
        key=lambda value: (str(value[0]), str(value[1])),
    )
    if not users:
        return set()
    rows = [
        (project_id, user_uuid, folder_uuid, kind, title)
        for project_id, user_uuid in users
        for folder_uuid, kind, title, _prefix in SYSTEM_FOLDERS
    ]
    result = session.execute(
        """
        INSERT INTO workspace_v3.folders (
            project_id, user_uuid, uuid, kind, title
        )
        SELECT *
        FROM unnest(
            %s::uuid[], %s::uuid[], %s::uuid[], %s::text[], %s::text[]
        )
        ON CONFLICT DO NOTHING
        RETURNING project_id, user_uuid, uuid
        """,
        (
            [row[0] for row in rows],
            [row[1] for row in rows],
            [row[2] for row in rows],
            [row[3] for row in rows],
            [row[4] for row in rows],
        ),
    )
    return {
        (
            _uuid(row["project_id"]),
            _uuid(row["user_uuid"]),
            _uuid(row["uuid"]),
        )
        for row in _mappings(
            result.fetchall(),
            ("project_id", "user_uuid", "uuid"),
        )
    }


def _sync_folder_memberships(
    session: typing.Any,
    scopes: list[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID | None]],
) -> set[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]]:
    if not scopes:
        return set()
    memberships = _active_folder_memberships(session, scopes)
    created_folders = _ensure_system_folders(session, memberships)
    current = {
        (
            _uuid(row["project_id"]),
            _uuid(row["stream_uuid"]),
            _uuid(row["user_uuid"]),
        ): bool(row["private"])
        for row in memberships
    }
    session.execute(
        "SELECT set_config('workspace_v3.suppress_folder_item_projection', 'on', true)"
    )
    try:
        deleted = session.execute(
            """
            WITH targets(project_id, stream_uuid, user_uuid) AS MATERIALIZED (
                SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[])
            ),
            desired AS MATERIALIZED (
                SELECT target.project_id, target.stream_uuid, target.user_uuid,
                       CASE WHEN stream.private THEN %s::uuid ELSE %s::uuid END
                           AS category_uuid
                FROM targets AS target
                JOIN workspace_v3.stream_bindings AS binding
                  ON binding.project_id = target.project_id
                 AND binding.stream_uuid = target.stream_uuid
                 AND binding.user_uuid = target.user_uuid
                JOIN workspace_v3.streams AS stream
                  ON stream.project_id = binding.project_id
                 AND stream.uuid = binding.stream_uuid
                WHERE NOT stream.is_archived
            )
            DELETE FROM workspace_v3.folder_items AS item
            USING targets AS target
            LEFT JOIN desired
              ON desired.project_id = target.project_id
             AND desired.stream_uuid = target.stream_uuid
             AND desired.user_uuid = target.user_uuid
            WHERE item.project_id = target.project_id
              AND item.stream_uuid = target.stream_uuid
              AND item.user_uuid = target.user_uuid
              AND item.automatic
              AND (
                    desired.project_id IS NULL
                    OR item.folder_uuid NOT IN (%s::uuid, desired.category_uuid)
                  )
            RETURNING item.uuid AS uuid,
                      item.project_id AS project_id,
                      item.user_uuid AS user_uuid,
                      item.folder_uuid AS folder_uuid,
                      item.stream_uuid AS stream_uuid,
                      item.order_index AS order_index,
                      item.pinned_at AS pinned_at,
                      item.chat_type AS chat_type,
                      item.automatic AS automatic,
                      item.created_at AS created_at,
                      item.updated_at AS updated_at
            """,
            (
                [scope[0] for scope in scopes],
                [scope[1] for scope in scopes],
                [scope[2] for scope in scopes],
                DIRECT_FOLDER_UUID,
                STREAMS_FOLDER_UUID,
                ALL_CHATS_FOLDER_UUID,
            ),
        )
        deleted_rows = _mappings(
            deleted.fetchall(),
            (
                "uuid",
                "project_id",
                "user_uuid",
                "folder_uuid",
                "stream_uuid",
                "order_index",
                "pinned_at",
                "chat_type",
                "automatic",
                "created_at",
                "updated_at",
            ),
        )
        item_rows: list[
            tuple[
                sys_uuid.UUID,
                sys_uuid.UUID,
                sys_uuid.UUID,
                sys_uuid.UUID,
                sys_uuid.UUID,
                str,
            ]
        ] = []
        for (project_id, stream_uuid, user_uuid), private in current.items():
            category_uuid = DIRECT_FOLDER_UUID if private else STREAMS_FOLDER_UUID
            category_prefix = "11" if private else "22"
            item_rows.extend(
                (
                    (
                        _folder_item_uuid("00", stream_uuid),
                        project_id,
                        user_uuid,
                        ALL_CHATS_FOLDER_UUID,
                        stream_uuid,
                        "private" if private else "stream",
                    ),
                    (
                        _folder_item_uuid(category_prefix, stream_uuid),
                        project_id,
                        user_uuid,
                        category_uuid,
                        stream_uuid,
                        "private" if private else "stream",
                    ),
                )
            )
        if item_rows:
            inserted = session.execute(
                """
                INSERT INTO workspace_v3.folder_items (
                    uuid, project_id, user_uuid, folder_uuid, stream_uuid,
                    chat_type, automatic
                )
                SELECT input.uuid, input.project_id, input.user_uuid,
                       input.folder_uuid, input.stream_uuid,
                       input.chat_type, true
                FROM unnest(
                    %s::uuid[], %s::uuid[], %s::uuid[],
                    %s::uuid[], %s::uuid[], %s::text[]
                ) AS input(
                    uuid, project_id, user_uuid, folder_uuid,
                    stream_uuid, chat_type
                )
                ON CONFLICT (
                    project_id, user_uuid, folder_uuid, stream_uuid
                ) DO UPDATE
                SET chat_type = EXCLUDED.chat_type,
                    updated_at = clock_timestamp()
                WHERE workspace_v3.folder_items.chat_type
                      IS DISTINCT FROM EXCLUDED.chat_type
                RETURNING uuid, project_id, user_uuid, folder_uuid,
                          stream_uuid, order_index, pinned_at, chat_type,
                          automatic, created_at, updated_at
                """,
                (
                    [row[0] for row in item_rows],
                    [row[1] for row in item_rows],
                    [row[2] for row in item_rows],
                    [row[3] for row in item_rows],
                    [row[4] for row in item_rows],
                    [row[5] for row in item_rows],
                ),
            )
            inserted_rows = _mappings(
                inserted.fetchall(),
                (
                    "uuid",
                    "project_id",
                    "user_uuid",
                    "folder_uuid",
                    "stream_uuid",
                    "order_index",
                    "pinned_at",
                    "chat_type",
                    "automatic",
                    "created_at",
                    "updated_at",
                ),
            )
        else:
            inserted_rows = []
    finally:
        session.execute(
            "SELECT set_config("
            "'workspace_v3.suppress_folder_item_projection', 'off', true)"
        )
    operations_by_folder: dict[
        tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID],
        list[dict[str, typing.Any]],
    ] = {}
    for action, rows in (("created", inserted_rows), ("deleted", deleted_rows)):
        for row in rows:
            key = (
                _uuid(row["project_id"]),
                _uuid(row["user_uuid"]),
                _uuid(row["folder_uuid"]),
            )
            operations_by_folder.setdefault(key, []).append(
                {
                    "action": action,
                    "uuid": str(row["uuid"]),
                    "folder_uuid": str(row["folder_uuid"]),
                    "stream_uuid": str(row["stream_uuid"]),
                }
            )
    queued = sorted(
        operations_by_folder.items(),
        key=lambda value: tuple(str(item) for item in value[0]),
    )
    if queued:
        session.execute(
            """
            INSERT INTO workspace_v3.projection_tasks (
                project_id, task_type, scope_type, scope_uuid,
                user_uuid, payload
            )
            SELECT input.project_id, 'folder_counters', 'user_folder',
                   input.folder_uuid, input.user_uuid, input.payload::jsonb
            FROM unnest(
                %s::uuid[], %s::uuid[], %s::uuid[], %s::text[]
            ) AS input(project_id, user_uuid, folder_uuid, payload)
            """,
            (
                [key[0] for key, _operations_value in queued],
                [key[1] for key, _operations_value in queued],
                [key[2] for key, _operations_value in queued],
                [
                    json.dumps({"operations": operations_value})
                    for _key, operations_value in queued
                ],
            ),
        )
    return created_folders


def _update_folder_counters(
    session: typing.Any,
    scopes: set[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]],
) -> set[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]]:
    ordered = sorted(
        scopes,
        key=lambda value: (str(value[0]), str(value[1]), str(value[2])),
    )
    if not ordered:
        return set()
    result = session.execute(
        """
        WITH targets(project_id, user_uuid, folder_uuid) AS MATERIALIZED (
            SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[])
        ),
        snapshots AS (
            SELECT target.project_id, target.user_uuid, target.folder_uuid,
                   COALESCE(sum(binding.unread_count), 0)::integer
                       AS unread_count,
                   COALESCE(sum(binding.active_unread_count), 0)::integer
                       AS active_unread_count,
                   COALESCE(sum(binding.passive_unread_count), 0)::integer
                       AS passive_unread_count
            FROM targets AS target
            LEFT JOIN workspace_v3.folder_items AS item
              ON item.project_id = target.project_id
             AND item.user_uuid = target.user_uuid
             AND item.folder_uuid = target.folder_uuid
            LEFT JOIN workspace_v3.stream_bindings AS binding
              ON binding.project_id = item.project_id
             AND binding.user_uuid = item.user_uuid
             AND binding.stream_uuid = item.stream_uuid
            GROUP BY target.project_id, target.user_uuid, target.folder_uuid
        )
        UPDATE workspace_v3.folders AS folder
        SET unread_count = snapshot.unread_count,
            active_unread_count = snapshot.active_unread_count,
            passive_unread_count = snapshot.passive_unread_count,
            updated_at = clock_timestamp()
        FROM snapshots AS snapshot
        WHERE folder.project_id = snapshot.project_id
          AND folder.user_uuid = snapshot.user_uuid
          AND folder.uuid = snapshot.folder_uuid
          AND (
                folder.unread_count,
                folder.active_unread_count,
                folder.passive_unread_count
              ) IS DISTINCT FROM (
                snapshot.unread_count,
                snapshot.active_unread_count,
                snapshot.passive_unread_count
              )
        RETURNING folder.project_id, folder.user_uuid, folder.uuid
        """,
        (
            [scope[0] for scope in ordered],
            [scope[1] for scope in ordered],
            [scope[2] for scope in ordered],
        ),
    )
    return {
        (
            _uuid(row["project_id"]),
            _uuid(row["user_uuid"]),
            _uuid(row["uuid"]),
        )
        for row in _mappings(
            result.fetchall(),
            ("project_id", "user_uuid", "uuid"),
        )
    }


def _folder_rows(
    session: typing.Any,
    scopes: set[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]],
) -> list[dict[str, typing.Any]]:
    ordered = sorted(
        scopes,
        key=lambda value: (str(value[0]), str(value[1]), str(value[2])),
    )
    if not ordered:
        return []
    result = session.execute(
        """
        WITH targets(project_id, user_uuid, folder_uuid) AS MATERIALIZED (
            SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[])
        )
        SELECT folder.uuid, folder.project_id, folder.user_uuid,
               folder.kind, folder.title, folder.background_color_value,
               folder.unread_count, folder.active_unread_count,
               folder.passive_unread_count, folder.created_at,
               folder.updated_at,
               COALESCE(
                   jsonb_agg(
                       jsonb_build_object(
                           'uuid', item.uuid,
                           'stream_uuid', item.stream_uuid,
                           'order_index', item.order_index,
                           'pinned_at', item.pinned_at,
                           'chat_type', item.chat_type,
                           'automatic', item.automatic
                       ) ORDER BY item.order_index NULLS LAST,
                                  item.created_at, item.uuid
                   ) FILTER (WHERE item.uuid IS NOT NULL),
                   '[]'::jsonb
               ) AS folder_items
        FROM targets AS target
        JOIN workspace_v3.folders AS folder
          ON folder.project_id = target.project_id
         AND folder.user_uuid = target.user_uuid
         AND folder.uuid = target.folder_uuid
        LEFT JOIN workspace_v3.folder_items AS item
          ON item.project_id = folder.project_id
         AND item.user_uuid = folder.user_uuid
         AND item.folder_uuid = folder.uuid
        GROUP BY folder.project_id, folder.user_uuid, folder.uuid
        ORDER BY folder.project_id, folder.user_uuid, folder.uuid
        """,
        (
            [scope[0] for scope in ordered],
            [scope[1] for scope in ordered],
            [scope[2] for scope in ordered],
        ),
    )
    return _mappings(
        result.fetchall(),
        (
            "uuid",
            "project_id",
            "user_uuid",
            "kind",
            "title",
            "background_color_value",
            "unread_count",
            "active_unread_count",
            "passive_unread_count",
            "created_at",
            "updated_at",
            "folder_items",
        ),
    )


def _serialize(value: object) -> object:
    if isinstance(value, sys_uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return (
            value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        )
    if hasattr(value, "isoformat"):
        return typing.cast(typing.Any, value).isoformat()
    return value


def _event_payload(kind: str, row: dict[str, typing.Any]) -> dict[str, typing.Any]:
    return {
        **{key: _serialize(value) for key, value in row.items()},
        "kind": kind,
    }


def _folder_event_payload(
    kind: str,
    row: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    payload = dict(row)
    payload["system_type"] = "all" if payload.pop("kind") == "all_chats" else "created"
    payload["unread_count"] = payload.pop("active_unread_count")
    payload.pop("passive_unread_count")
    return _event_payload(kind, payload)


def _query_consumers(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID | None,
    user_uuid: sys_uuid.UUID | None,
) -> tuple[tuple[str, sys_uuid.UUID], ...]:
    if stream_uuid is not None:
        user_rows = session.execute(
            """
            SELECT user_uuid
            FROM workspace_v3.stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
            ORDER BY user_uuid
            """,
            (project_id, stream_uuid),
        ).fetchall()
        users = [_uuid(_mapping(row, ("user_uuid",))["user_uuid"]) for row in user_rows]
    elif user_uuid is not None:
        users = [user_uuid]
    else:
        users = []
    if stream_uuid is not None:
        provider_rows = session.execute(
            """
            SELECT provider.uuid AS consumer_uuid
            FROM workspace_v3.streams AS stream
            JOIN workspace_v3.provider_consumers AS provider
              ON provider.project_id = stream.project_id
             AND provider.name = stream.source_name
             AND provider.enabled
            WHERE stream.project_id = %s AND stream.uuid = %s
              AND stream.source_name <> 'native'
            ORDER BY provider.uuid
            """,
            (project_id, stream_uuid),
        ).fetchall()
    elif user_uuid is not None:
        provider_rows = session.execute(
            """
            SELECT DISTINCT provider.uuid AS consumer_uuid
            FROM workspace_v3.stream_bindings AS binding
            JOIN workspace_v3.streams AS stream
              ON stream.project_id = binding.project_id
             AND stream.uuid = binding.stream_uuid
            JOIN workspace_v3.provider_consumers AS provider
              ON provider.project_id = stream.project_id
             AND provider.name = stream.source_name
             AND provider.enabled
            WHERE binding.project_id = %s AND binding.user_uuid = %s
              AND stream.source_name <> 'native'
            ORDER BY provider.uuid
            """,
            (project_id, user_uuid),
        ).fetchall()
    else:
        provider_rows = []
    providers = [
        _uuid(_mapping(row, ("consumer_uuid",))["consumer_uuid"])
        for row in provider_rows
    ]
    return tuple(
        sorted(
            {
                *(("user", value) for value in users),
                *(("provider", value) for value in providers),
            },
            key=lambda value: (value[0], str(value[1])),
        )
    )


def _resource_event_specification(
    session: typing.Any,
    *,
    project_id: sys_uuid.UUID,
    resource: str,
    entity_uuid: sys_uuid.UUID,
    object_type: str,
    action: str,
    consumers: tuple[tuple[str, sys_uuid.UUID], ...],
) -> dict[str, typing.Any] | None:
    recipients = tuple(
        consumer_uuid
        for consumer_type, consumer_uuid in consumers
        if consumer_type == "user"
    )
    payloads = v3_store.event_resource_payloads(
        session,
        project_id,
        resource,
        entity_uuid,
        recipients,
    )
    if not payloads:
        return None
    common_payload, recipient_payloads = v3_store.partition_event_payloads(
        object_type,
        payloads,
    )
    return {
        "project_id": project_id,
        "entity_uuid": entity_uuid,
        "object_type": object_type,
        "action": action,
        "payload": {"kind": f"{object_type}.{action}", **common_payload},
        "recipient_payloads": recipient_payloads,
        "consumers": consumers,
    }


def _audience_identity(
    project_id: sys_uuid.UUID,
    consumers: tuple[tuple[str, sys_uuid.UUID], ...],
) -> tuple[sys_uuid.UUID, str]:
    members = "\n".join(f"{kind}:{value}" for kind, value in consumers)
    digest = hashlib.sha256(members.encode("utf-8")).hexdigest()
    audience_uuid = sys_uuid.uuid5(
        AUDIENCE_NAMESPACE,
        f"{project_id}:{digest}",
    )
    return audience_uuid, digest


def _emit_events(
    session: typing.Any,
    specifications: list[dict[str, typing.Any]],
) -> int:
    specifications = [item for item in specifications if item["consumers"]]
    if not specifications:
        return 0
    audiences: dict[
        tuple[sys_uuid.UUID, tuple[tuple[str, sys_uuid.UUID], ...]],
        tuple[sys_uuid.UUID, str],
    ] = {}
    for specification in specifications:
        project_id = _uuid(specification["project_id"])
        consumers = specification["consumers"]
        key = (project_id, consumers)
        audiences[key] = _audience_identity(project_id, consumers)
        specification["audience_uuid"] = audiences[key][0]
        specification["event_uuid"] = sys_uuid.uuid4()
    session.execute(
        """
        INSERT INTO workspace_v3.event_audience_snapshots (
            uuid, project_id, membership_digest
        )
        SELECT input.uuid, input.project_id, input.membership_digest
        FROM unnest(%s::uuid[], %s::uuid[], %s::text[]) AS input(
            uuid, project_id, membership_digest
        )
        ON CONFLICT (project_id, membership_digest) DO NOTHING
        """,
        (
            [value[0] for value in audiences.values()],
            [key[0] for key in audiences],
            [value[1] for value in audiences.values()],
        ),
    )
    member_rows = sorted(
        {
            (project_id, audiences[(project_id, consumers)][0], kind, consumer_uuid)
            for project_id, consumers in audiences
            for kind, consumer_uuid in consumers
        },
        key=lambda value: tuple(str(item) for item in value),
    )
    session.execute(
        """
        INSERT INTO workspace_v3.event_audience_members (
            project_id, audience_snapshot_uuid, consumer_type, consumer_uuid
        )
        SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::text[], %s::uuid[])
        ON CONFLICT DO NOTHING
        """,
        (
            [row[0] for row in member_rows],
            [row[1] for row in member_rows],
            [row[2] for row in member_rows],
            [row[3] for row in member_rows],
        ),
    )
    event_rows = session.execute(
        """
        INSERT INTO workspace_v3.events (
            uuid, project_id, entity_uuid, audience_snapshot_uuid,
            object_type, action, payload
        )
        SELECT input.uuid, input.project_id, input.entity_uuid,
               input.audience_snapshot_uuid, input.object_type,
               input.action, input.payload::jsonb
        FROM unnest(
            %s::uuid[], %s::uuid[], %s::uuid[], %s::uuid[],
            %s::text[], %s::text[], %s::text[]
        ) AS input(
            uuid, project_id, entity_uuid, audience_snapshot_uuid,
            object_type, action, payload
        )
        RETURNING uuid, project_id, audience_snapshot_uuid, epoch_version
        """,
        (
            [item["event_uuid"] for item in specifications],
            [item["project_id"] for item in specifications],
            [item["entity_uuid"] for item in specifications],
            [item["audience_uuid"] for item in specifications],
            [item["object_type"] for item in specifications],
            [item["action"] for item in specifications],
            [
                json.dumps(item["payload"], default=_serialize)
                for item in specifications
            ],
        ),
    ).fetchall()
    event_ids = [
        _uuid(
            _mapping(
                row,
                (
                    "uuid",
                    "project_id",
                    "audience_snapshot_uuid",
                    "epoch_version",
                ),
            )["uuid"]
        )
        for row in event_rows
    ]
    recipient_rows = [
        (
            item["project_id"],
            item["event_uuid"],
            recipient_uuid,
            payload,
        )
        for item in specifications
        for recipient_uuid, payload in item.get("recipient_payloads", {}).items()
    ]
    provider_recipient_rows = [
        (
            item["project_id"],
            item["event_uuid"],
            consumer_uuid,
            next(iter(item["recipient_payloads"].values())),
        )
        for item in specifications
        if len(item.get("recipient_payloads", {})) == 1
        for consumer_type, consumer_uuid in item["consumers"]
        if consumer_type == "provider"
    ]
    if recipient_rows:
        session.execute(
            """
            INSERT INTO workspace_v3.event_recipient_payloads (
                project_id, event_uuid, consumer_type, consumer_uuid, payload
            )
            SELECT input.project_id, input.event_uuid, 'user',
                   input.consumer_uuid, input.payload::jsonb
            FROM unnest(
                %s::uuid[], %s::uuid[], %s::uuid[], %s::text[]
            ) AS input(
                project_id, event_uuid, consumer_uuid, payload
            )
            """,
            (
                [row[0] for row in recipient_rows],
                [row[1] for row in recipient_rows],
                [row[2] for row in recipient_rows],
                [json.dumps(row[3], default=_serialize) for row in recipient_rows],
            ),
        )
    if provider_recipient_rows:
        session.execute(
            """
            INSERT INTO workspace_v3.event_recipient_payloads (
                project_id, event_uuid, consumer_type, consumer_uuid, payload
            )
            SELECT input.project_id, input.event_uuid, 'provider',
                   input.consumer_uuid, input.payload::jsonb
            FROM unnest(
                %s::uuid[], %s::uuid[], %s::uuid[], %s::text[]
            ) AS input(
                project_id, event_uuid, consumer_uuid, payload
            )
            """,
            (
                [row[0] for row in provider_recipient_rows],
                [row[1] for row in provider_recipient_rows],
                [row[2] for row in provider_recipient_rows],
                [
                    json.dumps(row[3], default=_serialize)
                    for row in provider_recipient_rows
                ],
            ),
        )
    session.execute(
        """
        WITH maxima AS (
            SELECT project_id, audience_snapshot_uuid,
                   max(epoch_version) AS epoch_version
            FROM workspace_v3.events
            WHERE uuid = ANY(%s::uuid[])
            GROUP BY project_id, audience_snapshot_uuid
        )
        UPDATE workspace_v3.event_audience_snapshots AS audience
        SET current_epoch_version = GREATEST(
                audience.current_epoch_version, maxima.epoch_version
            ),
            updated_at = clock_timestamp()
        FROM maxima
        WHERE audience.project_id = maxima.project_id
          AND audience.uuid = maxima.audience_snapshot_uuid
        """,
        (event_ids,),
    )
    return len(event_ids)


def prune_event_journal(
    session: typing.Any,
    retention_hours: int = DEFAULT_EVENT_RETENTION_HOURS,
) -> int:
    """Prune expired events while preserving an exact reconnect floor."""
    if retention_hours <= 0:
        raise ValueError("Event retention must be positive")
    row = session.execute(
        """
        WITH deleted AS (
            DELETE FROM workspace_v3.events
            WHERE created_at < clock_timestamp() - make_interval(hours => %s)
            RETURNING project_id, audience_snapshot_uuid, epoch_version
        ),
        maxima AS (
            SELECT project_id, audience_snapshot_uuid,
                   max(epoch_version) AS epoch_version
            FROM deleted
            GROUP BY project_id, audience_snapshot_uuid
        ),
        advanced AS (
            UPDATE workspace_v3.event_audience_snapshots AS audience
            SET current_epoch_version = GREATEST(
                    audience.current_epoch_version,
                    maxima.epoch_version
                ),
                pruned_through_epoch_version = GREATEST(
                    audience.pruned_through_epoch_version,
                    maxima.epoch_version
                ),
                updated_at = clock_timestamp()
            FROM maxima
            WHERE audience.project_id = maxima.project_id
              AND audience.uuid = maxima.audience_snapshot_uuid
            RETURNING audience.uuid
        )
        SELECT count(*) AS deleted_count FROM deleted
        """,
        (retention_hours,),
    ).fetchone()
    return int(_mapping(row, ("deleted_count",))["deleted_count"])


def _message_rows(
    session: typing.Any,
    tasks: list[dict[str, typing.Any]],
) -> dict[tuple[sys_uuid.UUID, sys_uuid.UUID], dict[str, typing.Any]]:
    key_values = set()
    for task in tasks:
        if task["task_type"] == "reaction_snapshot":
            key_values.add((_uuid(task["project_id"]), _uuid(task["scope_uuid"])))
            continue
        payload = _payload(task["payload"])
        if task["task_type"] == "read_counters" and (
            payload.get("emit_message_events") or payload.get("emit_message_event")
        ):
            for operation in _operations(payload):
                key_values.add(
                    (
                        _uuid(task["project_id"]),
                        _uuid(operation["message_uuid"]),
                    )
                )
    keys = sorted(
        key_values,
        key=lambda value: (str(value[0]), str(value[1])),
    )
    if not keys:
        return {}
    rows = session.execute(
        """
        WITH targets(project_id, message_uuid) AS (
            SELECT * FROM unnest(%s::uuid[], %s::uuid[])
        )
        SELECT message.project_id, message.uuid, message.stream_uuid,
               message.topic_uuid, message.author_uuid, message.payload,
               message.source_name, message.reactions,
               message.reaction_users, message.created_at, message.updated_at
        FROM workspace_v3.messages AS message
        JOIN targets AS target
          ON target.project_id = message.project_id
         AND target.message_uuid = message.uuid
        """,
        ([key[0] for key in keys], [key[1] for key in keys]),
    ).fetchall()
    columns = (
        "project_id",
        "uuid",
        "stream_uuid",
        "topic_uuid",
        "author_uuid",
        "payload",
        "source_name",
        "reactions",
        "reaction_users",
        "created_at",
        "updated_at",
    )
    return {
        (_uuid(row["project_id"]), _uuid(row["uuid"])): row
        for row in _mappings(rows, columns)
    }


def _projection_events(
    session: typing.Any,
    tasks: list[dict[str, typing.Any]],
    messages: list[dict[str, typing.Any]],
    stream_bindings: list[dict[str, typing.Any]],
    topic_bindings: list[dict[str, typing.Any]],
) -> list[dict[str, typing.Any]]:
    events: list[dict[str, typing.Any]] = []
    consumer_cache: dict[
        tuple[sys_uuid.UUID, sys_uuid.UUID | None, sys_uuid.UUID | None],
        tuple[tuple[str, sys_uuid.UUID], ...],
    ] = {}

    def consumers(
        project_id: sys_uuid.UUID,
        stream_uuid: sys_uuid.UUID | None = None,
        user_uuid: sys_uuid.UUID | None = None,
    ) -> tuple[tuple[str, sys_uuid.UUID], ...]:
        key = (project_id, stream_uuid, user_uuid)
        if key not in consumer_cache:
            consumer_cache[key] = _query_consumers(
                session,
                project_id,
                stream_uuid,
                user_uuid,
            )
        return consumer_cache[key]

    message_by_key = _message_rows(session, tasks)
    message_by_key.update(
        {(_uuid(row["project_id"]), _uuid(row["uuid"])): row for row in messages}
    )
    for row in messages:
        project_id = _uuid(row["project_id"])
        stream_uuid = _uuid(row["stream_uuid"])
        specification = _resource_event_specification(
            session,
            project_id=project_id,
            resource="messages",
            entity_uuid=_uuid(row["uuid"]),
            object_type="message",
            action="updated",
            consumers=consumers(project_id, stream_uuid=stream_uuid),
        )
        if specification is not None:
            events.append(specification)
    for task in tasks:
        if task["task_type"] != "reaction_snapshot":
            continue
        project_id = _uuid(task["project_id"])
        message_uuid = _uuid(task["scope_uuid"])
        message = message_by_key.get((project_id, message_uuid))
        if message is None:
            continue
        for payload in _operations(_payload(task["payload"])):
            action = str(payload["action"])
            if action in {"insert", "created"}:
                action = "created"
            elif action in {"delete", "deleted"}:
                action = "deleted"
            else:
                action = "updated"
            reaction_uuid = _uuid(payload["reaction_uuid"])
            reaction = {
                "uuid": reaction_uuid,
                "project_id": project_id,
                "message_uuid": _uuid(payload["message_uuid"]),
                "user_uuid": _uuid(payload["user_uuid"]),
                "emoji_name": payload["emoji_name"],
                "source_name": payload["source_name"],
                "source": v3_store.source_projection(payload["source_name"]),
            }
            for name in ("old_emoji_name", "old_source_name"):
                if name in payload:
                    reaction[name] = payload[name]
            if "old_source_name" in payload:
                reaction["old_source"] = v3_store.source_projection(
                    payload["old_source_name"]
                )
            events.append(
                {
                    "project_id": project_id,
                    "entity_uuid": reaction_uuid,
                    "object_type": "message_reaction",
                    "action": action,
                    "payload": _event_payload(f"message_reaction.{action}", reaction),
                    "consumers": consumers(
                        project_id,
                        stream_uuid=_uuid(message["stream_uuid"]),
                    ),
                }
            )
    for row in stream_bindings:
        project_id = _uuid(row["project_id"])
        user_uuid = _uuid(row["user_uuid"])
        specification = _resource_event_specification(
            session,
            project_id=project_id,
            resource="streams",
            entity_uuid=_uuid(row["stream_uuid"]),
            object_type="stream",
            action="updated",
            consumers=consumers(project_id, user_uuid=user_uuid),
        )
        if specification is not None:
            events.append(specification)
    for row in topic_bindings:
        project_id = _uuid(row["project_id"])
        user_uuid = _uuid(row["user_uuid"])
        specification = _resource_event_specification(
            session,
            project_id=project_id,
            resource="stream_topics",
            entity_uuid=_uuid(row["topic_uuid"]),
            object_type="topic",
            action="updated",
            consumers=consumers(project_id, user_uuid=user_uuid),
        )
        if specification is not None:
            events.append(specification)
    for task in tasks:
        payload = _payload(task["payload"])
        if task["task_type"] != "read_counters" or not (
            payload.get("emit_message_events") or payload.get("emit_message_event")
        ):
            continue
        project_id = _uuid(task["project_id"])
        user_uuid = _uuid(task["user_uuid"])
        for operation in _operations(payload):
            message_uuid = _uuid(operation["message_uuid"])
            action = (
                "read"
                if operation["operation"] != "delete" and operation["read"]
                else "updated"
            )
            specification = _resource_event_specification(
                session,
                project_id=project_id,
                resource="messages",
                entity_uuid=message_uuid,
                object_type="message",
                action=action,
                consumers=consumers(project_id, user_uuid=user_uuid),
            )
            if specification is not None:
                events.append(specification)
    return events


def _folder_projection_events(
    tasks: list[dict[str, typing.Any]],
    folders: list[dict[str, typing.Any]],
    created_folders: set[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]],
    deleted_items: list[dict[str, typing.Any]],
) -> list[dict[str, typing.Any]]:
    events = []
    for row in folders:
        project_id = _uuid(row["project_id"])
        user_uuid = _uuid(row["user_uuid"])
        folder_uuid = _uuid(row["uuid"])
        action = (
            "created"
            if (project_id, user_uuid, folder_uuid) in created_folders
            else "updated"
        )
        events.append(
            {
                "project_id": project_id,
                "entity_uuid": folder_uuid,
                "object_type": "folder",
                "action": action,
                "payload": _folder_event_payload(f"folder.{action}", row),
                "consumers": (("user", user_uuid),),
            }
        )
    deleted_by_key = {
        (
            _uuid(row["project_id"]),
            _uuid(row["user_uuid"]),
            _uuid(row["uuid"]),
        ): row
        for row in deleted_items
    }
    for task in tasks:
        if task["task_type"] != "folder_counters":
            continue
        project_id = _uuid(task["project_id"])
        user_uuid = _uuid(task["user_uuid"])
        for operation in _operations(_payload(task["payload"])):
            if operation.get("action") != "deleted":
                continue
            item_uuid = _uuid(operation["uuid"])
            deleted_by_key.setdefault(
                (project_id, user_uuid, item_uuid),
                {
                    "uuid": item_uuid,
                    "project_id": project_id,
                    "user_uuid": user_uuid,
                    "folder_uuid": _uuid(operation["folder_uuid"]),
                    "stream_uuid": _uuid(operation["stream_uuid"]),
                },
            )
    for (project_id, user_uuid, item_uuid), row in sorted(
        deleted_by_key.items(),
        key=lambda value: tuple(str(item) for item in value[0]),
    ):
        events.append(
            {
                "project_id": project_id,
                "entity_uuid": item_uuid,
                "object_type": "folder_item",
                "action": "deleted",
                "payload": _event_payload("folder_item.deleted", row),
                "consumers": (("user", user_uuid),),
            }
        )
    return events


def _finish_tasks(
    session: typing.Any,
    tasks: list[dict[str, typing.Any]],
    worker_id: str,
) -> None:
    session.execute(
        """
        UPDATE workspace_v3.projection_tasks
        SET status = 'completed', lease_owner = NULL,
            lease_expires_at = NULL, next_retry_at = NULL,
            last_error = NULL, updated_at = clock_timestamp()
        WHERE uuid = ANY(%s::uuid[]) AND lease_owner = %s
        """,
        ([task["uuid"] for task in tasks], worker_id),
    )


def _fail_tasks(
    session: typing.Any,
    tasks: list[dict[str, typing.Any]],
    worker_id: str,
    error: BaseException,
    max_attempts: int,
) -> None:
    session.execute(
        """
        UPDATE workspace_v3.projection_tasks
        SET status = CASE
                WHEN attempts >= %s THEN 'dead_letter'
                ELSE 'failed'
            END,
            lease_owner = NULL, lease_expires_at = NULL,
            next_retry_at = CASE
                WHEN attempts >= %s THEN NULL
                ELSE clock_timestamp() + make_interval(
                    secs => LEAST(
                        5 * power(2, LEAST(attempts - 1, 8)), 1200
                    )
                )
            END,
            last_error = %s,
            updated_at = clock_timestamp()
        WHERE uuid = ANY(%s::uuid[]) AND lease_owner = %s
        """,
        (
            max_attempts,
            max_attempts,
            type(error).__name__[:255],
            [task["uuid"] for task in tasks],
            worker_id,
        ),
    )


def _empty_metrics(started_at: float) -> dict[str, float]:
    return {
        "claimed": 0.0,
        "completed": 0.0,
        "failed": 0.0,
        "operations": 0.0,
        "projections": 0.0,
        "events": 0.0,
        "elapsed_seconds": time.monotonic() - started_at,
    }


def claim_projection_tasks(
    session: typing.Any,
    worker_id: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[dict[str, typing.Any]]:
    """Lease a bounded batch; the caller commits before processing it."""
    return _claim_tasks(
        session,
        worker_id,
        batch_size,
        lease_seconds,
        max_attempts,
    )


def process_claimed_projection_tasks(
    session: typing.Any,
    worker_id: str,
    tasks: list[dict[str, typing.Any]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reaction_user_limit: int = DEFAULT_REACTION_USER_LIMIT,
) -> dict[str, float]:
    """Process an already committed lease without keeping claim locks open."""
    started_at = time.monotonic()
    if not tasks:
        return _empty_metrics(started_at)
    # These are short OLTP projections. PostgreSQL can otherwise spend more
    # time compiling a batch with stale/high-cardinality statistics than
    # executing it, which makes small user batches unpredictably slow.
    session.execute("SET LOCAL jit = off")
    session.execute("SAVEPOINT workspace_v3_projection_batch")
    try:
        operation_count = sum(
            len(_operations(_payload(task["payload"]))) for task in tasks
        )
        reaction_scopes = _unique_scopes(tasks, "reaction_snapshot", "message")
        stream_scopes = _unique_scopes(tasks, "read_counters", "user_stream")
        topic_scopes = _unique_scopes(tasks, "read_counters", "user_topic")
        membership_scopes = _unique_scopes(
            tasks,
            "folder_membership",
            "user_stream",
        )
        raw_folder_scopes = _unique_scopes(
            tasks,
            "folder_counters",
            "user_folder",
        )
        folder_scopes = {
            (project_id, typing.cast(sys_uuid.UUID, user_uuid), folder_uuid)
            for project_id, folder_uuid, user_uuid in raw_folder_scopes
        }
        supported = (
            len(reaction_scopes)
            + len(stream_scopes)
            + len(topic_scopes)
            + len(membership_scopes)
            + len(folder_scopes)
        )
        unsupported = [
            task
            for task in tasks
            if (task["task_type"], task["scope_type"])
            not in {
                ("reaction_snapshot", "message"),
                ("read_counters", "user_stream"),
                ("read_counters", "user_topic"),
                ("folder_membership", "user_stream"),
                ("folder_counters", "user_folder"),
            }
        ]
        if unsupported:
            raise ValueError("Unsupported Workspace v3 projection task")
        messages = _update_reaction_snapshots(
            session,
            reaction_scopes,
            reaction_user_limit,
        )
        stream_bindings = _update_stream_counters(session, stream_scopes)
        topic_bindings = _update_topic_counters(session, topic_scopes)
        created_folders = _sync_folder_memberships(session, membership_scopes)
        changed_folder_scopes = _update_folder_counters(session, folder_scopes)
        operation_folder_scopes = {
            (
                _uuid(task["project_id"]),
                _uuid(task["user_uuid"]),
                _uuid(task["scope_uuid"]),
            )
            for task in tasks
            if task["task_type"] == "folder_counters" and _payload(task["payload"])
        }
        folder_event_scopes = (
            created_folders | changed_folder_scopes | operation_folder_scopes
        )
        folders = _folder_rows(session, folder_event_scopes)
        events = _projection_events(
            session,
            tasks,
            messages,
            stream_bindings,
            topic_bindings,
        )
        events.extend(
            _folder_projection_events(
                tasks,
                folders,
                created_folders,
                [],
            )
        )
        event_count = _emit_events(session, events)
        _finish_tasks(session, tasks, worker_id)
    except Exception as error:
        session.execute("ROLLBACK TO SAVEPOINT workspace_v3_projection_batch")
        session.execute("RELEASE SAVEPOINT workspace_v3_projection_batch")
        _fail_tasks(session, tasks, worker_id, error, max_attempts)
        LOG.exception("Workspace v3 projection batch failed")
        return {
            "claimed": float(len(tasks)),
            "completed": 0.0,
            "failed": float(len(tasks)),
            "operations": 0.0,
            "projections": 0.0,
            "events": 0.0,
            "elapsed_seconds": time.monotonic() - started_at,
        }
    session.execute("RELEASE SAVEPOINT workspace_v3_projection_batch")
    return {
        "claimed": float(len(tasks)),
        "completed": float(len(tasks)),
        "failed": 0.0,
        "operations": float(operation_count),
        "projections": float(supported),
        "events": float(event_count),
        "elapsed_seconds": time.monotonic() - started_at,
    }


def process_projection_batch(
    session: typing.Any,
    worker_id: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reaction_user_limit: int = DEFAULT_REACTION_USER_LIMIT,
) -> dict[str, float]:
    """Claim and process in the caller transaction; prefer the two-phase API."""
    started_at = time.monotonic()
    tasks = claim_projection_tasks(
        session,
        worker_id,
        batch_size=batch_size,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
    )
    if not tasks:
        return _empty_metrics(started_at)
    return process_claimed_projection_tasks(
        session,
        worker_id,
        tasks,
        max_attempts=max_attempts,
        reaction_user_limit=reaction_user_limit,
    )
