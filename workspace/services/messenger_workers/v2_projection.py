# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Bounded, exact-scope projection worker for Messenger v2.

Request transactions append immutable domain outbox rows.  This module derives
exactly one durable task for every outbox row and processes tasks under a
fenced scope lease.  All projection writes and the corresponding public event
snapshot commit in the same worker transaction.
"""

import datetime
import itertools
import json
import logging
import time
import types
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.dm import v2_models
from workspace.external_bridge_control import file_repository


LOG = logging.getLogger(__name__)
DEFAULT_DERIVE_LIMIT = 100
DEFAULT_FANOUT_BATCH_SIZE = 1000
MAX_FANOUT_BATCH_SIZE = 5000
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_LEASE_SECONDS = 30
CLAIM_CANDIDATE_LIMIT = 128
CLAIM_PROJECT_LIMIT = 16
REACTION_COALESCE_LIMIT = 128
FAIR_SCHEDULER_LANES = (
    "fanout",
    "fanout",
    "fanout",
    "fanout",
    "interactive_read",
    "interactive_read",
    "reaction_snapshot",
    "read_state",
    "background",
    "background",
)
_FAIR_SCHEDULER_CYCLE = itertools.cycle(FAIR_SCHEDULER_LANES)
_FAIR_DERIVATION_CYCLE = itertools.cycle(FAIR_SCHEDULER_LANES)
LEGACY_FOLDER_SNAPSHOT_SOURCE_KINDS = (
    "legacy_message_state.deleted",
    "legacy_message_state.updated",
)


def process_one_provider_file_cleanup_task(
    session: typing.Any,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Delete one reset Zulip object after its database references are gone."""
    task = session.execute(
        """
        WITH candidate AS (
            SELECT uuid
            FROM messenger_provider_file_cleanup_tasks
            WHERE status IN ('pending', 'failed')
              AND next_retry_at <= NOW()
              AND (lease_expires_at IS NULL OR lease_expires_at <= NOW())
            ORDER BY created_at, uuid
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE messenger_provider_file_cleanup_tasks AS task
        SET status = 'running', attempts = attempts + 1,
            lease_owner = %s,
            lease_expires_at = NOW() + make_interval(secs => %s),
            updated_at = NOW()
        FROM candidate
        WHERE task.uuid = candidate.uuid
        RETURNING task.*
        """,
        (worker_id, lease_seconds),
    ).fetchone()
    if task is None:
        return False
    try:
        file_repository.delete_storage_object_if_unreferenced(
            session,
            task["file_uuid"],
            task["storage_type"],
            task["storage_id"],
            task["storage_object_id"],
        )
        file_storage.delete_workspace_file_metadata(
            _uuid(task["file_uuid"]),
            storage_type=task["storage_type"],
        )
    except Exception as error:
        delay_seconds = min(5 * (2 ** min(int(task["attempts"]) - 1, 8)), 1200)
        session.execute(
            """
            UPDATE messenger_provider_file_cleanup_tasks
            SET status = 'failed', safe_error = %s,
                lease_owner = NULL, lease_expires_at = NULL,
                next_retry_at = NOW() + make_interval(secs => %s),
                updated_at = NOW()
            WHERE uuid = %s AND lease_owner = %s
            """,
            (type(error).__name__[:128], delay_seconds, task["uuid"], worker_id),
        )
        LOG.exception(
            "Failed to delete reset Zulip file content",
            extra={"provider_file_cleanup_uuid": str(task["uuid"])},
        )
        return True
    session.execute(
        """
        UPDATE messenger_provider_file_cleanup_tasks
        SET status = 'completed', safe_error = NULL,
            lease_owner = NULL, lease_expires_at = NULL, updated_at = NOW()
        WHERE uuid = %s AND lease_owner = %s
        """,
        (task["uuid"], worker_id),
    )
    return True


def _plain(value: object) -> object:
    return resource_projection.simple(value)


def _uuid(value: object) -> sys_uuid.UUID:
    return sys_uuid.UUID(str(value))


