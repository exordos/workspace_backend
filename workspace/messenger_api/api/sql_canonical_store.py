# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""PostgreSQL-canonical Messenger store implementation."""

import contextlib
import collections.abc
import datetime
import logging
import time
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exceptions

from workspace.external_bridge_control import identity_linking
from workspace.external_bridge_control import provider_data
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import read_state


RESOURCE_MODELS: dict[str, typing.Any] = {
    **resource_projection.RESOURCE_MODELS,
    "files": models.WorkspaceVisibleFile,
    "message_reactions": models.WorkspaceVisibleMessageReaction,
}

_PROVIDER_TARGET_UNSET = object()
_PROVIDER_READ_STATE_MAX_MESSAGES = 500
_PROVIDER_TARGET_EXISTS = object()
_CURRENT_USER_PROVIDER_OPERATIONS = frozenset(
    {
        "message.create",
        "message.update",
        "message.delete",
        "reaction.create",
        "reaction.update",
        "reaction.delete",
        "read_state.set",
    }
)
_STREAM_OWNER_PROVIDER_OPERATIONS = frozenset(
    {
        "membership.add",
        "membership.remove",
        "stream.notification.update",
        "topic.notification.update",
        "stream.delete",
        "topic.create",
        "stream.update",
        "topic.update",
        "topic.delete",
    }
)
EVENT_RETENTION = datetime.timedelta(hours=72)
EVENT_PRUNE_BATCH_SIZE = 25000
SLOW_LIST_PROJECTION_SECONDS = 1.0
LOG = logging.getLogger(__name__)
MENTIONED_MESSAGE_UUIDS_SQL = """
    WITH authorized_streams AS MATERIALIZED (
        SELECT binding.stream_uuid
        FROM m_workspace_stream_bindings AS binding
        JOIN m_workspace_streams AS stream
          ON stream.project_id = binding.project_id
         AND stream.uuid = binding.stream_uuid
        LEFT JOIN m_confirmed_external_stream_access AS access
          ON access.project_id = binding.project_id
         AND access.user_uuid = binding.user_uuid
         AND access.stream_uuid = binding.stream_uuid
        WHERE binding.project_id = %s
          AND binding.user_uuid = %s
          AND (
              stream.source_name = 'native'
              OR access.user_uuid IS NOT NULL
          )
    )
    SELECT candidate.uuid
    FROM authorized_streams
    JOIN LATERAL (
        SELECT message.uuid, message.created_at
        FROM m_workspace_messages AS message
        {flags_join}
        WHERE message.project_id = %s
          AND message.stream_uuid = authorized_streams.stream_uuid
          {read_clause}
          AND POSITION(
              '](' || 'urn:user:' || LOWER(%s::text) || ')'
              IN LOWER(COALESCE(message.payload->>'content', ''))
          ) > 0
          {marker_clause}
        ORDER BY message.created_at {direction}, message.uuid {direction}
        LIMIT %s
    ) AS candidate ON TRUE
    ORDER BY candidate.created_at {direction}, candidate.uuid {direction}
    LIMIT %s
"""


class _IAMIdentityStore(typing.Protocol):
    def sync_iam_identity(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]: ...


COMPACT_MENTIONED_MESSAGE_UUIDS_SQL = """
    WITH authorized_streams AS MATERIALIZED (
        SELECT binding.stream_uuid
        FROM m_workspace_stream_bindings AS binding
        JOIN m_workspace_streams AS stream
          ON stream.project_id = binding.project_id
         AND stream.uuid = binding.stream_uuid
        LEFT JOIN m_confirmed_external_stream_access AS access
          ON access.project_id = binding.project_id
         AND access.user_uuid = binding.user_uuid
         AND access.stream_uuid = binding.stream_uuid
        WHERE binding.project_id = %s
          AND binding.user_uuid = %s
          AND (stream.source_name = 'native' OR access.user_uuid IS NOT NULL)
    )
    SELECT candidate.uuid
    FROM authorized_streams
    JOIN LATERAL (
        SELECT message.uuid, message.created_at
        FROM m_workspace_message_mentions_v1 AS mention
        JOIN m_workspace_messages AS message
          ON message.project_id = mention.project_id
         AND message.uuid = mention.message_uuid
        LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
          ON chunk.user_uuid = mention.user_uuid
         AND chunk.chunk_number = mention.ingest_sequence / 4096
        WHERE mention.project_id = %s
          AND mention.user_uuid = %s
          AND mention.stream_uuid = authorized_streams.stream_uuid
          {read_clause}
          {marker_clause}
        ORDER BY message.created_at {direction}, message.uuid {direction}
        LIMIT %s
    ) AS candidate ON TRUE
    ORDER BY candidate.created_at {direction}, candidate.uuid {direction}
    LIMIT %s
"""
COMPACT_READ_MESSAGE_UUIDS_SQL = """
    WITH authorized_streams AS MATERIALIZED (
        SELECT binding.stream_uuid
        FROM m_workspace_stream_bindings AS binding
        JOIN m_workspace_streams AS stream
          ON stream.project_id = binding.project_id
         AND stream.uuid = binding.stream_uuid
        LEFT JOIN m_confirmed_external_stream_access AS access
          ON access.project_id = binding.project_id
         AND access.user_uuid = binding.user_uuid
         AND access.stream_uuid = binding.stream_uuid
        WHERE binding.project_id = %s
          AND binding.user_uuid = %s
          AND (stream.source_name = 'native' OR access.user_uuid IS NOT NULL)
    )
    SELECT message.uuid
    FROM m_workspace_user_read_chunks_v1 AS chunk
    JOIN m_workspace_messages AS message
      ON message.project_id = %s
     AND message.ingest_sequence >= chunk.chunk_number * 4096
     AND message.ingest_sequence < (chunk.chunk_number + 1) * 4096
    JOIN authorized_streams
      ON authorized_streams.stream_uuid = message.stream_uuid
    WHERE chunk.user_uuid = %s
      AND get_bit(
            chunk.read_bits,
            (message.ingest_sequence %% 4096)::integer
          ) = 1
      {scope_clause}
      {marker_clause}
    ORDER BY message.created_at {direction}, message.uuid {direction}
    LIMIT %s
"""
BOUNDED_VISIBLE_EVENTS_SQL = """
    WITH direct_events AS (
        SELECT
            event."epoch_version", event."uuid", event."project_id",
            event."user_uuid", event."payload", event."created_at",
            event."updated_at", event."schema_version",
            event."object_type", event."action"
        FROM "m_workspace_events" AS event
        WHERE event."project_id" = %s
          AND event."user_uuid" = %s
          AND event."epoch_version" > %s
          AND (
              COALESCE(event."payload"->>'source_name', 'native') = 'native'
              OR (
                  event."object_type" = 'stream'
                  AND event."action" = 'deleted'
              )
              OR EXISTS (
                  SELECT 1
                  FROM "m_confirmed_external_account_access" AS access
                  WHERE access."project_id" = event."project_id"
                    AND access."user_uuid" = event."user_uuid"
                    AND access."account_type" =
                        event."payload"->>'source_name'
                    AND access."source_scope" = COALESCE(
                        event."payload"->'source'->>'source_scope',
                        event."payload"->'source'->>'server_url'
                    )
              )
          )
          AND (
              event."payload"->>'old_source_name' IS NULL
              OR event."payload"->>'old_source_name' = 'native'
              OR EXISTS (
                  SELECT 1
                  FROM "m_confirmed_external_account_access" AS old_access
                  WHERE old_access."project_id" = event."project_id"
                    AND old_access."user_uuid" = event."user_uuid"
                    AND old_access."account_type" =
                        event."payload"->>'old_source_name'
                    AND old_access."source_scope" = COALESCE(
                        event."payload"->'old_source'->>'source_scope',
                        event."payload"->'old_source'->>'server_url'
                    )
              )
          )
          AND (
              event."object_type" <> 'message'
              OR event."payload"->>'stream_uuid' IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM "m_workspace_stream_bindings" AS binding
                  WHERE binding."project_id" = event."project_id"
                    AND binding."stream_uuid" =
                        (event."payload"->>'stream_uuid')::uuid
                    AND binding."user_uuid" = event."user_uuid"
              )
          )
          AND (
              event."object_type" <> 'message'
              OR event."payload"->>'stream_uuid' IS NULL
              OR NOT EXISTS (
                  SELECT 1
                  FROM "m_workspace_streams" AS external_stream
                  WHERE external_stream."project_id" = event."project_id"
                    AND external_stream."uuid" =
                        (event."payload"->>'stream_uuid')::uuid
                    AND external_stream."source_name" <> 'native'
              )
              OR EXISTS (
                  SELECT 1
                  FROM "m_confirmed_external_stream_access" AS stream_access
                  WHERE stream_access."project_id" = event."project_id"
                    AND stream_access."user_uuid" = event."user_uuid"
                    AND stream_access."stream_uuid" =
                        (event."payload"->>'stream_uuid')::uuid
              )
          )
        ORDER BY event."epoch_version" ASC
        LIMIT %s
    ),
    broadcast_payloads AS (
        SELECT
            event."epoch_version", event."uuid", event."project_id",
            recipient."user_uuid",
            event."payload"
                || COALESCE(override."payload", '{}'::jsonb)
                || CASE
                    WHEN event."object_type" = 'user' THEN '{}'::jsonb
                    ELSE jsonb_build_object(
                        'user_uuid', recipient."user_uuid"
                    )
                END AS "payload",
            event."created_at", event."updated_at",
            event."schema_version", event."object_type", event."action"
        FROM "m_workspace_broadcast_message_events_v1" AS event
        JOIN "m_workspace_event_audience_members_v1" AS recipient
          ON recipient."audience_snapshot_uuid" =
              event."audience_snapshot_uuid"
         AND recipient."user_uuid" = %s
        LEFT JOIN "m_workspace_event_recipient_payloads_v1" AS override
          ON override."event_uuid" = event."uuid"
         AND override."user_uuid" = recipient."user_uuid"
        WHERE event."project_id" = %s
          AND event."epoch_version" > %s
    ),
    broadcast_events AS (
        SELECT event.*
        FROM broadcast_payloads AS event
        WHERE (
              COALESCE(event."payload"->>'source_name', 'native') = 'native'
              OR (
                  event."object_type" = 'stream'
                  AND event."action" = 'deleted'
              )
              OR EXISTS (
                  SELECT 1
                  FROM "m_confirmed_external_account_access" AS access
                  WHERE access."project_id" = event."project_id"
                    AND access."user_uuid" = event."user_uuid"
                    AND access."account_type" =
                        event."payload"->>'source_name'
                    AND access."source_scope" = COALESCE(
                        event."payload"->'source'->>'source_scope',
                        event."payload"->'source'->>'server_url'
                    )
              )
          )
          AND (
              event."payload"->>'old_source_name' IS NULL
              OR event."payload"->>'old_source_name' = 'native'
              OR EXISTS (
                  SELECT 1
                  FROM "m_confirmed_external_account_access" AS old_access
                  WHERE old_access."project_id" = event."project_id"
                    AND old_access."user_uuid" = event."user_uuid"
                    AND old_access."account_type" =
                        event."payload"->>'old_source_name'
                    AND old_access."source_scope" = COALESCE(
                        event."payload"->'old_source'->>'source_scope',
                        event."payload"->'old_source'->>'server_url'
                    )
              )
          )
          AND (
              event."object_type" <> 'message'
              OR event."payload"->>'stream_uuid' IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM "m_workspace_stream_bindings" AS binding
                  WHERE binding."project_id" = event."project_id"
                    AND binding."stream_uuid" =
                        (event."payload"->>'stream_uuid')::uuid
                    AND binding."user_uuid" = event."user_uuid"
              )
          )
          AND (
              event."object_type" <> 'message'
              OR event."payload"->>'stream_uuid' IS NULL
              OR NOT EXISTS (
                  SELECT 1
                  FROM "m_workspace_streams" AS external_stream
                  WHERE external_stream."project_id" = event."project_id"
                    AND external_stream."uuid" =
                        (event."payload"->>'stream_uuid')::uuid
                    AND external_stream."source_name" <> 'native'
              )
              OR EXISTS (
                  SELECT 1
                  FROM "m_confirmed_external_stream_access" AS stream_access
                  WHERE stream_access."project_id" = event."project_id"
                    AND stream_access."user_uuid" = event."user_uuid"
                    AND stream_access."stream_uuid" =
                        (event."payload"->>'stream_uuid')::uuid
              )
          )
        ORDER BY event."epoch_version" ASC
        LIMIT %s
    )
    SELECT event.*
    FROM (
        SELECT * FROM direct_events
        UNION ALL
        SELECT * FROM broadcast_events
    ) AS event
    ORDER BY event."epoch_version" ASC
    LIMIT %s
"""


