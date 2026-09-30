# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Legacy native store fixture for published-schema compatibility tests."""

import contextlib
import collections.abc
import datetime
import time
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import read_state


MENTIONED_MESSAGE_UUIDS_SQL = """
    WITH authorized_streams AS MATERIALIZED (
        SELECT binding.stream_uuid
        FROM m_workspace_stream_bindings AS binding
        JOIN m_workspace_streams AS stream
          ON stream.project_id = binding.project_id
         AND stream.uuid = binding.stream_uuid
        WHERE binding.project_id = %s
          AND binding.user_uuid = %s

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

COMPACT_MENTIONED_MESSAGE_UUIDS_SQL = """
    WITH authorized_streams AS MATERIALIZED (
        SELECT binding.stream_uuid
        FROM m_workspace_stream_bindings AS binding
        JOIN m_workspace_streams AS stream
          ON stream.project_id = binding.project_id
         AND stream.uuid = binding.stream_uuid
        WHERE binding.project_id = %s
          AND binding.user_uuid = %s

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
        WHERE binding.project_id = %s
          AND binding.user_uuid = %s

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


class LegacyReadStore(sql_canonical_store.SQLCanonicalReadStore):
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
            sql_canonical_store._public_dict(
                snapshots_by_uuid[message_uuid], "messages"
            )
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
            sql_canonical_store._public_dict(
                snapshots_by_uuid[message_uuid], "messages"
            )
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
            sql_canonical_store._public_dict(
                snapshots_by_uuid[message_uuid], "messages"
            )
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
            return [sql_canonical_store._public_dict(row, "messages") for row in rows]
        finally:
            duration = time.monotonic() - started_at
            if duration >= sql_canonical_store.SLOW_LIST_PROJECTION_SECONDS:
                sql_canonical_store.LOG.warning(
                    "Slow Messenger list projection",
                    extra={
                        "projection_resource": "messages",
                        "projection_duration_seconds": duration,
                    },
                )


class LegacyMessengerStore(
    LegacyReadStore, sql_canonical_store.SQLCanonicalMessengerStore
):
    def _is_direct_stream(self, stream_uuid: object) -> bool:
        return self._direct_stream_participants(stream_uuid) is not None

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

    def create_message(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        values = self._projection_values(values)
        values["uuid"] = values.get("uuid") or sys_uuid.uuid4()
        session = contexts.Context().get_session()
        row = helpers.create_workspace_user_message(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            session=session,
            enforce_visibility=True,
            compact_events=True,
            **values,
        )
        return sql_canonical_store._public_dict(row, "messages")

    def update_message(
        self,
        message_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        row = helpers.update_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
            self._projection_values(values),
            compact_events=True,
        )
        return sql_canonical_store._public_dict(row, "messages")

    def delete_message(
        self,
        message_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        helpers.delete_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
            compact_events=True,
        )
        return None

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
            row = helpers.create_workspace_user_stream_topic(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                values=values,
            )
        elif resource == "message_reactions":
            helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                values["message_uuid"],
            )
            row = helpers.create_workspace_message_reaction(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                compact_events=True,
                **values,
            )
        else:
            return super().create_resource(resource, values)
        return sql_canonical_store._public_dict(row, resource)

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
            row = helpers.update_workspace_user_stream(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values,
            )
        elif resource == "stream_topics":
            helpers.get_workspace_user_stream_topic(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
            )
            row = helpers.update_workspace_user_stream_topic(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values,
            )
        elif resource == "message_reactions":
            reaction = helpers.get_workspace_message_reaction(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
            )
            helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                reaction.message_uuid,
            )
            row = helpers.update_workspace_message_reaction(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values,
                compact_events=True,
            )
        elif resource == "stream_bindings":
            row = self._binding_for_update(resource_uuid)
            if self._is_direct_stream(row.stream_uuid):
                raise ra_exceptions.ValidationErrorException()
            row.update_dm(values=values)
            row.update(session=contexts.Context().get_session())
            helpers.create_workspace_stream_binding_updated_events(row)
        else:
            return super().update_resource(resource, resource_uuid, values)
        return sql_canonical_store._public_dict(row, resource)

    def delete_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
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
            helpers.delete_workspace_stream_binding(self.project_uuid, resource_uuid)
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
        else:
            return super().delete_resource(resource, resource_uuid)
        return None

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
            row = helpers.read_workspace_user_stream_messages(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_stream=stream,
                collect_message_uuids=False,
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
            row = helpers.read_workspace_user_stream_topic_messages(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_topic=topic,
                collect_message_uuids=False,
            )
        elif resource == "messages" and action == "read":
            session = contexts.Context().get_session()
            message = helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
            )
            row = helpers.read_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_message=message,
            )
        elif resource == "messages" and action == "read_up_to":
            session = contexts.Context().get_session()
            message = helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
            )
            row = helpers.read_workspace_user_topic_messages_to_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                session=session,
                current_message=message,
                collect_message_uuids=False,
            )
        elif resource == "messages" and action in {"star", "unstar"}:
            row = helpers.sync_workspace_user_message_flags(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                message_uuid=resource_uuid,
                values={"starred": action == "star"},
                session=contexts.Context().get_session(),
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
            row = helpers.get_or_create_workspace_stream_bindings(
                self.project_uuid,
                resource_uuid,
                self.user_uuid,
                role_user_uuids,
            )
        else:
            return super().perform_action(resource, resource_uuid, action, values)
        if isinstance(row, list):
            return [sql_canonical_store._public_dict(item, resource) for item in row]
        return sql_canonical_store._public_dict(row, resource)


class LegacyMessengerStoreFactory(
    sql_canonical_store.SQLCanonicalMessengerStoreFactory
):
    @contextlib.contextmanager
    def __call__(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> collections.abc.Iterator[api_store.MessengerStore]:
        store = typing.cast(
            api_store.MessengerStore,
            LegacyMessengerStore(project_uuid, user_uuid),
        )
        self._sync_request_iam_identity(store, user_uuid)
        yield store