def _guard_emitted_events(
    session: typing.Any,
    emitted: object,
    *,
    project_id: object,
    stream_uuid: object,
    membership_generations: typing.Mapping[object, object] | None = None,
    control_effect: bool = False,
) -> None:
    """Fence ready event rows to the membership generation that produced them."""
    items = emitted if isinstance(emitted, (list, tuple, set)) else [emitted]
    epoch_versions: list[int] = []
    event_members: list[tuple[sys_uuid.UUID, sys_uuid.UUID]] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, int):
            epoch_versions.append(item)
            continue
        event_uuid = getattr(item, "uuid", None)
        user_uuid = getattr(item, "user_uuid", None)
        if event_uuid is not None and user_uuid is not None:
            event_members.append((_uuid(event_uuid), _uuid(user_uuid)))
    if epoch_versions:
        event_members.extend(
            (_uuid(row["event_uuid"]), _uuid(row["user_uuid"]))
            for row in session.execute(
                """
                SELECT event.uuid AS event_uuid, member.user_uuid
                FROM m_workspace_broadcast_message_events_v1 AS event
                JOIN m_workspace_event_audience_members_v1 AS member
                  ON member.audience_snapshot_uuid = event.audience_snapshot_uuid
                WHERE event.project_id = %s
                  AND event.epoch_version = ANY(%s::bigint[])
                """,
                (project_id, epoch_versions),
            ).fetchall()
        )
    if not event_members:
        return
    expected = {
        _uuid(user_uuid): int(str(generation))
        for user_uuid, generation in (membership_generations or {}).items()
    }
    missing = sorted(
        {user_uuid for _, user_uuid in event_members if user_uuid not in expected},
        key=str,
    )
    if missing:
        expected.update(
            {
                _uuid(row["user_uuid"]): int(row["membership_generation"])
                for row in session.execute(
                    """
                    SELECT user_uuid, membership_generation
                    FROM messenger_stream_bindings
                    WHERE project_id = %s AND stream_uuid = %s
                      AND user_uuid = ANY(%s::uuid[])
                    """,
                    (project_id, stream_uuid, missing),
                ).fetchall()
            }
        )
    unresolved = sorted(
        {user_uuid for _, user_uuid in event_members if user_uuid not in expected},
        key=str,
    )
    if unresolved and control_effect:
        expected.update({user_uuid: 1 for user_uuid in unresolved})
        unresolved = []
    if unresolved:
        raise RuntimeError(
            "Cannot persist membership-fenced event without a generation for "
            + ", ".join(str(value) for value in unresolved)
        )
    persisted_stream_uuid: object | None = stream_uuid
    if control_effect:
        stream_exists = session.execute(
            """
            SELECT 1 FROM messenger_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, stream_uuid),
        ).fetchone()
        if stream_exists is None:
            persisted_stream_uuid = None
    ordered = sorted(
        set(event_members), key=lambda value: (str(value[0]), str(value[1]))
    )
    session.execute(
        """
        INSERT INTO messenger_event_membership_guards (
            event_uuid, project_id, user_uuid, stream_uuid,
            membership_generation, control_effect
        )
        SELECT input.event_uuid, %s, input.user_uuid, %s,
               input.membership_generation, %s
        FROM unnest(%s::uuid[], %s::uuid[], %s::integer[]) AS input(
            event_uuid, user_uuid, membership_generation
        )
        ON CONFLICT (event_uuid, user_uuid) DO NOTHING
        """,
        (
            project_id,
            persisted_stream_uuid,
            control_effect,
            [event_uuid for event_uuid, _ in ordered],
            [user_uuid for _, user_uuid in ordered],
            [expected[user_uuid] for _, user_uuid in ordered],
        ),
    )


def _lane_predicate(alias: str, kind_column: str, lane: str) -> str:
    interactive_read = f"""
        {alias}.{kind_column} = 'read_counters'
        AND {alias}.payload->>'source_kind' IN (
            'message.read', 'messages.read', 'stream.read', 'topic.read'
        )
    """
    predicates = {
        "fanout": f"{alias}.{kind_column} = 'fanout'",
        "interactive_read": interactive_read,
        "reaction_snapshot": f"{alias}.{kind_column} = 'reaction_snapshot'",
        "read_state": (
            f"{alias}.{kind_column} = 'read_counters' AND NOT ({interactive_read})"
        ),
        "background": (
            f"{alias}.{kind_column} IN ("
            "'content_mentions', 'folder_projection', "
            "'delivery_snapshot_event', 'topic_state_projection', "
            "'topic_membership_policy_rebuild')"
        ),
    }
    return predicates[lane]


def _derive_projection_tasks_for_lane(
    session: typing.Any,
    limit: int,
    lane_predicate: str,
) -> int:
    rows = session.execute(
        f"""
        WITH candidates AS (
            SELECT event.*
            FROM messenger_domain_outbox_events AS event
            LEFT JOIN messenger_projection_tasks AS task
              ON task.project_id = event.project_id
             AND task.outbox_event_uuid = event.uuid
            WHERE task.uuid IS NULL
              AND ({lane_predicate})
            ORDER BY event.created_at, event.uuid
            LIMIT %s
            FOR UPDATE OF event SKIP LOCKED
        )
        INSERT INTO messenger_projection_tasks (
            uuid, project_id, outbox_event_uuid, task_kind,
            scope_kind, scope_key, ordering_key, ordering_created_at, payload
        )
        SELECT
            messenger_uuid_v5(uuid, 'projection-task:' || event_kind),
            project_id, uuid, event_kind, scope_kind, scope_key,
            COALESCE(
                payload->>'placement_uuid',
                payload#>>'{{placement,uuid}}',
                payload->>'resource_uuid',
                payload->>'canonical_message_uuid',
                payload->>'topic_uuid',
                payload->>'stream_uuid',
                payload->>'folder_uuid',
                payload->>'user_uuid',
                uuid::text
            ),
            COALESCE(
                (payload->>'message_created_at')::timestamptz,
                (payload->>'audience_created_before')::timestamptz,
                (payload->>'membership_started_at')::timestamptz,
                created_at
            ),
            payload
        FROM candidates
        ON CONFLICT (project_id, outbox_event_uuid) DO NOTHING
        RETURNING uuid
        """,
        (limit,),
    ).fetchall()
    return len(rows)


def derive_projection_tasks(
    session: typing.Any, limit: int = DEFAULT_DERIVE_LIMIT
) -> int:
    """Derive a fair bounded outbox slice without starving fresh fanout."""
    preferred_lane = next(_FAIR_DERIVATION_CYCLE)
    derived = _derive_projection_tasks_for_lane(
        session,
        limit,
        _lane_predicate("event", "event_kind", preferred_lane),
    )
    if derived < limit:
        derived += _derive_projection_tasks_for_lane(
            session,
            limit - derived,
            "TRUE",
        )
    return derived


def _claim_task(
    session: typing.Any,
    worker_id: str,
    lease_seconds: int,
) -> typing.Any | None:
    preferred_lane = next(_FAIR_SCHEDULER_CYCLE)
    lane_name = preferred_lane
    candidate_lane_predicate = _lane_predicate("candidate", "task_kind", preferred_lane)
    task_lane_predicate = _lane_predicate("task", "task_kind", preferred_lane)
    project_id = _try_lock_claim_project(
        session,
        candidate_lane_predicate,
        worker_id,
    )
    if project_id is None:
        lane_name = "fallback"
        candidate_lane_predicate = "TRUE"
        task_lane_predicate = "TRUE"
        project_id = _try_lock_claim_project(
            session,
            candidate_lane_predicate,
            worker_id,
        )
    if project_id is None:
        return None
    claim_sql = f"""
        /* workspace_projection_lane={{lane_name}} */
        WITH candidates AS MATERIALIZED (
            SELECT candidate.project_id, candidate.uuid
            FROM messenger_projection_tasks AS candidate
            LEFT JOIN messenger_projection_scope_leases AS candidate_scope_lease
              ON candidate_scope_lease.project_id = candidate.project_id
             AND candidate_scope_lease.scope_kind = candidate.scope_kind
             AND candidate_scope_lease.scope_key = candidate.scope_key
            WHERE candidate.project_id = %s
              AND (
                    candidate.status IN ('pending', 'failed')
                    OR (
                        candidate.status = 'running'
                        AND candidate.lease_expires_at <= NOW()
                    )
                  )
              AND (
                    candidate.status = 'running'
                    OR candidate.next_retry_at IS NULL
                    OR candidate.next_retry_at <= NOW()
                  )
              AND (
                    candidate.lease_expires_at IS NULL
                    OR candidate.lease_expires_at <= NOW()
                  )
              AND candidate.status NOT IN ('completed', 'dead_letter')
              AND NOT EXISTS (
                    SELECT 1
                    FROM messenger_projection_tasks AS predecessor
                    WHERE predecessor.project_id = candidate.project_id
                      AND predecessor.scope_kind = candidate.scope_kind
                      AND predecessor.scope_key = candidate.scope_key
                      AND predecessor.ordering_key = candidate.ordering_key
                      AND predecessor.task_kind = candidate.task_kind
                      AND (
                            predecessor.created_at,
                            predecessor.ordering_created_at,
                            predecessor.outbox_event_uuid
                          ) < (
                            candidate.created_at,
                            candidate.ordering_created_at,
                            candidate.outbox_event_uuid
                          )
                      AND predecessor.status NOT IN (
                            'completed', 'dead_letter'
                          )
              )
              AND ({{candidate_lane_predicate}})
              AND (
                    candidate_scope_lease.uuid IS NULL
                    OR candidate_scope_lease.lease_expires_at IS NULL
                    OR candidate_scope_lease.lease_expires_at <= NOW()
                    OR candidate_scope_lease.owner = %s
                  )
            ORDER BY candidate.created_at, candidate.ordering_created_at,
                     candidate.outbox_event_uuid
            LIMIT {CLAIM_CANDIDATE_LIMIT}
        )
        SELECT task.uuid, task.project_id, task.outbox_event_uuid,
               task.task_kind, task.scope_kind, task.scope_key,
               task.ordering_key, task.ordering_created_at, task.payload,
               task.status, task.lease_owner, task.fencing_token,
               task.lease_expires_at, task.attempts, task.next_retry_at,
               task.last_error, task.progress_created_at, task.progress_uuid,
               task.processed_count, task.created_at, task.updated_at
             , event.created_at AS outbox_created_at
             , EXTRACT(EPOCH FROM (NOW() - task.created_at)) AS task_age_seconds
             , EXTRACT(EPOCH FROM (NOW() - event.created_at)) AS outbox_age_seconds
        FROM candidates AS candidate
        JOIN messenger_projection_tasks AS task
          ON task.project_id = candidate.project_id
         AND task.uuid = candidate.uuid
        JOIN messenger_domain_outbox_events AS event
          ON event.project_id = task.project_id
         AND event.uuid = task.outbox_event_uuid
        LEFT JOIN messenger_projection_scope_leases AS scope_lease
          ON scope_lease.project_id = task.project_id
         AND scope_lease.scope_kind = task.scope_kind
         AND scope_lease.scope_key = task.scope_key
        WHERE (
                task.status IN ('pending', 'failed')
                OR (
                    task.status = 'running'
                    AND task.lease_expires_at <= NOW()
                )
              )
          AND (
                task.status = 'running'
                OR task.next_retry_at IS NULL
                OR task.next_retry_at <= NOW()
              )
          AND (task.lease_expires_at IS NULL OR task.lease_expires_at <= NOW())
          AND NOT EXISTS (
                SELECT 1
                FROM messenger_projection_tasks AS predecessor
                WHERE predecessor.project_id = task.project_id
                  AND predecessor.scope_kind = task.scope_kind
                  AND predecessor.scope_key = task.scope_key
                  AND predecessor.ordering_key = task.ordering_key
                  AND predecessor.task_kind = task.task_kind
                  AND (
                        predecessor.created_at,
                        predecessor.ordering_created_at,
                        predecessor.outbox_event_uuid
                      ) < (
                        task.created_at,
                        task.ordering_created_at,
                        task.outbox_event_uuid
                      )
                  AND predecessor.status NOT IN ('completed', 'dead_letter')
          )
          AND task.status NOT IN ('completed', 'dead_letter')
          AND ({{task_lane_predicate}})
          AND (
                scope_lease.uuid IS NULL
                OR scope_lease.lease_expires_at IS NULL
                OR scope_lease.lease_expires_at <= NOW()
                OR scope_lease.owner = %s
              )
        ORDER BY task.created_at, task.ordering_created_at,
                 task.outbox_event_uuid
        LIMIT 1
        FOR UPDATE OF task SKIP LOCKED
    """
    task = session.execute(
        claim_sql.format(
            lane_name=lane_name,
            candidate_lane_predicate=candidate_lane_predicate,
            task_lane_predicate=task_lane_predicate,
        ),
        (project_id, worker_id, worker_id),
    ).fetchone()
    if task is None:
        return None
    lease = session.execute(
        """
        INSERT INTO messenger_projection_scope_leases (
            uuid, project_id, scope_kind, scope_key, owner,
            fencing_token, lease_expires_at
        ) VALUES (
            messenger_uuid_v5(%s, %s), %s, %s, %s, %s, 1,
            NOW() + make_interval(secs => %s)
        )
        ON CONFLICT (project_id, scope_kind, scope_key) DO UPDATE
        SET owner = EXCLUDED.owner,
            fencing_token = messenger_projection_scope_leases.fencing_token + 1,
            lease_expires_at = EXCLUDED.lease_expires_at,
            updated_at = NOW()
        WHERE messenger_projection_scope_leases.lease_expires_at IS NULL
           OR messenger_projection_scope_leases.lease_expires_at <= NOW()
           OR messenger_projection_scope_leases.owner = EXCLUDED.owner
        RETURNING fencing_token
        """,
        (
            task["project_id"],
            f"{task['scope_kind']}:{task['scope_key']}",
            task["project_id"],
            task["scope_kind"],
            task["scope_key"],
            worker_id,
            lease_seconds,
        ),
    ).fetchone()
    if lease is None:
        return None
    session.execute(
        """
        UPDATE messenger_projection_tasks
        SET status = 'running', lease_owner = %s, fencing_token = %s,
            lease_expires_at = NOW() + make_interval(secs => %s),
            attempts = attempts + 1, updated_at = NOW()
        WHERE project_id = %s AND uuid = %s
        """,
        (
            worker_id,
            lease["fencing_token"],
            lease_seconds,
            task["project_id"],
            task["uuid"],
        ),
    )
    task = dict(task)
    task["fencing_token"] = lease["fencing_token"]
    task["attempts"] += 1
    return task


def _try_lock_claim_project(
    session: typing.Any,
    lane_predicate: str,
    worker_id: str,
) -> object | None:
    """Take the event project lock before any projection task row lock."""
    rows = session.execute(
        f"""
        WITH per_project AS MATERIALIZED (
            SELECT DISTINCT ON (candidate.project_id)
                   candidate.project_id, candidate.created_at,
                   candidate.ordering_created_at,
                   candidate.outbox_event_uuid
            FROM messenger_projection_tasks AS candidate
            LEFT JOIN messenger_projection_scope_leases AS scope_lease
              ON scope_lease.project_id = candidate.project_id
             AND scope_lease.scope_kind = candidate.scope_kind
             AND scope_lease.scope_key = candidate.scope_key
            WHERE (
                    candidate.status IN ('pending', 'failed')
                    OR (
                        candidate.status = 'running'
                        AND candidate.lease_expires_at <= NOW()
                    )
                  )
              AND (
                    candidate.status = 'running'
                    OR candidate.next_retry_at IS NULL
                    OR candidate.next_retry_at <= NOW()
                  )
              AND (
                    candidate.lease_expires_at IS NULL
                    OR candidate.lease_expires_at <= NOW()
                  )
              AND candidate.status NOT IN ('completed', 'dead_letter')
              AND NOT EXISTS (
                    SELECT 1
                    FROM messenger_projection_tasks AS predecessor
                    WHERE predecessor.project_id = candidate.project_id
                      AND predecessor.scope_kind = candidate.scope_kind
                      AND predecessor.scope_key = candidate.scope_key
                      AND predecessor.ordering_key = candidate.ordering_key
                      AND predecessor.task_kind = candidate.task_kind
                      AND (
                            predecessor.created_at,
                            predecessor.ordering_created_at,
                            predecessor.outbox_event_uuid
                          ) < (
                            candidate.created_at,
                            candidate.ordering_created_at,
                            candidate.outbox_event_uuid
                          )
                      AND predecessor.status NOT IN (
                            'completed', 'dead_letter'
                          )
              )
              AND (
                    scope_lease.uuid IS NULL
                    OR scope_lease.lease_expires_at IS NULL
                    OR scope_lease.lease_expires_at <= NOW()
                    OR scope_lease.owner = %s
                  )
              AND ({lane_predicate})
            ORDER BY candidate.project_id, candidate.created_at,
                     candidate.ordering_created_at,
                     candidate.outbox_event_uuid
        ), candidates AS MATERIALIZED (
            SELECT candidate.project_id, candidate.created_at,
                   candidate.ordering_created_at,
                   candidate.outbox_event_uuid
            FROM per_project AS candidate
            ORDER BY candidate.created_at, candidate.ordering_created_at,
                     candidate.outbox_event_uuid
            LIMIT {CLAIM_PROJECT_LIMIT}
        )
        SELECT project_id
        FROM candidates
        ORDER BY created_at, ordering_created_at, outbox_event_uuid
        """,
        (worker_id,),
    ).fetchall()
    for row in rows:
        locked = session.execute(
            """
            SELECT pg_try_advisory_xact_lock(
                hashtextextended(%s::text, 0)
            ) AS locked
            """,
            (row["project_id"],),
        ).fetchone()["locked"]
        if locked:
            return row["project_id"]
    return None


def _v2_rows(
    model: typing.Any,
    project_id: object,
    filters: dict[str, typing.Any],
    *,
    order_by: dict[str, str] | None = None,
) -> list[typing.Any]:
    return model.objects.get_all(
        filters={"project_id": dm_filters.EQ(project_id), **filters},
        order_by=order_by,
        session=contexts.Context().get_session(),
    )


def _emit_message_updated_rows(
    session: typing.Any,
    project_id: object,
    rows: typing.Iterable[typing.Any],
) -> None:
    grouped: dict[object, list[typing.Any]] = {}
    for row in rows:
        grouped.setdefault(row.uuid, []).append(row)
    for placement_uuid in sorted(grouped, key=str):
        placement_rows = grouped[placement_uuid]
        emitted = messenger_events.create_message_updated_events(
            project_id,
            placement_rows,
            session=session,
            compact=True,
        )
        _guard_emitted_events(
            session,
            emitted,
            project_id=project_id,
            stream_uuid=placement_rows[0].stream_uuid,
        )


def _message_namespace(
    session: typing.Any,
    project_id: object,
    placement_uuid: object,
) -> typing.Any | None:
    row = session.execute(
        """
        SELECT placement.uuid, placement.stream_uuid, placement.topic_uuid,
               message.uuid AS canonical_message_uuid,
               message.author_uuid AS user_uuid, message.payload,
               message.created_at, message.updated_at, message.source_name,
               message.source, message.reaction_users
        FROM messenger_message_placements AS placement
        JOIN messenger_messages AS message
          ON message.project_id = placement.project_id
         AND message.uuid = placement.message_uuid
        WHERE placement.project_id = %s AND placement.uuid = %s
          AND message.deleted_at IS NULL
        """,
        (project_id, placement_uuid),
    ).fetchone()
    if row is None:
        return None
    return types.SimpleNamespace(**dict(row))


def _refresh_recipient_counters(
    session: typing.Any,
    project_id: object,
    stream_uuid: object,
    topic_uuid: object,
    user_uuids: typing.Iterable[object],
    scope_kind: str,
) -> None:
    recipients = [_uuid(value) for value in user_uuids]
    if not recipients:
        return
    if scope_kind == "user-stream":
        session.execute(
            """
        UPDATE messenger_stream_bindings AS binding
        SET unread_count = snapshot.unread_count,
            active_unread_count = snapshot.active_unread_count,
            passive_unread_count =
                snapshot.unread_count - snapshot.active_unread_count,
            last_message_uuid = snapshot.last_message_uuid,
            updated_at = NOW()
        FROM (
            SELECT target.user_uuid,
                   count(state.uuid) FILTER (WHERE state.read_at IS NULL) AS unread_count,
                   count(state.uuid) FILTER (
                       WHERE state.read_at IS NULL AND CASE
                           WHEN topic_binding.notification_mode = 'mute' THEN false
                           WHEN topic_binding.notification_mode = 'follow' THEN true
                           WHEN topic_binding.notification_mode = 'unmute'
                               THEN state.mentioned
                           WHEN target.notification_mode = 'all_messages' THEN true
                           WHEN target.notification_mode = 'mentions_only'
                               THEN state.mentioned
                           ELSE false
                       END
                   ) AS active_unread_count,
                   (array_agg(placement.uuid ORDER BY message.created_at DESC,
                              placement.uuid DESC))[1] AS last_message_uuid
            FROM messenger_stream_bindings AS target
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
            LEFT JOIN messenger_user_message_states AS state
              ON state.project_id = placement.project_id
             AND state.placement_uuid = placement.uuid
             AND state.user_uuid = target.user_uuid
             AND state.membership_generation = target.membership_generation
            LEFT JOIN messenger_user_topic_bindings AS topic_binding
              ON topic_binding.project_id = placement.project_id
             AND topic_binding.topic_uuid = placement.topic_uuid
             AND topic_binding.user_uuid = target.user_uuid
            WHERE target.project_id = %s AND target.stream_uuid = %s
              AND target.user_uuid = ANY(%s::uuid[]) AND target.active
            GROUP BY target.user_uuid
        ) AS snapshot
        WHERE binding.project_id = %s AND binding.stream_uuid = %s
          AND binding.user_uuid = snapshot.user_uuid
            """,
            (project_id, stream_uuid, recipients, project_id, stream_uuid),
        )
        return
    if scope_kind != "user-topic":
        raise ValueError(f"Unsupported counter scope {scope_kind}")
    session.execute(
        """
        UPDATE messenger_user_topic_bindings AS binding
        SET unread_count = snapshot.unread_count,
            active_unread_count = snapshot.active_unread_count,
            passive_unread_count =
                snapshot.unread_count - snapshot.active_unread_count,
            last_message_uuid = snapshot.last_message_uuid,
            updated_at = NOW()
        FROM (
            SELECT target.user_uuid,
                   count(state.uuid) FILTER (WHERE state.read_at IS NULL) AS unread_count,
                   count(state.uuid) FILTER (
                       WHERE state.read_at IS NULL AND CASE
                           WHEN target.notification_mode = 'mute' THEN false
                           WHEN target.notification_mode = 'follow' THEN true
                           WHEN target.notification_mode = 'unmute'
                               THEN state.mentioned
                           WHEN stream_binding.notification_mode = 'all_messages'
                               THEN true
                           WHEN stream_binding.notification_mode = 'mentions_only'
                               THEN state.mentioned
                           ELSE false
                       END
                   ) AS active_unread_count,
                   (array_agg(placement.uuid ORDER BY message.created_at DESC,
                              placement.uuid DESC))[1] AS last_message_uuid
            FROM messenger_user_topic_bindings AS target
            LEFT JOIN messenger_message_placements AS placement
              ON placement.project_id = target.project_id
             AND placement.topic_uuid = target.topic_uuid
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
            LEFT JOIN messenger_user_message_states AS state
              ON state.project_id = placement.project_id
             AND state.placement_uuid = placement.uuid
             AND state.user_uuid = target.user_uuid
            JOIN messenger_topics AS topic
              ON topic.project_id = target.project_id
             AND topic.uuid = target.topic_uuid
            JOIN messenger_stream_bindings AS stream_binding
              ON stream_binding.project_id = topic.project_id
             AND stream_binding.stream_uuid = topic.stream_uuid
             AND stream_binding.user_uuid = target.user_uuid
             AND stream_binding.active
            WHERE target.project_id = %s AND target.topic_uuid = %s
              AND target.user_uuid = ANY(%s::uuid[])
            GROUP BY target.user_uuid
        ) AS snapshot
        WHERE binding.project_id = %s AND binding.topic_uuid = %s
          AND binding.user_uuid = snapshot.user_uuid
        """,
        (project_id, topic_uuid, recipients, project_id, topic_uuid),
    )


def _emit_unread_snapshots(
    session: typing.Any,
    project_id: object,
    stream_uuid: object,
    topic_uuid: object,
    user_uuids: typing.Iterable[object],
    scope_kind: str,
) -> None:
    recipients = [_uuid(value) for value in user_uuids]
    if not recipients:
        return
    if scope_kind == "user-topic":
        topics = _v2_rows(
            v2_models.WorkspaceUserTopic,
            project_id,
            {
                "uuid": dm_filters.EQ(topic_uuid),
                "user_uuid": dm_filters.In(recipients),
            },
            order_by={"user_uuid": "asc"},
        )
        emitted = messenger_events.create_topic_updated_events(
            project_id, topics, session=session, compact=True
        )
        if topics:
            _guard_emitted_events(
                session,
                emitted,
                project_id=project_id,
                stream_uuid=stream_uuid,
            )
        return
    if scope_kind != "user-stream":
        raise ValueError(f"Unsupported counter scope {scope_kind}")
    streams = _v2_rows(
        v2_models.WorkspaceUserStream,
        project_id,
        {
            "uuid": dm_filters.EQ(stream_uuid),
            "user_uuid": dm_filters.In(recipients),
        },
        order_by={"user_uuid": "asc"},
    )
    emitted = messenger_events.create_stream_updated_events(
        project_id, streams, session=session, compact=True
    )
    if streams:
        _guard_emitted_events(
            session,
            emitted,
            project_id=project_id,
            stream_uuid=stream_uuid,
        )


def _enqueue_counter_outbox_events(
    session: typing.Any,
    *,
    source_event_uuid: object,
    project_id: object,
    source_kind: str,
    user_uuid: object,
    stream_uuid: object,
    topic_uuid: object,
    placement_uuid: object | None = None,
    include_topic: bool = True,
) -> None:
    common_payload = {
        "source_kind": source_kind,
        "user_uuid": user_uuid,
        "stream_uuid": stream_uuid,
        "topic_uuid": topic_uuid,
    }
    if placement_uuid is not None:
        common_payload["placement_uuid"] = placement_uuid
    scopes = [("user-stream", stream_uuid)]
    if include_topic:
        scopes.append(("user-topic", topic_uuid))
    for scope_kind, scope_uuid in scopes:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                messenger_uuid_v5(%s, %s), %s, 'read_counters', %s, %s,
                %s::jsonb
            )
            ON CONFLICT (project_id, uuid) DO NOTHING
            """,
            (
                source_event_uuid,
                f"counter:{scope_kind}:{user_uuid}",
                project_id,
                scope_kind,
                f"{project_id}:{user_uuid}:{scope_uuid}",
                json.dumps(_plain(common_payload)),
            ),
        )


