# Frozen from 724bb466b6d7fab96470c0b4c0131c302e90392e for historical migration tests.
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

from workspace.tests.integration.legacy_messenger import events as messenger_events
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.tests.integration.legacy_messenger import resource_projection
from workspace.messenger_api.api import store as api_store
from workspace.tests.integration.legacy_messenger import helpers
from workspace.tests.integration.legacy_messenger import models
from migrations import legacy_messenger_read_state as read_state


RESOURCE_MODELS: dict[str, typing.Any] = {
    **resource_projection.RESOURCE_MODELS,
    "files": models.WorkspaceVisibleFile,
    "message_reactions": models.WorkspaceVisibleMessageReaction,
}

EVENT_RETENTION = datetime.timedelta(hours=72)
EVENT_PRUNE_BATCH_SIZE = 25000
SLOW_LIST_PROJECTION_SECONDS = 1.0
LOG = logging.getLogger(__name__)


class _IAMIdentityStore(typing.Protocol):
    def sync_iam_identity(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]: ...


BOUNDED_VISIBLE_EVENTS_SQL = """
    WITH direct_events AS (
        SELECT
            event."epoch_version", event."uuid", event."project_id",
            event."user_uuid", event."payload", event."created_at",
            event."updated_at", event."schema_version",
            event."object_type", event."action"
        FROM "m_workspace_events" AS event
        LEFT JOIN LATERAL (
            SELECT COALESCE(
                NULLIF(event."payload"->>'stream_uuid', '')::uuid,
                CASE
                    WHEN event."object_type" = 'stream'
                    THEN NULLIF(event."payload"->>'uuid', '')::uuid
                END,
                (
                    SELECT message."stream_uuid"
                    FROM "m_workspace_messages" AS message
                    WHERE message."project_id" = event."project_id"
                      AND message."uuid" = NULLIF(
                          event."payload"->>'message_uuid', ''
                      )::uuid
                ),
                (
                    SELECT placement."stream_uuid"
                    FROM "messenger_message_placements" AS placement
                    WHERE placement."project_id" = event."project_id"
                      AND placement."uuid" = NULLIF(
                          event."payload"->>'message_uuid', ''
                      )::uuid
                )
            ) AS stream_uuid,
            COALESCE(
                (
                    SELECT message."stream_uuid"
                    FROM "m_workspace_messages" AS message
                    WHERE message."project_id" = event."project_id"
                      AND message."uuid" = NULLIF(
                          event."payload"->>'old_message_uuid', ''
                      )::uuid
                ),
                (
                    SELECT placement."stream_uuid"
                    FROM "messenger_message_placements" AS placement
                    WHERE placement."project_id" = event."project_id"
                      AND placement."uuid" = NULLIF(
                          event."payload"->>'old_message_uuid', ''
                      )::uuid
                )
            ) AS old_stream_uuid
        ) AS event_stream ON TRUE
        WHERE event."project_id" = %s
          AND event."user_uuid" = %s
          AND event."epoch_version" > %s


          AND (
              NOT EXISTS (
                  SELECT 1
                  FROM "messenger_event_membership_guards" AS guard
                  WHERE guard."event_uuid" = event."uuid"
                    AND guard."user_uuid" = event."user_uuid"
              )
              OR EXISTS (
                  SELECT 1
                  FROM "messenger_event_membership_guards" AS guard
                  LEFT JOIN "messenger_stream_bindings" AS binding
                    ON binding."project_id" = guard."project_id"
                   AND binding."stream_uuid" = guard."stream_uuid"
                   AND binding."user_uuid" = guard."user_uuid"
                   AND binding."active"
                   AND binding."membership_generation" =
                       guard."membership_generation"
                  WHERE guard."event_uuid" = event."uuid"
                    AND guard."user_uuid" = event."user_uuid"
                    AND (guard."control_effect" OR binding."uuid" IS NOT NULL)
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
        LEFT JOIN LATERAL (
            SELECT COALESCE(
                NULLIF(event."payload"->>'stream_uuid', '')::uuid,
                CASE
                    WHEN event."object_type" = 'stream'
                    THEN NULLIF(event."payload"->>'uuid', '')::uuid
                END,
                (
                    SELECT message."stream_uuid"
                    FROM "m_workspace_messages" AS message
                    WHERE message."project_id" = event."project_id"
                      AND message."uuid" = NULLIF(
                          event."payload"->>'message_uuid', ''
                      )::uuid
                ),
                (
                    SELECT placement."stream_uuid"
                    FROM "messenger_message_placements" AS placement
                    WHERE placement."project_id" = event."project_id"
                      AND placement."uuid" = NULLIF(
                          event."payload"->>'message_uuid', ''
                      )::uuid
                )
            ) AS stream_uuid,
            COALESCE(
                (
                    SELECT message."stream_uuid"
                    FROM "m_workspace_messages" AS message
                    WHERE message."project_id" = event."project_id"
                      AND message."uuid" = NULLIF(
                          event."payload"->>'old_message_uuid', ''
                      )::uuid
                ),
                (
                    SELECT placement."stream_uuid"
                    FROM "messenger_message_placements" AS placement
                    WHERE placement."project_id" = event."project_id"
                      AND placement."uuid" = NULLIF(
                          event."payload"->>'old_message_uuid', ''
                      )::uuid
                )
            ) AS old_stream_uuid
        ) AS event_stream ON TRUE
        WHERE TRUE
          AND (
              NOT EXISTS (
                  SELECT 1
                  FROM "messenger_event_membership_guards" AS guard
                  WHERE guard."event_uuid" = event."uuid"
                    AND guard."user_uuid" = event."user_uuid"
              )
              OR EXISTS (
                  SELECT 1
                  FROM "messenger_event_membership_guards" AS guard
                  LEFT JOIN "messenger_stream_bindings" AS binding
                    ON binding."project_id" = guard."project_id"
                   AND binding."stream_uuid" = guard."stream_uuid"
                   AND binding."user_uuid" = guard."user_uuid"
                   AND binding."active"
                   AND binding."membership_generation" =
                       guard."membership_generation"
                  WHERE guard."event_uuid" = event."uuid"
                    AND guard."user_uuid" = event."user_uuid"
                    AND (guard."control_effect" OR binding."uuid" IS NOT NULL)
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
    # Canonical rows contain the persisted public fields. Passing
    # the row explicitly avoids the transitional serializer's per-row lookup.
    result = resource_projection.as_dict(row, resource)
    result.pop("viewer_user_uuid", None)
    if resource == "files":
        result.pop("acl_mode", None)
    return result


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
            SELECT "user_uuid", "direct_user_uuid", "private"
            FROM "m_workspace_streams"
            WHERE "project_id" = %s AND "uuid" = %s
            """,
            (self.project_uuid, stream_uuid),
        ).fetchone()
        if row is None:
            raise ra_exceptions.ValidationErrorException()
        models.WorkspaceStream.validate_private_identity(
            row["private"], row["direct_user_uuid"]
        )
        if not row["private"]:
            return None
        return {row["user_uuid"], row["direct_user_uuid"]}

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

    def create_resource(
        self,
        resource: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        values = self._projection_values(values)
        values["uuid"] = values.get("uuid") or sys_uuid.uuid4()
        if resource == "files":
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
        if resource == "files":
            row = helpers.update_workspace_file(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values,
            )
        else:
            raise ValueError(f"Unsupported Messenger update resource {resource}")
        return _public_dict(row, resource)

    def delete_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        if resource == "files":
            helpers.delete_workspace_file(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        else:
            raise ValueError(f"Unsupported Messenger delete resource {resource}")
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
        if resource == "users" and action == "presence":
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