class EventCursor(typing.TypedDict):
    epoch_generation: str
    current_epoch_version: int
    minimum_epoch_version: int


def _public_dict(row: typing.Any, resource: str) -> dict[str, typing.Any]:
    # Canonical rows already contain the provider and delivery columns.  Passing
    # the row explicitly avoids the transitional serializer's per-row lookup.
    result = resource_projection.as_dict(row, resource, canonical=row)
    result.pop("viewer_user_uuid", None)
    if resource == "files":
        result.pop("acl_mode", None)
    return result


def _eq_filter_matches(
    filters: typing.Mapping[str, typing.Any],
    name: str,
    expected: object,
) -> bool:
    clause = filters.get(name)
    return isinstance(clause, dm_filters.EQ) and clause.value == expected


def _is_mentioned_page(filters: typing.Mapping[str, typing.Any]) -> bool:
    names = set(filters)
    return (
        names
        in (
            {"project_id", "user_uuid", "mentioned"},
            {"project_id", "user_uuid", "mentioned", "read"},
        )
        and _eq_filter_matches(filters, "mentioned", True)
        and ("read" not in filters or _eq_filter_matches(filters, "read", False))
    )


def _database_timestamp(value: datetime.datetime) -> datetime.datetime:
    """Normalize UTC models for PostgreSQL columns without time zone."""
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def prune_expired_events(
    session: typing.Any,
    now: datetime.datetime,
    retention: datetime.timedelta = EVENT_RETENTION,
    batch_size: int = EVENT_PRUNE_BATCH_SIZE,
) -> int:
    """Prune one bounded event batch after advancing durable watermarks."""
    if batch_size < 1:
        raise ValueError("Event prune batch size must be positive")
    cutoff = now - retention
    result = session.execute(
        """
        WITH candidates AS MATERIALIZED (
            SELECT expired.*
            FROM (
                SELECT
                    'direct'::text AS "source",
                    event."epoch_version", event."project_id",
                    event."user_uuid",
                    NULL::uuid AS "audience_snapshot_uuid",
                    event."created_at"
                FROM "m_workspace_events" AS event
                WHERE event."created_at" < %s
                UNION ALL
                SELECT
                    'broadcast'::text AS "source",
                    event."epoch_version", event."project_id",
                    NULL::uuid AS "user_uuid",
                    event."audience_snapshot_uuid",
                    event."created_at"
                FROM "m_workspace_broadcast_message_events_v1" AS event
                WHERE event."created_at" < %s
            ) AS expired
            ORDER BY expired."created_at", expired."epoch_version"
            LIMIT %s
        ), locked_projects AS MATERIALIZED (
            SELECT
                projects."project_id",
                pg_advisory_xact_lock(
                    hashtextextended(projects."project_id"::text, 0)
                ) AS "locked"
            FROM (
                SELECT DISTINCT "project_id"
                FROM candidates
                ORDER BY "project_id"
            ) AS projects
        ), advanced_direct_cursors AS (
            INSERT INTO "m_workspace_event_cursors" (
                "project_id", "user_uuid", "current_epoch_version",
                "pruned_through_epoch_version"
            )
            SELECT
                "project_id", "user_uuid",
                MAX("epoch_version"), MAX("epoch_version")
            FROM candidates
            JOIN locked_projects USING ("project_id")
            WHERE "source" = 'direct'
            GROUP BY "project_id", "user_uuid"
            ON CONFLICT ("project_id", "user_uuid") DO UPDATE
            SET
                "current_epoch_version" = GREATEST(
                    "m_workspace_event_cursors"."current_epoch_version",
                    EXCLUDED."current_epoch_version"
                ),
                "pruned_through_epoch_version" = GREATEST(
                    "m_workspace_event_cursors"."pruned_through_epoch_version",
                    EXCLUDED."pruned_through_epoch_version"
                ),
                "updated_at" = NOW()
            RETURNING 1
        ), advanced_broadcast_cursors AS (
            UPDATE "m_workspace_event_audience_snapshots_v1" AS audience
            SET
                "current_epoch_version" = GREATEST(
                    audience."current_epoch_version",
                    expired."epoch_version"
                ),
                "pruned_through_epoch_version" = GREATEST(
                    audience."pruned_through_epoch_version",
                    expired."epoch_version"
                )
            FROM (
                SELECT
                    candidates."audience_snapshot_uuid",
                    MAX(candidates."epoch_version") AS "epoch_version"
                FROM candidates
                JOIN locked_projects USING ("project_id")
                WHERE "source" = 'broadcast'
                GROUP BY candidates."audience_snapshot_uuid"
            ) AS expired
            WHERE audience."uuid" = expired."audience_snapshot_uuid"
            RETURNING 1
        ), deleted_recipient_events AS (
            DELETE FROM "m_workspace_events" AS event
            USING candidates, locked_projects
            WHERE candidates."source" = 'direct'
              AND locked_projects."project_id" = candidates."project_id"
              AND event."epoch_version" = candidates."epoch_version"
            RETURNING 1
        ), deleted_broadcast_events AS (
            DELETE FROM "m_workspace_broadcast_message_events_v1" AS event
            USING candidates, locked_projects
            WHERE candidates."source" = 'broadcast'
              AND locked_projects."project_id" = candidates."project_id"
              AND event."epoch_version" = candidates."epoch_version"
            RETURNING 1
        )
        SELECT
            (SELECT COUNT(*) FROM deleted_recipient_events)
            + (SELECT COUNT(*) FROM deleted_broadcast_events) AS "count"
        """,
        (cutoff, cutoff, batch_size),
    ).fetchone()["count"]
    # Audience membership is immutable and shared. Remove it only after the
    # last referencing event has been pruned. First fold the final audience
    # watermark into durable per-user cursors; this is once per membership
    # revision, not once per broadcast event. Payload overrides cascade with
    # their event row.
    session.execute(
        """
        INSERT INTO "m_workspace_event_cursors" (
            "project_id", "user_uuid", "current_epoch_version",
            "pruned_through_epoch_version"
        )
        SELECT
            audience."project_id", member."user_uuid",
            MAX(audience."current_epoch_version"),
            MAX(audience."pruned_through_epoch_version")
        FROM "m_workspace_event_audience_snapshots_v1" AS audience
        JOIN "m_workspace_event_audience_members_v1" AS member
          ON member."audience_snapshot_uuid" = audience."uuid"
        WHERE NOT EXISTS (
            SELECT 1
            FROM "m_workspace_broadcast_message_events_v1" AS event
            WHERE event."audience_snapshot_uuid" = audience."uuid"
        )
        GROUP BY audience."project_id", member."user_uuid"
        ON CONFLICT ("project_id", "user_uuid") DO UPDATE
        SET
            "current_epoch_version" = GREATEST(
                "m_workspace_event_cursors"."current_epoch_version",
                EXCLUDED."current_epoch_version"
            ),
            "pruned_through_epoch_version" = GREATEST(
                "m_workspace_event_cursors"."pruned_through_epoch_version",
                EXCLUDED."pruned_through_epoch_version"
            ),
            "updated_at" = NOW()
        """,
        (),
    )
    session.execute(
        """
        DELETE FROM "m_workspace_event_audience_snapshots_v1" AS audience
        WHERE NOT EXISTS (
            SELECT 1
            FROM "m_workspace_broadcast_message_events_v1" AS event
            WHERE event."audience_snapshot_uuid" = audience."uuid"
        )
        """,
        (),
    )
    return result