def _enqueue_folder_outbox_events(
    session: typing.Any,
    *,
    source_event_uuid: object,
    project_id: object,
    source_kind: str,
    user_uuid: object,
    stream_uuid: object,
) -> None:
    stream = session.execute(
        """
        SELECT private FROM messenger_streams
        WHERE project_id = %s AND uuid = %s
        """,
        (project_id, stream_uuid),
    ).fetchone()
    if stream is None:
        return
    folder_uuids = {
        helpers_uuid
        for helpers_uuid in (
            sys_uuid.UUID("00000000-0000-0000-0000-000000000000"),
            sys_uuid.UUID(
                "00000000-0000-0000-0000-000000000001"
                if stream["private"]
                else "00000000-0000-0000-0000-000000000002"
            ),
        )
    }
    folder_uuids.update(
        _uuid(row["folder_uuid"])
        for row in session.execute(
            """
            SELECT DISTINCT folder_uuid
            FROM messenger_folder_items
            WHERE project_id = %s AND user_uuid = %s AND stream_uuid = %s
            """,
            (project_id, user_uuid, stream_uuid),
        ).fetchall()
    )
    for folder_uuid in sorted(folder_uuids, key=str):
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                messenger_uuid_v5(%s, %s), %s, 'folder_projection',
                'user-folder', %s, %s::jsonb
            )
            ON CONFLICT (project_id, uuid) DO NOTHING
            """,
            (
                source_event_uuid,
                f"folder:{user_uuid}:{folder_uuid}",
                project_id,
                f"{project_id}:{user_uuid}:{folder_uuid}",
                json.dumps(
                    _plain(
                        {
                            "source_kind": source_kind,
                            "user_uuid": user_uuid,
                            "stream_uuid": stream_uuid,
                            "folder_uuid": folder_uuid,
                        }
                    )
                ),
            ),
        )


def _process_fanout(
    session: typing.Any,
    task: typing.Mapping[str, typing.Any],
    batch_size: int,
) -> bool:
    project_id = task["project_id"]
    placement_uuid = _uuid(task["payload"]["placement_uuid"])
    message = _message_namespace(session, project_id, placement_uuid)
    if message is None:
        return True
    audience_created_before = task["payload"].get(
        "audience_created_before", message.created_at
    )
    root_uuid = sys_uuid.uuid5(_uuid(task["outbox_event_uuid"]), "fanout-root")
    root = session.execute(
        """
        INSERT INTO messenger_fanout_roots (
            uuid, project_id, outbox_event_uuid, placement_uuid
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT (project_id, outbox_event_uuid) DO UPDATE
        SET updated_at = NOW()
        RETURNING next_user_uuid, processed_count
        """,
        (root_uuid, project_id, task["outbox_event_uuid"], placement_uuid),
    ).fetchone()
    cursor = root["next_user_uuid"]
    recipients = session.execute(
        """
        SELECT user_uuid, membership_generation
        FROM messenger_stream_bindings
        WHERE project_id = %s AND stream_uuid = %s AND active
          AND membership_started_at <= %s::timestamptz
          AND (%s::uuid IS NULL OR user_uuid > %s::uuid)
        ORDER BY user_uuid
        LIMIT %s
        FOR UPDATE
        """,
        (
            project_id,
            message.stream_uuid,
            audience_created_before,
            cursor,
            cursor,
            batch_size + 1,
        ),
    ).fetchall()
    has_more = len(recipients) > batch_size
    recipients = recipients[:batch_size]
    if not recipients:
        session.execute(
            """
            UPDATE messenger_fanout_roots
            SET status = 'completed', updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, root_uuid),
        )
        return True
    content = str(message.payload.get("content", "")).lower()
    author_uuid = _uuid(message.user_uuid)
    now = datetime.datetime.now(datetime.timezone.utc)
    accepted_recipients = []
    for recipient in recipients:
        user_uuid = _uuid(recipient["user_uuid"])
        membership_generation = int(recipient["membership_generation"])
        current_membership = session.execute(
            """
            SELECT 1 FROM messenger_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
              AND active AND membership_generation = %s
              AND membership_started_at <= %s::timestamptz
            """,
            (
                project_id,
                message.stream_uuid,
                user_uuid,
                membership_generation,
                audience_created_before,
            ),
        ).fetchone()
        if current_membership is None:
            continue
        accepted_recipients.append(user_uuid)
        row_uuid = sys_uuid.uuid5(placement_uuid, str(user_uuid))
        session.execute(
            """
            INSERT INTO messenger_user_message_bindings (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, relation_role, visibility,
                permissions, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'visible',
                '{"read":true,"react":true,"star":true,"pin":true}'::jsonb,
                %s, %s
            )
            ON CONFLICT (project_id, placement_uuid, user_uuid) DO UPDATE
            SET membership_generation = EXCLUDED.membership_generation,
                relation_role = EXCLUDED.relation_role,
                visibility = EXCLUDED.visibility,
                permissions = EXCLUDED.permissions,
                updated_at = NOW()
            WHERE messenger_user_message_bindings.membership_generation
                  <> EXCLUDED.membership_generation
            """,
            (
                row_uuid,
                project_id,
                placement_uuid,
                user_uuid,
                membership_generation,
                "author" if user_uuid == author_uuid else "member",
                now,
                now,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_user_message_states (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, read_at, mentioned,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE
            SET membership_generation = EXCLUDED.membership_generation,
                read_at = EXCLUDED.read_at,
                mentioned = EXCLUDED.mentioned,
                starred = false,
                pinned = false,
                updated_at = NOW()
            WHERE messenger_user_message_states.membership_generation
                  <> EXCLUDED.membership_generation
            """,
            (
                row_uuid,
                project_id,
                placement_uuid,
                user_uuid,
                membership_generation,
                now if user_uuid == author_uuid else None,
                f"](urn:user:{str(user_uuid).lower()})" in content,
                now,
                now,
            ),
        )
    recipient_uuids = accepted_recipients
    if task["payload"].get("emit_public_event", True):
        emitted = messenger_events.create_message_events(
            project_id,
            message,
            recipient_uuids,
            session=session,
            compact=True,
        )
        _guard_emitted_events(
            session,
            emitted,
            project_id=project_id,
            stream_uuid=message.stream_uuid,
            membership_generations={
                _uuid(recipient["user_uuid"]): int(recipient["membership_generation"])
                for recipient in recipients
                if _uuid(recipient["user_uuid"]) in set(recipient_uuids)
            },
        )
    for user_uuid in recipient_uuids:
        _enqueue_counter_outbox_events(
            session,
            source_event_uuid=task["outbox_event_uuid"],
            project_id=project_id,
            source_kind="message.created",
            placement_uuid=placement_uuid,
            user_uuid=user_uuid,
            stream_uuid=message.stream_uuid,
            topic_uuid=message.topic_uuid,
        )
    batch_no = int(root["processed_count"]) // batch_size
    session.execute(
        """
        INSERT INTO messenger_fanout_batch_tasks (
            uuid, project_id, fanout_root_uuid, batch_no,
            start_user_uuid, end_user_uuid, batch_size, status
        ) VALUES (
            messenger_uuid_v5(%s, %s), %s, %s, %s, %s, %s, %s, 'completed'
        )
        ON CONFLICT (project_id, fanout_root_uuid, batch_no) DO UPDATE
        SET status = 'completed', updated_at = NOW()
        """,
        (
            root_uuid,
            f"batch:{batch_no}",
            project_id,
            root_uuid,
            batch_no,
            recipients[0]["user_uuid"],
            recipients[-1]["user_uuid"],
            len(recipients),
        ),
    )
    session.execute(
        """
        UPDATE messenger_fanout_roots
        SET next_user_uuid = %s,
            processed_count = processed_count + %s,
            status = %s,
            updated_at = NOW()
        WHERE project_id = %s AND uuid = %s
        """,
        (
            recipients[-1]["user_uuid"],
            len(recipients),
            "running" if has_more else "completed",
            project_id,
            root_uuid,
        ),
    )
    return not has_more


def _process_content_mentions(
    session: typing.Any,
    task: typing.Mapping[str, typing.Any],
    batch_size: int,
) -> bool:
    placement_uuid = _uuid(task["payload"]["placement_uuid"])
    message = _message_namespace(session, task["project_id"], placement_uuid)
    if message is None:
        return True
    content = str(message.payload.get("content", "")).lower()
    states = session.execute(
        """
        SELECT user_uuid
        FROM messenger_user_message_states
        WHERE project_id = %s AND placement_uuid = %s
          AND (%s::uuid IS NULL OR user_uuid > %s::uuid)
        ORDER BY user_uuid
        LIMIT %s
        """,
        (
            task["project_id"],
            placement_uuid,
            task["progress_uuid"],
            task["progress_uuid"],
            min(max(batch_size, 1), MAX_FANOUT_BATCH_SIZE) + 1,
        ),
    ).fetchall()
    has_more = len(states) > batch_size
    states = states[:batch_size]
    recipient_uuids = []
    for row in states:
        user_uuid = _uuid(row["user_uuid"])
        recipient_uuids.append(user_uuid)
        session.execute(
            """
            UPDATE messenger_user_message_states
            SET mentioned = %s, updated_at = NOW()
            WHERE project_id = %s AND placement_uuid = %s AND user_uuid = %s
            """,
            (
                f"](urn:user:{str(user_uuid).lower()})" in content,
                task["project_id"],
                placement_uuid,
                user_uuid,
            ),
        )
        _enqueue_counter_outbox_events(
            session,
            source_event_uuid=task["outbox_event_uuid"],
            project_id=task["project_id"],
            source_kind=task["payload"]["source_kind"],
            user_uuid=user_uuid,
            stream_uuid=message.stream_uuid,
            topic_uuid=message.topic_uuid,
            placement_uuid=placement_uuid,
        )
    if (
        task["payload"]["source_kind"] == "message.updated"
        and task["payload"].get("emit_message_updated", True)
        and recipient_uuids
    ):
        rows = _v2_rows(
            v2_models.WorkspaceUserMessage,
            task["project_id"],
            {
                "uuid": dm_filters.EQ(placement_uuid),
                "user_uuid": dm_filters.In(recipient_uuids),
            },
            order_by={"user_uuid": "asc"},
        )
        _emit_message_updated_rows(session, task["project_id"], rows)
    if states:
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET progress_uuid = %s,
                processed_count = processed_count + %s,
                updated_at = NOW()
            WHERE project_id = %s AND uuid = %s AND fencing_token = %s
            """,
            (
                states[-1]["user_uuid"],
                len(states),
                task["project_id"],
                task["uuid"],
                task["fencing_token"],
            ),
        )
    return not has_more


def _process_reaction_snapshot(
    session: typing.Any, task: typing.Mapping[str, typing.Any]
) -> None:
    placement_uuid = _uuid(task["payload"]["placement_uuid"])
    message = _message_namespace(session, task["project_id"], placement_uuid)
    if message is None:
        return
    siblings = session.execute(
        """
        SELECT sibling.uuid, sibling.project_id, sibling.outbox_event_uuid,
               sibling.task_kind, sibling.scope_kind, sibling.scope_key,
               sibling.ordering_key, sibling.ordering_created_at,
               sibling.payload, sibling.status, sibling.lease_owner,
               sibling.fencing_token, sibling.lease_expires_at,
               sibling.attempts, sibling.next_retry_at, sibling.last_error,
               sibling.progress_created_at, sibling.progress_uuid,
               sibling.processed_count, sibling.created_at,
               sibling.updated_at, event.created_at AS outbox_created_at
        FROM messenger_projection_tasks AS sibling
        JOIN messenger_domain_outbox_events AS event
          ON event.project_id = sibling.project_id
         AND event.uuid = sibling.outbox_event_uuid
        WHERE sibling.project_id = %s
          AND sibling.task_kind = 'reaction_snapshot'
          AND sibling.scope_kind = %s AND sibling.scope_key = %s
          AND sibling.uuid <> %s
          AND sibling.status IN ('pending', 'failed')
          AND (
                sibling.next_retry_at IS NULL
                OR sibling.next_retry_at <= NOW()
              )
          AND (
                sibling.lease_expires_at IS NULL
                OR sibling.lease_expires_at <= NOW()
              )
        ORDER BY event.created_at, event.uuid
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (
            task["project_id"],
            task["scope_kind"],
            task["scope_key"],
            task["uuid"],
            REACTION_COALESCE_LIMIT - 1,
        ),
    ).fetchall()
    snapshots = sorted(
        [task, *(dict(sibling) for sibling in siblings)],
        key=lambda sibling: (
            sibling["outbox_created_at"],
            str(sibling["outbox_event_uuid"]),
        ),
    )
    reaction_fact_count = session.execute(
        """
        SELECT count(*) AS reaction_fact_count
        FROM messenger_message_reaction_facts
        WHERE project_id = %s AND canonical_message_uuid = %s
        """,
        (task["project_id"], message.canonical_message_uuid),
    ).fetchone()["reaction_fact_count"]
    if isinstance(task, dict):
        task["_execution_stats"] = {
            "coalesced_task_count": len(siblings),
            "reaction_fact_count": int(reaction_fact_count),
            "reaction_event_count": sum(
                bool(snapshot["payload"].get("emit_reaction_event"))
                for snapshot in snapshots
            ),
        }
    event_placement_uuids = {
        _uuid(snapshot["payload"].get("placement_uuid", placement_uuid))
        for snapshot in snapshots
        if snapshot["payload"].get("emit_reaction_event")
    }
    messages_by_placement = {placement_uuid: message}
    for event_placement_uuid in sorted(event_placement_uuids, key=str):
        if event_placement_uuid in messages_by_placement:
            continue
        event_message = _message_namespace(
            session,
            task["project_id"],
            event_placement_uuid,
        )
        if event_message is None:
            raise RuntimeError(
                "Cannot emit a reaction event for a missing message placement"
            )
        if event_message.canonical_message_uuid != message.canonical_message_uuid:
            raise RuntimeError(
                "Coalesced reaction event crossed a canonical message boundary"
            )
        messages_by_placement[event_placement_uuid] = event_message
    if isinstance(task, dict):
        task["_execution_stats"]["reaction_placement_count"] = len(
            event_placement_uuids
        )
    session.execute(
        """
        UPDATE messenger_messages AS message
        SET reactions = COALESCE(snapshot.reactions, '{}'::jsonb),
            reaction_users = COALESCE(snapshot.reaction_users, '{}'::jsonb)
        FROM (
            SELECT target.uuid,
                   jsonb_object_agg(grouped.emoji_name, grouped.reaction_count)
                       FILTER (WHERE grouped.emoji_name IS NOT NULL) AS reactions,
                   jsonb_object_agg(grouped.emoji_name, grouped.users)
                       FILTER (WHERE grouped.emoji_name IS NOT NULL) AS reaction_users
            FROM messenger_messages AS target
            LEFT JOIN (
                SELECT canonical_message_uuid, emoji_name,
                       count(*) AS reaction_count,
                       jsonb_agg(user_uuid::text ORDER BY created_at, uuid) AS users
                FROM messenger_message_reaction_facts
                WHERE project_id = %s AND canonical_message_uuid = %s
                GROUP BY canonical_message_uuid, emoji_name
            ) AS grouped ON grouped.canonical_message_uuid = target.uuid
            WHERE target.project_id = %s AND target.uuid = (
                SELECT message_uuid FROM messenger_message_placements
                WHERE project_id = %s AND uuid = %s
            )
            GROUP BY target.uuid
        ) AS snapshot
        WHERE message.project_id = %s AND message.uuid = snapshot.uuid
        """,
        (
            task["project_id"],
            message.canonical_message_uuid,
            task["project_id"],
            task["project_id"],
            placement_uuid,
            task["project_id"],
        ),
    )
    for snapshot in snapshots:
        payload = snapshot["payload"]
        if not payload.get("emit_reaction_event"):
            continue
        event_message = messages_by_placement[
            _uuid(payload.get("placement_uuid", placement_uuid))
        ]
        reaction_values = dict(payload["reaction"])
        for field in ("uuid", "project_id", "message_uuid", "user_uuid"):
            if reaction_values.get(field) is not None:
                reaction_values[field] = _uuid(reaction_values[field])
        reaction = types.SimpleNamespace(**reaction_values)
        source_kind = payload["source_kind"]
        if source_kind == "message_reaction.created":
            emitted = messenger_events.create_message_reaction_created_event(
                reaction, event_message, session=session
            )
        elif source_kind == "message_reaction.updated":
            old_message = types.SimpleNamespace(
                uuid=_uuid(payload["old_message_uuid"]),
                source_name=payload["old_source_name"],
                source=payload["old_source"],
            )
            emitted = messenger_events.create_message_reaction_updated_event(
                reaction,
                event_message,
                old_message,
                payload["old_emoji_name"],
                session=session,
            )
        elif source_kind == "message_reaction.deleted":
            emitted = messenger_events.create_message_reaction_deleted_event(
                reaction, event_message, session=session
            )
        _guard_emitted_events(
            session,
            emitted,
            project_id=task["project_id"],
            stream_uuid=event_message.stream_uuid,
        )
    if any(
        snapshot["payload"].get("emit_message_updated", True) for snapshot in snapshots
    ):
        rows = _v2_rows(
            v2_models.WorkspaceUserMessage,
            task["project_id"],
            {"canonical_message_uuid": dm_filters.EQ(message.canonical_message_uuid)},
            order_by={"uuid": "asc", "user_uuid": "asc"},
        )
        _emit_message_updated_rows(session, task["project_id"], rows)
    if siblings:
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'completed', lease_owner = NULL,
                lease_expires_at = NULL, next_retry_at = NULL,
                last_error = NULL,
                payload = jsonb_set(
                    payload,
                    '{_execution_stats}',
                    COALESCE(payload->'_execution_stats', '{}'::jsonb)
                    || jsonb_build_object(
                        'schema_version', 1,
                        'last_outcome', 'coalesced',
                        'coalesced_by_task_uuid', %s::text,
                        'last_finished_at', to_jsonb(NOW()),
                        'queue_wait_ms', ROUND(
                            EXTRACT(EPOCH FROM (NOW() - created_at)) * 1000
                        )
                    )
                ),
                updated_at = NOW()
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
              AND status IN ('pending', 'failed')
            """,
            (
                task["uuid"],
                task["project_id"],
                [sibling["uuid"] for sibling in siblings],
            ),
        )


def _process_read_counters(
    session: typing.Any, task: typing.Mapping[str, typing.Any]
) -> None:
    payload = task["payload"]
    user_uuid = _uuid(payload["user_uuid"])
    topic_value = payload.get("topic_uuid")
    topic_uuid = None if topic_value is None else _uuid(topic_value)
    if task["scope_kind"] == "user-topic" and topic_uuid is None:
        raise ValueError("Topic counter task is missing its topic UUID")
    if not payload.get("emit_message_read", False):
        # Counter tasks are authoritative snapshots.  One claimed snapshot can
        # absorb every idle snapshot-only sibling for the same user scope;
        # tasks that must emit a per-message read event remain independent.
        session.execute(
            """
            WITH siblings AS MATERIALIZED (
                SELECT uuid
                FROM messenger_projection_tasks
                WHERE project_id = %s AND task_kind = 'read_counters'
                  AND scope_kind = %s AND scope_key = %s AND uuid <> %s
                  AND status IN ('pending', 'failed')
                  AND (lease_expires_at IS NULL OR lease_expires_at <= NOW())
                  AND COALESCE(
                      (payload->>'emit_message_read')::boolean,
                      FALSE
                  ) = FALSE
                FOR UPDATE SKIP LOCKED
            )
            UPDATE messenger_projection_tasks AS task
            SET status = 'completed', lease_owner = NULL,
                lease_expires_at = NULL, next_retry_at = NULL,
                last_error = NULL, updated_at = NOW()
            FROM siblings
            WHERE task.project_id = %s AND task.uuid = siblings.uuid
            """,
            (
                task["project_id"],
                task["scope_kind"],
                task["scope_key"],
                task["uuid"],
                task["project_id"],
            ),
        )
    _refresh_recipient_counters(
        session,
        task["project_id"],
        _uuid(payload["stream_uuid"]),
        topic_uuid,
        [user_uuid],
        task["scope_kind"],
    )
    if task["scope_kind"] == "user-stream":
        _enqueue_folder_outbox_events(
            session,
            source_event_uuid=task["outbox_event_uuid"],
            project_id=task["project_id"],
            source_kind=payload["source_kind"],
            user_uuid=user_uuid,
            stream_uuid=_uuid(payload["stream_uuid"]),
        )
    if payload.get("placement_uuid") and payload.get("emit_message_read"):
        messages = _v2_rows(
            v2_models.WorkspaceUserMessage,
            task["project_id"],
            {
                "uuid": dm_filters.EQ(_uuid(payload["placement_uuid"])),
                "user_uuid": dm_filters.EQ(user_uuid),
            },
        )
        if messages:
            emitted = messenger_events.create_message_read_event(
                messages[0], session=session
            )
            _guard_emitted_events(
                session,
                emitted,
                project_id=task["project_id"],
                stream_uuid=payload["stream_uuid"],
            )
    _emit_unread_snapshots(
        session,
        task["project_id"],
        _uuid(payload["stream_uuid"]),
        topic_uuid,
        [user_uuid],
        task["scope_kind"],
    )


def _process_folder_projection(
    session: typing.Any, task: typing.Mapping[str, typing.Any]
) -> None:
    payload = task["payload"]
    folder_uuid = _uuid(payload["folder_uuid"])
    project_id = task["project_id"]
    user_uuid = _uuid(payload["user_uuid"])
    source_kind = payload["source_kind"]
    stream_uuid = payload.get("stream_uuid")
    if source_kind in LEGACY_FOLDER_SNAPSHOT_SOURCE_KINDS:
        # Legacy flag repair can enqueue the same authoritative folder rebuild
        # once per message.  The claimed rebuild absorbs every idle sibling for
        # this scope because none of them carries a historical snapshot.
        session.execute(
            """
            WITH siblings AS MATERIALIZED (
                SELECT uuid
                FROM messenger_projection_tasks
                WHERE project_id = %s AND task_kind = 'folder_projection'
                  AND scope_kind = 'user-folder' AND scope_key = %s
                  AND uuid <> %s AND status IN ('pending', 'failed')
                  AND (lease_expires_at IS NULL OR lease_expires_at <= NOW())
                  AND payload->>'source_kind' IN (
                      'legacy_message_state.deleted',
                      'legacy_message_state.updated'
                  )
                FOR UPDATE SKIP LOCKED
            )
            UPDATE messenger_projection_tasks AS task
            SET status = 'completed', lease_owner = NULL,
                lease_expires_at = NULL, next_retry_at = NULL,
                last_error = NULL, updated_at = NOW()
            FROM siblings
            WHERE task.project_id = %s AND task.uuid = siblings.uuid
            """,
            (project_id, task["scope_key"], task["uuid"], project_id),
        )
    if source_kind == "folder.deleted":
        if not payload.get("emit_public_event", True):
            return
        emitted = messenger_events.create_folder_deleted_event(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )
        _guard_emitted_events(
            session,
            emitted,
            project_id=project_id,
            stream_uuid=None,
            control_effect=True,
        )
        return
    binding = session.execute(
        """
        SELECT rule
        FROM messenger_user_folder_bindings
        WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
        FOR UPDATE
        """,
        (project_id, user_uuid, folder_uuid),
    ).fetchone()
    if binding is None:
        return
    rule = binding["rule"]
    if rule != "custom":
        session.execute(
            """
            DELETE FROM messenger_folder_items AS item
            WHERE item.project_id = %s AND item.user_uuid = %s
              AND item.folder_uuid = %s AND item.automatic
              AND NOT EXISTS (
                  SELECT 1
                  FROM messenger_stream_bindings AS membership
                  JOIN messenger_streams AS stream
                    ON stream.project_id = membership.project_id
                   AND stream.uuid = membership.stream_uuid
                  WHERE membership.project_id = item.project_id
                    AND membership.user_uuid = item.user_uuid
                    AND membership.stream_uuid = item.stream_uuid
                    AND membership.active AND NOT stream.is_archived
                    AND stream.deleted_at IS NULL
                    AND (
                        %s = 'all_chats'
                        OR (%s = 'personal' AND stream.private)
                        OR (%s = 'channels' AND NOT stream.private)
                    )
              )
            """,
            (project_id, user_uuid, folder_uuid, rule, rule, rule),
        )
        prefix = {"all_chats": "00", "personal": "11", "channels": "22"}[rule]
        session.execute(
            """
            INSERT INTO messenger_folder_items (
                uuid, project_id, user_uuid, folder_uuid, stream_uuid,
                chat_type, automatic, created_at, updated_at
            )
            SELECT (%s || substr(stream.uuid::text, 3))::uuid,
                   membership.project_id, membership.user_uuid, %s,
                   stream.uuid,
                   CASE WHEN %s = 'personal' OR stream.private
                        THEN 'private' ELSE 'stream' END,
                   true, NOW(), NOW()
            FROM messenger_stream_bindings AS membership
            JOIN messenger_streams AS stream
              ON stream.project_id = membership.project_id
             AND stream.uuid = membership.stream_uuid
            WHERE membership.project_id = %s AND membership.user_uuid = %s
              AND membership.active AND NOT stream.is_archived
              AND stream.deleted_at IS NULL
              AND (
                  %s = 'all_chats'
                  OR (%s = 'personal' AND stream.private)
                  OR (%s = 'channels' AND NOT stream.private)
              )
            ON CONFLICT (project_id, user_uuid, folder_uuid, stream_uuid)
            DO UPDATE SET chat_type = EXCLUDED.chat_type,
                          automatic = true,
                          updated_at = NOW()
            """,
            (
                prefix,
                folder_uuid,
                rule,
                project_id,
                user_uuid,
                rule,
                rule,
                rule,
            ),
        )
    session.execute(
        """
        WITH snapshot AS (
            SELECT
                COALESCE(sum(stream_binding.active_unread_count), 0)::integer
                    AS unread_count,
                COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'uuid', item.uuid,
                            CASE WHEN %s = 'custom'
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
                        ) ORDER BY item.pinned_at DESC NULLS LAST,
                                   item.order_index ASC NULLS LAST,
                                   item.created_at, item.uuid
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
            WHERE target.project_id = %s AND target.user_uuid = %s
              AND target.folder_uuid = %s
        )
        UPDATE messenger_user_folder_bindings
        SET unread_count = snapshot.unread_count,
            folder_items_snapshot = snapshot.folder_items_snapshot,
            snapshot_version = snapshot_version + 1,
            snapshot_updated_at = NOW(), updated_at = NOW()
        FROM snapshot
        WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
        """,
        (
            rule,
            project_id,
            user_uuid,
            folder_uuid,
            project_id,
            user_uuid,
            folder_uuid,
        ),
    )
    if not payload.get("emit_public_event", True):
        return
    folders = v2_models.WorkspaceUserFolder.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
            "uuid": dm_filters.EQ(folder_uuid),
        },
        session=session,
    )
    if source_kind == "folder.created":
        emitted = [
            messenger_events.create_folder_event(folder, session=session)
            for folder in folders
        ]
    else:
        emitted = messenger_events.create_folder_updated_events(
            project_id,
            folders,
            folder_uuid,
            session=session,
            compact=True,
        )
    if source_kind == "folder_item.deleted":
        emitted = [
            *typing.cast(typing.Iterable[object], emitted),
            messenger_events.create_folder_item_deleted_event(
                project_id=project_id,
                user_uuid=user_uuid,
                item_uuid=_uuid(payload["item_uuid"]),
                session=session,
            ),
        ]
    _guard_emitted_events(
        session,
        emitted,
        project_id=project_id,
        stream_uuid=stream_uuid,
        control_effect=stream_uuid is None or source_kind == "stream.deleted",
    )


