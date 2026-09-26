# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Compact PostgreSQL store for the Workspace v3 Messenger model.

The adapter preserves the established v1 HTTP resources while keeping the new
schema canonical: one resource row, explicit per-user membership/flag rows,
and durable audience-scoped events. Provider details are intentionally absent;
``source_name`` is authoritative and ``source`` is a compatibility projection.
"""

import collections.abc
import contextlib
import datetime
import hashlib
import json
import random
import typing
import uuid as sys_uuid

import psycopg.errors
from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api import event_origin
from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models
from workspace.workspace_v3 import constants

_SOURCE_RESOURCES = frozenset(
    {"streams", "stream_topics", "messages", "message_reactions"}
)
_RECIPIENT_EVENT_FIELDS = {
    "folder": frozenset({"user_uuid"}),
    "folder_item": frozenset({"user_uuid"}),
    "message": frozenset(
        {"user_uuid", "read", "pinned", "starred", "mentioned", "is_own"}
    ),
    "stream": frozenset(
        {
            "user_uuid",
            "role",
            "notification_mode",
            "unread_count",
            "active_unread_count",
            "passive_unread_count",
            "direct_user_uuid",
            "last_message_uuid",
        }
    ),
    "topic": frozenset(
        {
            "user_uuid",
            "notification_mode",
            "unread_count",
            "active_unread_count",
            "passive_unread_count",
            "last_message_uuid",
            "summary_has_new_messages",
        }
    ),
}
_USER_SCOPED = frozenset(
    {"folders", "folder_items", "streams", "stream_topics", "messages"}
)
_TOPIC_NOTIFICATION_MODES = frozenset({"mute", "default", "follow"})
_MUTED_STREAM_TOPIC_NOTIFICATION_MODES = _TOPIC_NOTIFICATION_MODES | {"unmute"}
_STREAM_BINDING_ROLES = frozenset(
    {"guest", "member", "moderator", "administrator", "owner"}
)
_STREAM_NOTIFICATION_MODES = frozenset({"all_messages", "mentions_only", "muted"})
_RESOURCE_OBJECT_TYPES = {
    "folders": "folder",
    "folder_items": "folder_item",
    "files": "file",
    "streams": "stream",
    "stream_bindings": "stream_binding",
    "stream_topics": "topic",
    "topic_bindings": "topic_binding",
    "messages": "message",
    "message_flags": "message_flag",
    "message_reactions": "message_reaction",
    "users": "user",
}
_ORDERABLE_FIELDS = {
    "folders": frozenset({"uuid", "created_at", "updated_at", "title"}),
    "folder_items": frozenset(
        {"uuid", "created_at", "updated_at", "order_index", "pinned_at"}
    ),
    "files": frozenset({"uuid", "created_at", "updated_at", "name"}),
    "streams": frozenset({"uuid", "created_at", "updated_at", "name"}),
    "stream_bindings": frozenset({"uuid", "created_at", "updated_at"}),
    "stream_topics": frozenset({"uuid", "created_at", "updated_at", "name"}),
    "messages": frozenset({"uuid", "created_at", "updated_at"}),
    "message_reactions": frozenset({"uuid", "created_at", "updated_at"}),
    "users": frozenset({"uuid", "created_at", "updated_at", "username"}),
}
_EVENT_RESOURCE_QUERIES = {
    "messages": """
        SELECT message.uuid, message.project_id, flag.user_uuid,
               message.stream_uuid, message.topic_uuid,
               message.author_uuid, message.payload,
               message.source_name, flag.read, flag.pinned,
               flag.starred, flag.mentioned,
               message.author_uuid = flag.user_uuid AS is_own,
               message.reactions, message.reaction_users,
               message.created_at, message.updated_at
        FROM workspace_v3.messages AS message
        JOIN workspace_v3.message_flags AS flag
          ON flag.project_id = message.project_id
         AND flag.message_uuid = message.uuid
        WHERE message.project_id = %s AND message.uuid = %s
          AND flag.user_uuid = ANY(%s::uuid[])
    """,
    "streams": """
        SELECT stream.uuid, stream.project_id,
               binding.user_uuid, stream.name,
               COALESCE(stream.description, '') AS description,
               stream.owner_uuid AS owner, binding.role,
               binding.notification_mode, binding.unread_count,
               binding.active_unread_count,
               binding.passive_unread_count, stream.source_name,
               stream.invite_only, stream.announce, stream.private,
               stream.is_archived,
               CASE WHEN stream.direct_user_uuid IS NOT NULL THEN COALESCE(
                   (
                       SELECT peer.user_uuid
                       FROM workspace_v3.stream_bindings AS peer
                       WHERE peer.project_id = stream.project_id
                         AND peer.stream_uuid = stream.uuid
                         AND peer.user_uuid <> binding.user_uuid
                       ORDER BY peer.user_uuid
                       LIMIT 1
                   ), binding.user_uuid
               ) END AS direct_user_uuid,
               stream.private_index, stream.color,
               binding.last_message_uuid, stream.default_topic_uuid,
               stream.created_at, stream.updated_at
        FROM workspace_v3.streams AS stream
        JOIN workspace_v3.stream_bindings AS binding
          ON binding.project_id = stream.project_id
         AND binding.stream_uuid = stream.uuid
        WHERE stream.project_id = %s AND stream.uuid = %s
          AND binding.user_uuid = ANY(%s::uuid[])
    """,
    "stream_topics": """
        SELECT topic.uuid, topic.project_id, topic.stream_uuid,
               binding.user_uuid, topic.name, topic.color,
               binding.last_message_uuid, binding.unread_count,
               binding.active_unread_count,
               binding.passive_unread_count,
               COALESCE(topic.uuid = stream.default_topic_uuid, FALSE)
                   AS is_default,
               topic.is_done, binding.notification_mode,
               topic.source_name, topic.summary,
               topic.summary_last_message_uuid,
               binding.summary_has_new_messages,
               topic.summary_enabled, topic.summary_system_prompt,
               topic.summary_reasoning_effort,
               topic.created_at, topic.updated_at
        FROM workspace_v3.topics AS topic
        JOIN workspace_v3.topic_bindings AS binding
          ON binding.project_id = topic.project_id
         AND binding.topic_uuid = topic.uuid
        JOIN workspace_v3.streams AS stream
          ON stream.project_id = topic.project_id
         AND stream.uuid = topic.stream_uuid
        WHERE topic.project_id = %s AND topic.uuid = %s
          AND binding.user_uuid = ANY(%s::uuid[])
    """,
}


def _session() -> typing.Any:
    return contexts.Context().get_session()


def resolve_provider_consumer(
    project_uuid: sys_uuid.UUID,
    iam_user_uuid: sys_uuid.UUID,
) -> dict[str, typing.Any] | None:
    row = (
        _session()
        .execute(
            """
        SELECT uuid, name
        FROM workspace_v3.provider_consumers
        WHERE project_id = %s AND iam_user_uuid = %s AND enabled
        """,
            (project_uuid, iam_user_uuid),
        )
        .fetchone()
    )
    return None if row is None else _row(row)


def _utc(value: datetime.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _simple(value: typing.Any) -> typing.Any:
    if isinstance(value, datetime.datetime):
        return _utc(value)
    if isinstance(value, sys_uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _simple(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_simple(item) for item in value]
    if hasattr(value, "properties"):
        return _simple(resource_projection.simple(value))
    return value


def _row(value: typing.Any) -> dict[str, typing.Any]:
    return dict(typing.cast(typing.Mapping[str, typing.Any], value))


def source_projection(source_name: str) -> dict[str, typing.Any]:
    source: dict[str, typing.Any] = {"kind": source_name}
    if source_name == "zulip":
        # Legacy clients require this field to distinguish a valid Zulip source.
        # The clean schema deliberately does not retain provider-local identifiers,
        # so zero is the established compatibility sentinel and is never used for
        # routing or identity.
        source["stream_id"] = 0
    return source


def restore_topic_summary_after_message_deletion(
    session: typing.Any,
    project_uuid: sys_uuid.UUID,
    message_uuid: sys_uuid.UUID,
) -> sys_uuid.UUID | None:
    message = session.execute(
        """
        SELECT topic_uuid, created_at
        FROM workspace_v3.messages
        WHERE project_id = %s AND uuid = %s
        """,
        (project_uuid, message_uuid),
    ).fetchone()
    if message is None:
        return None
    topic_uuid = sys_uuid.UUID(str(message["topic_uuid"]))
    deleted_created_at = message["created_at"]
    if deleted_created_at.tzinfo is not None:
        deleted_created_at = deleted_created_at.astimezone(
            datetime.timezone.utc
        ).replace(tzinfo=None)
    session.execute(
        """
        UPDATE m_workspace_llm_endpoints AS endpoint
        SET claim_token = NULL, claim_expires_at = NULL, updated_at = NOW()
        FROM m_workspace_topic_summary_jobs AS job
        WHERE job.topic_uuid = %s
          AND endpoint.uuid = job.endpoint_uuid
          AND endpoint.claim_token = job.endpoint_claim_token
        """,
        (topic_uuid,),
    )
    session.execute(
        "DELETE FROM m_workspace_topic_summary_jobs WHERE topic_uuid = %s",
        (topic_uuid,),
    )
    session.execute(
        """
        UPDATE m_workspace_topic_summary_journal
        SET invalidated_at = NOW()
        WHERE topic_uuid = %s AND invalidated_at IS NULL
          AND (boundary_message_created_at, boundary_message_uuid) >= (%s, %s)
        """,
        (topic_uuid, deleted_created_at, message_uuid),
    )
    topic = session.execute(
        """
        SELECT topic.summary, topic.summary_last_message_uuid,
               (
                   topic.summary_last_message_uuid IS NULL
                   OR boundary.created_at IS NULL
                   OR (boundary.created_at, boundary.uuid) >= (%s, %s)
               ) AS summary_covered_deleted_message
        FROM workspace_v3.topics AS topic
        LEFT JOIN workspace_v3.messages AS boundary
          ON boundary.project_id = topic.project_id
         AND boundary.uuid = topic.summary_last_message_uuid
        WHERE topic.project_id = %s AND topic.uuid = %s
        FOR UPDATE OF topic
        """,
        (deleted_created_at, message_uuid, project_uuid, topic_uuid),
    ).fetchone()
    if (
        topic is None
        or topic["summary"] is None
        or not topic["summary_covered_deleted_message"]
    ):
        return None
    restored = session.execute(
        """
        SELECT journal.summary, journal.boundary_message_uuid
        FROM m_workspace_topic_summary_journal AS journal
        JOIN workspace_v3.messages AS boundary
          ON boundary.project_id = %s
         AND boundary.topic_uuid = %s
         AND boundary.uuid = journal.boundary_message_uuid
        WHERE journal.topic_uuid = %s AND journal.invalidated_at IS NULL
          AND (
                journal.boundary_message_created_at,
                journal.boundary_message_uuid
              ) < (%s, %s)
        ORDER BY journal.boundary_message_created_at DESC,
                 journal.boundary_message_uuid DESC,
                 journal.generated_at DESC, journal.uuid DESC
        LIMIT 1
        """,
        (
            project_uuid,
            topic_uuid,
            topic_uuid,
            deleted_created_at,
            message_uuid,
        ),
    ).fetchone()
    restored_summary = None if restored is None else restored["summary"]
    restored_boundary = None if restored is None else restored["boundary_message_uuid"]
    session.execute(
        """
        UPDATE workspace_v3.topics
        SET summary = %s, summary_last_message_uuid = %s,
            updated_at = clock_timestamp()
        WHERE project_id = %s AND uuid = %s
        """,
        (restored_summary, restored_boundary, project_uuid, topic_uuid),
    )
    session.execute(
        """
        UPDATE workspace_v3.topic_bindings AS binding
        SET summary_has_new_messages = CASE
                WHEN %s::uuid IS NULL THEN NULL
                ELSE EXISTS (
                    SELECT 1
                    FROM workspace_v3.messages AS candidate
                    JOIN workspace_v3.message_flags AS flag
                      ON flag.project_id = candidate.project_id
                     AND flag.message_uuid = candidate.uuid
                     AND flag.user_uuid = binding.user_uuid
                    JOIN workspace_v3.messages AS boundary
                      ON boundary.project_id = candidate.project_id
                     AND boundary.topic_uuid = candidate.topic_uuid
                     AND boundary.uuid = %s
                    WHERE candidate.project_id = binding.project_id
                      AND candidate.topic_uuid = binding.topic_uuid
                      AND (candidate.created_at, candidate.uuid) >
                          (boundary.created_at, boundary.uuid)
                )
            END,
            updated_at = clock_timestamp()
        WHERE binding.project_id = %s AND binding.topic_uuid = %s
        """,
        (restored_boundary, restored_boundary, project_uuid, topic_uuid),
    )
    return topic_uuid


def _public(value: typing.Any, resource: str) -> dict[str, typing.Any]:
    result = _simple(_row(value))
    result.pop("private_index", None)
    if resource == "users":
        result.pop("disabled", None)
        result.pop("is_bot", None)
    if resource in _SOURCE_RESOURCES:
        result["source"] = source_projection(result["source_name"])
    if resource == "folders":
        result["system_type"] = (
            "all" if result.pop("kind") == "all_chats" else "created"
        )
        result["unread_count"] = result.pop("active_unread_count")
        result.pop("passive_unread_count", None)
        result.pop("total_unread_count", None)
    return result


def event_resource_payloads(
    session: typing.Any,
    project_uuid: sys_uuid.UUID,
    resource: str,
    resource_uuid: object,
    recipients: tuple[sys_uuid.UUID, ...],
) -> dict[sys_uuid.UUID, dict[str, typing.Any]]:
    if not recipients:
        return {}
    result = session.execute(
        _EVENT_RESOURCE_QUERIES[resource],
        (project_uuid, resource_uuid, list(recipients)),
    )
    columns = tuple(column.name for column in result.description)
    rows = (
        _row(value)
        if isinstance(value, collections.abc.Mapping)
        else dict(zip(columns, value, strict=True))
        for value in result
    )
    return {
        sys_uuid.UUID(str(row["user_uuid"])): _public(row, resource) for row in rows
    }


def partition_event_payloads(
    object_type: str,
    payloads: dict[sys_uuid.UUID, dict[str, typing.Any]],
) -> tuple[
    dict[str, typing.Any],
    dict[sys_uuid.UUID, dict[str, typing.Any]],
]:
    return _partition_event_payloads(object_type, payloads)


def _partition_event_payloads(
    object_type: str,
    payloads: dict[sys_uuid.UUID, dict[str, typing.Any]],
) -> tuple[
    dict[str, typing.Any],
    dict[sys_uuid.UUID, dict[str, typing.Any]],
]:
    first = next(iter(payloads.values()))
    recipient_fields = _RECIPIENT_EVENT_FIELDS.get(object_type, frozenset())
    common = {
        key: value
        for key, value in first.items()
        if key not in recipient_fields
        and all(
            key in payload and payload[key] == value for payload in payloads.values()
        )
    }
    overlays = {
        recipient: {key: value for key, value in payload.items() if key not in common}
        for recipient, payload in payloads.items()
    }
    return common, {recipient: value for recipient, value in overlays.items() if value}


def _not_found(resource: str, resource_uuid: object) -> typing.NoReturn:
    raise ra_exceptions.ResourceNotFoundError(
        resource=resource,
        path=str(resource_uuid),
    )


def _translate_private_member_limit(
    error: psycopg.errors.CheckViolation,
) -> typing.NoReturn:
    if error.diag.constraint_name in {
        "stream_bindings_private_member_limit_check",
        "streams_private_member_limit_check",
    }:
        raise messenger_exceptions.PrivateStreamMemberLimitError() from error
    raise error


def _filter_value(value: typing.Any) -> typing.Any:
    return getattr(value, "_value", value)


def _compile_filter(
    expression: typing.Any,
    fields: frozenset[str],
) -> tuple[str, list[typing.Any]]:
    if isinstance(expression, dm_filters.AND):
        compiled = [_compile_filter(item, fields) for item in expression._clauses]
        return (
            "(" + " AND ".join(item[0] for item in compiled) + ")",
            [value for item in compiled for value in item[1]],
        )
    if isinstance(expression, dm_filters.OR):
        compiled = [_compile_filter(item, fields) for item in expression._clauses]
        return (
            "(" + " OR ".join(item[0] for item in compiled) + ")",
            [value for item in compiled for value in item[1]],
        )
    if not isinstance(expression, dict):
        raise ra_exceptions.ValidationErrorException()
    clauses: list[str] = []
    parameters: list[typing.Any] = []
    for field, predicate in expression.items():
        if field not in fields:
            raise ra_exceptions.ValidationErrorException()
        value = _filter_value(predicate)
        if isinstance(predicate, dm_filters.In):
            clauses.append(f'resource."{field}" = ANY(%s)')
            parameters.append(list(value))
            continue
        operator = "="
        for filter_type, candidate in (
            (dm_filters.GT, ">"),
            (dm_filters.GE, ">="),
            (dm_filters.LT, "<"),
            (dm_filters.LE, "<="),
        ):
            if isinstance(predicate, filter_type):
                operator = candidate
                break
        if value is None and operator == "=":
            clauses.append(f'resource."{field}" IS NULL')
        else:
            clauses.append(f'resource."{field}" {operator} %s')
            parameters.append(value)
    return ("(" + " AND ".join(clauses or ["TRUE"]) + ")", parameters)


class MessengerV3Store:
    """Read and mutate v3 rows through the unchanged Messenger store boundary."""

    def __init__(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> None:
        self.project_uuid = sys_uuid.UUID(str(project_uuid))
        self.user_uuid = sys_uuid.UUID(str(user_uuid))
        self._known_audience_snapshots: set[sys_uuid.UUID] = set()

    def _resource_sql(self, resource: str) -> tuple[str, list[typing.Any]]:
        project = self.project_uuid
        user = self.user_uuid
        queries = {
            "users": """
                SELECT user_row.*,
                       COALESCE(
                           NULLIF(
                               btrim(concat_ws(
                                   ' ', user_row.first_name, user_row.last_name
                               )),
                               ''
                           ),
                           user_row.username
                       ) AS display_name
                FROM workspace_v3.users AS user_row
                WHERE user_row.source = 'iam'
                   OR EXISTS (
                       SELECT 1
                       FROM workspace_v3.stream_bindings AS binding
                       WHERE binding.project_id = %s
                         AND binding.user_uuid = user_row.uuid
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM workspace_v3.streams AS stream
                       WHERE stream.project_id = %s
                         AND (
                             stream.owner_uuid = user_row.uuid
                             OR stream.direct_user_uuid = user_row.uuid
                         )
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM workspace_v3.messages AS message
                       WHERE message.project_id = %s
                         AND message.author_uuid = user_row.uuid
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM workspace_v3.message_reactions AS reaction
                       WHERE reaction.project_id = %s
                         AND reaction.user_uuid = user_row.uuid
                   )
            """,
            "streams": """
                SELECT stream.uuid, stream.project_id,
                       binding.user_uuid, stream.name,
                       COALESCE(stream.description, '') AS description,
                       stream.owner_uuid AS owner, binding.role,
                       binding.notification_mode, binding.unread_count,
                       binding.active_unread_count,
                       binding.passive_unread_count, stream.source_name,
                       stream.invite_only, stream.announce, stream.private,
                       stream.is_archived,
                       CASE WHEN stream.direct_user_uuid IS NOT NULL THEN COALESCE(
                           (
                               SELECT peer.user_uuid
                               FROM workspace_v3.stream_bindings AS peer
                               WHERE peer.project_id = stream.project_id
                                 AND peer.stream_uuid = stream.uuid
                                 AND peer.user_uuid <> binding.user_uuid
                               ORDER BY peer.user_uuid
                               LIMIT 1
                           ), binding.user_uuid
                       ) END AS direct_user_uuid,
                       stream.private_index, stream.color,
                       binding.last_message_uuid, stream.default_topic_uuid,
                       stream.created_at, stream.updated_at
                FROM workspace_v3.streams AS stream
                JOIN workspace_v3.stream_bindings AS binding
                  ON binding.project_id = stream.project_id
                 AND binding.stream_uuid = stream.uuid
                WHERE stream.project_id = %s AND binding.user_uuid = %s
            """,
            "stream_bindings": """
                SELECT binding.*
                FROM workspace_v3.stream_bindings AS binding
                WHERE binding.project_id = %s
                  AND EXISTS (
                      SELECT 1 FROM workspace_v3.stream_bindings AS viewer
                      WHERE viewer.project_id = binding.project_id
                        AND viewer.stream_uuid = binding.stream_uuid
                        AND viewer.user_uuid = %s
                  )
            """,
            "stream_topics": """
                SELECT topic.uuid, topic.project_id, topic.stream_uuid,
                       binding.user_uuid, topic.name, topic.color,
                       binding.last_message_uuid, binding.unread_count,
                       binding.active_unread_count,
                       binding.passive_unread_count,
                       COALESCE(topic.uuid = stream.default_topic_uuid, FALSE)
                           AS is_default,
                       topic.is_done, binding.notification_mode,
                       topic.source_name, topic.summary,
                       topic.summary_last_message_uuid,
                       binding.summary_has_new_messages,
                       topic.summary_enabled, topic.summary_system_prompt,
                       topic.summary_reasoning_effort,
                       topic.created_at, topic.updated_at
                FROM workspace_v3.topics AS topic
                JOIN workspace_v3.topic_bindings AS binding
                  ON binding.project_id = topic.project_id
                 AND binding.topic_uuid = topic.uuid
                JOIN workspace_v3.streams AS stream
                  ON stream.project_id = topic.project_id
                 AND stream.uuid = topic.stream_uuid
                WHERE topic.project_id = %s AND binding.user_uuid = %s
            """,
            "messages": """
                WITH visible_flags AS NOT MATERIALIZED (
                    SELECT project_id, message_uuid, user_uuid,
                           read, pinned, starred, mentioned
                    FROM workspace_v3.message_flags
                    WHERE project_id = %s AND user_uuid = %s
                )
                SELECT message.uuid, message.project_id, flag.user_uuid,
                       message.stream_uuid, message.topic_uuid,
                       message.author_uuid, message.payload,
                       message.source_name, flag.read, flag.pinned,
                       flag.starred, flag.mentioned,
                       message.author_uuid = flag.user_uuid AS is_own,
                       message.reactions, message.reaction_users,
                       message.created_at, message.updated_at
                FROM visible_flags AS flag
                JOIN workspace_v3.messages AS message
                  ON message.project_id = flag.project_id
                 AND message.uuid = flag.message_uuid
            """,
            "message_reactions": """
                SELECT reaction.*
                FROM workspace_v3.message_reactions AS reaction
                JOIN workspace_v3.message_flags AS viewer
                  ON viewer.project_id = reaction.project_id
                 AND viewer.message_uuid = reaction.message_uuid
                 AND viewer.user_uuid = %s
                WHERE reaction.project_id = %s
            """,
            "folders": """
                SELECT folder.uuid, folder.project_id, folder.user_uuid,
                       folder.kind, folder.title,
                       folder.background_color_value,
                       folder.unread_count AS total_unread_count,
                       folder.active_unread_count,
                       folder.passive_unread_count,
                       folder.created_at, folder.updated_at
                FROM workspace_v3.folders AS folder
                WHERE folder.project_id = %s AND folder.user_uuid = %s
            """,
            "folder_items": """
                SELECT item.uuid, item.project_id, item.user_uuid,
                       item.folder_uuid, item.stream_uuid, item.order_index,
                       item.pinned_at, item.chat_type,
                       binding.unread_count, binding.active_unread_count,
                       binding.passive_unread_count,
                       item.created_at, item.updated_at
                FROM workspace_v3.folder_items AS item
                JOIN workspace_v3.stream_bindings AS binding
                  ON binding.project_id = item.project_id
                 AND binding.stream_uuid = item.stream_uuid
                 AND binding.user_uuid = item.user_uuid
                WHERE item.project_id = %s AND item.user_uuid = %s
            """,
            "files": """
                SELECT file.*
                FROM workspace_v3.files AS file
                WHERE (
                    file.acl_mode = 'public'
                    OR (file.project_id = %s AND file.user_uuid = %s)
                    OR (
                        file.project_id = %s
                        AND file.acl_mode = 'stream'
                        AND EXISTS (
                            SELECT 1
                            FROM workspace_v3.stream_bindings AS binding
                            WHERE binding.project_id = file.project_id
                              AND binding.stream_uuid = file.stream_uuid
                              AND binding.user_uuid = %s
                        )
                    )
                )
            """,
        }
        try:
            query = queries[resource]
        except KeyError as exc:
            raise ValueError(f"Unsupported Messenger resource {resource}") from exc
        if resource == "users":
            return query, [project, project, project, project]
        if resource == "message_reactions":
            return query, [user, project]
        if resource == "files":
            return query, [project, user, project, user]
        return query, [project, user]

    def _query_resources(
        self,
        resource: str,
        filters: typing.Any,
        order_by: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]:
        fields = _ORDERABLE_FIELDS[resource] | frozenset(
            {
                "project_id",
                "user_uuid",
                "stream_uuid",
                "topic_uuid",
                "message_uuid",
                "folder_uuid",
                "author_uuid",
                "read",
                "pinned",
                "starred",
                "mentioned",
                "source_name",
                "role",
                "private",
                "is_archived",
                "direct_user_uuid",
                "system_type",
            }
        )
        where, parameters = _compile_filter(filters or {}, fields)
        order = order_by or {"uuid": "asc"}
        terms = []
        for field, direction in order.items():
            if field not in _ORDERABLE_FIELDS[resource]:
                raise ra_exceptions.ValidationErrorException()
            normalized = direction.lower()
            if normalized not in {"asc", "desc"}:
                raise ra_exceptions.ValidationErrorException()
            terms.append(f'resource."{field}" {normalized.upper()}')
        resource_sql, scope_parameters = self._resource_sql(resource)
        query = (
            f"SELECT resource.* FROM ({resource_sql}) AS resource "
            f"WHERE {where} ORDER BY {', '.join(terms)}"
        )
        parameters = [*scope_parameters, *parameters]
        if limit is not None:
            query += " LIMIT %s"
            parameters.append(limit)
        rows = _session().execute(query, parameters).fetchall()
        result = [_public(item, resource) for item in rows]
        if resource == "folders" and result:
            items = self._query_resources("folder_items", {}, {"uuid": "asc"})
            grouped: dict[str, list[dict[str, typing.Any]]] = {}
            for item in items:
                grouped.setdefault(str(item["folder_uuid"]), []).append(item)
            for folder in result:
                folder["folder_items"] = grouped.get(str(folder["uuid"]), [])
        return result

    def sync_iam_identity(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        user_uuid = sys_uuid.UUID(str(values["user_uuid"]))
        email = values.get("email") or None
        avatar = (
            "urn:gravatar:"
            + hashlib.md5(
                (email or str(user_uuid)).strip().lower().encode(),
                usedforsecurity=False,
            ).hexdigest()
        )
        _session().execute(
            """
            INSERT INTO workspace_v3.users (
                uuid, created_at, updated_at, username, source, status,
                first_name, last_name, email, avatar
            ) VALUES (
                %s, clock_timestamp(), clock_timestamp(), %s, 'iam', 'active',
                %s, %s, %s, %s
            )
            ON CONFLICT (uuid) DO UPDATE
            SET username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                email = EXCLUDED.email,
                updated_at = CASE WHEN (
                    workspace_v3.users.username,
                    workspace_v3.users.first_name,
                    workspace_v3.users.last_name,
                    workspace_v3.users.email
                ) IS DISTINCT FROM (
                    EXCLUDED.username, EXCLUDED.first_name,
                    EXCLUDED.last_name, EXCLUDED.email
                ) THEN clock_timestamp() ELSE workspace_v3.users.updated_at END
            """,
            (
                user_uuid,
                values["username"],
                values.get("first_name") or None,
                values.get("last_name") or None,
                email,
                avatar,
            ),
        )
        _session().execute(
            """
            INSERT INTO workspace_v3.folders (
                uuid, project_id, user_uuid, kind, title,
                created_at, updated_at
            )
            SELECT input.uuid, %s, %s, input.kind, input.title,
                   TIMESTAMPTZ '2000-01-01 00:00:00+00'
                       + (input.ordinality - 1) * INTERVAL '1 second',
                   TIMESTAMPTZ '2000-01-01 00:00:00+00'
                       + (input.ordinality - 1) * INTERVAL '1 second'
            FROM unnest(%s::uuid[], %s::text[], %s::text[])
                WITH ORDINALITY AS input(uuid, kind, title, ordinality)
            ON CONFLICT DO NOTHING
            """,
            (
                self.project_uuid,
                user_uuid,
                [folder[0] for folder in constants.SYSTEM_FOLDERS],
                [folder[1] for folder in constants.SYSTEM_FOLDERS],
                [folder[2] for folder in constants.SYSTEM_FOLDERS],
            ),
        )
        return self.get_resource("users", user_uuid)

    def filter_resources(
        self,
        resource: str,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]:
        return self._query_resources(resource, filters, order_by, limit)

    def filter_message_page(
        self,
        filters: dict[str, typing.Any],
        marker_uuid: sys_uuid.UUID | None,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        scoped: typing.Any = filters.copy()
        if marker_uuid is not None:
            marker = self.get_resource("messages", marker_uuid)
            comparison = dm_filters.GT if sort_direction == "asc" else dm_filters.LT
            keyset = dm_filters.OR(
                {"created_at": comparison(marker["created_at"])},
                dm_filters.AND(
                    {"created_at": dm_filters.EQ(marker["created_at"])},
                    {"uuid": comparison(marker_uuid)},
                ),
            )
            scoped = dm_filters.AND(scoped, keyset)
        return self._query_resources(
            "messages",
            scoped,
            {"created_at": sort_direction, "uuid": sort_direction},
            limit,
        )

    def get_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any]:
        rows = self._query_resources(
            resource,
            {"uuid": dm_filters.EQ(resource_uuid)},
            limit=1,
        )
        if not rows:
            _not_found(resource, resource_uuid)
        return rows[0]

    def _stream_recipients(self, stream_uuid: object) -> tuple[sys_uuid.UUID, ...]:
        rows = (
            _session()
            .execute(
                """
            SELECT user_uuid
            FROM workspace_v3.stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
            ORDER BY user_uuid
            """,
                (self.project_uuid, stream_uuid),
            )
            .fetchall()
        )
        return tuple(sys_uuid.UUID(str(row["user_uuid"])) for row in rows)

    def _user_event_recipients(self) -> tuple[sys_uuid.UUID, ...]:
        rows = (
            _session()
            .execute(
                """
            SELECT DISTINCT project_user.user_uuid
            FROM (
                SELECT user_uuid FROM workspace_v3.stream_bindings
                WHERE project_id = %s
                UNION
                SELECT user_uuid FROM workspace_v3.folders
                WHERE project_id = %s
            ) AS project_user
            ORDER BY project_user.user_uuid
            """,
                (self.project_uuid, self.project_uuid),
            )
            .fetchall()
        )
        return tuple(sys_uuid.UUID(str(row["user_uuid"])) for row in rows)

    def _emit_user_updated_all_projects(self, user_uuid: sys_uuid.UUID) -> None:
        rows = (
            _session()
            .execute(
                """
            SELECT DISTINCT project_id
            FROM (
                SELECT project_id FROM workspace_v3.stream_bindings
                WHERE user_uuid = %(user)s
                UNION SELECT project_id FROM workspace_v3.streams
                WHERE owner_uuid = %(user)s OR direct_user_uuid = %(user)s
                UNION SELECT project_id FROM workspace_v3.messages
                WHERE author_uuid = %(user)s
                UNION SELECT project_id FROM workspace_v3.message_flags
                WHERE user_uuid = %(user)s
                UNION SELECT project_id FROM workspace_v3.message_reactions
                WHERE user_uuid = %(user)s
                UNION SELECT project_id FROM workspace_v3.folders
                WHERE user_uuid = %(user)s
            ) AS affected_project
            ORDER BY project_id
            """,
                {"user": user_uuid},
            )
            .fetchall()
        )
        for row in rows:
            project_uuid = sys_uuid.UUID(str(row["project_id"]))
            store = (
                self
                if project_uuid == self.project_uuid
                else MessengerV3Store(project_uuid, self.user_uuid)
            )
            store._emit_resource(
                "users",
                user_uuid,
                "updated",
                store._user_event_recipients(),
            )

    def _emit_provider_user_updated_all_projects(
        self,
        user_uuid: sys_uuid.UUID,
    ) -> None:
        rows = (
            _session()
            .execute(
                """
            SELECT project_id
            FROM workspace_v3.provider_entity_states
            WHERE entity_type = 'user' AND entity_uuid = %s
            ORDER BY project_id
            """,
                (user_uuid,),
            )
            .fetchall()
        )
        for row in rows:
            project_uuid = sys_uuid.UUID(str(row["project_id"]))
            store = (
                self
                if project_uuid == self.project_uuid
                else MessengerV3Store(project_uuid, self.user_uuid)
            )
            store._emit_resource(
                "users",
                user_uuid,
                "updated",
                store._user_event_recipients(),
            )

    def _message_projection_task_ids(
        self,
        message_uuid: sys_uuid.UUID,
    ) -> tuple[sys_uuid.UUID, ...]:
        rows = (
            _session()
            .execute(
                """
            SELECT task.uuid
            FROM workspace_v3.projection_tasks AS task
            WHERE task.project_id = %s
              AND task.task_type = 'read_counters'
              AND task.status = 'pending'
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(
                      COALESCE(task.payload -> 'operations', '[]'::jsonb)
                  ) AS operation
                  WHERE operation ->> 'message_uuid' = %s
              )
            """,
                (self.project_uuid, str(message_uuid)),
            )
            .fetchall()
        )
        return tuple(sys_uuid.UUID(str(row["uuid"])) for row in rows)

    def _suppress_new_message_projection_events(
        self,
        message_uuid: sys_uuid.UUID,
        previous_task_uuids: tuple[sys_uuid.UUID, ...],
    ) -> None:
        _session().execute(
            """
            UPDATE workspace_v3.projection_tasks AS task
            SET payload = task.payload
                    || '{"emit_message_events": false}'::jsonb,
                updated_at = clock_timestamp()
            WHERE task.project_id = %s
              AND task.task_type = 'read_counters'
              AND task.status = 'pending'
              AND task.uuid <> ALL(%s::uuid[])
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(
                      COALESCE(task.payload -> 'operations', '[]'::jsonb)
                  ) AS operation
                  WHERE operation ->> 'message_uuid' = %s
              )
            """,
            (self.project_uuid, list(previous_task_uuids), str(message_uuid)),
        )

    def _stream_is_direct(self, stream_uuid: object) -> bool:
        row = (
            _session()
            .execute(
                """
            SELECT direct_user_uuid
            FROM workspace_v3.streams
            WHERE project_id = %s AND uuid = %s
            """,
                (self.project_uuid, stream_uuid),
            )
            .fetchone()
        )
        if row is None:
            _not_found("streams", stream_uuid)
        return row["direct_user_uuid"] is not None

    def _provider_consumers_for_stream(
        self,
        stream_uuid: object,
    ) -> tuple[sys_uuid.UUID, ...]:
        rows = (
            _session()
            .execute(
                """
            SELECT provider.uuid
            FROM workspace_v3.streams AS stream
            JOIN workspace_v3.provider_consumers AS provider
              ON provider.project_id = stream.project_id
             AND provider.name = stream.source_name
             AND provider.enabled
            WHERE stream.project_id = %s AND stream.uuid = %s
              AND stream.source_name <> 'native'
            ORDER BY provider.uuid
            """,
                (self.project_uuid, stream_uuid),
            )
            .fetchall()
        )
        return tuple(sys_uuid.UUID(str(row["uuid"])) for row in rows)

    def _provider_consumers_for_user(
        self,
        user_uuid: object,
    ) -> tuple[sys_uuid.UUID, ...]:
        rows = (
            _session()
            .execute(
                """
            SELECT DISTINCT provider.uuid
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
                (self.project_uuid, user_uuid),
            )
            .fetchall()
        )
        return tuple(sys_uuid.UUID(str(row["uuid"])) for row in rows)

    def _provider_consumers_for_resource(
        self,
        resource: str,
        resource_uuid: object,
    ) -> tuple[sys_uuid.UUID, ...]:
        if resource == "users":
            return self._provider_consumers_for_user(resource_uuid)
        if resource == "streams":
            stream_uuid = resource_uuid
        elif resource == "stream_bindings":
            row = (
                _session()
                .execute(
                    """
                SELECT stream_uuid FROM workspace_v3.stream_bindings
                WHERE project_id = %s AND uuid = %s
                """,
                    (self.project_uuid, resource_uuid),
                )
                .fetchone()
            )
            if row is None:
                return ()
            stream_uuid = row["stream_uuid"]
        elif resource == "stream_topics":
            row = (
                _session()
                .execute(
                    """
                SELECT stream_uuid FROM workspace_v3.topics
                WHERE project_id = %s AND uuid = %s
                """,
                    (self.project_uuid, resource_uuid),
                )
                .fetchone()
            )
            if row is None:
                return ()
            stream_uuid = row["stream_uuid"]
        elif resource == "topic_bindings":
            row = (
                _session()
                .execute(
                    """
                SELECT stream_uuid FROM workspace_v3.topic_bindings
                WHERE project_id = %s AND uuid = %s
                """,
                    (self.project_uuid, resource_uuid),
                )
                .fetchone()
            )
            if row is None:
                return ()
            stream_uuid = row["stream_uuid"]
        elif resource == "messages":
            row = (
                _session()
                .execute(
                    """
                SELECT stream_uuid FROM workspace_v3.messages
                WHERE project_id = %s AND uuid = %s
                """,
                    (self.project_uuid, resource_uuid),
                )
                .fetchone()
            )
            if row is None:
                return ()
            stream_uuid = row["stream_uuid"]
        elif resource == "message_flags":
            row = (
                _session()
                .execute(
                    """
                SELECT message.stream_uuid
                FROM workspace_v3.message_flags AS flag
                JOIN workspace_v3.messages AS message
                  ON message.project_id = flag.project_id
                 AND message.uuid = flag.message_uuid
                WHERE flag.project_id = %s AND flag.uuid = %s
                """,
                    (self.project_uuid, resource_uuid),
                )
                .fetchone()
            )
            if row is None:
                return ()
            stream_uuid = row["stream_uuid"]
        elif resource == "message_reactions":
            row = (
                _session()
                .execute(
                    """
                SELECT message.stream_uuid
                FROM workspace_v3.message_reactions AS reaction
                JOIN workspace_v3.messages AS message
                  ON message.project_id = reaction.project_id
                 AND message.uuid = reaction.message_uuid
                WHERE reaction.project_id = %s AND reaction.uuid = %s
                """,
                    (self.project_uuid, resource_uuid),
                )
                .fetchone()
            )
            if row is None:
                return ()
            stream_uuid = row["stream_uuid"]
        elif resource == "files":
            row = (
                _session()
                .execute(
                    """
                SELECT stream_uuid FROM workspace_v3.files
                WHERE project_id = %s AND uuid = %s
                """,
                    (self.project_uuid, resource_uuid),
                )
                .fetchone()
            )
            if row is None or row["stream_uuid"] is None:
                return ()
            stream_uuid = row["stream_uuid"]
        else:
            return ()
        return self._provider_consumers_for_stream(stream_uuid)

    def _require_stream(self, stream_uuid: object) -> dict[str, typing.Any]:
        return self.get_resource("streams", sys_uuid.UUID(str(stream_uuid)))

    def _require_manage_stream(
        self,
        stream_uuid: object,
        *,
        summary_prompt: bool = False,
    ) -> None:
        row = (
            _session()
            .execute(
                """
            SELECT role
            FROM workspace_v3.stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
              AND role IN ('owner', 'administrator')
            """,
                (self.project_uuid, stream_uuid, self.user_uuid),
            )
            .fetchone()
        )
        if row is None:
            if summary_prompt:
                raise messenger_exceptions.TopicSummaryPromptForbiddenError()
            _not_found("streams", stream_uuid)

    def _emit(
        self,
        *,
        kind: str,
        object_type: str,
        action: str,
        entity_uuid: object,
        payloads: dict[sys_uuid.UUID, dict[str, typing.Any]],
        provider_consumers: typing.Iterable[sys_uuid.UUID] = (),
        provider_payload: dict[str, typing.Any] | None = None,
    ) -> int:
        session = _session()
        consumers: list[tuple[str, sys_uuid.UUID]] = [
            ("user", user_uuid) for user_uuid in sorted(payloads, key=str)
        ]
        origin = event_origin.current()
        providers = tuple(sorted(set(provider_consumers), key=str))
        consumers.extend(
            ("provider", provider_uuid)
            for provider_uuid in providers
            if origin != ("provider", provider_uuid)
        )
        if not consumers:
            return 0
        digest = hashlib.sha256(
            "\n".join(f"{kind_}:{uuid}" for kind_, uuid in consumers).encode()
        ).hexdigest()
        snapshot_uuid = sys_uuid.uuid5(
            sys_uuid.UUID("4a72ce87-e8a3-4f58-9bd7-b8c1d69c19b2"),
            f"{self.project_uuid}:{digest}",
        )
        if snapshot_uuid not in self._known_audience_snapshots:
            session.execute(
                """
                INSERT INTO workspace_v3.event_audience_snapshots (
                    uuid, project_id, membership_digest
                ) VALUES (%s, %s, %s)
                ON CONFLICT (project_id, membership_digest) DO NOTHING
                """,
                (snapshot_uuid, self.project_uuid, digest),
            )
            if consumers:
                session.execute(
                    """
                    INSERT INTO workspace_v3.event_audience_members (
                        project_id, audience_snapshot_uuid,
                        consumer_type, consumer_uuid
                    )
                    SELECT %s, %s, input.consumer_type, input.consumer_uuid
                    FROM unnest(%s::text[], %s::uuid[])
                        AS input(consumer_type, consumer_uuid)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        self.project_uuid,
                        snapshot_uuid,
                        [item[0] for item in consumers],
                        [item[1] for item in consumers],
                    ),
                )
            self._known_audience_snapshots.add(snapshot_uuid)
        if payloads:
            common_payload, recipient_payloads = _partition_event_payloads(
                object_type,
                payloads,
            )
        else:
            common_payload, recipient_payloads = {}, {}
        base_payload = {"kind": kind, **common_payload}
        event_uuid = sys_uuid.uuid4()
        event = session.execute(
            """
            INSERT INTO workspace_v3.events (
                uuid, project_id, entity_uuid, audience_snapshot_uuid,
                object_type, action, payload,
                origin_consumer_type, origin_consumer_uuid
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            RETURNING epoch_version
            """,
            (
                event_uuid,
                self.project_uuid,
                entity_uuid,
                snapshot_uuid,
                object_type,
                action,
                json.dumps(_simple(base_payload)),
                None if origin is None else origin[0],
                None if origin is None else origin[1],
            ),
        ).fetchone()
        epoch_version = int(event["epoch_version"])
        if recipient_payloads:
            session.execute(
                """
                INSERT INTO workspace_v3.event_recipient_payloads (
                    project_id, event_uuid, consumer_type, consumer_uuid, payload
                )
                SELECT %s, %s, 'user', input.user_uuid, input.payload::jsonb
                FROM unnest(%s::uuid[], %s::text[])
                    AS input(user_uuid, payload)
                """,
                (
                    self.project_uuid,
                    event_uuid,
                    list(recipient_payloads),
                    [
                        json.dumps(_simple(recipient_payloads[user_uuid]))
                        for user_uuid in recipient_payloads
                    ],
                ),
            )
        if provider_payload is None and len(recipient_payloads) == 1:
            provider_payload = next(iter(recipient_payloads.values()))
        provider_uuids = [
            consumer_uuid
            for consumer_type, consumer_uuid in consumers
            if consumer_type == "provider"
        ]
        if provider_payload is not None and provider_uuids:
            session.execute(
                """
                INSERT INTO workspace_v3.event_recipient_payloads (
                    project_id, event_uuid, consumer_type, consumer_uuid, payload
                )
                SELECT %s, %s, 'provider', input.consumer_uuid, %s::jsonb
                FROM unnest(%s::uuid[]) AS input(consumer_uuid)
                """,
                (
                    self.project_uuid,
                    event_uuid,
                    json.dumps(_simple(provider_payload)),
                    provider_uuids,
                ),
            )
        return epoch_version

    def _emit_resource(
        self,
        resource: str,
        resource_uuid: object,
        action: str,
        recipients: typing.Iterable[sys_uuid.UUID],
    ) -> None:
        recipient_values = tuple(sorted(set(recipients), key=str))
        payloads = self._event_resource_payloads(
            resource,
            resource_uuid,
            recipient_values,
        )
        object_type = _RESOURCE_OBJECT_TYPES[resource]
        provider_consumers = self._provider_consumers_for_resource(
            resource,
            resource_uuid,
        )
        self._emit(
            kind=f"{object_type}.{action}",
            object_type=object_type,
            action=action,
            entity_uuid=resource_uuid,
            payloads=payloads,
            provider_consumers=provider_consumers,
            provider_payload=self._provider_entity_payload(
                resource,
                resource_uuid,
                provider_consumers,
            ),
        )

    def _provider_entity_payload(
        self,
        resource: str,
        resource_uuid: object,
        provider_consumers: typing.Iterable[sys_uuid.UUID],
    ) -> dict[str, typing.Any] | None:
        providers = tuple(provider_consumers)
        if not providers:
            return None
        provider_resource = "topics" if resource == "stream_topics" else resource
        if provider_resource not in {
            "users",
            "streams",
            "stream_bindings",
            "topics",
            "topic_bindings",
            "messages",
            "message_flags",
            "message_reactions",
        }:
            return None
        # Import lazily: provider_store builds on MessengerV3Store, while this
        # path is used only after both modules are initialized.
        from workspace.messenger_api import provider_store

        provider = (
            _session()
            .execute(
                """
            WITH owner AS MATERIALIZED (
                SELECT provider_uuid
                FROM workspace_v3.provider_entity_states
                WHERE project_id = %s
                  AND entity_type = %s
                  AND entity_uuid = %s
            )
            SELECT provider.uuid, provider.name
            FROM workspace_v3.provider_consumers AS provider
            LEFT JOIN owner ON owner.provider_uuid = provider.uuid
            WHERE provider.project_id = %s
              AND provider.uuid = ANY(%s::uuid[])
              AND provider.enabled
              AND (
                  NOT EXISTS (SELECT 1 FROM owner)
                  OR owner.provider_uuid IS NOT NULL
              )
            ORDER BY (owner.provider_uuid IS NOT NULL) DESC, provider.uuid
            LIMIT 1
            """,
                (
                    self.project_uuid,
                    provider_store.RESOURCE_TYPES[provider_resource],
                    resource_uuid,
                    self.project_uuid,
                    list(providers),
                ),
            )
            .fetchone()
        )
        if provider is None:
            return None
        store = provider_store.ProviderEntityStore(
            _session(),
            self.project_uuid,
            self.user_uuid,
            provider,
        )
        return store.refresh_owned_state(
            provider_resource,
            sys_uuid.UUID(str(resource_uuid)),
        )

    def _emit_provider_resource(
        self,
        resource: str,
        resource_uuid: object,
        action: str = "updated",
    ) -> None:
        provider_consumers = self._provider_consumers_for_resource(
            resource,
            resource_uuid,
        )
        provider_payload = self._provider_entity_payload(
            resource,
            resource_uuid,
            provider_consumers,
        )
        if provider_payload is None:
            return
        object_type = _RESOURCE_OBJECT_TYPES[resource]
        self._emit(
            kind=f"{object_type}.{action}",
            object_type=object_type,
            action=action,
            entity_uuid=resource_uuid,
            payloads={},
            provider_consumers=provider_consumers,
            provider_payload={"uuid": str(resource_uuid), **provider_payload},
        )

    def _emit_provider_resources(
        self,
        resource: str,
        resource_uuids: typing.Iterable[object],
        action: str = "updated",
    ) -> None:
        ordered = tuple(
            sorted(
                {sys_uuid.UUID(str(value)) for value in resource_uuids},
                key=str,
            )
        )
        object_type = _RESOURCE_OBJECT_TYPES[resource]
        for offset in range(0, len(ordered), 500):
            items = []
            consumers: set[sys_uuid.UUID] = set()
            for resource_uuid in ordered[offset : offset + 500]:
                providers = self._provider_consumers_for_resource(
                    resource,
                    resource_uuid,
                )
                payload = self._provider_entity_payload(
                    resource,
                    resource_uuid,
                    providers,
                )
                if payload is None:
                    continue
                consumers.update(providers)
                items.append({"uuid": str(resource_uuid), **payload})
            if not items or not consumers:
                continue
            self._emit(
                kind=f"{object_type}.{action}",
                object_type=object_type,
                action=action,
                entity_uuid=ordered[offset],
                payloads={},
                provider_consumers=consumers,
                provider_payload={"items": items},
            )

    def _event_resource_payloads(
        self,
        resource: str,
        resource_uuid: object,
        recipients: tuple[sys_uuid.UUID, ...],
    ) -> dict[sys_uuid.UUID, dict[str, typing.Any]]:
        if not recipients:
            return {}
        if resource in _EVENT_RESOURCE_QUERIES:
            return event_resource_payloads(
                _session(),
                self.project_uuid,
                resource,
                resource_uuid,
                recipients,
            )
        payloads: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        original_user = self.user_uuid
        try:
            for recipient in recipients:
                self.user_uuid = recipient
                try:
                    payloads[recipient] = self.get_resource(
                        resource,
                        sys_uuid.UUID(str(resource_uuid)),
                    )
                except ra_exceptions.ResourceNotFoundError:
                    continue
        finally:
            self.user_uuid = original_user
        return payloads

    def _create_folder(self, values: dict[str, typing.Any]) -> dict[str, typing.Any]:
        folder_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        _session().execute(
            """
            INSERT INTO workspace_v3.folders (
                uuid, project_id, user_uuid, kind, title,
                background_color_value
            ) VALUES (%s, %s, %s, 'custom', %s, %s)
            """,
            (
                folder_uuid,
                self.project_uuid,
                self.user_uuid,
                values["title"],
                values.get("background_color_value"),
            ),
        )
        row = self.get_resource("folders", folder_uuid)
        self._emit(
            kind="folder.created",
            object_type="folder",
            action="created",
            entity_uuid=folder_uuid,
            payloads={self.user_uuid: row},
        )
        return row

    def _create_folder_item(
        self, values: dict[str, typing.Any]
    ) -> dict[str, typing.Any]:
        item_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        self._require_stream(values["stream_uuid"])
        folder_uuid = sys_uuid.UUID(str(values["folder_uuid"]))
        self.get_resource("folders", folder_uuid)
        folder_kind = (
            _session()
            .execute(
                """
            SELECT kind FROM workspace_v3.folders
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
                (self.project_uuid, self.user_uuid, folder_uuid),
            )
            .fetchone()["kind"]
        )
        if folder_kind != "custom":
            raise ra_exceptions.ValidationErrorException()
        _session().execute(
            """
            INSERT INTO workspace_v3.folder_items (
                uuid, project_id, user_uuid, folder_uuid, stream_uuid,
                order_index, pinned_at, chat_type, automatic
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false)
            """,
            (
                item_uuid,
                self.project_uuid,
                self.user_uuid,
                values["folder_uuid"],
                values["stream_uuid"],
                values.get("order_index"),
                values.get("pinned_at"),
                values["chat_type"],
            ),
        )
        row = self.get_resource("folder_items", item_uuid)
        self._emit(
            kind="folder.updated",
            object_type="folder",
            action="updated",
            entity_uuid=values["folder_uuid"],
            payloads={
                self.user_uuid: self.get_resource(
                    "folders", sys_uuid.UUID(str(values["folder_uuid"]))
                )
            },
        )
        return row

    def _create_stream(self, values: dict[str, typing.Any]) -> dict[str, typing.Any]:
        if values.get("source_name", "native") != "native":
            raise ra_exceptions.ValidationErrorException()
        direct_user = values.get("direct_user_uuid")
        if direct_user is not None:
            direct_user = sys_uuid.UUID(str(direct_user))
            self.get_resource("users", direct_user)
        private = bool(values.get("private", False) or direct_user is not None)
        participant_values = [self.user_uuid]
        if direct_user is not None and direct_user != self.user_uuid:
            participant_values.append(direct_user)
        participants = tuple(sorted(participant_values, key=str))
        private_index = (
            helpers.build_private_stream_index(self.user_uuid, direct_user)
            if direct_user is not None
            else None
        )
        stream_uuid = (
            helpers.deterministic_direct_stream_uuid(
                self.project_uuid,
                self.user_uuid,
                direct_user,
            )
            if direct_user is not None
            else sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        )
        topic_uuid = sys_uuid.uuid4()
        session = _session()
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
            (stream_uuid,),
        )
        if direct_user is not None:
            existing = session.execute(
                """
                SELECT source_name, private, direct_user_uuid, private_index
                FROM workspace_v3.streams
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, stream_uuid),
            ).fetchone()
            if existing is not None:
                if (
                    existing["source_name"] != "native"
                    or not existing["private"]
                    or existing["direct_user_uuid"] is None
                    or existing["private_index"] != private_index
                ):
                    raise ra_exceptions.ValidationErrorException()
                return self.get_resource("streams", stream_uuid)
        session.execute(
            """
            INSERT INTO workspace_v3.streams (
                uuid, project_id, name, description, owner_uuid,
                source_name, invite_only, announce, direct_user_uuid,
                private, private_index, color, default_topic_uuid,
                history_public_to_subscribers
            ) VALUES (
                %s, %s, %s, %s, %s, 'native', %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            """,
            (
                stream_uuid,
                self.project_uuid,
                values["name"],
                values.get("description"),
                self.user_uuid,
                values.get("invite_only", False),
                values.get("announce", False),
                direct_user,
                private,
                private_index,
                values.get("color", random.randint(0, 0xFFFFFF)),
                topic_uuid,
                values.get("history_public_to_subscribers", True),
            ),
        )
        session.execute(
            """
            INSERT INTO workspace_v3.stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid, role
            )
            SELECT gen_random_uuid(), %s, %s, input.user_uuid, %s, 'owner'
            FROM unnest(%s::uuid[]) AS input(user_uuid)
            """,
            (self.project_uuid, stream_uuid, self.user_uuid, list(participants)),
        )
        session.execute(
            """
            INSERT INTO workspace_v3.topics (
                uuid, project_id, stream_uuid, name, source_name
            ) VALUES (%s, %s, %s, 'General Topic', 'native')
            """,
            (topic_uuid, self.project_uuid, stream_uuid),
        )
        session.execute(
            """
            INSERT INTO workspace_v3.topic_bindings (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid
            )
            SELECT gen_random_uuid(), %s, %s, %s, input.user_uuid
            FROM unnest(%s::uuid[]) AS input(user_uuid)
            """,
            (self.project_uuid, stream_uuid, topic_uuid, list(participants)),
        )
        self._emit_resource("streams", stream_uuid, "created", participants)
        self._emit_resource("stream_topics", topic_uuid, "created", participants)
        return self.get_resource("streams", stream_uuid)

    def _create_topic(self, values: dict[str, typing.Any]) -> dict[str, typing.Any]:
        stream = self._require_stream(values["stream_uuid"])
        topic_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        session = _session()
        session.execute(
            """
            INSERT INTO workspace_v3.topics (
                uuid, project_id, stream_uuid, name, color, source_name
            ) VALUES (%s, %s, %s, %s, %s, 'native')
            """,
            (
                topic_uuid,
                self.project_uuid,
                stream["uuid"],
                values["name"],
                values.get("color", random.randint(0, 0xFFFFFF)),
            ),
        )
        session.execute(
            """
            INSERT INTO workspace_v3.topic_bindings (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid
            )
            SELECT gen_random_uuid(), binding.project_id, binding.stream_uuid,
                   %s, binding.user_uuid
            FROM workspace_v3.stream_bindings AS binding
            WHERE binding.project_id = %s AND binding.stream_uuid = %s
            """,
            (topic_uuid, self.project_uuid, stream["uuid"]),
        )
        recipients = self._stream_recipients(stream["uuid"])
        self._emit_resource("stream_topics", topic_uuid, "created", recipients)
        return self.get_resource("stream_topics", topic_uuid)

    def _create_reaction(self, values: dict[str, typing.Any]) -> dict[str, typing.Any]:
        message = self.get_resource(
            "messages", sys_uuid.UUID(str(values["message_uuid"]))
        )
        reaction_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        row = (
            _session()
            .execute(
                """
            INSERT INTO workspace_v3.message_reactions (
                uuid, project_id, message_uuid, user_uuid,
                emoji_name, source_name
            ) VALUES (%s, %s, %s, %s, %s, 'native')
            ON CONFLICT (project_id, message_uuid, user_uuid, emoji_name)
            DO NOTHING
            RETURNING uuid
            """,
                (
                    reaction_uuid,
                    self.project_uuid,
                    message["uuid"],
                    self.user_uuid,
                    values["emoji_name"],
                ),
            )
            .fetchone()
        )
        if row is None:
            row = (
                _session()
                .execute(
                    """
                SELECT uuid
                FROM workspace_v3.message_reactions
                WHERE project_id = %s AND message_uuid = %s
                  AND user_uuid = %s AND emoji_name = %s
                """,
                    (
                        self.project_uuid,
                        message["uuid"],
                        self.user_uuid,
                        values["emoji_name"],
                    ),
                )
                .fetchone()
            )
        return self.get_resource("message_reactions", row["uuid"])

    def _create_file(self, values: dict[str, typing.Any]) -> dict[str, typing.Any]:
        file_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        acl_mode = values.get("acl_mode", "stream")
        stream_uuid = values.get("stream_uuid")
        if stream_uuid is not None:
            self._require_stream(stream_uuid)
        _session().execute(
            """
            INSERT INTO workspace_v3.files (
                uuid, project_id, user_uuid, stream_uuid, acl_mode,
                name, description, content_type, size_bytes, hash,
                storage_type, storage_id, storage_object_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                file_uuid,
                self.project_uuid,
                self.user_uuid,
                stream_uuid,
                acl_mode,
                values["name"],
                values.get("description", ""),
                values["content_type"],
                values["size_bytes"],
                values["hash"],
                values["storage_type"],
                values.get("storage_id", ""),
                values["storage_object_id"],
            ),
        )
        row = self.get_resource("files", file_uuid)
        recipients = (
            (self.user_uuid,)
            if stream_uuid is None
            else self._stream_recipients(stream_uuid)
        )
        self._emit_resource("files", file_uuid, "created", recipients)
        return row

    def create_resource(
        self,
        resource: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        creators = {
            "folders": self._create_folder,
            "folder_items": self._create_folder_item,
            "files": self._create_file,
            "streams": self._create_stream,
            "stream_topics": self._create_topic,
            "message_reactions": self._create_reaction,
        }
        try:
            return creators[resource](values)
        except KeyError as exc:
            raise ValueError(f"Unsupported Messenger create {resource}") from exc

    def create_message(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        stream = self._require_stream(values["stream_uuid"])
        topic_uuid = values.get("topic_uuid") or stream.get("default_topic_uuid")
        if topic_uuid is None:
            raise messenger_exceptions.StreamDefaultTopicNotConfiguredError()
        topic_uuid = sys_uuid.UUID(str(topic_uuid))
        topic = self.get_resource("stream_topics", topic_uuid)
        if sys_uuid.UUID(str(topic["stream_uuid"])) != sys_uuid.UUID(
            str(stream["uuid"])
        ):
            raise ra_exceptions.ValidationErrorException()
        message_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        session = _session()
        session.execute(
            """
            INSERT INTO workspace_v3.messages (
                uuid, project_id, stream_uuid, topic_uuid,
                author_uuid, payload, source_name
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'native')
            """,
            (
                message_uuid,
                self.project_uuid,
                stream["uuid"],
                topic_uuid,
                self.user_uuid,
                json.dumps(_simple(values["payload"])),
            ),
        )
        session.execute(
            """
            INSERT INTO workspace_v3.message_flags (
                project_id, stream_uuid, message_uuid, user_uuid,
                read, mentioned
            )
            SELECT message.project_id, message.stream_uuid, message.uuid,
                   binding.user_uuid,
                   binding.user_uuid = message.author_uuid,
                   position(
                       'urn:user:' || lower(binding.user_uuid::text)
                       IN lower(message.payload ->> 'content')
                   ) > 0
            FROM workspace_v3.messages AS message
            JOIN workspace_v3.stream_bindings AS binding
              ON binding.project_id = message.project_id
             AND binding.stream_uuid = message.stream_uuid
            WHERE message.project_id = %s AND message.uuid = %s
            """,
            (self.project_uuid, message_uuid),
        )
        recipients = self._stream_recipients(stream["uuid"])
        self._emit_resource("messages", message_uuid, "created", recipients)
        return self.get_resource("messages", message_uuid)

    def update_message(
        self,
        message_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        message = self.get_resource("messages", message_uuid)
        if sys_uuid.UUID(str(message["author_uuid"])) != self.user_uuid:
            _not_found("messages", message_uuid)
        if set(values) != {"payload"}:
            raise ra_exceptions.ValidationErrorException()
        pending_projection_tasks = self._message_projection_task_ids(message_uuid)
        _session().execute(
            """
            UPDATE workspace_v3.messages
            SET payload = %s::jsonb, updated_at = clock_timestamp()
            WHERE project_id = %s AND uuid = %s
            """,
            (json.dumps(_simple(values["payload"])), self.project_uuid, message_uuid),
        )
        _session().execute(
            """
            UPDATE workspace_v3.message_flags AS flag
            SET mentioned = position(
                    'urn:user:' || lower(flag.user_uuid::text)
                    IN lower(message.payload ->> 'content')
                ) > 0,
                updated_at = clock_timestamp()
            FROM workspace_v3.messages AS message
            WHERE flag.project_id = message.project_id
              AND flag.message_uuid = message.uuid
              AND message.project_id = %s AND message.uuid = %s
            """,
            (self.project_uuid, message_uuid),
        )
        self._suppress_new_message_projection_events(
            message_uuid,
            pending_projection_tasks,
        )
        recipients = self._stream_recipients(message["stream_uuid"])
        self._emit_resource("messages", message_uuid, "updated", recipients)
        return self.get_resource("messages", message_uuid)

    def delete_message(
        self,
        message_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        message = self.get_resource("messages", message_uuid)
        if sys_uuid.UUID(str(message["author_uuid"])) != self.user_uuid:
            _not_found("messages", message_uuid)
        recipients = self._stream_recipients(message["stream_uuid"])
        provider_consumers = self._provider_consumers_for_stream(message["stream_uuid"])
        provider_payload = self._provider_entity_payload(
            "messages",
            message_uuid,
            provider_consumers,
        )
        restored_topic_uuid = restore_topic_summary_after_message_deletion(
            _session(),
            self.project_uuid,
            message_uuid,
        )
        _session().execute(
            "DELETE FROM workspace_v3.messages WHERE project_id = %s AND uuid = %s",
            (self.project_uuid, message_uuid),
        )
        payload = {
            "kind": "message.deleted",
            "uuid": str(message_uuid),
            "stream_uuid": message["stream_uuid"],
            "topic_uuid": message["topic_uuid"],
            "author_uuid": message["author_uuid"],
            "source_name": message["source_name"],
            "source": message["source"],
        }
        self._emit(
            kind="message.deleted",
            object_type="message",
            action="deleted",
            entity_uuid=message_uuid,
            payloads={recipient: payload for recipient in recipients},
            provider_consumers=provider_consumers,
            provider_payload=provider_payload,
        )
        if restored_topic_uuid is not None:
            self._emit_resource(
                "stream_topics",
                restored_topic_uuid,
                "updated",
                recipients,
            )
        return None

    def _update_columns(
        self,
        table: str,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
        allowed: frozenset[str],
        *,
        user_scoped: bool = False,
        emit_event: bool = True,
    ) -> dict[str, typing.Any]:
        invalid = set(values) - allowed
        if invalid:
            raise ra_exceptions.ValidationErrorException()
        current = self.get_resource(resource, resource_uuid)
        if not values:
            return current
        assignments = [f'"{name}" = %s' for name in values]
        parameters = [
            json.dumps(_simple(value)) if name == "payload" else value
            for name, value in values.items()
        ]
        query = (
            f'UPDATE workspace_v3."{table}" SET '
            + ", ".join(assignments)
            + ", updated_at = clock_timestamp() "
            + "WHERE project_id = %s AND uuid = %s"
        )
        parameters.extend((self.project_uuid, resource_uuid))
        if user_scoped:
            query += " AND user_uuid = %s"
            parameters.append(self.user_uuid)
        try:
            _session().execute(query, parameters)
        except psycopg.errors.CheckViolation as error:
            _translate_private_member_limit(error)
        updated = self.get_resource(resource, resource_uuid)
        if emit_event:
            if resource in {"streams", "stream_topics"}:
                recipients = self._stream_recipients(
                    updated["uuid"] if resource == "streams" else updated["stream_uuid"]
                )
            else:
                recipients = (self.user_uuid,)
            self._emit_resource(resource, resource_uuid, "updated", recipients)
        return updated

    def update_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        if resource == "folders":
            self.get_resource(resource, resource_uuid)
            kind = (
                _session()
                .execute(
                    """
                SELECT kind FROM workspace_v3.folders
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                """,
                    (self.project_uuid, self.user_uuid, resource_uuid),
                )
                .fetchone()["kind"]
            )
            if kind != "custom":
                raise ra_exceptions.ValidationErrorException()
            return self._update_columns(
                "folders",
                resource,
                resource_uuid,
                values,
                frozenset({"title", "background_color_value"}),
                user_scoped=True,
            )
        if resource == "folder_items":
            return self._update_columns(
                "folder_items",
                resource,
                resource_uuid,
                values,
                frozenset({"order_index"}),
                user_scoped=True,
            )
        if resource == "files":
            current = self.get_resource(resource, resource_uuid)
            if (
                sys_uuid.UUID(str(current["project_id"])) != self.project_uuid
                or sys_uuid.UUID(str(current["user_uuid"])) != self.user_uuid
            ):
                _not_found(resource, resource_uuid)
            return self._update_columns(
                "files",
                resource,
                resource_uuid,
                values,
                frozenset({"name", "description"}),
            )
        if resource == "streams":
            self._require_manage_stream(resource_uuid)
            return self._update_columns(
                "streams",
                resource,
                resource_uuid,
                values,
                frozenset({"name", "description", "invite_only", "announce", "color"}),
            )
        if resource == "stream_bindings":
            current = self.get_resource(resource, resource_uuid)
            stream_uuid = sys_uuid.UUID(str(current["stream_uuid"]))
            if self._stream_is_direct(stream_uuid):
                raise ra_exceptions.ValidationErrorException()
            invalid = set(values) - {"role", "notification_mode"}
            if invalid:
                raise ra_exceptions.ValidationErrorException()
            if (
                "role" in values
                and values["role"] not in _STREAM_BINDING_ROLES
                or "notification_mode" in values
                and values["notification_mode"] not in _STREAM_NOTIFICATION_MODES
            ):
                raise ra_exceptions.ValidationErrorException()
            if not values:
                return current
            if (
                "role" in values
                or sys_uuid.UUID(str(current["user_uuid"])) != self.user_uuid
            ):
                self._require_manage_stream(stream_uuid)
            assignments = [f'"{name}" = %s' for name in values]
            parameters: list[typing.Any] = list(values.values())
            if "notification_mode" in values:
                assignments.append("notification_updated_at = clock_timestamp()")
            parameters.extend((self.project_uuid, resource_uuid))
            _session().execute(
                "UPDATE workspace_v3.stream_bindings SET "
                + ", ".join(assignments)
                + ", updated_at = clock_timestamp() "
                + "WHERE project_id = %s AND uuid = %s",
                parameters,
            )
            updated = self.get_resource(resource, resource_uuid)
            self._emit_resource(
                resource,
                resource_uuid,
                "updated",
                self._stream_recipients(stream_uuid),
            )
            return updated
        if resource == "stream_topics":
            return self._update_columns(
                "topics",
                resource,
                resource_uuid,
                values,
                frozenset({"name", "color"}),
            )
        if resource == "message_reactions":
            current = self.get_resource(resource, resource_uuid)
            if sys_uuid.UUID(str(current["user_uuid"])) != self.user_uuid:
                _not_found(resource, resource_uuid)
            if values.get("emoji_name") == current["emoji_name"]:
                return current
            return self._update_columns(
                "message_reactions",
                resource,
                resource_uuid,
                values,
                frozenset({"emoji_name"}),
                emit_event=False,
            )
        raise ValueError(f"Unsupported Messenger update {resource}")

    def delete_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        current = self.get_resource(resource, resource_uuid)
        table = {
            "folders": "folders",
            "folder_items": "folder_items",
            "files": "files",
            "streams": "streams",
            "stream_bindings": "stream_bindings",
            "stream_topics": "topics",
            "message_reactions": "message_reactions",
        }.get(resource)
        if table is None:
            raise ValueError(f"Unsupported Messenger delete {resource}")
        if resource == "folders":
            kind = (
                _session()
                .execute(
                    """
                SELECT kind FROM workspace_v3.folders
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                """,
                    (self.project_uuid, self.user_uuid, resource_uuid),
                )
                .fetchone()["kind"]
            )
            if kind != "custom":
                raise ra_exceptions.ValidationErrorException()
        if resource == "folder_items":
            automatic = (
                _session()
                .execute(
                    """
                SELECT automatic FROM workspace_v3.folder_items
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                """,
                    (self.project_uuid, self.user_uuid, resource_uuid),
                )
                .fetchone()["automatic"]
            )
            if automatic:
                raise ra_exceptions.ValidationErrorException()
        if resource in {"folders", "folder_items", "files", "message_reactions"}:
            owner_field = "user_uuid"
            if (
                resource == "files"
                and sys_uuid.UUID(str(current["project_id"])) != self.project_uuid
            ) or sys_uuid.UUID(str(current[owner_field])) != self.user_uuid:
                _not_found(resource, resource_uuid)
        if resource == "stream_bindings":
            stream_uuid = current["stream_uuid"]
            if self._stream_is_direct(stream_uuid):
                raise ra_exceptions.ValidationErrorException()
            if sys_uuid.UUID(str(current["user_uuid"])) != self.user_uuid:
                self._require_manage_stream(stream_uuid)
            recipients = self._stream_recipients(stream_uuid)
        elif resource in {"streams", "stream_topics"}:
            stream_uuid = (
                current["uuid"] if resource == "streams" else current["stream_uuid"]
            )
            recipients = self._stream_recipients(stream_uuid)
            if resource == "streams":
                if self._stream_is_direct(stream_uuid):
                    raise ra_exceptions.ValidationErrorException()
                self._require_manage_stream(stream_uuid)
        elif resource == "message_reactions":
            # The projection worker emits the complete legacy-compatible
            # reaction event after it has rebuilt the message aggregate.
            # Keep this direct deletion event provider-only so old clients do
            # not observe a preliminary payload containing just the UUID.
            recipients = ()
        else:
            recipients = (self.user_uuid,)
        provider_consumers = (
            self._provider_consumers_for_stream(stream_uuid)
            if resource in {"streams", "stream_bindings", "stream_topics"}
            else self._provider_consumers_for_resource(resource, resource_uuid)
        )
        provider_payload = self._provider_entity_payload(
            resource,
            resource_uuid,
            provider_consumers,
        )
        query = (
            f'DELETE FROM workspace_v3."{table}" WHERE project_id = %s AND uuid = %s'
        )
        parameters: list[typing.Any] = [self.project_uuid, resource_uuid]
        if resource in {"folders", "folder_items"}:
            query += " AND user_uuid = %s"
            parameters.append(self.user_uuid)
        _session().execute(query, parameters)
        payload = {"uuid": str(resource_uuid)}
        if resource == "stream_topics":
            payload["stream_uuid"] = current["stream_uuid"]
        object_type = _RESOURCE_OBJECT_TYPES[resource]
        self._emit(
            kind=f"{object_type}.deleted",
            object_type=object_type,
            action="deleted",
            entity_uuid=resource_uuid,
            payloads={recipient: payload for recipient in recipients},
            provider_consumers=provider_consumers,
            provider_payload=provider_payload,
        )
        if resource == "stream_bindings":
            removed_user_uuid = sys_uuid.UUID(str(current["user_uuid"]))
            self._emit(
                kind="stream.deleted",
                object_type="stream",
                action="deleted",
                entity_uuid=stream_uuid,
                payloads={removed_user_uuid: {"uuid": str(stream_uuid)}},
            )
        elif resource == "stream_topics" and current["is_default"]:
            self._emit_resource("streams", stream_uuid, "updated", recipients)
        return None

    def _add_users(
        self,
        stream_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> list[dict[str, typing.Any]]:
        self._require_stream(stream_uuid)
        self._require_manage_stream(stream_uuid)
        rows = [
            (sys_uuid.UUID(str(user_uuid)), role)
            for role, user_uuids in values.items()
            for user_uuid in user_uuids
        ]
        if not rows:
            return []
        session = _session()
        history_public_to_subscribers = bool(
            session.execute(
                """
                SELECT history_public_to_subscribers
                FROM workspace_v3.streams
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, stream_uuid),
            ).fetchone()["history_public_to_subscribers"]
        )
        try:
            inserted = session.execute(
                """
                INSERT INTO workspace_v3.stream_bindings (
                    uuid, project_id, stream_uuid, user_uuid, who_uuid, role
                )
                SELECT gen_random_uuid(), %s, %s, input.user_uuid, %s, input.role
                FROM unnest(%s::uuid[], %s::text[]) AS input(user_uuid, role)
                ON CONFLICT (project_id, stream_uuid, user_uuid) DO NOTHING
                RETURNING uuid, user_uuid
                """,
                (
                    self.project_uuid,
                    stream_uuid,
                    self.user_uuid,
                    [row[0] for row in rows],
                    [row[1] for row in rows],
                ),
            ).fetchall()
        except psycopg.errors.CheckViolation as error:
            _translate_private_member_limit(error)
        added_users = [sys_uuid.UUID(str(row["user_uuid"])) for row in inserted]
        if not added_users:
            return []
        session.execute(
            """
            INSERT INTO workspace_v3.topic_bindings (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                last_message_uuid
            )
            SELECT gen_random_uuid(), topic.project_id, topic.stream_uuid,
                   topic.uuid, input.user_uuid,
                   CASE WHEN %s THEN (
                       SELECT message.uuid
                       FROM workspace_v3.messages AS message
                       WHERE message.project_id = topic.project_id
                         AND message.topic_uuid = topic.uuid
                       ORDER BY message.created_at DESC, message.uuid DESC
                       LIMIT 1
                   ) END
            FROM workspace_v3.topics AS topic
            CROSS JOIN unnest(%s::uuid[]) AS input(user_uuid)
            WHERE topic.project_id = %s AND topic.stream_uuid = %s
            """,
            (
                history_public_to_subscribers,
                added_users,
                self.project_uuid,
                stream_uuid,
            ),
        )
        if history_public_to_subscribers:
            session.execute(
                """
                INSERT INTO workspace_v3.message_flags (
                    project_id, stream_uuid, message_uuid, user_uuid, read, mentioned
                )
                SELECT message.project_id, message.stream_uuid, message.uuid,
                       input.user_uuid, true,
                       position(
                           'urn:user:' || lower(input.user_uuid::text)
                           IN lower(message.payload ->> 'content')
                       ) > 0
                FROM workspace_v3.messages AS message
                CROSS JOIN unnest(%s::uuid[]) AS input(user_uuid)
                WHERE message.project_id = %s AND message.stream_uuid = %s
                """,
                (added_users, self.project_uuid, stream_uuid),
            )
            session.execute(
                """
                UPDATE workspace_v3.stream_bindings AS binding
                SET last_message_uuid = (
                    SELECT message.uuid
                    FROM workspace_v3.messages AS message
                    WHERE message.project_id = binding.project_id
                      AND message.stream_uuid = binding.stream_uuid
                    ORDER BY message.created_at DESC, message.uuid DESC
                    LIMIT 1
                    ),
                    updated_at = clock_timestamp()
                WHERE binding.project_id = %s AND binding.stream_uuid = %s
                  AND binding.user_uuid = ANY(%s::uuid[])
                """,
                (self.project_uuid, stream_uuid, added_users),
            )
        result = [
            self.get_resource("stream_bindings", sys_uuid.UUID(str(row["uuid"])))
            for row in inserted
        ]
        recipients = self._stream_recipients(stream_uuid)
        added_user_set = set(added_users)
        existing_recipients = tuple(
            recipient for recipient in recipients if recipient not in added_user_set
        )
        if existing_recipients:
            self._emit(
                kind="stream_bindings.created",
                object_type="stream_binding",
                action="created",
                entity_uuid=stream_uuid,
                payloads={
                    recipient: {
                        "uuid": str(stream_uuid),
                        "items": result,
                    }
                    for recipient in existing_recipients
                },
            )
        self._emit_resource("streams", stream_uuid, "created", added_users)
        self._emit_provider_resources(
            "stream_bindings",
            (row["uuid"] for row in inserted),
            "created",
        )
        return result

    def perform_action(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        action: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any] | list[dict[str, typing.Any]]:
        if resource == "folder_items" and action in {"pin", "unpin"}:
            item = self.get_resource(resource, resource_uuid)
            _session().execute(
                """
                UPDATE workspace_v3.folder_items
                SET pinned_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                """,
                (action == "pin", self.project_uuid, self.user_uuid, resource_uuid),
            )
            row = self.get_resource(resource, resource_uuid)
            self._emit(
                kind="folder.updated",
                object_type="folder",
                action="updated",
                entity_uuid=item["folder_uuid"],
                payloads={
                    self.user_uuid: self.get_resource(
                        "folders", sys_uuid.UUID(str(item["folder_uuid"]))
                    )
                },
            )
            return row
        if resource == "stream_bindings" and action == "add_users":
            return self._add_users(resource_uuid, values)
        if resource == "streams" and action in {"archive", "unarchive"}:
            self._require_manage_stream(resource_uuid)
            _session().execute(
                """
                UPDATE workspace_v3.streams
                SET is_archived = %s, updated_at = clock_timestamp()
                WHERE project_id = %s AND uuid = %s
                """,
                (action == "archive", self.project_uuid, resource_uuid),
            )
            recipients = self._stream_recipients(resource_uuid)
            self._emit_resource("streams", resource_uuid, "updated", recipients)
            return self.get_resource("streams", resource_uuid)
        if resource == "streams" and action == "notifications":
            if values["notification_mode"] not in _STREAM_NOTIFICATION_MODES:
                raise ra_exceptions.ValidationErrorException()
            stream = self.get_resource("streams", resource_uuid)
            binding = (
                _session()
                .execute(
                    """
                UPDATE workspace_v3.stream_bindings
                SET notification_mode = %s,
                    notification_updated_at = clock_timestamp(),
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
                RETURNING uuid
                """,
                    (
                        values["notification_mode"],
                        self.project_uuid,
                        resource_uuid,
                        self.user_uuid,
                    ),
                )
                .fetchone()
            )
            topic_bindings = []
            if values["notification_mode"] != "muted":
                topic_bindings = (
                    _session()
                    .execute(
                        """
                    UPDATE workspace_v3.topic_bindings
                    SET notification_mode = 'default',
                        notification_updated_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE project_id = %s AND stream_uuid = %s
                      AND user_uuid = %s AND notification_mode = 'unmute'
                    RETURNING uuid
                    """,
                        (self.project_uuid, resource_uuid, self.user_uuid),
                    )
                    .fetchall()
                )
            row = self.get_resource("streams", resource_uuid)
            self._emit(
                kind="stream.updated",
                object_type="stream",
                action="updated",
                entity_uuid=resource_uuid,
                payloads={self.user_uuid: row},
            )
            if binding is not None:
                self._emit_provider_resource("stream_bindings", binding["uuid"])
            self._emit_provider_resources(
                "topic_bindings",
                (item["uuid"] for item in topic_bindings),
            )
            if (
                stream["notification_mode"] == "muted"
                and row["notification_mode"] != "muted"
            ):
                topic_rows = self.filter_resources(
                    "stream_topics",
                    {"stream_uuid": dm_filters.EQ(resource_uuid)},
                )
                for topic_row in topic_rows:
                    self._emit(
                        kind="topic.updated",
                        object_type="topic",
                        action="updated",
                        entity_uuid=topic_row["uuid"],
                        payloads={self.user_uuid: topic_row},
                    )
            return row
        if resource in {"messages", "stream_topics", "streams"} and action in {
            "read",
            "read_up_to",
        }:
            return self._read_action(resource, resource_uuid, action)
        if resource == "messages" and action in {"star", "unstar"}:
            starred = action == "star"
            flag = (
                _session()
                .execute(
                    """
                UPDATE workspace_v3.message_flags
                SET starred = %s, updated_at = clock_timestamp()
                WHERE project_id = %s AND message_uuid = %s AND user_uuid = %s
                  AND starred IS DISTINCT FROM %s
                RETURNING uuid
                """,
                    (
                        starred,
                        self.project_uuid,
                        resource_uuid,
                        self.user_uuid,
                        starred,
                    ),
                )
                .fetchone()
            )
            row = self.get_resource("messages", resource_uuid)
            if flag is not None:
                self._emit_provider_resource("message_flags", flag["uuid"])
            return row
        if resource == "stream_topics" and action == "toggle_done":
            topic = self.get_resource(resource, resource_uuid)
            _session().execute(
                """
                UPDATE workspace_v3.topics
                SET is_done = NOT is_done, version = version + 1,
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, resource_uuid),
            )
            recipients = self._stream_recipients(topic["stream_uuid"])
            self._emit_resource(resource, resource_uuid, "updated", recipients)
            return self.get_resource(resource, resource_uuid)
        if resource == "stream_topics" and action == "notifications":
            topic = self.get_resource(resource, resource_uuid)
            stream = self.get_resource(
                "streams", sys_uuid.UUID(str(topic["stream_uuid"]))
            )
            allowed_modes = (
                _MUTED_STREAM_TOPIC_NOTIFICATION_MODES
                if stream["notification_mode"] == "muted"
                else _TOPIC_NOTIFICATION_MODES
            )
            if values["notification_mode"] not in allowed_modes:
                raise messenger_exceptions.InvalidTopicNotificationModeError(
                    mode=values["notification_mode"]
                )
            binding = (
                _session()
                .execute(
                    """
                UPDATE workspace_v3.topic_bindings
                SET notification_mode = %s,
                    notification_updated_at = clock_timestamp(),
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND topic_uuid = %s AND user_uuid = %s
                RETURNING uuid
                """,
                    (
                        values["notification_mode"],
                        self.project_uuid,
                        resource_uuid,
                        self.user_uuid,
                    ),
                )
                .fetchone()
            )
            row = self.get_resource(resource, resource_uuid)
            self._emit(
                kind="topic.updated",
                object_type="topic",
                action="updated",
                entity_uuid=resource_uuid,
                payloads={self.user_uuid: row},
            )
            if binding is not None:
                self._emit_provider_resource("topic_bindings", binding["uuid"])
            return row
        if resource == "stream_topics" and action == "set_default":
            topic = self.get_resource(resource, resource_uuid)
            self._require_manage_stream(topic["stream_uuid"])
            stream = self.get_resource(
                "streams", sys_uuid.UUID(str(topic["stream_uuid"]))
            )
            previous_topic_uuid = stream.get("default_topic_uuid")
            _session().execute(
                """
                UPDATE workspace_v3.streams
                SET default_topic_uuid = %s, updated_at = clock_timestamp()
                WHERE project_id = %s AND uuid = %s
                """,
                (resource_uuid, self.project_uuid, topic["stream_uuid"]),
            )
            recipients = self._stream_recipients(topic["stream_uuid"])
            self._emit_resource("streams", topic["stream_uuid"], "updated", recipients)
            if previous_topic_uuid is not None and str(previous_topic_uuid) != str(
                resource_uuid
            ):
                self._emit_resource(
                    resource,
                    previous_topic_uuid,
                    "updated",
                    recipients,
                )
            self._emit_resource(resource, resource_uuid, "updated", recipients)
            return self.get_resource(resource, resource_uuid)
        if resource == "stream_topics" and action == "set_summary_prompt":
            topic = self.get_resource(resource, resource_uuid)
            self._require_manage_stream(
                topic["stream_uuid"],
                summary_prompt=True,
            )
            allowed = frozenset(
                {
                    "summary_system_prompt",
                    "summary_reasoning_effort",
                    "summary_enabled",
                }
            )
            invalid = set(values) - allowed
            if invalid:
                raise ra_exceptions.ValidationErrorException()
            topic_properties = models.WorkspaceStreamTopic.properties.properties
            if any(
                not topic_properties[name].get_property_type().validate(value)
                for name, value in values.items()
            ):
                raise ra_exceptions.ValidationErrorException()
            updated = self._update_columns(
                "topics",
                resource,
                resource_uuid,
                values,
                allowed,
            )
            if values.get("summary_enabled") is False:
                _session().execute(
                    """
                    UPDATE m_workspace_llm_endpoints AS endpoint
                    SET claim_token = NULL,
                        claim_expires_at = NULL,
                        updated_at = NOW()
                    FROM m_workspace_topic_summary_jobs AS job
                    WHERE job.topic_uuid = %s
                      AND endpoint.uuid = job.endpoint_uuid
                      AND endpoint.claim_token = job.endpoint_claim_token
                    """,
                    (resource_uuid,),
                )
                _session().execute(
                    """
                    DELETE FROM m_workspace_topic_summary_jobs
                    WHERE topic_uuid = %s
                    """,
                    (resource_uuid,),
                )
            return updated
        if resource == "users" and action == "presence":
            if resource_uuid != self.user_uuid:
                _not_found(resource, resource_uuid)
            updates = {"status": values["status"]}
            if "emoji" in values:
                updates["status_emoji"] = values["emoji"]
            if "text" in values:
                updates["status_text"] = values["text"]
            updates["last_ping_at"] = datetime.datetime.now(datetime.timezone.utc)
            assignments = ", ".join(f'"{name}" = %s' for name in updates)
            _session().execute(
                f"UPDATE workspace_v3.users SET {assignments}, "
                "updated_at = clock_timestamp() WHERE uuid = %s",
                [*updates.values(), resource_uuid],
            )
            row = self.get_resource(resource, resource_uuid)
            self._emit_resource(
                "users",
                resource_uuid,
                "updated",
                self._user_event_recipients(),
            )
            return row
        if resource == "users" and action in {"avatar_upload", "avatar_reset"}:
            if resource_uuid != self.user_uuid:
                _not_found(resource, resource_uuid)
            user = self.get_resource(resource, resource_uuid)
            previous_file_uuid = None
            if str(user["avatar"]).startswith("urn:image:"):
                previous_file_uuid = sys_uuid.UUID(
                    str(user["avatar"]).removeprefix("urn:image:")
                )
            if action == "avatar_upload":
                self._create_file({**values, "acl_mode": "public"})
            avatar = (
                f"urn:image:{values['uuid']}"
                if action == "avatar_upload"
                else "urn:gravatar:"
                + hashlib.md5(
                    str(user.get("email") or resource_uuid).strip().lower().encode(),
                    usedforsecurity=False,
                ).hexdigest()
            )
            _session().execute(
                """
                UPDATE workspace_v3.users
                SET avatar = %s, updated_at = clock_timestamp()
                WHERE uuid = %s
                """,
                (avatar, resource_uuid),
            )
            if previous_file_uuid is not None:
                previous_file = (
                    _session()
                    .execute(
                        """
                    SELECT project_id FROM workspace_v3.files
                    WHERE uuid = %s
                    """,
                        (previous_file_uuid,),
                    )
                    .fetchone()
                )
                if (
                    previous_file is not None
                    and previous_file["project_id"] == self.project_uuid
                ):
                    self.delete_resource("files", previous_file_uuid)
            row = self.get_resource(resource, resource_uuid)
            self._emit_user_updated_all_projects(resource_uuid)
            return row
        raise ValueError(f"Unsupported Messenger action {resource}.{action}")

    def set_topic_summary(
        self,
        topic_uuid: sys_uuid.UUID,
        summary: str | None,
        summary_last_message_uuid: sys_uuid.UUID | None,
    ) -> dict[str, typing.Any]:
        if summary is None and summary_last_message_uuid is not None:
            raise messenger_exceptions.InvalidTopicSummaryStateError()
        summary_type = models.WorkspaceStreamTopic.properties.properties[
            "summary"
        ].get_property_type()
        if not summary_type.validate(summary):
            raise ra_exceptions.ValidationErrorException()

        session = _session()
        topic = session.execute(
            """
            SELECT stream_uuid, summary_last_message_uuid
            FROM workspace_v3.topics
            WHERE project_id = %s AND uuid = %s
            FOR UPDATE
            """,
            (self.project_uuid, topic_uuid),
        ).fetchone()
        if topic is None:
            _not_found("stream_topics", topic_uuid)
        self.get_resource("stream_topics", topic_uuid)

        incoming_boundary = None
        if summary_last_message_uuid is not None:
            incoming_boundary = session.execute(
                """
                SELECT uuid, created_at
                FROM workspace_v3.messages
                WHERE project_id = %s AND topic_uuid = %s AND uuid = %s
                """,
                (self.project_uuid, topic_uuid, summary_last_message_uuid),
            ).fetchone()
            if incoming_boundary is None:
                raise messenger_exceptions.InvalidTopicSummaryBoundaryError()

        current_boundary = None
        if topic["summary_last_message_uuid"] is not None:
            current_boundary = session.execute(
                """
                SELECT uuid, created_at
                FROM workspace_v3.messages
                WHERE project_id = %s AND topic_uuid = %s AND uuid = %s
                """,
                (
                    self.project_uuid,
                    topic_uuid,
                    topic["summary_last_message_uuid"],
                ),
            ).fetchone()
        if summary is not None:
            if incoming_boundary is None:
                raise messenger_exceptions.InvalidTopicSummaryBoundaryError()
            if current_boundary is not None and (
                incoming_boundary["created_at"],
                str(incoming_boundary["uuid"]),
            ) < (
                current_boundary["created_at"],
                str(current_boundary["uuid"]),
            ):
                raise messenger_exceptions.TopicSummaryConflictError()
            boundary_created_at = incoming_boundary["created_at"]
            if boundary_created_at.tzinfo is not None:
                boundary_created_at = boundary_created_at.astimezone(
                    datetime.timezone.utc
                ).replace(tzinfo=None)
            session.execute(
                """
                INSERT INTO m_workspace_topic_summary_journal (
                    uuid, topic_uuid, project_id, summary,
                    boundary_message_uuid, boundary_message_created_at,
                    generated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    sys_uuid.uuid4(),
                    topic_uuid,
                    self.project_uuid,
                    summary,
                    summary_last_message_uuid,
                    boundary_created_at,
                ),
            )
        session.execute(
            """
            UPDATE workspace_v3.topics
            SET summary = %s,
                summary_last_message_uuid = %s,
                updated_at = clock_timestamp()
            WHERE project_id = %s AND uuid = %s
            """,
            (
                summary,
                summary_last_message_uuid,
                self.project_uuid,
                topic_uuid,
            ),
        )
        session.execute(
            """
            UPDATE workspace_v3.topic_bindings AS binding
            SET summary_has_new_messages = CASE
                    WHEN %s::uuid IS NULL THEN NULL
                    ELSE EXISTS (
                        SELECT 1
                        FROM workspace_v3.messages AS candidate
                        JOIN workspace_v3.message_flags AS flag
                          ON flag.project_id = candidate.project_id
                         AND flag.message_uuid = candidate.uuid
                         AND flag.user_uuid = binding.user_uuid
                        JOIN workspace_v3.messages AS boundary
                          ON boundary.project_id = candidate.project_id
                         AND boundary.topic_uuid = candidate.topic_uuid
                         AND boundary.uuid = %s
                        WHERE candidate.project_id = binding.project_id
                          AND candidate.topic_uuid = binding.topic_uuid
                          AND (candidate.created_at, candidate.uuid) >
                              (boundary.created_at, boundary.uuid)
                    )
                END,
                updated_at = clock_timestamp()
            WHERE binding.project_id = %s AND binding.topic_uuid = %s
            """,
            (
                summary_last_message_uuid,
                summary_last_message_uuid,
                self.project_uuid,
                topic_uuid,
            ),
        )
        self._emit_resource(
            "stream_topics",
            topic_uuid,
            "updated",
            self._stream_recipients(topic["stream_uuid"]),
        )
        return self.get_resource("stream_topics", topic_uuid)

    def _read_action(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        action: str,
    ) -> dict[str, typing.Any]:
        session = _session()
        if resource == "messages":
            self.get_resource(resource, resource_uuid)
            operator = "=" if action == "read" else "<="
            changed = session.execute(
                f"""
                UPDATE workspace_v3.message_flags AS flag
                SET read = true, updated_at = clock_timestamp()
                FROM workspace_v3.messages AS candidate,
                     workspace_v3.messages AS boundary
                WHERE boundary.project_id = %s AND boundary.uuid = %s
                  AND candidate.project_id = boundary.project_id
                  AND candidate.topic_uuid = boundary.topic_uuid
                  AND (candidate.created_at, candidate.uuid) {operator}
                      (boundary.created_at, boundary.uuid)
                  AND flag.project_id = candidate.project_id
                  AND flag.message_uuid = candidate.uuid
                  AND flag.user_uuid = %s AND NOT flag.read
                RETURNING flag.uuid
                """,
                (self.project_uuid, resource_uuid, self.user_uuid),
            ).fetchall()
            self._emit_provider_resources(
                "message_flags",
                (row["uuid"] for row in changed),
            )
            return self.get_resource(resource, resource_uuid)
        if resource == "stream_topics":
            topic = self.get_resource(resource, resource_uuid)
            predicate = "message.topic_uuid = %s"
            identifier = resource_uuid
        else:
            topic = self.get_resource(resource, resource_uuid)
            predicate = "message.stream_uuid = %s"
            identifier = resource_uuid
        changed = session.execute(
            f"""
            UPDATE workspace_v3.message_flags AS flag
            SET read = true, updated_at = clock_timestamp()
            FROM workspace_v3.messages AS message
            WHERE message.project_id = %s AND {predicate}
              AND flag.project_id = message.project_id
              AND flag.message_uuid = message.uuid
              AND flag.user_uuid = %s AND NOT flag.read
            RETURNING flag.uuid
            """,
            (self.project_uuid, identifier, self.user_uuid),
        ).fetchall()
        self._emit_provider_resources(
            "message_flags",
            (row["uuid"] for row in changed),
        )
        return topic

    def create_draft(
        self,
        values: dict[str, typing.Any],
    ) -> tuple[dict[str, typing.Any], bool]:
        draft_uuid = sys_uuid.UUID(str(values["uuid"]))
        self._require_stream(values["stream_uuid"])
        current = (
            _session()
            .execute(
                """
            SELECT * FROM workspace_v3.drafts
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
                (self.project_uuid, self.user_uuid, draft_uuid),
            )
            .fetchone()
        )
        expected = {
            "stream_uuid": sys_uuid.UUID(str(values["stream_uuid"])),
            "topic_uuid": sys_uuid.UUID(str(values["topic_uuid"])),
            "payload": _simple(values["payload"]),
        }
        if current is not None:
            existing = _row(current)
            if any(existing[name] != value for name, value in expected.items()):
                raise messenger_exceptions.DraftConflictError()
            return self._draft(current), False
        topic = self.get_resource("stream_topics", expected["topic_uuid"])
        if sys_uuid.UUID(str(topic["stream_uuid"])) != expected["stream_uuid"]:
            raise ra_exceptions.ValidationErrorException()
        row = (
            _session()
            .execute(
                """
            INSERT INTO workspace_v3.drafts (
                uuid, project_id, user_uuid, stream_uuid, topic_uuid, payload
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            RETURNING *
            """,
                (
                    draft_uuid,
                    self.project_uuid,
                    self.user_uuid,
                    expected["stream_uuid"],
                    expected["topic_uuid"],
                    json.dumps(expected["payload"]),
                ),
            )
            .fetchone()
        )
        return self._draft(row), True

    @staticmethod
    def _draft(row: typing.Any) -> dict[str, typing.Any]:
        return _simple(_row(row))

    def get_draft(self, draft_uuid: sys_uuid.UUID) -> dict[str, typing.Any]:
        row = (
            _session()
            .execute(
                """
            SELECT * FROM workspace_v3.drafts
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
                (self.project_uuid, self.user_uuid, draft_uuid),
            )
            .fetchone()
        )
        if row is None:
            _not_found("drafts", draft_uuid)
        return self._draft(row)

    def filter_draft_page(
        self,
        filters: dict[str, typing.Any],
        marker_uuid: sys_uuid.UUID | None,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        where, parameters = _compile_filter(
            filters,
            frozenset({"uuid", "stream_uuid", "topic_uuid", "updated_at"}),
        )
        keyset = ""
        if marker_uuid is not None:
            marker = self.get_draft(marker_uuid)
            operator = ">" if sort_direction == "asc" else "<"
            keyset = (
                f' AND (resource."updated_at", resource."uuid") {operator} (%s, %s)'
            )
            parameters.extend((marker["updated_at"], marker_uuid))
        query = f"""
            SELECT resource.*
            FROM workspace_v3.drafts AS resource
            WHERE resource.project_id = %s AND resource.user_uuid = %s
              AND {where}{keyset}
            ORDER BY resource.updated_at {sort_direction},
                     resource.uuid {sort_direction}
        """
        parameters = [self.project_uuid, self.user_uuid, *parameters]
        if limit is not None:
            query += " LIMIT %s"
            parameters.append(limit)
        return [self._draft(row) for row in _session().execute(query, parameters)]

    def update_draft(
        self,
        draft_uuid: sys_uuid.UUID,
        payload: dict[str, typing.Any],
        expected_revision: int,
    ) -> dict[str, typing.Any]:
        row = (
            _session()
            .execute(
                """
            UPDATE workspace_v3.drafts
            SET payload = %s::jsonb, revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
              AND revision = %s
            RETURNING *
            """,
                (
                    json.dumps(_simple(payload)),
                    self.project_uuid,
                    self.user_uuid,
                    draft_uuid,
                    expected_revision,
                ),
            )
            .fetchone()
        )
        if row is None:
            current = self.get_draft(draft_uuid)
            raise messenger_exceptions.DraftPreconditionFailedError(current)
        return self._draft(row)

    def delete_draft(
        self,
        draft_uuid: sys_uuid.UUID,
        expected_revision: int,
    ) -> None:
        deleted = (
            _session()
            .execute(
                """
            DELETE FROM workspace_v3.drafts
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
              AND revision = %s
            RETURNING uuid
            """,
                (self.project_uuid, self.user_uuid, draft_uuid, expected_revision),
            )
            .fetchone()
        )
        if deleted is None:
            current = self.get_draft(draft_uuid)
            raise messenger_exceptions.DraftPreconditionFailedError(current)

    def _cursor(
        self,
        consumer_type: str = "user",
        consumer_uuid: sys_uuid.UUID | None = None,
    ) -> dict[str, typing.Any]:
        consumer_uuid = consumer_uuid or self.user_uuid
        session = _session()
        session.execute(
            """
            INSERT INTO workspace_v3.event_cursors (
                project_id, consumer_type, consumer_uuid
            ) VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (self.project_uuid, consumer_type, consumer_uuid),
        )
        row = session.execute(
            """
            SELECT cursor.epoch_generation,
                   GREATEST(
                       cursor.current_epoch_version,
                       COALESCE(max(audience.current_epoch_version), 0),
                       COALESCE(max(event.epoch_version), 0)
                   ) AS current_epoch_version,
                   GREATEST(
                       cursor.pruned_through_epoch_version,
                       COALESCE(max(audience.pruned_through_epoch_version), 0)
                   ) AS pruned_through_epoch_version
            FROM workspace_v3.event_cursors AS cursor
            LEFT JOIN workspace_v3.event_audience_members AS member
              ON member.project_id = cursor.project_id
             AND member.consumer_type = cursor.consumer_type
             AND member.consumer_uuid = cursor.consumer_uuid
            LEFT JOIN workspace_v3.event_audience_snapshots AS audience
              ON audience.project_id = member.project_id
             AND audience.uuid = member.audience_snapshot_uuid
            LEFT JOIN workspace_v3.events AS event
              ON event.project_id = audience.project_id
             AND event.audience_snapshot_uuid = audience.uuid
            WHERE cursor.project_id = %s AND cursor.consumer_type = %s
              AND cursor.consumer_uuid = %s
            GROUP BY cursor.epoch_generation,
                     cursor.current_epoch_version,
                     cursor.pruned_through_epoch_version
            """,
            (self.project_uuid, consumer_type, consumer_uuid),
        ).fetchone()
        return _row(row)

    @staticmethod
    def _after_epoch(
        filters: dict[str, typing.Any],
    ) -> tuple[int, tuple[typing.Any, ...]]:
        clause = filters.get("epoch_version")
        clauses = (
            clause._clauses
            if isinstance(clause, dm_filters.AND)
            else (() if clause is None else (clause,))
        )
        after = 0
        for item in clauses:
            value = int(_filter_value(item))
            if isinstance(item, dm_filters.GT):
                after = max(after, value)
            elif isinstance(item, (dm_filters.GE, dm_filters.EQ)):
                after = max(after, value - 1)
        return after, clauses

    def events_after(
        self,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
        epoch_generation: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]:
        return self.events_after_for_consumer(
            filters,
            consumer_type="user",
            consumer_uuid=self.user_uuid,
            order_by=order_by,
            epoch_generation=epoch_generation,
            limit=limit,
        )

    def events_after_for_consumer(
        self,
        filters: dict[str, typing.Any],
        *,
        consumer_type: str,
        consumer_uuid: sys_uuid.UUID,
        order_by: dict[str, str] | None = None,
        epoch_generation: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]:
        after, clauses = self._after_epoch(filters)
        cursor = self._cursor(consumer_type, consumer_uuid)
        generation = str(cursor["epoch_generation"])
        current = int(cursor["current_epoch_version"])
        minimum = int(cursor["pruned_through_epoch_version"]) + 1
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
        direction = (order_by or {"epoch_version": "asc"})["epoch_version"].lower()
        if direction not in {"asc", "desc"}:
            raise ra_exceptions.ValidationErrorException()
        query = """
            SELECT event.schema_version, event.uuid, event.epoch_version,
                   event.project_id, %s::uuid AS user_uuid,
                   event.object_type, event.action,
                   event.payload ||
                       COALESCE(recipient.payload, '{}'::jsonb) AS payload,
                   event.created_at, event.updated_at
            FROM workspace_v3.events AS event
            JOIN workspace_v3.event_audience_members AS member
              ON member.project_id = event.project_id
             AND member.audience_snapshot_uuid = event.audience_snapshot_uuid
             AND member.consumer_type = %s
             AND member.consumer_uuid = %s
            LEFT JOIN workspace_v3.event_recipient_payloads AS recipient
              ON recipient.project_id = event.project_id
             AND recipient.event_uuid = event.uuid
             AND recipient.consumer_type = %s
             AND recipient.consumer_uuid = %s
            WHERE event.project_id = %s AND event.epoch_version > %s
        """
        parameters: list[typing.Any] = [
            consumer_uuid,
            consumer_type,
            consumer_uuid,
            consumer_type,
            consumer_uuid,
            self.project_uuid,
            after,
        ]
        for item in clauses:
            value = int(_filter_value(item))
            if isinstance(item, dm_filters.GT):
                operator = ">"
            elif isinstance(item, dm_filters.GE):
                operator = ">="
            elif isinstance(item, dm_filters.LT):
                operator = "<"
            elif isinstance(item, dm_filters.LE):
                operator = "<="
            else:
                operator = "="
            query += f" AND event.epoch_version {operator} %s"
            parameters.append(value)
        query += f" ORDER BY event.epoch_version {direction}"
        if limit is not None:
            query += " LIMIT %s"
            parameters.append(limit)
        return [_simple(_row(row)) for row in _session().execute(query, parameters)]

    def current_epoch(self) -> int:
        return int(self._cursor()["current_epoch_version"])

    def event_cursor(self) -> dict[str, typing.Any]:
        return self.event_cursor_for_consumer("user", self.user_uuid)

    def event_cursor_for_consumer(
        self,
        consumer_type: str,
        consumer_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any]:
        cursor = self._cursor(consumer_type, consumer_uuid)
        return {
            "epoch_generation": str(cursor["epoch_generation"]),
            "current_epoch_version": int(cursor["current_epoch_version"]),
            "minimum_epoch_version": int(cursor["pruned_through_epoch_version"]) + 1,
        }


class MessengerV3StoreFactory:
    """Open v3 stores inside the request-owned database transaction."""

    @staticmethod
    def _sync_request_iam_identity(
        store: MessengerV3Store,
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
    def __call__(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> typing.Iterator[api_store.MessengerStore]:
        store = MessengerV3Store(project_uuid, user_uuid)
        self._sync_request_iam_identity(store, user_uuid)
        yield typing.cast(api_store.MessengerStore, store)

    draft_store = __call__
    event_store = __call__

    @staticmethod
    def move_stream_projection(**kwargs: object) -> None:
        del kwargs