class SQLCanonicalReadStore:
    """Serve the current public Messenger contract directly from PostgreSQL."""

    def __init__(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> None:
        self.project_uuid = sys_uuid.UUID(str(project_uuid))
        self.user_uuid = sys_uuid.UUID(str(user_uuid))

    def _read_state_mode(self) -> str:
        return read_state.project_mode(
            contexts.Context().get_session(),
            self.project_uuid,
        )

    def _scope_filters(
        self,
        resource: str,
        filters: dict[str, typing.Any],
    ) -> typing.Any:
        result = filters.copy()
        model = RESOURCE_MODELS[resource]
        properties = model.properties.properties
        if "project_id" in properties and resource != "files":
            result["project_id"] = dm_filters.EQ(self.project_uuid)
        if resource == "files":
            result = dm_filters.AND(
                result,
                dm_filters.OR(
                    dm_filters.AND(
                        {"project_id": dm_filters.EQ(self.project_uuid)},
                        {"viewer_user_uuid": dm_filters.EQ(self.user_uuid)},
                    ),
                    {"acl_mode": dm_filters.EQ("public")},
                ),
            )
        elif "viewer_user_uuid" in properties:
            result["viewer_user_uuid"] = dm_filters.EQ(self.user_uuid)
        elif resource in resource_projection.USER_SCOPED_RESOURCES:
            result["user_uuid"] = dm_filters.EQ(self.user_uuid)
        return result

    def sync_iam_identity(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        row = models.WorkspaceUser.sync_iam_identity(**values)
        return _public_dict(row, "users")

    def filter_resources(
        self,
        resource: str,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]:
        model = (
            models.WorkspaceDirectoryUser
            if resource == "users"
            else RESOURCE_MODELS[resource]
        )
        read_state_mode = None
        if resource == "streams":
            read_state_mode = self._read_state_mode()
        rows: typing.Any = ()
        started_at = time.monotonic()
        try:
            rows = model.objects.get_all(
                filters=self._scope_filters(resource, filters),
                order_by=order_by,
                limit=limit,
            )
        finally:
            duration = time.monotonic() - started_at
            if resource == "streams":
                row_count = len(rows)
                LOG.info(
                    "Messenger stream collection projection: "
                    "read_state_mode=%s rows=%d duration_seconds=%.3f",
                    read_state_mode,
                    row_count,
                    duration,
                    extra={
                        "projection_kind": "stream_collection",
                        "projection_duration_seconds": duration,
                        "read_state_mode": read_state_mode,
                        "projection_row_count": row_count,
                    },
                )
            if duration >= SLOW_LIST_PROJECTION_SECONDS:
                LOG.warning(
                    "Slow Messenger list projection",
                    extra={
                        "projection_resource": resource,
                        "projection_duration_seconds": duration,
                    },
                )
        return [_public_dict(row, resource) for row in rows]

    def _filter_mentioned_message_page(
        self,
        marker: typing.Any,
        sort_direction: str,
        limit: int | None,
        unread_only: bool,
    ) -> list[dict[str, typing.Any]]:
        if sort_direction not in {"asc", "desc"}:
            raise ra_exceptions.ValidationErrorException()
        direction = sort_direction.upper()
        marker_clause = ""
        marker_params: tuple[object, ...] = ()
        if marker is not None:
            operator = ">" if sort_direction == "asc" else "<"
            marker_clause = (
                f"AND (message.created_at {operator} %s OR "
                f"(message.created_at = %s AND message.uuid {operator} %s))"
            )
            marker_created_at = _database_timestamp(marker.created_at)
            marker_params = (marker_created_at, marker_created_at, marker.uuid)
        session = contexts.Context().get_session()
        if read_state.mode_uses_compact_state(
            read_state.project_mode(session, self.project_uuid)
        ):
            read_clause = ""
            if unread_only:
                read_clause = """
                  AND COALESCE(
                        get_bit(
                            chunk.read_bits,
                            (mention.ingest_sequence %% 4096)::integer
                        ),
                        0
                      ) = 0
                """
            statement = COMPACT_MENTIONED_MESSAGE_UUIDS_SQL.format(
                read_clause=read_clause,
                marker_clause=marker_clause,
                direction=direction,
            )
            candidate_rows = session.execute(
                statement,
                (
                    self.project_uuid,
                    self.user_uuid,
                    self.project_uuid,
                    self.user_uuid,
                    *marker_params,
                    limit,
                    limit,
                ),
            ).fetchall()
        else:
            candidate_rows = None
        flags_join = ""
        read_clause = ""
        read_params: tuple[object, ...] = ()
        if unread_only:
            flags_join = """
                LEFT JOIN m_workspace_user_message_flags AS flags
                  ON flags.project_id = message.project_id
                 AND flags.uuid = message.uuid
                 AND flags.user_uuid = %s
            """
            read_clause = "AND COALESCE(flags.read, FALSE) = FALSE"
            read_params = (self.user_uuid,)
        if candidate_rows is None:
            statement = MENTIONED_MESSAGE_UUIDS_SQL.format(
                flags_join=flags_join,
                read_clause=read_clause,
                marker_clause=marker_clause,
                direction=direction,
            )
            candidate_rows = session.execute(
                statement,
                (
                    self.project_uuid,
                    self.user_uuid,
                    *read_params,
                    self.project_uuid,
                    self.user_uuid,
                    *marker_params,
                    limit,
                    limit,
                ),
            ).fetchall()
        message_uuids = [row["uuid"] for row in candidate_rows]
        snapshots = helpers.get_compact_workspace_user_message_snapshots(
            self.project_uuid,
            message_uuids,
            [self.user_uuid],
            session=session,
        )
        snapshots_by_uuid = {row["uuid"]: row for row in snapshots}
        return [
            _public_dict(snapshots_by_uuid[message_uuid], "messages")
            for message_uuid in message_uuids
        ]

    def _filter_compact_unread_message_page(
        self,
        filters: dict[str, typing.Any],
        marker: typing.Any,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]] | None:
        if not set(filters).issubset({"read", "stream_uuid", "topic_uuid"}):
            return None
        read_filter = filters.get("read")
        if (
            not isinstance(read_filter, dm_filters.EQ)
            or read_filter._value is not False
        ):
            return None
        direction = sort_direction.upper()
        where = [
            "stats.project_id = %s",
            "binding.user_uuid = %s",
            "(stream.source_name = 'native' OR access.user_uuid IS NOT NULL)",
            "stats.message_count > COALESCE(topic_reads.read_count, 0)",
        ]
        params: list[object] = [self.project_uuid, self.user_uuid]
        for name in ("stream_uuid", "topic_uuid"):
            value_filter = filters.get(name)
            if value_filter is None:
                continue
            if not isinstance(value_filter, dm_filters.EQ):
                return None
            where.append(f"stats.{name} = %s")
            params.append(value_filter._value)
        marker_clause = ""
        marker_params: tuple[object, ...] = ()
        if marker is not None:
            operator = ">" if sort_direction == "asc" else "<"
            marker_clause = (
                f"AND (message.created_at {operator} %s OR "
                f"(message.created_at = %s AND message.uuid {operator} %s))"
            )
            marker_created_at = _database_timestamp(marker.created_at)
            marker_params = (marker_created_at, marker_created_at, marker.uuid)
        params.extend(
            (
                self.project_uuid,
                self.user_uuid,
                *marker_params,
                limit,
            )
        )
        session = contexts.Context().get_session()
        rows = session.execute(
            f"""
            WITH candidate_topics AS MATERIALIZED (
                SELECT stats.topic_uuid, stats.stream_uuid
                FROM m_workspace_topic_message_stats_v1 AS stats
                JOIN m_workspace_stream_bindings AS binding
                  ON binding.project_id = stats.project_id
                 AND binding.stream_uuid = stats.stream_uuid
                JOIN m_workspace_streams AS stream
                  ON stream.project_id = stats.project_id
                 AND stream.uuid = stats.stream_uuid
                LEFT JOIN m_confirmed_external_stream_access AS access
                  ON access.project_id = stats.project_id
                 AND access.user_uuid = binding.user_uuid
                 AND access.stream_uuid = stats.stream_uuid
                LEFT JOIN m_workspace_user_topic_read_stats_v1 AS topic_reads
                  ON topic_reads.project_id = stats.project_id
                 AND topic_reads.user_uuid = binding.user_uuid
                 AND topic_reads.topic_uuid = stats.topic_uuid
                WHERE {" AND ".join(where)}
            )
            SELECT message.uuid
            FROM candidate_topics AS candidate
            JOIN m_workspace_messages AS message
              ON message.project_id = %s
             AND message.stream_uuid = candidate.stream_uuid
             AND message.topic_uuid = candidate.topic_uuid
            LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
              ON chunk.user_uuid = %s
             AND chunk.chunk_number = message.ingest_sequence / 4096
            WHERE COALESCE(
                    get_bit(
                        chunk.read_bits,
                        (message.ingest_sequence %% 4096)::integer
                    ),
                    0
                  ) = 0
              {marker_clause}
            ORDER BY message.created_at {direction}, message.uuid {direction}
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        message_uuids = [row["uuid"] for row in rows]
        snapshots = helpers.get_compact_workspace_user_message_snapshots(
            self.project_uuid,
            message_uuids,
            [self.user_uuid],
            session=session,
        )
        snapshots_by_uuid = {row["uuid"]: row for row in snapshots}
        return [
            _public_dict(snapshots_by_uuid[message_uuid], "messages")
            for message_uuid in message_uuids
        ]

    def _filter_compact_read_message_page(
        self,
        filters: dict[str, typing.Any],
        marker: typing.Any,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]] | None:
        if not set(filters).issubset({"read", "stream_uuid", "topic_uuid"}):
            return None
        read_filter = filters.get("read")
        if not isinstance(read_filter, dm_filters.EQ) or read_filter._value is not True:
            return None
        scope_clauses = []
        scope_params: list[object] = []
        for name in ("stream_uuid", "topic_uuid"):
            value_filter = filters.get(name)
            if value_filter is None:
                continue
            if not isinstance(value_filter, dm_filters.EQ):
                return None
            scope_clauses.append(f"AND message.{name} = %s")
            scope_params.append(value_filter._value)
        marker_clause = ""
        marker_params: tuple[object, ...] = ()
        if marker is not None:
            operator = ">" if sort_direction == "asc" else "<"
            marker_clause = (
                f"AND (message.created_at {operator} %s OR "
                f"(message.created_at = %s AND message.uuid {operator} %s))"
            )
            marker_created_at = _database_timestamp(marker.created_at)
            marker_params = (marker_created_at, marker_created_at, marker.uuid)
        session = contexts.Context().get_session()
        rows = session.execute(
            COMPACT_READ_MESSAGE_UUIDS_SQL.format(
                scope_clause=" ".join(scope_clauses),
                marker_clause=marker_clause,
                direction=sort_direction.upper(),
            ),
            (
                self.project_uuid,
                self.user_uuid,
                self.project_uuid,
                self.user_uuid,
                *scope_params,
                *marker_params,
                limit,
            ),
        ).fetchall()
        message_uuids = [row["uuid"] for row in rows]
        snapshots = helpers.get_compact_workspace_user_message_snapshots(
            self.project_uuid,
            message_uuids,
            [self.user_uuid],
            session=session,
        )
        snapshots_by_uuid = {row["uuid"]: row for row in snapshots}
        return [
            _public_dict(snapshots_by_uuid[message_uuid], "messages")
            for message_uuid in message_uuids
        ]

    def filter_message_page(
        self,
        filters: dict[str, typing.Any],
        marker_uuid: sys_uuid.UUID | None,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        started_at = time.monotonic()
        requested_filters = filters.copy()
        scoped_filters = self._scope_filters("messages", filters)
        try:
            marker = None
            if marker_uuid is not None:
                marker_filters = scoped_filters.copy()
                marker_filters["uuid"] = dm_filters.EQ(marker_uuid)
                marker = models.WorkspaceUserMessage.objects.get_one(
                    filters=marker_filters,
                )
            if _is_mentioned_page(scoped_filters):
                return self._filter_mentioned_message_page(
                    marker,
                    sort_direction,
                    limit,
                    "read" in scoped_filters,
                )
            read_filter = requested_filters.get("read")
            if set(requested_filters).issubset(
                {"read", "stream_uuid", "topic_uuid"}
            ) and isinstance(read_filter, dm_filters.EQ):
                session = contexts.Context().get_session()
                if read_state.mode_uses_compact_state(
                    read_state.project_mode(session, self.project_uuid)
                ):
                    if read_filter._value is False:
                        compact_page = self._filter_compact_unread_message_page(
                            requested_filters,
                            marker,
                            sort_direction,
                            limit,
                        )
                    elif read_filter._value is True:
                        compact_page = self._filter_compact_read_message_page(
                            requested_filters,
                            marker,
                            sort_direction,
                            limit,
                        )
                    else:
                        compact_page = None
                    if compact_page is not None:
                        return compact_page
            if marker is not None:
                compare = dm_filters.GT if sort_direction == "asc" else dm_filters.LT
                keyset = dm_filters.OR(
                    {"created_at": compare(marker.created_at)},
                    dm_filters.AND(
                        {"created_at": dm_filters.EQ(marker.created_at)},
                        {"uuid": compare(marker.uuid)},
                    ),
                )
                scoped_filters = dm_filters.AND(scoped_filters, keyset)
            query = {
                "filters": scoped_filters,
                "order_by": {
                    "created_at": sort_direction,
                    "uuid": sort_direction,
                },
            }
            if limit is not None:
                query["limit"] = limit
            rows = models.WorkspaceUserMessage.objects.get_all(**query)
            return [_public_dict(row, "messages") for row in rows]
        finally:
            duration = time.monotonic() - started_at
            if duration >= SLOW_LIST_PROJECTION_SECONDS:
                LOG.warning(
                    "Slow Messenger list projection",
                    extra={
                        "projection_resource": "messages",
                        "projection_duration_seconds": duration,
                    },
                )

    def filter_draft_page(
        self,
        filters: dict[str, typing.Any],
        marker_uuid: sys_uuid.UUID | None,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        allowed_filters = {"stream_uuid", "topic_uuid"}
        if not set(filters).issubset(allowed_filters):
            raise ra_exceptions.ValidationErrorException()
        where = ['"project_id" = %s', '"user_uuid" = %s']
        params: list[object] = [self.project_uuid, self.user_uuid]
        scoped_filters = {
            "project_id": dm_filters.EQ(self.project_uuid),
            "user_uuid": dm_filters.EQ(self.user_uuid),
        }
        for name, clause in filters.items():
            if not isinstance(clause, dm_filters.EQ):
                raise ra_exceptions.ValidationErrorException()
            where.append(f'"{name}" = %s')
            params.append(clause.value)
            scoped_filters[name] = clause
        if marker_uuid is not None:
            marker_filters = scoped_filters.copy()
            marker_filters["uuid"] = dm_filters.EQ(marker_uuid)
            marker = models.WorkspaceDraft.objects.get_one(
                filters=marker_filters,
            )
            operator = ">" if sort_direction == "asc" else "<"
            where.append(
                f'("updated_at" {operator} %s OR '
                f'("updated_at" = %s AND "uuid" {operator} %s))'
            )
            params.extend([marker.updated_at, marker.updated_at, marker.uuid])
        direction = sort_direction.upper()
        statement = (
            'SELECT "uuid" FROM "m_workspace_drafts" WHERE '
            + " AND ".join(where)
            + f' ORDER BY "updated_at" {direction}, "uuid" {direction}'
        )
        if limit is not None:
            statement += " LIMIT %s"
            params.append(limit)
        session = contexts.Context().get_session()
        result = session.execute(statement, tuple(params))
        draft_uuids = [row["uuid"] for row in result.fetchall()]
        if not draft_uuids:
            return []
        rows = models.WorkspaceDraft.objects.get_all(
            filters={
                "uuid": dm_filters.In(draft_uuids),
                "project_id": dm_filters.EQ(self.project_uuid),
                "user_uuid": dm_filters.EQ(self.user_uuid),
            },
            session=session,
        )
        rows_by_uuid = {row.uuid: row for row in rows}
        return [
            _public_dict(rows_by_uuid[draft_uuid], "drafts")
            for draft_uuid in draft_uuids
        ]

    def get_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any]:
        read_state_mode = None
        if resource == "streams":
            read_state_mode = self._read_state_mode()
        row = None
        started_at = time.monotonic()
        try:
            if resource == "folder_items":
                row = helpers.get_workspace_user_folder_item(
                    self.project_uuid,
                    self.user_uuid,
                    resource_uuid,
                )
            elif resource == "files":
                row = models.WorkspaceVisibleFile.objects.get_one(
                    filters=self._scope_filters(
                        resource,
                        {"uuid": dm_filters.EQ(resource_uuid)},
                    )
                )
            else:
                model = RESOURCE_MODELS[resource]
                row = model.objects.get_one(
                    filters=self._scope_filters(
                        resource,
                        {model.get_id_property_name(): dm_filters.EQ(resource_uuid)},
                    )
                )
        finally:
            if resource == "streams":
                duration = time.monotonic() - started_at
                row_count = int(row is not None)
                LOG.info(
                    "Messenger exact stream projection: "
                    "read_state_mode=%s rows=%d duration_seconds=%.3f",
                    read_state_mode,
                    row_count,
                    duration,
                    extra={
                        "projection_kind": "stream_exact",
                        "projection_duration_seconds": duration,
                        "read_state_mode": read_state_mode,
                        "projection_row_count": row_count,
                    },
                )
        return _public_dict(row, resource)

    def get_draft(
        self,
        draft_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any]:
        row = helpers.get_workspace_draft(
            self.project_uuid,
            self.user_uuid,
            draft_uuid,
        )
        return _public_dict(row, "drafts")


class SQLCanonicalMessengerStore(SQLCanonicalReadStore):
    """Read and mutate canonical Messenger state in the request transaction."""

    def event_cursor(self) -> EventCursor:
        return PostgresEventStore(
            self.project_uuid,
            self.user_uuid,
        ).event_cursor()

    def events_after(
        self,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
        epoch_generation: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]:
        return PostgresEventStore(
            self.project_uuid,
            self.user_uuid,
        ).events_after(
            filters,
            order_by=order_by,
            epoch_generation=epoch_generation,
            limit=limit,
        )

    @staticmethod
    def _projection_values(
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        return resource_projection.projection_values(values)

    def _binding(
        self,
        stream_uuid: object,
        user_uuid: object | None = None,
    ) -> models.WorkspaceStreamBinding:
        return models.WorkspaceStreamBinding.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self.project_uuid),
                "stream_uuid": dm_filters.EQ(stream_uuid),
                "user_uuid": dm_filters.EQ(user_uuid or self.user_uuid),
            }
        )

    def _stream_participants(self, stream_uuid: object) -> tuple[object, ...]:
        return tuple(
            models.get_stream_recipients(
                self.project_uuid,
                typing.cast(sys_uuid.UUID, stream_uuid),
                session=contexts.Context().get_session(),
            )
        )

    def _direct_stream_participants(
        self,
        stream_uuid: object,
    ) -> set[object] | None:
        session = contexts.Context().get_session()
        row = session.execute(
            """
            SELECT "user_uuid", "direct_user_uuid", "private_index"
            FROM "m_workspace_streams"
            WHERE "project_id" = %s AND "uuid" = %s
            """,
            (self.project_uuid, stream_uuid),
        ).fetchone()
        if row is None:
            raise ra_exceptions.ValidationErrorException()
        if row["private_index"] is None:
            return None
        return {row["user_uuid"], row["direct_user_uuid"]}

    def _is_direct_stream(self, stream_uuid: object) -> bool:
        return self._direct_stream_participants(stream_uuid) is not None

    def _validate_stream_participants(
        self,
        stream_uuid: object,
        participants: collections.abc.Iterable[object],
    ) -> None:
        expected_participants = self._direct_stream_participants(stream_uuid)
        if expected_participants is None:
            return
        if set(participants) != expected_participants:
            raise ra_exceptions.ValidationErrorException()

    def _delete_replaced_avatar_file(self, avatar: str) -> None:
        if not avatar.startswith(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX):
            return
        helpers.delete_workspace_avatar_file(
            self.user_uuid,
            sys_uuid.UUID(avatar[len(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX) :]),
        )

    def _provider_account_uuids_for_stream(
        self,
        stream: models.WorkspaceStream,
    ) -> tuple[object, ...]:
        """Resolve every selected provider route for a native self-DM."""
        if stream.external_account_uuid is not None:
            return (stream.external_account_uuid,)
        if stream.private_index != helpers.build_private_stream_index(
            self.user_uuid,
            self.user_uuid,
        ):
            return ()
        session = contexts.Context().get_session()
        rows = session.execute(
            """
            SELECT DISTINCT external_account_uuid
            FROM m_external_chats_v2
            WHERE owner_user_uuid = %s AND project_id = %s
              AND projection_stream_uuid = %s AND selected
              AND status IN ('syncing', 'live', 'degraded')
              AND NOT transition_pending
            ORDER BY external_account_uuid
            """,
            (self.user_uuid, self.project_uuid, stream.uuid),
        ).fetchall()
        return tuple(row["external_account_uuid"] for row in rows)

    def _provider_user_account_uuids_for_stream(
        self,
        stream: models.WorkspaceStream,
    ) -> tuple[object, ...]:
        """Resolve provider routes owned by the current Workspace user."""
        session = contexts.Context().get_session()
        rows = session.execute(
            """
            SELECT DISTINCT external_account_uuid
            FROM m_external_chats_v2
            WHERE owner_user_uuid = %s AND project_id = %s
              AND projection_stream_uuid = %s AND selected
              AND status IN ('syncing', 'live', 'degraded')
              AND NOT transition_pending
            ORDER BY external_account_uuid
            """,
            (self.user_uuid, self.project_uuid, stream.uuid),
        ).fetchall()
        account_uuids = tuple(row["external_account_uuid"] for row in rows)
        if account_uuids:
            return account_uuids
        if (
            stream.external_account_uuid is not None
            and stream.user_uuid == self.user_uuid
        ):
            return (stream.external_account_uuid,)
        return ()

    def _provider_account_uuid_for_stream(
        self,
        stream: models.WorkspaceStream,
    ) -> object | None:
        """Resolve the sole selected provider route for a stream operation."""
        account_uuids = self._provider_account_uuids_for_stream(stream)
        return account_uuids[0] if len(account_uuids) == 1 else None

    def _provider_targets_for_stream(
        self,
        stream_uuid: object,
        operation_kind: str,
    ) -> tuple[typing.Any, ...]:
        """Resolve the author's selected provider routes before message creation."""
        stream = models.WorkspaceStream.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self.project_uuid),
                "uuid": dm_filters.EQ(stream_uuid),
            },
            session=contexts.Context().get_session(),
        )
        account_uuids = self._provider_user_account_uuids_for_stream(stream)
        if not account_uuids:
            return ()
        self._lock_provider_accounts(account_uuids)
        return tuple(
            target
            for account_uuid in account_uuids
            if (
                target := self._provider_target(
                    stream_uuid,
                    operation_kind,
                    account_locked=True,
                    external_account_uuid=account_uuid,
                )
            )
            is not None
        )

    def _message_provider_targets(
        self,
        message: models.WorkspaceMessage,
        operation_kind: str,
        *,
        account_locked: bool = False,
    ) -> tuple[typing.Any, ...]:
        """Resolve every durable provider route for a message operation."""
        session = contexts.Context().get_session()
        provenance = session.execute(
            """
            SELECT external_account_uuid
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (self.project_uuid, message.uuid),
        ).fetchone()
        if provenance is None:
            return ()
        message_account_uuid = provenance["external_account_uuid"]
        if message_account_uuid is None:
            create_routes = session.execute(
                """
                SELECT DISTINCT external_account_uuid
                FROM m_external_operations_v2
                WHERE owner_user_uuid = %s AND action = 'message.create'
                  AND target_type = 'message' AND target_uuid = %s
                ORDER BY external_account_uuid
                """,
                (self.user_uuid, message.uuid),
            ).fetchall()
            account_uuids = tuple(
                route["external_account_uuid"] for route in create_routes
            )
        else:
            actor_routes = session.execute(
                """
                SELECT DISTINCT actor_chat.external_account_uuid,
                       actor_account.provider_realm_uuid AS actor_realm_uuid,
                       provenance_account.provider_realm_uuid
                           AS provenance_realm_uuid,
                       actor_account.settings->>'server_url' AS actor_server_url,
                       provenance_account.settings->>'server_url'
                           AS provenance_server_url
                FROM m_external_chats_v2 AS provenance_chat
                JOIN m_external_accounts_v2 AS provenance_account
                  ON provenance_account.uuid = provenance_chat.external_account_uuid
                JOIN m_external_chats_v2 AS actor_chat
                  ON actor_chat.project_id = provenance_chat.project_id
                 AND actor_chat.projection_stream_uuid =
                     provenance_chat.projection_stream_uuid
                 AND actor_chat.provider = provenance_chat.provider
                 AND actor_chat.provider_chat_id =
                     provenance_chat.provider_chat_id
                JOIN m_external_accounts_v2 AS actor_account
                  ON actor_account.uuid = actor_chat.external_account_uuid
                 AND actor_account.provider = provenance_account.provider
                WHERE provenance_chat.external_account_uuid = %s
                  AND provenance_chat.project_id = %s
                  AND provenance_chat.projection_stream_uuid = %s
                  AND actor_chat.owner_user_uuid = %s
                  AND actor_chat.selected
                  AND actor_chat.status IN ('syncing', 'live', 'degraded')
                  AND NOT actor_chat.transition_pending
                ORDER BY actor_chat.external_account_uuid
                """,
                (
                    message_account_uuid,
                    self.project_uuid,
                    message.stream_uuid,
                    self.user_uuid,
                ),
            ).fetchall()
            account_uuids = tuple(
                route["external_account_uuid"]
                for route in actor_routes
                if self._provider_route_identity_matches(route)
            )
        if not account_uuids:
            return ()
        if not account_locked:
            self._lock_provider_accounts(account_uuids)
        return tuple(
            target
            for account_uuid in account_uuids
            if (
                target := self._provider_target(
                    message.stream_uuid,
                    operation_kind,
                    account_locked=True,
                    external_account_uuid=account_uuid,
                )
            )
            is not None
        )

    @staticmethod
    def _provider_route_identity_matches(
        route: typing.Mapping[str, typing.Any],
    ) -> bool:
        """Match a provider realm, normalizing pre-discovery origins."""
        actor_realm_uuid = route["actor_realm_uuid"]
        provenance_realm_uuid = route["provenance_realm_uuid"]
        if actor_realm_uuid is not None and provenance_realm_uuid is not None:
            return actor_realm_uuid == provenance_realm_uuid
        try:
            return identity_linking.normalize_provider_origin(
                route["actor_server_url"]
            ) == identity_linking.normalize_provider_origin(
                route["provenance_server_url"]
            )
        except ValueError:
            return False

    def _provider_target(
        self,
        stream_uuid: object,
        operation_kind: str | None = None,
        *,
        account_locked: bool = False,
        external_account_uuid: object | None = None,
    ) -> typing.Any:
        session = contexts.Context().get_session()
        stream = models.WorkspaceStream.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self.project_uuid),
                "uuid": dm_filters.EQ(stream_uuid),
            },
            session=session,
        )
        native_routes = getattr(self, "_provider_native_routes", None)
        if native_routes is None:
            native_routes = {}
            self._provider_native_routes = native_routes
        native_routes[sys_uuid.UUID(str(stream_uuid))] = (
            stream.external_account_uuid is None
        )
        external_account_uuid = (
            self._provider_account_uuid_for_stream(stream)
            if external_account_uuid is None
            else external_account_uuid
        )
        if external_account_uuid is None:
            return None
        if operation_kind is None:
            return _PROVIDER_TARGET_EXISTS
        if (
            operation_kind == "stream.update"
            and session.execute(
                """
            SELECT 1
            FROM m_external_chats_v2
            WHERE external_account_uuid = %s
              AND project_id = %s
              AND projection_stream_uuid = %s
              AND selected
              AND source->>'chat_type' = 'group'
              AND jsonb_array_length(
                      COALESCE(source->'participants', '[]'::jsonb)
                  ) > 1
            LIMIT 1
            """,
                (external_account_uuid, self.project_uuid, stream_uuid),
            ).fetchone()
            is not None
        ):
            # Zulip group DMs have no rename endpoint.  Their Workspace title
            # is therefore an intentionally local channel label.
            return None
        if operation_kind in {
            "stream.notification.update",
            "topic.notification.update",
        } and (
            getattr(stream, "private_index", None) is not None
            or self.user_uuid != stream.user_uuid
        ):
            # Zulip exposes these settings only for channels and their topics.
            # Preserve Workspace-local notification controls for projected DMs
            # and members who do not own the selected provider account.
            return None
        if not account_locked:
            self._lock_provider_account(external_account_uuid)
        required_capability = provider_data._required_capability(operation_kind)
        if required_capability is None:
            raise ra_exceptions.ValidationErrorException()
        if operation_kind in _CURRENT_USER_PROVIDER_OPERATIONS:
            route_owner_uuid = self.user_uuid
        elif operation_kind in _STREAM_OWNER_PROVIDER_OPERATIONS:
            route_owner_uuid = stream.user_uuid
        else:
            raise ra_exceptions.ValidationErrorException()
        try:
            account, _chat, bridge = provider_data.resolve_provider_target(
                session,
                project_id=self.project_uuid,
                owner_user_uuid=route_owner_uuid,
                external_account_uuid=external_account_uuid,
                stream_uuid=stream_uuid,
                capability_name=required_capability,
            )
        except provider_data.ProviderUnavailableError as exc:
            if operation_kind in {
                "membership.add",
                "membership.remove",
                "stream.notification.update",
                "topic.notification.update",
            }:
                try:
                    account, _chat, bridge = (
                        provider_data.resolve_provider_queue_target(
                            session,
                            project_id=self.project_uuid,
                            owner_user_uuid=route_owner_uuid,
                            external_account_uuid=external_account_uuid,
                            stream_uuid=stream_uuid,
                            allow_policy_blocked=operation_kind == "membership.remove",
                        )
                    )
                except provider_data.ProviderPolicyBlockedError as policy_exc:
                    if operation_kind == "membership.add":
                        raise (
                            messenger_exceptions.ExternalResourceForbiddenError()
                        ) from policy_exc
                    raise ra_exceptions.ValidationErrorException() from policy_exc
                except provider_data.ProviderUnavailableError:
                    raise ra_exceptions.ValidationErrorException() from exc
                provider_data.lock_provider_causal_lane(
                    session,
                    bridge_instance_uuid=bridge.uuid,
                    external_account_uuid=account.uuid,
                    causal_lane=stream_uuid,
                )
                return account, bridge
            raise ra_exceptions.ValidationErrorException() from exc
        provider_data.lock_provider_causal_lane(
            session,
            bridge_instance_uuid=bridge.uuid,
            external_account_uuid=account.uuid,
            causal_lane=stream_uuid,
        )
        return account, bridge

    def _lock_provider_account(self, account_uuid: object) -> None:
        """Establish account-before-message/outbox lock ordering."""
        self._lock_provider_accounts((account_uuid,))

    def _lock_provider_accounts(
        self,
        account_uuids: collections.abc.Iterable[object],
    ) -> None:
        """Lock all account resources before any account row or provider lane."""
        session = contexts.Context().get_session()
        ordered = tuple(
            sorted(
                {sys_uuid.UUID(str(value)) for value in account_uuids},
                key=str,
            )
        )
        if not ordered:
            return
        read_state.lock_external_account_resources(
            session,
            ordered,
            shared=True,
        )
        session.execute(
            """
            SELECT uuid
            FROM m_external_accounts_v2
            WHERE uuid = ANY(%s::uuid[])
            ORDER BY uuid
            FOR KEY SHARE
            """,
            (list(ordered),),
        ).fetchone()

    def _lock_provider_account_for_stream(
        self,
        stream_uuid: object,
    ) -> tuple[object, ...]:
        """Lock projected accounts and lanes without capability validation."""
        session = contexts.Context().get_session()
        stream = models.WorkspaceStream.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self.project_uuid),
                "uuid": dm_filters.EQ(stream_uuid),
            },
            session=session,
        )
        external_account_uuids = self._provider_account_uuids_for_stream(stream)
        if not external_account_uuids:
            return ()
        self._lock_provider_accounts(external_account_uuids)
        for external_account_uuid in external_account_uuids:
            try:
                account, _chat, bridge = provider_data.resolve_provider_queue_target(
                    session,
                    project_id=self.project_uuid,
                    owner_user_uuid=stream.user_uuid,
                    external_account_uuid=external_account_uuid,
                    stream_uuid=stream_uuid,
                    allow_policy_blocked=True,
                )
            except (
                provider_data.ProviderUnavailableError,
                storage_exceptions.RecordNotFound,
            ):
                # Preserve idempotent reads when no live route exists. A changed
                # read validates the provider target later and keeps the existing
                # public error contract.
                continue
            provider_data.lock_provider_causal_lane(
                session,
                bridge_instance_uuid=bridge.uuid,
                external_account_uuid=account.uuid,
                causal_lane=stream_uuid,
            )
        return external_account_uuids

    def _lock_provider_read_accounts_for_stream(
        self,
        stream_uuid: object,
    ) -> tuple[object, ...]:
        """Lock the current user's read routes and their causal lanes."""
        session = contexts.Context().get_session()
        stream = models.WorkspaceStream.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self.project_uuid),
                "uuid": dm_filters.EQ(stream_uuid),
            },
            session=session,
        )
        external_account_uuids = self._provider_user_account_uuids_for_stream(stream)
        if not external_account_uuids:
            return ()
        self._lock_provider_accounts(external_account_uuids)
        for external_account_uuid in external_account_uuids:
            try:
                account, _chat, bridge = provider_data.resolve_provider_queue_target(
                    session,
                    project_id=self.project_uuid,
                    owner_user_uuid=self.user_uuid,
                    external_account_uuid=external_account_uuid,
                    stream_uuid=stream_uuid,
                    allow_policy_blocked=True,
                )
            except (
                provider_data.ProviderUnavailableError,
                storage_exceptions.RecordNotFound,
            ):
                continue
            provider_data.lock_provider_causal_lane(
                session,
                bridge_instance_uuid=bridge.uuid,
                external_account_uuid=account.uuid,
                causal_lane=stream_uuid,
            )
        return external_account_uuids

    def _queue_provider_operation(
        self,
        *,
        operation_kind: str,
        target_type: str,
        target_uuid: object,
        stream_uuid: object,
        payload: object,
        provider_target: typing.Any = _PROVIDER_TARGET_UNSET,
    ) -> external_models.ExternalOperation | None:
        target = (
            self._provider_target(stream_uuid, operation_kind)
            if provider_target is _PROVIDER_TARGET_UNSET
            else provider_target
        )
        if target is None:
            return None
        account, bridge = target
        operation, _record_uuid = provider_data.enqueue_provider_operation_in_lane(
            contexts.Context().get_session(),
            operation_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=bridge.uuid,
            external_account_uuid=account.uuid,
            project_id=self.project_uuid,
            owner_user_uuid=self.user_uuid,
            operation_kind=operation_kind,
            target_type=target_type,
            target_uuid=target_uuid,
            payload=resource_projection.simple(payload),
            causal_lane=stream_uuid,
        )
        return operation

    def _queue_provider_read(
        self,
        *,
        stream_uuid: object,
        topic_uuid: object | None,
        message_uuids: collections.abc.Sequence[object],
        target_type: str,
        target_uuid: object,
        provider_target: typing.Any = _PROVIDER_TARGET_UNSET,
    ) -> external_models.ExternalOperation | None:
        """Queue exact, retry-safe provider read-state projection chunks."""
        if not message_uuids:
            return None
        target = provider_target
        session = (
            contexts.Context().get_session()
            if target is not _PROVIDER_TARGET_UNSET and target is not None
            else None
        )
        first_operation = None
        for offset in range(0, len(message_uuids), _PROVIDER_READ_STATE_MAX_MESSAGES):
            chunk = message_uuids[offset : offset + _PROVIDER_READ_STATE_MAX_MESSAGES]
            provider_message_ids = (
                provider_data._provider_message_ids_for_read_page(
                    session,
                    external_account_uuid=target[0].uuid,
                    project_id=self.project_uuid,
                    message_uuids=chunk,
                )
                if session is not None
                else None
            )
            queue_values: dict[str, typing.Any] = {
                "operation_kind": "read_state.set",
                "target_type": target_type,
                "target_uuid": target_uuid,
                "stream_uuid": stream_uuid,
                "payload": {
                    "stream_uuid": str(stream_uuid),
                    "topic_uuid": None if topic_uuid is None else str(topic_uuid),
                    "reader_uuid": str(self.user_uuid),
                    "message_uuids": [str(message_uuid) for message_uuid in chunk],
                    **(
                        {}
                        if provider_message_ids is None
                        else {"provider_message_ids": provider_message_ids}
                    ),
                    "read": True,
                },
            }
            if target is not _PROVIDER_TARGET_UNSET:
                queue_values["provider_target"] = target
            operation = self._queue_provider_operation(**queue_values)
            if first_operation is None:
                first_operation = operation
        return first_operation

    def _provider_read_batch_callback(
        self,
        *,
        provider_account_locked: bool,
        stream_uuid: object,
        topic_uuid: object | None,
        target_type: str,
        target_uuid: object,
    ) -> typing.Callable[[collections.abc.Sequence[object]], None] | None:
        """Resolve once and enqueue bounded exact provider read snapshots."""
        if not provider_account_locked:
            return None
        provider_target: typing.Any = _PROVIDER_TARGET_UNSET

        def queue(message_uuids: collections.abc.Sequence[object]) -> None:
            nonlocal provider_target
            if provider_target is _PROVIDER_TARGET_UNSET:
                provider_target = self._provider_target(
                    stream_uuid,
                    "read_state.set",
                    account_locked=True,
                )
            self._queue_provider_read(
                stream_uuid=stream_uuid,
                topic_uuid=topic_uuid,
                message_uuids=message_uuids,
                target_type=target_type,
                target_uuid=target_uuid,
                provider_target=provider_target,
            )

        return queue

    def _provider_read_snapshot_callback(
        self,
        *,
        provider_account_uuids: collections.abc.Sequence[object],
        stream_uuid: object,
        topic_uuid: object | None,
        target_type: str,
        target_uuid: object,
    ) -> read_state.BulkReadSnapshotCallback | None:
        """Queue one exact provider read snapshot from compact state."""
        if not provider_account_uuids:
            return None
        provider_targets: tuple[typing.Any, ...] | None = None

        def queue(
            session: typing.Any,
            candidate_sql: str,
            candidate_values: collections.abc.Sequence[object],
            candidate_chunks: collections.abc.Sequence[
                collections.abc.Mapping[str, object]
            ]
            | None = None,
        ) -> None:
            nonlocal provider_targets
            if provider_targets is None:
                provider_targets = tuple(
                    target
                    for external_account_uuid in provider_account_uuids
                    if (
                        target := self._provider_target(
                            stream_uuid,
                            "read_state.set",
                            account_locked=True,
                            external_account_uuid=external_account_uuid,
                        )
                    )
                    is not None
                )
            native_routes = getattr(self, "_provider_native_routes", {})
            native_route = native_routes.get(sys_uuid.UUID(str(stream_uuid)), False)
            for account, bridge in provider_targets:
                target_candidate_sql = candidate_sql
                target_candidate_values = candidate_values
                target_candidate_chunks = candidate_chunks
                if native_route:
                    target_candidate_sql = f"""
                        SELECT candidate.uuid, candidate.created_at
                        FROM ({candidate_sql}) AS candidate
                        JOIN m_workspace_messages AS message
                          ON message.uuid = candidate.uuid
                        WHERE message.external_account_uuid = %s
                           OR (
                                message.external_account_uuid IS NULL
                                AND EXISTS (
                                    SELECT 1
                                    FROM m_external_operations_v2 AS operation
                                    WHERE operation.external_account_uuid = %s
                                      AND operation.owner_user_uuid = %s
                                      AND operation.action = 'message.create'
                                      AND operation.target_type = 'message'
                                      AND operation.target_uuid = message.uuid
                                )
                           )
                    """
                    target_candidate_values = (
                        *candidate_values,
                        account.uuid,
                        account.uuid,
                        self.user_uuid,
                    )
                    target_candidate_chunks = None
                read_revision = provider_data._capability_revision(
                    bridge.capabilities,
                    provider_data.PROVIDER_READ_PAGING_CAPABILITY,
                )
                use_lazy_snapshot = (
                    read_revision >= provider_data.PROVIDER_READ_PAGING_REVISION
                )
                candidate_chunk_mode: bool | None = False
                if use_lazy_snapshot:
                    candidate_chunk_mode = (
                        True if target_candidate_chunks is not None else None
                    )
                try:
                    provider_data.enqueue_provider_read_operation(
                        session,
                        operation_uuid=sys_uuid.uuid4(),
                        bridge_instance_uuid=bridge.uuid,
                        external_account_uuid=account.uuid,
                        project_id=self.project_uuid,
                        owner_user_uuid=self.user_uuid,
                        target_type=target_type,
                        target_uuid=target_uuid,
                        payload={
                            "stream_uuid": str(stream_uuid),
                            "topic_uuid": (
                                None if topic_uuid is None else str(topic_uuid)
                            ),
                            "reader_uuid": str(self.user_uuid),
                            "read": True,
                        },
                        candidate_sql=target_candidate_sql,
                        candidate_values=target_candidate_values,
                        candidate_chunks=target_candidate_chunks,
                        use_candidate_chunks=candidate_chunk_mode,
                    )
                except provider_data.ProviderUnavailableError as exc:
                    raise ra_exceptions.ValidationErrorException() from exc

        return queue

    def _provider_read_scope_callbacks(
        self,
        *,
        provider_account_uuids: collections.abc.Sequence[object],
        stream_uuid: object,
        topic_uuid: object | None,
        target_type: str,
        target_uuid: object,
        where_sql: str,
        where_values: collections.abc.Sequence[object],
    ) -> tuple[
        read_state.BulkReadSnapshotCallback | None,
        typing.Callable[[typing.Any], None],
    ]:
        """Project the complete provider scope, including idempotent reads."""
        provider_callback = self._provider_read_snapshot_callback(
            provider_account_uuids=provider_account_uuids,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            target_type=target_type,
            target_uuid=target_uuid,
        )
        if provider_callback is None:
            return None, lambda _session: None
        scope_sql = f"""
            SELECT message.uuid, message.created_at, message.ingest_sequence
            FROM m_workspace_messages AS message
            WHERE message.project_id = %s AND {where_sql}
            ORDER BY message.created_at, message.uuid
        """
        scope_values = (self.project_uuid, *where_values)
        reconciled = False

        def queue(
            session: typing.Any,
            candidate_sql: str,
            candidate_values: collections.abc.Sequence[object],
            candidate_chunks: collections.abc.Sequence[
                collections.abc.Mapping[str, object]
            ]
            | None = None,
        ) -> None:
            del candidate_chunks
            nonlocal reconciled
            changed = (
                session.execute(
                    f"SELECT 1 FROM ({candidate_sql}) AS candidate LIMIT 1",
                    candidate_values,
                ).fetchone()
                is not None
            )
            try:
                provider_callback(session, scope_sql, scope_values)
            except (
                ra_exceptions.ValidationErrorException,
                storage_exceptions.RecordNotFound,
            ):
                if changed:
                    raise
            reconciled = True

        def reconcile_idempotent(session: typing.Any) -> None:
            nonlocal reconciled
            if reconciled:
                return
            try:
                provider_callback(session, scope_sql, scope_values)
            except (
                ra_exceptions.ValidationErrorException,
                storage_exceptions.RecordNotFound,
            ):
                pass
            reconciled = True

        return queue, reconcile_idempotent

    def create_resource(
        self,
        resource: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        values = self._projection_values(values)
        values["uuid"] = values.get("uuid") or sys_uuid.uuid4()
        if resource == "folders":
            row = helpers.create_workspace_user_folder(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                **values,
            )
        elif resource == "folder_items":
            row = helpers.create_workspace_user_folder_item(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                **values,
            )
        elif resource == "streams":
            row = helpers.get_or_create_workspace_user_stream(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                **values,
            )
        elif resource == "stream_topics":
            provider_target = self._provider_target(
                values["stream_uuid"],
                "topic.create",
            )
            row = helpers.create_workspace_user_stream_topic(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                values=values,
            )
            self._queue_provider_operation(
                operation_kind="topic.create",
                target_type="topic",
                target_uuid=row.uuid,
                stream_uuid=row.stream_uuid,
                payload=_public_dict(row, resource),
                provider_target=provider_target,
            )
        elif resource == "message_reactions":
            message = helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                values["message_uuid"],
            )
            provider_targets = self._message_provider_targets(
                message,
                "reaction.create",
            )
            row = helpers.create_workspace_message_reaction(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                compact_events=True,
                **values,
            )
            for provider_target in provider_targets:
                self._queue_provider_operation(
                    operation_kind="reaction.create",
                    target_type="reaction",
                    target_uuid=row.uuid,
                    stream_uuid=message.stream_uuid,
                    payload=_public_dict(row, resource),
                    provider_target=provider_target,
                )
        elif resource == "files":
            row = helpers.create_workspace_file(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                **values,
            )
        else:
            raise ValueError(f"Unsupported Messenger create resource {resource}")
        return _public_dict(row, resource)

    def update_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        values = self._projection_values(values)
        if resource == "folders":
            row = helpers.update_workspace_user_folder(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                **values,
            )
        elif resource == "streams":
            provider_target = self._provider_target(
                resource_uuid,
                "stream.update",
            )
            row = helpers.update_workspace_user_stream(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values,
            )
            self._queue_provider_operation(
                operation_kind="stream.update",
                target_type="stream",
                target_uuid=row.uuid,
                stream_uuid=row.uuid,
                payload=_public_dict(row, resource),
                provider_target=provider_target,
            )
        elif resource == "stream_topics":
            topic = helpers.get_workspace_user_stream_topic(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
            )
            source_stream_uuid = topic.stream_uuid
            source_target = self._provider_target(
                source_stream_uuid,
                "topic.update",
            )
            destination_stream_uuid = sys_uuid.UUID(
                str(values.get("stream_uuid", source_stream_uuid))
            )
            if destination_stream_uuid != source_stream_uuid:
                destination_target = self._provider_target(destination_stream_uuid)
                if source_target is not None or destination_target is not None:
                    # Provider topic movement has no negotiated capability in v1.
                    # Reject before touching canonical state instead of producing
                    # a local-only move or addressing the destination account.
                    raise ra_exceptions.ValidationErrorException()
            row = helpers.update_workspace_user_stream_topic(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values,
            )
            self._queue_provider_operation(
                operation_kind="topic.update",
                target_type="topic",
                target_uuid=row.uuid,
                stream_uuid=row.stream_uuid,
                payload=_public_dict(row, resource),
                provider_target=source_target,
            )
        elif resource == "message_reactions":
            reaction = helpers.get_workspace_message_reaction(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
            )
            previous_message_uuid = reaction.message_uuid
            previous_emoji_name = reaction.emoji_name
            message = helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                reaction.message_uuid,
            )
            provider_targets = self._message_provider_targets(
                message,
                "reaction.update",
            )
            row = helpers.update_workspace_message_reaction(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values,
                compact_events=True,
            )
            provider_payload = _public_dict(row, resource)
            provider_payload.update(
                {
                    "previous_message_uuid": str(previous_message_uuid),
                    "previous_emoji_name": previous_emoji_name,
                }
            )
            for provider_target in provider_targets:
                self._queue_provider_operation(
                    operation_kind="reaction.update",
                    target_type="reaction",
                    target_uuid=row.uuid,
                    stream_uuid=message.stream_uuid,
                    payload=provider_payload,
                    provider_target=provider_target,
                )
        elif resource == "files":
            row = helpers.update_workspace_file(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values,
            )
        elif resource == "stream_bindings":
            row = self._binding_for_update(resource_uuid)
            if self._is_direct_stream(row.stream_uuid):
                raise ra_exceptions.ValidationErrorException()
            row.update_dm(values=values)
            row.update(session=contexts.Context().get_session())
            helpers.create_workspace_stream_binding_updated_events(row)
        else:
            raise ValueError(f"Unsupported Messenger update resource {resource}")
        return _public_dict(row, resource)

    def _binding_for_update(
        self,
        binding_uuid: sys_uuid.UUID,
    ) -> models.WorkspaceStreamBinding:
        return models.WorkspaceStreamBinding.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self.project_uuid),
                "uuid": dm_filters.EQ(binding_uuid),
            }
        )

    def delete_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        provider_stream_uuid = None
        provider_payload = None
        provider_target: typing.Any = _PROVIDER_TARGET_UNSET
        provider_targets: tuple[typing.Any, ...] | None = None
        if resource == "streams":
            provider_stream_uuid = resource_uuid
            provider_payload = {"uuid": str(resource_uuid)}
        elif resource == "stream_topics":
            topic = helpers.get_workspace_user_stream_topic(
                self.project_uuid, self.user_uuid, resource_uuid
            )
            provider_stream_uuid = topic.stream_uuid
            provider_payload = _public_dict(topic, resource)
        elif resource == "message_reactions":
            reaction = helpers.get_workspace_message_reaction(
                self.project_uuid, self.user_uuid, resource_uuid
            )
            message = helpers.get_workspace_user_message(
                self.project_uuid, self.user_uuid, reaction.message_uuid
            )
            provider_stream_uuid = message.stream_uuid
            provider_payload = _public_dict(reaction, resource)
            provider_targets = self._message_provider_targets(
                message,
                "reaction.delete",
            )
        if provider_stream_uuid is not None:
            operation_kind, target_type = {
                "streams": ("stream.delete", "stream"),
                "stream_topics": ("topic.delete", "topic"),
                "message_reactions": ("reaction.delete", "reaction"),
            }[resource]
            targets = (
                provider_targets if provider_targets is not None else (provider_target,)
            )
            for target in targets:
                self._queue_provider_operation(
                    operation_kind=operation_kind,
                    target_type=target_type,
                    target_uuid=resource_uuid,
                    stream_uuid=provider_stream_uuid,
                    payload=provider_payload,
                    provider_target=target,
                )
        if resource == "folders":
            helpers.delete_workspace_user_folder(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "folder_items":
            helpers.delete_workspace_user_folder_item(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "streams":
            helpers.delete_workspace_user_stream(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "stream_bindings":
            binding = self._binding_for_update(resource_uuid)
            remaining = tuple(
                participant
                for participant in self._stream_participants(binding.stream_uuid)
                if participant != binding.user_uuid
            )
            self._validate_stream_participants(binding.stream_uuid, remaining)
            try:
                provider_target = self._provider_target(
                    binding.stream_uuid,
                    "membership.remove",
                )
            except (
                ra_exceptions.ValidationErrorException,
                storage_exceptions.RecordNotFound,
            ):
                # Local access revocation must still commit for malformed
                # legacy projections or when no durable provider route exists.
                provider_target = None
            provider_payload = _public_dict(binding, resource)
            helpers.delete_workspace_stream_binding(self.project_uuid, resource_uuid)
            self._queue_provider_operation(
                operation_kind="membership.remove",
                target_type="stream_binding",
                target_uuid=binding.uuid,
                stream_uuid=binding.stream_uuid,
                payload=provider_payload,
                provider_target=provider_target,
            )
        elif resource == "stream_topics":
            helpers.delete_workspace_user_stream_topic(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "message_reactions":
            helpers.delete_workspace_message_reaction(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                compact_events=True,
            )
        elif resource == "files":
            helpers.delete_workspace_file(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        else:
            raise ValueError(f"Unsupported Messenger delete resource {resource}")
        return None

    def create_message(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        values = self._projection_values(values)
        values["uuid"] = values.get("uuid") or sys_uuid.uuid4()
        provider_targets = self._provider_targets_for_stream(
            values["stream_uuid"],
            "message.create",
        )
        session = contexts.Context().get_session()
        row = helpers.create_workspace_user_message(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            session=session,
            enforce_visibility=True,
            compact_events=True,
            **values,
        )
        for provider_target in provider_targets:
            self._queue_provider_operation(
                operation_kind="message.create",
                target_type="message",
                target_uuid=row.uuid,
                stream_uuid=row.stream_uuid,
                payload=_public_dict(row, "messages"),
                provider_target=provider_target,
            )
        return _public_dict(row, "messages")

    def update_message(
        self,
        message_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        message = helpers.get_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
        )
        provider_targets = self._message_provider_targets(message, "message.update")
        row = helpers.update_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
            self._projection_values(values),
            compact_events=True,
        )
        for provider_target in provider_targets:
            self._queue_provider_operation(
                operation_kind="message.update",
                target_type="message",
                target_uuid=row.uuid,
                stream_uuid=row.stream_uuid,
                payload=_public_dict(row, "messages"),
                provider_target=provider_target,
            )
        return _public_dict(row, "messages")

    def delete_message(
        self,
        message_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        message = helpers.get_workspace_user_message(
            self.project_uuid, self.user_uuid, message_uuid
        )
        provider_targets = self._message_provider_targets(message, "message.delete")
        helpers.delete_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
            compact_events=True,
        )
        for provider_target in provider_targets:
            self._queue_provider_operation(
                operation_kind="message.delete",
                target_type="message",
                target_uuid=message_uuid,
                stream_uuid=message.stream_uuid,
                payload=_public_dict(message, "messages"),
                provider_target=provider_target,
            )
        return None

    def create_draft(
        self,
        values: dict[str, typing.Any],
    ) -> tuple[dict[str, typing.Any], bool]:
        draft, created = helpers.create_workspace_draft(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            draft_uuid=values["uuid"],
            stream_uuid=values["stream_uuid"],
            topic_uuid=values["topic_uuid"],
            payload=values["payload"],
        )
        return _public_dict(draft, "drafts"), created

    def update_draft(
        self,
        draft_uuid: sys_uuid.UUID,
        payload: dict[str, typing.Any],
        expected_revision: int,
    ) -> dict[str, typing.Any]:
        draft, updated = helpers.update_workspace_draft(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            draft_uuid=draft_uuid,
            payload=payload,
            expected_revision=expected_revision,
        )
        if not updated:
            raise messenger_exceptions.DraftPreconditionFailedError(
                _public_dict(draft, "drafts")
            )
        return _public_dict(draft, "drafts")

    def delete_draft(
        self,
        draft_uuid: sys_uuid.UUID,
        expected_revision: int,
    ) -> None:
        draft, deleted = helpers.delete_workspace_draft(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            draft_uuid=draft_uuid,
            expected_revision=expected_revision,
        )
        if not deleted:
            raise messenger_exceptions.DraftPreconditionFailedError(
                _public_dict(draft, "drafts")
            )

    def perform_action(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        action: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any] | list[dict[str, typing.Any]]:
        resource_uuid = sys_uuid.UUID(str(resource_uuid))
        if resource == "folder_items" and action in {"pin", "unpin"}:
            function = (
                helpers.pin_workspace_user_folder_item
                if action == "pin"
                else helpers.unpin_workspace_user_folder_item
            )
            row = function(self.project_uuid, self.user_uuid, resource_uuid)
        elif resource == "streams" and action in {"archive", "unarchive"}:
            row = helpers.update_workspace_user_stream(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                {"is_archived": action == "archive"},
            )
        elif resource == "streams" and action == "notifications":
            provider_target = self._provider_target(
                resource_uuid,
                "stream.notification.update",
            )
            row = helpers.update_workspace_user_stream_notifications(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values["notification_mode"],
            )
            binding = models.WorkspaceStreamBinding.objects.get_one(
                filters={
                    "project_id": dm_filters.EQ(self.project_uuid),
                    "stream_uuid": dm_filters.EQ(resource_uuid),
                    "user_uuid": dm_filters.EQ(self.user_uuid),
                },
                session=contexts.Context().get_session(),
            )
            self._queue_provider_operation(
                operation_kind="stream.notification.update",
                target_type="stream",
                target_uuid=resource_uuid,
                stream_uuid=resource_uuid,
                payload={
                    "uuid": str(resource_uuid),
                    "stream_uuid": str(resource_uuid),
                    "user_uuid": str(self.user_uuid),
                    "notification_mode": binding.notification_mode,
                    "notification_updated_at": binding.notification_updated_at,
                },
                provider_target=provider_target,
            )
        elif resource == "streams" and action == "read":
            session = contexts.Context().get_session()
            stream = helpers.get_workspace_user_stream(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
            )
            provider_account_uuids = self._lock_provider_read_accounts_for_stream(
                resource_uuid
            )
            provider_callback, reconcile_provider = self._provider_read_scope_callbacks(
                provider_account_uuids=provider_account_uuids,
                stream_uuid=resource_uuid,
                topic_uuid=None,
                target_type="stream",
                target_uuid=resource_uuid,
                where_sql="message.stream_uuid = %s",
                where_values=(resource_uuid,),
            )
            row = helpers.read_workspace_user_stream_messages(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_stream=stream,
                collect_message_uuids=False,
                message_uuid_snapshot_callback=provider_callback,
            )
            reconcile_provider(session)
        elif resource == "stream_bindings" and action == "add_users":
            role_user_uuids = {
                role: [sys_uuid.UUID(str(value)) for value in user_uuids]
                for role, user_uuids in values.items()
            }
            current_participants = set(self._stream_participants(resource_uuid))
            participants = current_participants | {
                user_uuid
                for user_uuids in role_user_uuids.values()
                for user_uuid in user_uuids
            }
            self._validate_stream_participants(resource_uuid, tuple(participants))
            new_participants = participants - current_participants
            if new_participants:
                try:
                    provider_target = self._provider_target(
                        resource_uuid,
                        "membership.add",
                    )
                except (
                    ra_exceptions.ValidationErrorException,
                    storage_exceptions.RecordNotFound,
                ):
                    # Preserve canonical membership for malformed legacy
                    # projections that have no durable provider route.
                    provider_target = None
            else:
                provider_target = _PROVIDER_TARGET_UNSET
            row = helpers.get_or_create_workspace_stream_bindings(
                self.project_uuid,
                resource_uuid,
                self.user_uuid,
                role_user_uuids,
                restore_external_membership=True,
            )
            queued_binding_uuids = set()
            for binding in row:
                if (
                    binding.user_uuid not in new_participants
                    or binding.uuid in queued_binding_uuids
                ):
                    continue
                queued_binding_uuids.add(binding.uuid)
                self._queue_provider_operation(
                    operation_kind="membership.add",
                    target_type="stream_binding",
                    target_uuid=binding.uuid,
                    stream_uuid=resource_uuid,
                    payload=_public_dict(binding, resource),
                    provider_target=provider_target,
                )
        elif resource == "stream_topics" and action == "toggle_done":
            row = helpers.toggle_workspace_user_stream_topic_done(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "stream_topics" and action == "notifications":
            topic = helpers.get_workspace_user_stream_topic(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
            )
            provider_target = self._provider_target(
                topic.stream_uuid,
                "topic.notification.update",
            )
            row = helpers.update_workspace_user_stream_topic_notifications(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values["notification_mode"],
            )
            flags = models.WorkspaceUserTopicFlags.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(resource_uuid),
                    "project_id": dm_filters.EQ(self.project_uuid),
                    "user_uuid": dm_filters.EQ(self.user_uuid),
                },
                session=contexts.Context().get_session(),
            )
            self._queue_provider_operation(
                operation_kind="topic.notification.update",
                target_type="topic",
                target_uuid=resource_uuid,
                stream_uuid=topic.stream_uuid,
                payload={
                    "uuid": str(resource_uuid),
                    "stream_uuid": str(topic.stream_uuid),
                    "user_uuid": str(self.user_uuid),
                    "notification_mode": flags.notification_mode,
                    "notification_updated_at": flags.notification_updated_at,
                },
                provider_target=provider_target,
            )
        elif resource == "stream_topics" and action == "set_default":
            row = helpers.set_workspace_user_stream_topic_default(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "stream_topics" and action == "set_summary_prompt":
            prompt_values = {}
            for name in (
                "summary_system_prompt",
                "summary_reasoning_effort",
                "summary_enabled",
            ):
                if name in values:
                    prompt_values[name] = values[name]
            row = helpers.set_workspace_user_stream_topic_summary_prompt(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                **prompt_values,
            )
        elif resource == "stream_topics" and action == "read":
            session = contexts.Context().get_session()
            topic = helpers.get_workspace_user_stream_topic(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
            )
            provider_account_uuids = self._lock_provider_read_accounts_for_stream(
                topic.stream_uuid
            )
            provider_callback, reconcile_provider = self._provider_read_scope_callbacks(
                provider_account_uuids=provider_account_uuids,
                stream_uuid=topic.stream_uuid,
                topic_uuid=resource_uuid,
                target_type="topic",
                target_uuid=resource_uuid,
                where_sql=("message.stream_uuid = %s AND message.topic_uuid = %s"),
                where_values=(topic.stream_uuid, resource_uuid),
            )
            row = helpers.read_workspace_user_stream_topic_messages(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_topic=topic,
                collect_message_uuids=False,
                message_uuid_snapshot_callback=provider_callback,
            )
            reconcile_provider(session)
        elif resource == "messages" and action == "read":
            session = contexts.Context().get_session()
            message = helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
            )
            provider_account_uuids = self._lock_provider_read_accounts_for_stream(
                message.stream_uuid
            )
            provider_callback = self._provider_read_snapshot_callback(
                provider_account_uuids=provider_account_uuids,
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                target_type="message",
                target_uuid=resource_uuid,
            )
            row, message_uuids = helpers.read_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_message=message,
                return_message_uuids=True,
            )
            if provider_callback is not None:
                try:
                    provider_callback(
                        session,
                        """
                            SELECT message.uuid, message.created_at,
                                   message.ingest_sequence
                            FROM m_workspace_messages AS message
                            WHERE message.project_id = %s
                              AND message.uuid = %s
                            ORDER BY message.created_at, message.uuid
                        """,
                        (self.project_uuid, message.uuid),
                    )
                except (
                    ra_exceptions.ValidationErrorException,
                    storage_exceptions.RecordNotFound,
                ):
                    if message_uuids:
                        raise
        elif resource == "messages" and action == "read_up_to":
            session = contexts.Context().get_session()
            message = helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
            )
            # Lock the provider account before the UPDATE snapshot.
            # A concurrent provider event can make a boundary row unread after
            # any separate probe. Capability validation stays after RETURNING
            # so an idempotent no-op does not depend on provider availability.
            provider_account_uuids = self._lock_provider_read_accounts_for_stream(
                message.stream_uuid
            )
            boundary_created_at = _database_timestamp(message.created_at)
            provider_callback, reconcile_provider = self._provider_read_scope_callbacks(
                provider_account_uuids=provider_account_uuids,
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                target_type="message",
                target_uuid=resource_uuid,
                where_sql=(
                    "message.stream_uuid = %s "
                    "AND message.topic_uuid = %s "
                    "AND (message.created_at, message.uuid) <= (%s, %s)"
                ),
                where_values=(
                    message.stream_uuid,
                    message.topic_uuid,
                    boundary_created_at,
                    resource_uuid,
                ),
            )
            row = helpers.read_workspace_user_topic_messages_to_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_message=message,
                collect_message_uuids=False,
                message_uuid_snapshot_callback=provider_callback,
            )
            reconcile_provider(session)
        elif resource == "messages" and action in {"star", "unstar"}:
            row = helpers.sync_workspace_user_message_flags(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                message_uuid=resource_uuid,
                values={"starred": action == "star"},
                session=contexts.Context().get_session(),
            )
        elif resource == "users" and action == "presence":
            projection_values = {"status": values["status"]}
            if "emoji" in values:
                projection_values["status_emoji"] = values["emoji"]
            if "text" in values:
                projection_values["status_text"] = values["text"]
            row = helpers.update_workspace_user_presence(
                self.project_uuid,
                resource_uuid,
                self.user_uuid,
                projection_values,
            )
        elif resource == "users" and action == "avatar_upload":
            user = helpers.get_workspace_own_user(resource_uuid, self.user_uuid)
            old_avatar = user.avatar
            file_uuid = values.pop("uuid")
            helpers.create_workspace_avatar_file(
                self.project_uuid,
                self.user_uuid,
                file_uuid,
                **values,
            )
            row = helpers.update_workspace_user_avatar(
                resource_uuid,
                self.user_uuid,
                f"{models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX}{file_uuid}",
            )
            self._delete_replaced_avatar_file(old_avatar)
        elif resource == "users" and action == "avatar_reset":
            user = helpers.get_workspace_own_user(resource_uuid, self.user_uuid)
            old_avatar = user.avatar
            avatar = (
                models.build_workspace_user_gravatar_avatar(user.email)
                if user.email
                else models.build_workspace_user_default_avatar(user.uuid)
            )
            row = helpers.update_workspace_user_avatar(
                resource_uuid, self.user_uuid, avatar
            )
            self._delete_replaced_avatar_file(old_avatar)
        else:
            raise ValueError(f"Unsupported Messenger action {resource}.{action}")
        if isinstance(row, list):
            return [_public_dict(item, resource) for item in row]
        return _public_dict(row, resource)


class PostgresEventStore:
    """Serve the unchanged Messenger event cursor contract from PostgreSQL."""

    def __init__(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> None:
        self.project_uuid = sys_uuid.UUID(str(project_uuid))
        self.user_uuid = sys_uuid.UUID(str(user_uuid))

    def _cursor(self) -> typing.Mapping[str, typing.Any]:
        session = contexts.Context().get_session()
        session.execute(
            """
            INSERT INTO "m_workspace_event_cursors" (
                "project_id", "user_uuid"
            ) VALUES (%s, %s)
            ON CONFLICT ("project_id", "user_uuid") DO NOTHING
            """,
            (self.project_uuid, self.user_uuid),
        )
        return session.execute(
            """
            SELECT
                cursor."epoch_generation",
                GREATEST(
                    cursor."current_epoch_version",
                    COALESCE(MAX(audience."current_epoch_version"), 0)
                ) AS "current_epoch_version",
                GREATEST(
                    cursor."pruned_through_epoch_version",
                    COALESCE(MAX(audience."pruned_through_epoch_version"), 0)
                ) AS "pruned_through_epoch_version"
            FROM "m_workspace_event_cursors" AS cursor
            LEFT JOIN "m_workspace_event_audience_members_v1" AS member
              ON member."user_uuid" = cursor."user_uuid"
            LEFT JOIN "m_workspace_event_audience_snapshots_v1" AS audience
              ON audience."uuid" = member."audience_snapshot_uuid"
             AND audience."project_id" = cursor."project_id"
            WHERE cursor."project_id" = %s AND cursor."user_uuid" = %s
            GROUP BY
                cursor."epoch_generation", cursor."current_epoch_version",
                cursor."pruned_through_epoch_version"
            """,
            (self.project_uuid, self.user_uuid),
        ).fetchone()

    @staticmethod
    def _after_epoch_version(
        filters: dict[str, typing.Any],
    ) -> tuple[int, tuple[typing.Any, ...]]:
        clause = filters.get("epoch_version")
        clauses = (
            clause.clauses
            if isinstance(clause, dm_filters.AND)
            else (() if clause is None else (clause,))
        )
        after = 0
        for item in clauses:
            value = int(item.value)
            if isinstance(item, dm_filters.GT):
                after = max(after, value)
            elif isinstance(item, (dm_filters.GE, dm_filters.EQ)):
                after = max(after, value - 1)
        return after, clauses

    def _validate_event_cursor(
        self,
        after: int,
        epoch_generation: str | None,
    ) -> typing.Mapping[str, typing.Any]:
        cursor = self._cursor()
        generation = str(cursor["epoch_generation"])
        current = cursor["current_epoch_version"]
        minimum = cursor["pruned_through_epoch_version"] + 1
        reason = None
        if after > 0 and epoch_generation is None:
            reason = "epoch_generation_required"
        elif epoch_generation is not None and epoch_generation != generation:
            reason = "epoch_generation_changed"
        elif after > current:
            reason = "future_epoch"
        elif after < minimum - 1:
            reason = "epoch_pruned"
        if reason is not None:
            raise messenger_exceptions.EventsCursorExpiredError(
                reason=reason,
                epoch_generation=generation,
                current_epoch_version=current,
                minimum_epoch_version=minimum,
            )
        return cursor

    def events_after(
        self,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
        epoch_generation: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]:
        after, clauses = self._after_epoch_version(filters)
        self._validate_event_cursor(after, epoch_generation)
        if (
            set(filters) == {"epoch_version"}
            and (order_by is None or order_by == {"epoch_version": "asc"})
            and limit is not None
        ):
            session = contexts.Context().get_session()
            events = session.execute(
                BOUNDED_VISIBLE_EVENTS_SQL,
                (
                    self.project_uuid,
                    self.user_uuid,
                    after,
                    limit,
                    self.user_uuid,
                    self.project_uuid,
                    after,
                    limit,
                    limit,
                ),
            ).fetchall()
            result = [
                messenger_events.event_row_to_messenger_event(event) for event in events
            ]
        else:
            scoped_filters = {
                name: value
                for name, value in filters.items()
                if name != "epoch_version"
            }
            scoped_filters.update(
                {
                    "project_id": dm_filters.EQ(self.project_uuid),
                    "user_uuid": dm_filters.EQ(self.user_uuid),
                    "epoch_version": dm_filters.GT(after),
                }
            )
            events = models.WorkspaceVisibleEvent.objects.get_all(
                filters=scoped_filters,
                order_by=order_by or {"epoch_version": "asc"},
                limit=limit,
            )
            result = [messenger_events.pack_workspace_event(event) for event in events]
        for item in clauses:
            value = int(item.value)
            if isinstance(item, dm_filters.GT):
                result = [event for event in result if event["epoch_version"] > value]
            elif isinstance(item, dm_filters.GE):
                result = [event for event in result if event["epoch_version"] >= value]
            elif isinstance(item, dm_filters.LT):
                result = [event for event in result if event["epoch_version"] < value]
            elif isinstance(item, dm_filters.LE):
                result = [event for event in result if event["epoch_version"] <= value]
            else:
                result = [event for event in result if event["epoch_version"] == value]
        return result

    def current_epoch(self) -> int:
        return self.event_cursor()["current_epoch_version"]

    def event_cursor(self) -> EventCursor:
        cursor = self._cursor()
        return {
            "epoch_generation": str(cursor["epoch_generation"]),
            "current_epoch_version": cursor["current_epoch_version"],
            "minimum_epoch_version": cursor["pruned_through_epoch_version"] + 1,
        }


class SQLCanonicalMessengerStoreFactory:
    """Open PostgreSQL stores without owning or nesting a DB transaction."""

    @staticmethod
    def _sync_request_iam_identity(
        store: _IAMIdentityStore,
        user_uuid: str | sys_uuid.UUID,
    ) -> None:
        try:
            request_context = typing.cast(typing.Any, contexts.get_context())
        except contexts.ContextIsNotExistsInStorage:
            return
        if getattr(type(request_context), "iam_context", None) is None:
            return
        iam_user = request_context.iam_context.get_introspection_info().user_info
        store.sync_iam_identity(
            {
                "user_uuid": sys_uuid.UUID(str(user_uuid)),
                "username": iam_user.name,
                "first_name": iam_user.first_name,
                "last_name": iam_user.last_name,
                "email": iam_user.email,
            }
        )

    @contextlib.contextmanager
    def draft_store(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> collections.abc.Iterator[api_store.MessengerStore]:
        store = typing.cast(
            api_store.MessengerStore,
            SQLCanonicalMessengerStore(project_uuid, user_uuid),
        )
        self._sync_request_iam_identity(store, user_uuid)
        yield store

    @contextlib.contextmanager
    def event_store(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> collections.abc.Iterator[PostgresEventStore]:
        self._sync_request_iam_identity(
            typing.cast(
                api_store.MessengerStore,
                SQLCanonicalMessengerStore(project_uuid, user_uuid),
            ),
            user_uuid,
        )
        yield PostgresEventStore(project_uuid, user_uuid)

    @staticmethod
    def move_stream_projection(**kwargs: object) -> None:
        """SQL rows are moved atomically by the external-chat transition."""
        del kwargs

    @contextlib.contextmanager
    def __call__(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> collections.abc.Iterator[api_store.MessengerStore]:
        store = typing.cast(
            api_store.MessengerStore,
            SQLCanonicalMessengerStore(project_uuid, user_uuid),
        )
        self._sync_request_iam_identity(store, user_uuid)
        yield store