def _process_stream_folder_projection(
    session: typing.Any,
    task: typing.Mapping[str, typing.Any],
    batch_size: int,
) -> bool:
    payload = task["payload"]
    stream_uuid = _uuid(payload["stream_uuid"])
    rows = session.execute(
        """
        SELECT user_uuid
        FROM messenger_stream_bindings
        WHERE project_id = %s AND stream_uuid = %s AND active
          AND (%s::uuid IS NULL OR user_uuid > %s::uuid)
        ORDER BY user_uuid
        LIMIT %s
        """,
        (
            task["project_id"],
            stream_uuid,
            task["progress_uuid"],
            task["progress_uuid"],
            min(max(batch_size, 1), MAX_FANOUT_BATCH_SIZE) + 1,
        ),
    ).fetchall()
    has_more = len(rows) > batch_size
    rows = rows[:batch_size]
    for row in rows:
        _enqueue_folder_outbox_events(
            session,
            source_event_uuid=task["outbox_event_uuid"],
            project_id=task["project_id"],
            source_kind=payload["source_kind"],
            user_uuid=row["user_uuid"],
            stream_uuid=stream_uuid,
        )
    if rows:
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET progress_uuid = %s,
                processed_count = processed_count + %s,
                updated_at = NOW()
            WHERE project_id = %s AND uuid = %s AND fencing_token = %s
            """,
            (
                rows[-1]["user_uuid"],
                len(rows),
                task["project_id"],
                task["uuid"],
                task["fencing_token"],
            ),
        )
    return not has_more


def _process_topic_state(
    session: typing.Any, task: typing.Mapping[str, typing.Any]
) -> None:
    payload = task["payload"]
    if not payload.get("emit_public_event", True):
        return
    filters: dict[str, typing.Any] = {
        "uuid": dm_filters.EQ(_uuid(payload["topic_uuid"]))
    }
    if payload.get("recipient_uuid"):
        filters["user_uuid"] = dm_filters.EQ(_uuid(payload["recipient_uuid"]))
    topics = _v2_rows(
        v2_models.WorkspaceUserTopic,
        task["project_id"],
        filters,
        order_by={"user_uuid": "asc"},
    )
    factory = (
        messenger_events.create_topic_events
        if payload["source_kind"] == "topic.created"
        else messenger_events.create_topic_updated_events
    )
    emitted = factory(task["project_id"], topics, session=session, compact=True)
    if topics:
        _guard_emitted_events(
            session,
            emitted,
            project_id=task["project_id"],
            stream_uuid=topics[0].stream_uuid,
        )


def _process_topic_membership_policy_rebuild(
    session: typing.Any,
    task: typing.Mapping[str, typing.Any],
    batch_size: int,
) -> bool:
    payload = task["payload"]
    project_id = task["project_id"]
    user_uuid = _uuid(payload["user_uuid"])
    topic_uuid = _uuid(payload["topic_uuid"])
    stream_uuid = _uuid(payload["stream_uuid"])
    generation = int(payload["membership_generation"])
    membership = session.execute(
        """
        SELECT 1 FROM messenger_stream_bindings
        WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
          AND active AND membership_generation = %s
        """,
        (project_id, stream_uuid, user_uuid, generation),
    ).fetchone()
    if membership is None:
        return True
    rows = session.execute(
        """
        SELECT placement.uuid, message.author_uuid, message.payload,
               message.created_at
        FROM messenger_message_placements AS placement
        JOIN messenger_messages AS message
          ON message.project_id = placement.project_id
         AND message.uuid = placement.message_uuid
        WHERE placement.project_id = %s AND placement.topic_uuid = %s
          AND message.created_at < %s::timestamptz
          AND (
                %s::timestamp IS NULL
                OR (message.created_at, placement.uuid)
                   < (%s::timestamp, %s::uuid)
              )
        ORDER BY message.created_at DESC, placement.uuid DESC
        LIMIT %s
        """,
        (
            project_id,
            topic_uuid,
            payload["membership_started_at"],
            task["progress_created_at"],
            task["progress_created_at"],
            task["progress_uuid"],
            min(max(batch_size, 1), MAX_FANOUT_BATCH_SIZE) + 1,
        ),
    ).fetchall()
    has_more = len(rows) > batch_size
    rows = rows[:batch_size]
    now = datetime.datetime.now(datetime.timezone.utc)
    for row in rows:
        placement_uuid = _uuid(row["uuid"])
        row_uuid = sys_uuid.uuid5(placement_uuid, str(user_uuid))
        session.execute(
            """
            INSERT INTO messenger_user_message_bindings (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, relation_role, visibility,
                permissions, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'visible',
                '{"read":true,"react":true,"star":true,"pin":true}'::jsonb,
                %s, %s
            )
            ON CONFLICT (project_id, placement_uuid, user_uuid) DO UPDATE
            SET membership_generation = EXCLUDED.membership_generation,
                relation_role = EXCLUDED.relation_role,
                visibility = EXCLUDED.visibility,
                permissions = EXCLUDED.permissions,
                updated_at = NOW()
            WHERE messenger_user_message_bindings.membership_generation
                  <> EXCLUDED.membership_generation
            """,
            (
                row_uuid,
                project_id,
                placement_uuid,
                user_uuid,
                generation,
                "author" if _uuid(row["author_uuid"]) == user_uuid else "member",
                now,
                now,
            ),
        )
        content = str(row["payload"].get("content", "")).lower()
        session.execute(
            """
            INSERT INTO messenger_user_message_states (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, read_at, mentioned,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE
            SET membership_generation = EXCLUDED.membership_generation,
                read_at = EXCLUDED.read_at,
                mentioned = EXCLUDED.mentioned,
                starred = false,
                pinned = false,
                updated_at = NOW()
            WHERE messenger_user_message_states.membership_generation
                  <> EXCLUDED.membership_generation
            """,
            (
                row_uuid,
                project_id,
                placement_uuid,
                user_uuid,
                generation,
                now,
                f"](urn:user:{str(user_uuid).lower()})" in content,
                now,
                now,
            ),
        )
    if rows:
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET progress_created_at = %s, progress_uuid = %s,
                processed_count = processed_count + %s, updated_at = NOW()
            WHERE project_id = %s AND uuid = %s AND fencing_token = %s
            """,
            (
                rows[-1]["created_at"],
                rows[-1]["uuid"],
                len(rows),
                project_id,
                task["uuid"],
                task["fencing_token"],
            ),
        )
    if not has_more:
        _enqueue_counter_outbox_events(
            session,
            source_event_uuid=task["outbox_event_uuid"],
            project_id=project_id,
            source_kind="stream_binding.created",
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
        )
    return not has_more


