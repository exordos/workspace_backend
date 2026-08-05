# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""PostgreSQL-canonical Messenger store implementation."""

import contextlib
import collections.abc
import datetime
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exceptions

from workspace.external_bridge_control import provider_data
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models


RESOURCE_MODELS: dict[str, typing.Any] = {
    **resource_projection.RESOURCE_MODELS,
    "files": models.WorkspaceVisibleFile,
    "message_reactions": models.WorkspaceVisibleMessageReaction,
}

_PROVIDER_TARGET_UNSET = object()
_PROVIDER_TARGET_EXISTS = object()
EVENT_RETENTION = datetime.timedelta(hours=72)
EVENT_PRUNE_BATCH_SIZE = 25000
REACTION_ACTIVITY_PAGE_SQL = """
    SELECT message.*
    FROM "m_workspace_messages" AS activity
    JOIN "m_workspace_user_messages_view" AS message
      ON message."project_id" = activity."project_id"
     AND message."uuid" = activity."uuid"
    WHERE activity."project_id" = %s
      AND activity."user_uuid" = %s
      AND activity."reaction_count" > 0
      AND message."user_uuid" = %s
      AND (
          %s::timestamptz IS NULL
          OR activity."latest_reaction_at" < %s::timestamptz
          OR (
              activity."latest_reaction_at" = %s::timestamptz
              AND activity."uuid" < %s::uuid
          )
      )
    ORDER BY activity."latest_reaction_at" DESC, activity."uuid" DESC
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
                  FROM "m_workspace_streams" AS external_stream
                  JOIN "m_confirmed_external_account_access" AS stream_access
                    ON stream_access."project_id" =
                        external_stream."project_id"
                   AND stream_access."user_uuid" = event."user_uuid"
                   AND stream_access."account_type" =
                        external_stream."source_name"
                   AND stream_access."source_scope" = COALESCE(
                        external_stream."source"->>'source_scope',
                        external_stream."source"->>'server_url'
                   )
                  WHERE external_stream."project_id" = event."project_id"
                    AND external_stream."uuid" =
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
                  FROM "m_workspace_streams" AS external_stream
                  JOIN "m_confirmed_external_account_access" AS stream_access
                    ON stream_access."project_id" =
                        external_stream."project_id"
                   AND stream_access."user_uuid" = event."user_uuid"
                   AND stream_access."account_type" =
                        external_stream."source_name"
                   AND stream_access."source_scope" = COALESCE(
                        external_stream."source"->>'source_scope',
                        external_stream."source"->>'server_url'
                   )
                  WHERE external_stream."project_id" = event."project_id"
                    AND external_stream."uuid" =
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
        rows = model.objects.get_all(
            filters=self._scope_filters(resource, filters),
            order_by=order_by,
            limit=limit,
        )
        return [_public_dict(row, resource) for row in rows]

    def filter_message_page(
        self,
        filters: dict[str, typing.Any],
        marker_uuid: sys_uuid.UUID | None,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        scoped_filters = self._scope_filters("messages", filters)
        if marker_uuid is not None:
            marker_filters = scoped_filters.copy()
            marker_filters["uuid"] = dm_filters.EQ(marker_uuid)
            marker = models.WorkspaceUserMessage.objects.get_one(
                filters=marker_filters,
            )
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

    def filter_reaction_activity_page(
        self,
        marker_uuid: sys_uuid.UUID | None,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        session = contexts.Context().get_session()
        marker_at = None
        marker_value = None
        if marker_uuid is not None:
            models.WorkspaceUserMessage.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(marker_uuid),
                    "project_id": dm_filters.EQ(self.project_uuid),
                    "user_uuid": dm_filters.EQ(self.user_uuid),
                    "author_uuid": dm_filters.EQ(self.user_uuid),
                },
                session=session,
            )
            marker = models.WorkspaceMessage.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(marker_uuid),
                    "project_id": dm_filters.EQ(self.project_uuid),
                    "user_uuid": dm_filters.EQ(self.user_uuid),
                },
                session=session,
            )
            marker_at = marker.latest_reaction_at
            marker_value = marker.uuid
        result = session.execute(
            REACTION_ACTIVITY_PAGE_SQL,
            (
                self.project_uuid,
                self.user_uuid,
                self.user_uuid,
                marker_at,
                marker_at,
                marker_at,
                marker_value,
                limit,
            ),
        )
        return [_public_dict(row, "messages") for row in result.fetchall()]

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

    def _provider_target(
        self,
        stream_uuid: object,
        operation_kind: str | None = None,
        *,
        account_locked: bool = False,
    ) -> typing.Any:
        session = contexts.Context().get_session()
        stream = models.WorkspaceStream.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self.project_uuid),
                "uuid": dm_filters.EQ(stream_uuid),
            },
            session=session,
        )
        if stream.external_account_uuid is None:
            return None
        if operation_kind is None:
            return _PROVIDER_TARGET_EXISTS
        if not account_locked:
            self._lock_provider_account(stream.external_account_uuid)
        required_capability = provider_data._required_capability(operation_kind)
        if required_capability is None:
            raise ra_exceptions.ValidationErrorException()
        try:
            account, _chat, bridge = provider_data.resolve_provider_target(
                session,
                project_id=self.project_uuid,
                owner_user_uuid=stream.user_uuid,
                external_account_uuid=stream.external_account_uuid,
                stream_uuid=stream_uuid,
                capability_name=required_capability,
            )
        except provider_data.ProviderUnavailableError as exc:
            if operation_kind in {"membership.add", "membership.remove"}:
                try:
                    account, _chat, bridge = (
                        provider_data.resolve_provider_queue_target(
                            session,
                            project_id=self.project_uuid,
                            owner_user_uuid=stream.user_uuid,
                            external_account_uuid=stream.external_account_uuid,
                            stream_uuid=stream_uuid,
                            allow_policy_blocked=operation_kind
                            == "membership.remove",
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
                return account, bridge
            raise ra_exceptions.ValidationErrorException() from exc
        return account, bridge

    def _lock_provider_account(self, account_uuid: object) -> None:
        """Establish account-before-message/outbox lock ordering."""
        contexts.Context().get_session().execute(
            """
            SELECT uuid
            FROM m_external_accounts_v2
            WHERE uuid = %s
            FOR KEY SHARE
            """,
            (account_uuid,),
        ).fetchone()

    def _lock_provider_account_for_stream(self, stream_uuid: object) -> bool:
        """Lock a projected account without validating mutable capabilities."""
        session = contexts.Context().get_session()
        stream = models.WorkspaceStream.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self.project_uuid),
                "uuid": dm_filters.EQ(stream_uuid),
            },
            session=session,
        )
        if stream.external_account_uuid is None:
            return False
        self._lock_provider_account(stream.external_account_uuid)
        return True

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
        operation, _record_uuid = provider_data.enqueue_provider_operation(
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
        """Queue one exact, retry-safe provider read-state projection."""
        if not message_uuids:
            return None
        queue_values: dict[str, typing.Any] = {
            "operation_kind": "read_state.set",
            "target_type": target_type,
            "target_uuid": target_uuid,
            "stream_uuid": stream_uuid,
            "payload": {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None if topic_uuid is None else str(topic_uuid),
                "reader_uuid": str(self.user_uuid),
                "message_uuids": [str(message_uuid) for message_uuid in message_uuids],
                "read": True,
            },
        }
        if provider_target is not _PROVIDER_TARGET_UNSET:
            queue_values["provider_target"] = provider_target
        return self._queue_provider_operation(**queue_values)

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
            provider_target = self._provider_target(
                message.stream_uuid,
                "reaction.create",
            )
            row = helpers.create_workspace_message_reaction(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                compact_events=True,
                **values,
            )
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
            provider_target = self._provider_target(
                message.stream_uuid,
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
        if provider_stream_uuid is not None:
            operation_kind, target_type = {
                "streams": ("stream.delete", "stream"),
                "stream_topics": ("topic.delete", "topic"),
                "message_reactions": ("reaction.delete", "reaction"),
            }[resource]
            self._queue_provider_operation(
                operation_kind=operation_kind,
                target_type=target_type,
                target_uuid=resource_uuid,
                stream_uuid=provider_stream_uuid,
                payload=provider_payload,
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
        provider_target = self._provider_target(
            values["stream_uuid"],
            "message.create",
        )
        row = helpers.create_workspace_user_message(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            enforce_visibility=True,
            compact_events=True,
            **values,
        )
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
        provider_target = self._provider_target(
            message.stream_uuid,
            "message.update",
        )
        row = helpers.update_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
            self._projection_values(values),
            compact_events=True,
        )
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
        provider_target = self._provider_target(
            message.stream_uuid,
            "message.delete",
        )
        helpers.delete_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
            compact_events=True,
        )
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
            row = helpers.update_workspace_user_stream_notifications(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values["notification_mode"],
            )
        elif resource == "streams" and action == "read":
            session = contexts.Context().get_session()
            stream = helpers.get_workspace_user_stream(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
            )
            provider_account_locked = self._lock_provider_account_for_stream(
                resource_uuid
            )
            row, message_uuids = helpers.read_workspace_user_stream_messages(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_stream=stream,
                return_message_uuids=True,
            )
            provider_target = (
                self._provider_target(
                    resource_uuid,
                    "read_state.set",
                    account_locked=provider_account_locked,
                )
                if message_uuids
                else _PROVIDER_TARGET_UNSET
            )
            self._queue_provider_read(
                stream_uuid=resource_uuid,
                topic_uuid=None,
                message_uuids=message_uuids,
                target_type="stream",
                target_uuid=resource_uuid,
                provider_target=provider_target,
            )
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
            row = helpers.update_workspace_user_stream_topic_notifications(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values["notification_mode"],
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
            provider_account_locked = self._lock_provider_account_for_stream(
                topic.stream_uuid
            )
            row, message_uuids = helpers.read_workspace_user_stream_topic_messages(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_topic=topic,
                return_message_uuids=True,
            )
            provider_target = (
                self._provider_target(
                    topic.stream_uuid,
                    "read_state.set",
                    account_locked=provider_account_locked,
                )
                if message_uuids
                else _PROVIDER_TARGET_UNSET
            )
            self._queue_provider_read(
                stream_uuid=topic.stream_uuid,
                topic_uuid=resource_uuid,
                message_uuids=message_uuids,
                target_type="topic",
                target_uuid=resource_uuid,
                provider_target=provider_target,
            )
        elif resource == "messages" and action == "read":
            session = contexts.Context().get_session()
            message = helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
            )
            provider_account_locked = self._lock_provider_account_for_stream(
                message.stream_uuid
            )
            row, message_uuids = helpers.read_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_message=message,
                return_message_uuids=True,
            )
            provider_target = (
                self._provider_target(
                    message.stream_uuid,
                    "read_state.set",
                    account_locked=provider_account_locked,
                )
                if message_uuids
                else _PROVIDER_TARGET_UNSET
            )
            self._queue_provider_read(
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                message_uuids=message_uuids,
                target_type="message",
                target_uuid=resource_uuid,
                provider_target=provider_target,
            )
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
            provider_account_locked = self._lock_provider_account_for_stream(
                message.stream_uuid
            )
            row, message_uuids = helpers.read_workspace_user_topic_messages_to_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_message=message,
                return_message_uuids=True,
            )
            provider_target = (
                self._provider_target(
                    message.stream_uuid,
                    "read_state.set",
                    account_locked=provider_account_locked,
                )
                if message_uuids
                else _PROVIDER_TARGET_UNSET
            )
            self._queue_provider_read(
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                message_uuids=message_uuids,
                target_type="message",
                target_uuid=resource_uuid,
                provider_target=provider_target,
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
                messenger_events.event_row_to_messenger_event(event)
                for event in events
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
            result = [
                messenger_events.pack_workspace_event(event) for event in events
            ]
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

    @contextlib.contextmanager
    def draft_store(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> collections.abc.Iterator[api_store.MessengerStore]:
        yield typing.cast(
            api_store.MessengerStore,
            SQLCanonicalMessengerStore(project_uuid, user_uuid),
        )

    @contextlib.contextmanager
    def event_store(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> collections.abc.Iterator[PostgresEventStore]:
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
        yield typing.cast(
            api_store.MessengerStore,
            SQLCanonicalMessengerStore(project_uuid, user_uuid),
        )