def _deliver_snapshot_batch(
    session: typing.Any, task: typing.Mapping[str, typing.Any]
) -> None:
    payload = task["payload"]
    source_kind = payload["source_kind"]
    if len(payload.get("recipients") or []) > MAX_FANOUT_BATCH_SIZE:
        raise ValueError("delivery recipient batch exceeds the hard maximum")
    for placement_payload in payload.get("placements") or []:
        if len(placement_payload.get("recipients") or []) > MAX_FANOUT_BATCH_SIZE:
            raise ValueError("delivery recipient batch exceeds the hard maximum")
    expected_generations = payload.get("membership_generations") or {}
    if source_kind in {
        "stream_bindings.created",
        "stream_binding.updated",
        "stream_binding.deleted",
    }:
        if source_kind == "stream_binding.deleted":
            binding_values = dict(payload["binding"])
            for field in (
                "uuid",
                "project_id",
                "stream_uuid",
                "user_uuid",
                "who_uuid",
            ):
                if binding_values.get(field) is not None:
                    binding_values[field] = _uuid(binding_values[field])
            binding = types.SimpleNamespace(**binding_values)
            emitted = messenger_events.create_stream_binding_deleted_events(
                binding,
                [_uuid(value) for value in payload["recipients"]],
                session=session,
            )
            _guard_emitted_events(
                session,
                emitted,
                project_id=task["project_id"],
                stream_uuid=binding.stream_uuid,
                membership_generations=expected_generations,
            )
            return
        binding_uuids = payload.get("binding_uuids") or [payload["binding_uuid"]]
        rows = session.execute(
            """
            SELECT uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
                   notification_mode, notification_updated_at,
                   created_at, updated_at
            FROM messenger_stream_bindings
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            ORDER BY created_at, uuid
            """,
            (task["project_id"], [_uuid(value) for value in binding_uuids]),
        ).fetchall()
        bindings = [types.SimpleNamespace(**dict(row)) for row in rows]
        if not bindings:
            return
        recipients = [_uuid(value) for value in payload["recipients"]]
        if source_kind == "stream_bindings.created":
            emitted = []
            for recipient in recipients:
                emitted.append(
                    messenger_events.create_stream_bindings_created_event(
                        bindings,
                        recipient,
                        session=session,
                    )
                )
        else:
            emitted = messenger_events.create_stream_binding_updated_events(
                bindings[0], recipients, session=session
            )
        _guard_emitted_events(
            session,
            emitted,
            project_id=task["project_id"],
            stream_uuid=bindings[0].stream_uuid,
            membership_generations=expected_generations,
        )
        return
    if source_kind == "stream.deleted":
        emitted = []
        if payload.get("emit_public_event", True):
            for recipient in payload["recipients"]:
                emitted.append(
                    messenger_events.create_stream_deleted_event(
                        task["project_id"],
                        _uuid(recipient),
                        _uuid(payload["stream_uuid"]),
                        payload["source_name"],
                        payload["source"],
                        session=session,
                    )
                )
            _guard_emitted_events(
                session,
                emitted,
                project_id=task["project_id"],
                stream_uuid=payload["stream_uuid"],
                membership_generations=expected_generations,
                control_effect=True,
            )
        return
    if source_kind == "topic.deleted":
        emitted = []
        if payload.get("emit_public_event", True):
            for recipient in payload["recipients"]:
                emitted.append(
                    messenger_events.create_topic_deleted_event(
                        task["project_id"],
                        _uuid(recipient),
                        _uuid(payload["topic_uuid"]),
                        _uuid(payload["stream_uuid"]),
                        payload["source_name"],
                        payload["source"],
                        session=session,
                    )
                )
            _guard_emitted_events(
                session,
                emitted,
                project_id=task["project_id"],
                stream_uuid=payload["stream_uuid"],
                membership_generations=expected_generations,
            )
        return
    if source_kind in {"stream.created", "stream.updated"}:
        if not payload.get("emit_public_event", True):
            return
        filters: dict[str, typing.Any] = {
            "uuid": dm_filters.EQ(_uuid(payload["resource_uuid"]))
        }
        recipients = payload.get("recipients")
        if recipients:
            filters["user_uuid"] = dm_filters.In([_uuid(value) for value in recipients])
        rows = _v2_rows(
            v2_models.WorkspaceUserStream,
            task["project_id"],
            filters,
            order_by={"user_uuid": "asc"},
        )
        factory = (
            messenger_events.create_stream_events
            if source_kind == "stream.created"
            else messenger_events.create_stream_updated_events
        )
        emitted = factory(task["project_id"], rows, session=session, compact=True)
        if rows:
            _guard_emitted_events(
                session,
                emitted,
                project_id=task["project_id"],
                stream_uuid=rows[0].uuid,
                membership_generations=expected_generations,
            )
        return
    if source_kind == "message.deleted":
        if not payload.get("emit_public_event", True):
            return
        placements = payload.get("placements")
        if placements is None:
            placements = [
                {
                    **payload["placement"],
                    "recipients": payload["recipients"],
                    "membership_generations": expected_generations,
                }
            ]
        for placement in placements:
            emitted = messenger_events.create_message_deleted_events(
                task["project_id"],
                [_uuid(value) for value in placement["recipients"]],
                _uuid(placement["uuid"]),
                _uuid(placement["stream_uuid"]),
                _uuid(placement["topic_uuid"]),
                _uuid(payload["author_uuid"]),
                payload["source_name"],
                payload["source"],
                session=session,
                compact=True,
            )
            _guard_emitted_events(
                session,
                emitted,
                project_id=task["project_id"],
                stream_uuid=placement["stream_uuid"],
                membership_generations=placement.get("membership_generations") or {},
            )
        return
    if source_kind == "message.updated":
        if payload.get("canonical_message_uuid"):
            filters = {
                "canonical_message_uuid": dm_filters.EQ(
                    _uuid(payload["canonical_message_uuid"])
                )
            }
        else:
            filters = {"uuid": dm_filters.EQ(_uuid(payload["placement_uuid"]))}
        if payload.get("recipient_uuid"):
            filters["user_uuid"] = dm_filters.EQ(_uuid(payload["recipient_uuid"]))
        rows = _v2_rows(
            v2_models.WorkspaceUserMessage,
            task["project_id"],
            filters,
            order_by={"uuid": "asc", "user_uuid": "asc"},
        )
        _emit_message_updated_rows(session, task["project_id"], rows)


def _revoke_broadcast_membership_events(
    session: typing.Any,
    *,
    project_id: object,
    stream_uuid: object,
    user_uuid: object,
    membership_generation: int,
) -> None:
    """Permanently fence broadcast history from a removed membership epoch."""
    session.execute(
        """
        DELETE FROM m_workspace_event_recipient_payloads_v1 AS recipient
        USING m_workspace_broadcast_message_events_v1 AS event,
              messenger_event_membership_guards AS guard
        WHERE recipient.event_uuid = event.uuid
          AND recipient.user_uuid = %s
          AND guard.event_uuid = event.uuid
          AND guard.project_id = %s
          AND guard.stream_uuid = %s
          AND guard.user_uuid = recipient.user_uuid
          AND guard.membership_generation = %s
          AND NOT guard.control_effect
        """,
        (user_uuid, project_id, stream_uuid, membership_generation),
    )
    session.execute(
        """
        DELETE FROM m_workspace_event_audience_members_v1 AS member
        USING m_workspace_broadcast_message_events_v1 AS event,
              messenger_event_membership_guards AS guard
        WHERE member.audience_snapshot_uuid = event.audience_snapshot_uuid
          AND member.user_uuid = %s
          AND guard.event_uuid = event.uuid
          AND guard.project_id = %s
          AND guard.stream_uuid = %s
          AND guard.user_uuid = member.user_uuid
          AND guard.membership_generation = %s
          AND NOT guard.control_effect
        """,
        (user_uuid, project_id, stream_uuid, membership_generation),
    )


def _process_delivery_snapshot(
    session: typing.Any,
    task: typing.Mapping[str, typing.Any],
    batch_size: int,
) -> bool:
    """Resolve delivery audiences in bounded worker-owned keyset batches."""
    original_payload = task["payload"]
    if "recipients" in original_payload:
        _deliver_snapshot_batch(session, task)
        return True
    source_kind = original_payload["source_kind"]
    batched_kinds = {
        "stream.updated",
        "stream_binding.updated",
        "stream_bindings.created",
        "stream_binding.deleted",
        "stream.deleted",
        "topic.deleted",
        "message.deleted",
    }
    if source_kind not in batched_kinds:
        _deliver_snapshot_batch(session, task)
        return True
    cursor = task["progress_uuid"]
    if source_kind == "stream_binding.deleted" and cursor is None:
        binding = original_payload["binding"]
        _revoke_broadcast_membership_events(
            session,
            project_id=task["project_id"],
            stream_uuid=_uuid(binding["stream_uuid"]),
            user_uuid=_uuid(binding["user_uuid"]),
            membership_generation=int(binding["membership_generation"]),
        )
    limit = min(max(batch_size, 1), MAX_FANOUT_BATCH_SIZE)
    if source_kind == "message.deleted":
        placement = original_payload["placement"]
        rows = session.execute(
            """
            SELECT user_uuid, membership_generation
            FROM messenger_user_message_bindings
            WHERE project_id = %s AND placement_uuid = %s
              AND (%s::uuid IS NULL OR user_uuid > %s::uuid)
            ORDER BY user_uuid
            LIMIT %s
            """,
            (
                task["project_id"],
                _uuid(placement["uuid"]),
                cursor,
                cursor,
                limit + 1,
            ),
        ).fetchall()
    else:
        stream_uuid = _uuid(original_payload["stream_uuid"])
        excluded_user = original_payload.get("exclude_user_uuid")
        excluded_bindings = [
            _uuid(value)
            for value in original_payload.get("exclude_binding_uuids") or []
        ]
        rows = session.execute(
            """
            SELECT uuid, user_uuid, membership_generation
            FROM messenger_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND active
              AND (%s::uuid IS NULL OR user_uuid <> %s::uuid)
              AND NOT (uuid = ANY(%s::uuid[]))
              AND (%s::uuid IS NULL OR user_uuid > %s::uuid)
            ORDER BY user_uuid
            LIMIT %s
            """,
            (
                task["project_id"],
                stream_uuid,
                excluded_user,
                excluded_user,
                excluded_bindings,
                cursor,
                cursor,
                limit + 1,
            ),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return True
    payload = dict(original_payload)
    payload["recipients"] = [row["user_uuid"] for row in rows]
    payload["membership_generations"] = {
        str(row["user_uuid"]): row["membership_generation"] for row in rows
    }
    batch_task = dict(task)
    batch_task["payload"] = payload
    _deliver_snapshot_batch(session, batch_task)
    if source_kind == "message.deleted":
        placement = original_payload["placement"]
        for row in rows:
            _enqueue_counter_outbox_events(
                session,
                source_event_uuid=task["outbox_event_uuid"],
                project_id=task["project_id"],
                source_kind=source_kind,
                user_uuid=row["user_uuid"],
                stream_uuid=placement["stream_uuid"],
                topic_uuid=placement["topic_uuid"],
                placement_uuid=placement["uuid"],
            )
    if source_kind == "topic.deleted":
        for row in rows:
            _enqueue_counter_outbox_events(
                session,
                source_event_uuid=task["outbox_event_uuid"],
                project_id=task["project_id"],
                source_kind=source_kind,
                user_uuid=row["user_uuid"],
                stream_uuid=original_payload["stream_uuid"],
                topic_uuid=original_payload["topic_uuid"],
                include_topic=False,
            )
    if source_kind == "stream.deleted" and original_payload.get("all_recipients"):
        for row in rows:
            _enqueue_folder_outbox_events(
                session,
                source_event_uuid=task["outbox_event_uuid"],
                project_id=task["project_id"],
                source_kind=source_kind,
                user_uuid=row["user_uuid"],
                stream_uuid=original_payload["stream_uuid"],
            )
    session.execute(
        """
        UPDATE messenger_projection_tasks
        SET progress_uuid = %s,
            processed_count = processed_count + %s,
            updated_at = NOW()
        WHERE project_id = %s AND uuid = %s AND fencing_token = %s
        """,
        (
            rows[-1]["user_uuid"],
            len(rows),
            task["project_id"],
            task["uuid"],
            task["fencing_token"],
        ),
    )
    return not has_more


def _process_task(
    session: typing.Any,
    task: typing.Mapping[str, typing.Any],
    fanout_batch_size: int,
) -> bool:
    handlers: dict[str, typing.Callable[..., object]] = {
        "reaction_snapshot": _process_reaction_snapshot,
        "read_counters": _process_read_counters,
        "folder_projection": _process_folder_projection,
        "topic_state_projection": _process_topic_state,
    }
    if task["task_kind"] == "fanout":
        return _process_fanout(session, task, fanout_batch_size)
    if task["task_kind"] == "content_mentions":
        return _process_content_mentions(session, task, fanout_batch_size)
    if task["task_kind"] == "delivery_snapshot_event":
        return _process_delivery_snapshot(session, task, fanout_batch_size)
    if (
        task["task_kind"] == "folder_projection"
        and not task["payload"].get("user_uuid")
        and task["payload"].get("stream_uuid")
    ):
        return _process_stream_folder_projection(session, task, fanout_batch_size)
    if task["task_kind"] == "topic_membership_policy_rebuild":
        return _process_topic_membership_policy_rebuild(
            session, task, fanout_batch_size
        )
    handler = handlers.get(task["task_kind"])
    if handler is None:
        raise ValueError(f"Unsupported Messenger v2 task kind {task['task_kind']}")
    handler(session, task)
    return True


def _purge_completed_tombstone(
    session: typing.Any,
    task: typing.Mapping[str, typing.Any],
) -> None:
    """Physically remove a hidden root after all root-scoped work is durable."""
    payload = task["payload"]
    source_kind = payload.get("source_kind")
    project_id = task["project_id"]
    canonical_value = payload.get("canonical_message_uuid")
    if canonical_value is not None:
        canonical_uuid = _uuid(canonical_value)
        deleted = session.execute(
            """
            SELECT 1 FROM messenger_messages
            WHERE project_id = %s AND uuid = %s AND deleted_at IS NOT NULL
            """,
            (project_id, canonical_uuid),
        ).fetchone()
        if deleted is not None:
            unfinished = session.execute(
                """
                SELECT 1
                FROM messenger_domain_outbox_events AS event
                LEFT JOIN messenger_projection_tasks AS projection
                  ON projection.project_id = event.project_id
                 AND projection.outbox_event_uuid = event.uuid
                WHERE event.project_id = %s AND event.uuid <> %s
                  AND (
                      event.payload->>'canonical_message_uuid' = %s
                      OR (
                          event.scope_kind = 'message'
                          AND event.scope_key = %s
                      )
                  )
                  AND (
                      projection.uuid IS NULL
                      OR projection.status NOT IN ('completed', 'dead_letter')
                  )
                LIMIT 1
                """,
                (
                    project_id,
                    task["outbox_event_uuid"],
                    str(canonical_uuid),
                    f"{project_id}:{canonical_uuid}",
                ),
            ).fetchone()
            if unfinished is None:
                session.execute(
                    """
                    DELETE FROM messenger_messages
                    WHERE project_id = %s AND uuid = %s
                      AND deleted_at IS NOT NULL
                    """,
                    (project_id, canonical_uuid),
                )
    if source_kind == "topic.deleted":
        session.execute(
            """
            DELETE FROM messenger_topics
            WHERE project_id = %s AND uuid = %s AND deleted_at IS NOT NULL
            """,
            (project_id, _uuid(payload["topic_uuid"])),
        )
        session.execute(
            """
            DELETE FROM messenger_messages AS message
            WHERE message.project_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM messenger_message_placements AS placement
                  WHERE placement.project_id = message.project_id
                    AND placement.message_uuid = message.uuid
              )
            """,
            (project_id,),
        )
    if source_kind == "stream.deleted":
        session.execute(
            """
            DELETE FROM messenger_streams
            WHERE project_id = %s AND uuid = %s AND deleted_at IS NOT NULL
            """,
            (project_id, _uuid(payload["stream_uuid"])),
        )
        session.execute(
            """
            DELETE FROM messenger_messages AS message
            WHERE message.project_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM messenger_message_placements AS placement
                  WHERE placement.project_id = message.project_id
                    AND placement.message_uuid = message.uuid
              )
            """,
            (project_id,),
        )


def process_one_projection_task(
    session: typing.Any,
    worker_id: str,
    *,
    fanout_batch_size: int = DEFAULT_FANOUT_BATCH_SIZE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    metrics: dict[str, float] | None = None,
) -> bool:
    """Process one claimed task; return ``False`` when the queue is empty."""
    if not 1 <= fanout_batch_size <= MAX_FANOUT_BATCH_SIZE:
        raise ValueError(
            f"fanout_batch_size must be between 1 and {MAX_FANOUT_BATCH_SIZE}"
        )
    claim_started_at = time.monotonic()
    claimed_at = datetime.datetime.now(datetime.timezone.utc)
    task = _claim_task(session, worker_id, lease_seconds)
    claim_seconds = time.monotonic() - claim_started_at
    if metrics is not None:
        metrics["claim_seconds"] = metrics.get("claim_seconds", 0.0) + claim_seconds
        metrics["claim_seconds_max"] = max(
            metrics.get("claim_seconds_max", 0.0), claim_seconds
        )
    if task is None:
        if metrics is not None:
            metrics["empty_claims"] = metrics.get("empty_claims", 0.0) + 1
            metrics["empty_claim_seconds"] = (
                metrics.get("empty_claim_seconds", 0.0) + claim_seconds
            )
        return False
    started_at = time.monotonic()
    task_kind = str(task["task_kind"])
    if metrics is not None:
        metrics["claimed"] = metrics.get("claimed", 0.0) + 1
        claimed_key = f"claimed_{task_kind}"
        metrics[claimed_key] = metrics.get(claimed_key, 0.0) + 1
        task_age = float(task["task_age_seconds"])
        metrics["task_age_seconds_max"] = max(
            metrics.get("task_age_seconds_max", 0.0), task_age
        )
    session.execute("SAVEPOINT messenger_v2_projection_task", ())
    outcome = "running"
    try:
        completed = _process_task(session, task, fanout_batch_size)
    except Exception as error:
        session.execute("ROLLBACK TO SAVEPOINT messenger_v2_projection_task", ())
        attempts = int(task["attempts"])
        status = "dead_letter" if attempts >= max_attempts else "failed"
        outcome = status
        delay = min(2 ** min(attempts, 10), 300)
        duration = time.monotonic() - started_at
        execution_stats = _finish_execution_stats(
            task,
            worker_id=worker_id,
            claimed_at=claimed_at,
            claim_seconds=claim_seconds,
            processing_seconds=duration,
            outcome=outcome,
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = %s, lease_owner = NULL, lease_expires_at = NULL,
                next_retry_at = CASE WHEN %s = 'failed'
                    THEN NOW() + make_interval(secs => %s) ELSE NULL END,
                last_error = %s,
                payload = jsonb_set(
                    payload,
                    '{_execution_stats}',
                    %s::jsonb || jsonb_build_object(
                        'processed_count', processed_count
                    )
                ),
                updated_at = NOW()
            WHERE project_id = %s AND uuid = %s AND fencing_token = %s
            """,
            (
                status,
                status,
                delay,
                str(error)[:4096],
                json.dumps(execution_stats),
                task["project_id"],
                task["uuid"],
                task["fencing_token"],
            ),
        )
        LOG.exception(
            "Failed to process Messenger v2 projection task",
            extra={"task_uuid": str(task["uuid"]), "task_kind": task["task_kind"]},
        )
        if metrics is not None:
            outcome = "dead_letter" if status == "dead_letter" else "retry"
            metrics[outcome] = metrics.get(outcome, 0.0) + 1
            outcome_key = f"{outcome}_{task_kind}"
            metrics[outcome_key] = metrics.get(outcome_key, 0.0) + 1
    else:
        session.execute("RELEASE SAVEPOINT messenger_v2_projection_task", ())
        if completed:
            _purge_completed_tombstone(session, task)
        outcome = "completed" if completed else "requeued"
        duration = time.monotonic() - started_at
        execution_stats = _finish_execution_stats(
            task,
            worker_id=worker_id,
            claimed_at=claimed_at,
            claim_seconds=claim_seconds,
            processing_seconds=duration,
            outcome=outcome,
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = %s, lease_owner = NULL, lease_expires_at = NULL,
                attempts = CASE WHEN %s THEN attempts ELSE 0 END,
                next_retry_at = NULL, last_error = NULL,
                payload = jsonb_set(
                    payload,
                    '{_execution_stats}',
                    %s::jsonb || jsonb_build_object(
                        'processed_count', processed_count
                    )
                ),
                updated_at = NOW()
            WHERE project_id = %s AND uuid = %s AND fencing_token = %s
            """,
            (
                "completed" if completed else "pending",
                completed,
                json.dumps(execution_stats),
                task["project_id"],
                task["uuid"],
                task["fencing_token"],
            ),
        )
        if metrics is not None:
            metrics[outcome] = metrics.get(outcome, 0.0) + 1
            outcome_key = f"{outcome}_{task_kind}"
            metrics[outcome_key] = metrics.get(outcome_key, 0.0) + 1
            if completed and task_kind == "fanout":
                delay = (
                    datetime.datetime.now(datetime.timezone.utc)
                    - task["outbox_created_at"]
                ).total_seconds()
                metrics["fanout_outbox_to_websocket_seconds_max"] = max(
                    metrics.get("fanout_outbox_to_websocket_seconds_max", 0.0),
                    delay,
                )
    finally:
        session.execute(
            """
            UPDATE messenger_projection_scope_leases
            SET owner = NULL, lease_expires_at = NOW(), updated_at = NOW()
            WHERE project_id = %s AND scope_kind = %s AND scope_key = %s
              AND owner = %s AND fencing_token = %s
            """,
            (
                task["project_id"],
                task["scope_kind"],
                task["scope_key"],
                worker_id,
                task["fencing_token"],
            ),
        )
    if metrics is not None:
        duration = time.monotonic() - started_at
        metrics["processing_seconds"] = (
            metrics.get("processing_seconds", 0.0) + duration
        )
        duration_key = f"processing_seconds_{task_kind}"
        metrics[duration_key] = metrics.get(duration_key, 0.0) + duration
        max_key = f"processing_seconds_max_{task_kind}"
        metrics[max_key] = max(metrics.get(max_key, 0.0), duration)
    return True


def _finish_execution_stats(
    task: typing.Mapping[str, typing.Any],
    *,
    worker_id: str,
    claimed_at: datetime.datetime,
    claim_seconds: float,
    processing_seconds: float,
    outcome: str,
) -> dict[str, object]:
    """Build a compact per-task latency record without a history hot spot."""
    existing = dict(
        task.get("execution_stats")
        or task.get("payload", {}).get("_execution_stats")
        or {}
    )
    finished_at = datetime.datetime.now(datetime.timezone.utc)
    processing_ms = round(processing_seconds * 1000, 3)
    raw_queue_wait_ms = float(task["task_age_seconds"]) * 1000
    raw_outbox_wait_ms = float(task["outbox_age_seconds"]) * 1000
    raw_derivation_delay_ms = (
        task["created_at"] - task["outbox_created_at"]
    ).total_seconds() * 1000
    raw_outbox_to_finish_ms = (
        finished_at - task["outbox_created_at"]
    ).total_seconds() * 1000
    observed_clock_skew_ms = max(
        0.0,
        -raw_queue_wait_ms,
        -raw_outbox_wait_ms,
        -raw_derivation_delay_ms,
        -raw_outbox_to_finish_ms,
    )
    existing.update(
        {
            "schema_version": 1,
            "claim_count": int(existing.get("claim_count", 0)) + 1,
            "first_claimed_at": existing.get(
                "first_claimed_at", claimed_at.isoformat()
            ),
            "last_claimed_at": claimed_at.isoformat(),
            "last_finished_at": finished_at.isoformat(),
            "last_worker_id": worker_id,
            "last_outcome": outcome,
            "queue_wait_ms": round(max(0.0, raw_queue_wait_ms), 3),
            "outbox_wait_ms": round(max(0.0, raw_outbox_wait_ms), 3),
            "derivation_delay_ms": round(max(0.0, raw_derivation_delay_ms), 3),
            "observed_clock_skew_ms": round(observed_clock_skew_ms, 3),
            "claim_duration_ms": round(claim_seconds * 1000, 3),
            "processing_duration_ms": processing_ms,
            "total_processing_duration_ms": round(
                float(existing.get("total_processing_duration_ms", 0)) + processing_ms,
                3,
            ),
            "outbox_to_finish_ms": round(max(0.0, raw_outbox_to_finish_ms), 3),
        }
    )
    existing.update(task.get("_execution_stats") or {})
    return existing


def projection_queue_metrics(session: typing.Any) -> dict[str, float]:
    """Return bounded queue gauges for structured worker metrics."""
    row = session.execute(
        """
        SELECT count(*)::bigint AS unfinished,
               count(*) FILTER (WHERE status = 'pending')::bigint AS pending,
               count(*) FILTER (WHERE status = 'failed')::bigint AS failed,
               count(*) FILTER (WHERE status = 'running')::bigint AS running,
               EXTRACT(EPOCH FROM (NOW() - min(created_at)))
                   AS oldest_pending_task_seconds,
               EXTRACT(EPOCH FROM (
                   NOW() - min(created_at) FILTER (WHERE task_kind = 'fanout')
               )) AS oldest_pending_fanout_seconds
        FROM messenger_projection_tasks
        WHERE status NOT IN ('completed', 'dead_letter')
        """,
        (),
    ).fetchone()
    return {name: float(value or 0) for name, value in dict(row).items()}


def drain_projection_queue(
    session: typing.Any,
    worker_id: str,
    *,
    limit: int = 1000,
    fanout_batch_size: int = DEFAULT_FANOUT_BATCH_SIZE,
) -> int:
    """Derive and process a bounded queue slice, primarily for tests/operators."""
    processed = 0
    while processed < limit:
        derive_projection_tasks(session, min(DEFAULT_DERIVE_LIMIT, limit - processed))
        if not process_one_projection_task(
            session,
            worker_id,
            fanout_batch_size=fanout_batch_size,
        ):
            break
        processed += 1
    return processed
