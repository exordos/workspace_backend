# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Messenger v2 store behind the unchanged Workspace v1 HTTP contract."""

import contextlib
import json
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exceptions

from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import read_state
from workspace.messenger_api.dm import v2_models


CORE_RESOURCE_MODELS: dict[str, typing.Any] = {
    "folders": v2_models.WorkspaceUserFolder,
    "folder_items": v2_models.WorkspaceUserFolderItem,
    "streams": v2_models.WorkspaceUserStream,
    "stream_bindings": v2_models.WorkspaceStreamBindingView,
    "stream_topics": v2_models.WorkspaceUserTopic,
    "messages": v2_models.WorkspaceUserMessage,
    "message_reactions": v2_models.WorkspaceMessageReactionView,
}
CORE_RESOURCES = frozenset(CORE_RESOURCE_MODELS)
_INTERNAL_FIELDS = {
    "binding_uuid",
    "canonical_message_uuid",
    "viewer_user_uuid",
    "private_index",
    "deleted_at",
    "visible",
}


def _simple_source(value: object) -> dict[str, typing.Any]:
    source = resource_projection.simple(value)
    if not isinstance(source, dict) or source.get("kind") != "native":
        raise ra_exceptions.ValidationErrorException()
    return source


def _public(row: typing.Any, resource: str) -> dict[str, typing.Any]:
    result = resource_projection.simple(row)
    for name in _INTERNAL_FIELDS:
        result.pop(name, None)
    if resource in {"streams", "stream_topics", "messages", "message_reactions"}:
        result.setdefault("provider", None)
        result.setdefault("delivery", None)
    return result


class MessengerV2Store(sql_canonical_store.SQLCanonicalMessengerStore):
    """Use the v2 canonical schema for native Messenger resources."""

    def _legacy_message_uuid(
        self,
        session: typing.Any,
        placement_uuid: object,
    ) -> sys_uuid.UUID:
        """Resolve the rolling compatibility identity for a v2 placement."""
        row = session.execute(
            """
            SELECT COALESCE(legacy_public_uuid, uuid) AS uuid
            FROM messenger_message_placements
            WHERE project_id = %s AND uuid = %s
            """,
            (self.project_uuid, placement_uuid),
        ).fetchone()
        if row is None:
            raise ra_exceptions.ValidationErrorException()
        return sys_uuid.UUID(str(row["uuid"]))

    def _syncs_compact_read_state(self, session: typing.Any) -> bool:
        """Return whether v2 read actions must update compact compatibility state."""
        return read_state.mode_uses_compact_state(
            read_state.project_mode(session, self.project_uuid)
        )

    def _v2_scope_filters(
        self,
        resource: str,
        filters: dict[str, typing.Any],
    ) -> typing.Any:
        result = filters.copy()
        model = CORE_RESOURCE_MODELS[resource]
        properties = model.properties.properties
        if "project_id" in properties:
            result["project_id"] = dm_filters.EQ(self.project_uuid)
        if "visible" in properties:
            result["visible"] = dm_filters.EQ(True)
        if "viewer_user_uuid" in properties:
            result["viewer_user_uuid"] = dm_filters.EQ(self.user_uuid)
        elif resource in resource_projection.USER_SCOPED_RESOURCES:
            result["user_uuid"] = dm_filters.EQ(self.user_uuid)
        return result

    def filter_resources(
        self,
        resource: str,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]:
        if resource not in CORE_RESOURCES:
            return super().filter_resources(resource, filters, order_by, limit)
        rows = CORE_RESOURCE_MODELS[resource].objects.get_all(
            filters=self._v2_scope_filters(resource, filters),
            order_by=order_by,
            limit=limit,
        )
        return [_public(row, resource) for row in rows]

    def filter_message_page(
        self,
        filters: dict[str, typing.Any],
        marker_uuid: sys_uuid.UUID | None,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        scoped_filters = self._v2_scope_filters("messages", filters)
        marker = None
        if marker_uuid is not None:
            marker = self._message(marker_uuid)
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
        query: dict[str, typing.Any] = {
            "filters": scoped_filters,
            "order_by": {
                "created_at": sort_direction,
                "uuid": sort_direction,
            },
        }
        if limit is not None:
            query["limit"] = limit
        rows = v2_models.WorkspaceUserMessage.objects.get_all(**query)
        return [_public(row, "messages") for row in rows]

    def get_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any]:
        if resource not in CORE_RESOURCES:
            return super().get_resource(resource, resource_uuid)
        if resource == "messages":
            return _public(self._message(resource_uuid), resource)
        row = CORE_RESOURCE_MODELS[resource].objects.get_one(
            filters=self._v2_scope_filters(
                resource,
                {"uuid": dm_filters.EQ(resource_uuid)},
            )
        )
        return _public(row, resource)

    def _enqueue(
        self,
        event_kind: str,
        scope_kind: str,
        scope_key: str,
        payload: dict[str, typing.Any],
    ) -> sys_uuid.UUID:
        event_uuid = sys_uuid.uuid4()
        contexts.Context().get_session().execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                event_uuid,
                self.project_uuid,
                event_kind,
                scope_kind,
                scope_key,
                json.dumps(resource_projection.simple(payload)),
            ),
        )
        return event_uuid

    def _enqueue_stream_snapshot(
        self,
        stream_uuid: object,
        *,
        source_kind: str,
    ) -> None:
        self._enqueue(
            "delivery_snapshot_event",
            "resource",
            f"{self.project_uuid}:stream:{stream_uuid}",
            {
                "source_kind": source_kind,
                "resource_kind": "stream",
                "resource_uuid": stream_uuid,
                "stream_uuid": stream_uuid,
            },
        )

    def _enqueue_folder_projections(
        self,
        *,
        user_uuid: object,
        stream_uuid: object,
        private: bool,
        source_kind: str,
    ) -> None:
        session = contexts.Context().get_session()
        folder_uuids = {
            helpers.ALL_CHATS_FOLDER_UUID,
            helpers.PERSONAL_FOLDER_UUID if private else helpers.CHANNELS_FOLDER_UUID,
        }
        folder_uuids.update(
            row["folder_uuid"]
            for row in session.execute(
                """
                SELECT DISTINCT folder_uuid
                FROM messenger_folder_items
                WHERE project_id = %s AND user_uuid = %s AND stream_uuid = %s
                """,
                (self.project_uuid, user_uuid, stream_uuid),
            ).fetchall()
        )
        for folder_uuid in sorted(folder_uuids, key=str):
            self._enqueue_folder_projection(
                user_uuid=user_uuid,
                folder_uuid=folder_uuid,
                source_kind=source_kind,
                stream_uuid=stream_uuid,
            )

    def _enqueue_folder_projection(
        self,
        *,
        user_uuid: object,
        folder_uuid: object,
        source_kind: str,
        stream_uuid: object | None = None,
        item_uuid: object | None = None,
    ) -> None:
        payload = {
            "source_kind": source_kind,
            "user_uuid": user_uuid,
            "folder_uuid": folder_uuid,
        }
        if stream_uuid is not None:
            payload["stream_uuid"] = stream_uuid
        if item_uuid is not None:
            payload["item_uuid"] = item_uuid
        self._enqueue(
            "folder_projection",
            "user-folder",
            f"{self.project_uuid}:{user_uuid}:{folder_uuid}",
            payload,
        )

    def _enqueue_counter_projections(
        self,
        *,
        source_kind: str,
        user_uuid: object,
        stream_uuid: object,
        topic_uuid: object,
        placement_uuid: object | None = None,
        emit_message_read: bool = False,
    ) -> None:
        common = {
            "source_kind": source_kind,
            "user_uuid": user_uuid,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
        }
        if placement_uuid is not None:
            common["placement_uuid"] = placement_uuid
        self._enqueue(
            "read_counters",
            "user-stream",
            f"{self.project_uuid}:{user_uuid}:{stream_uuid}",
            common,
        )
        self._enqueue(
            "read_counters",
            "user-topic",
            f"{self.project_uuid}:{user_uuid}:{topic_uuid}",
            {**common, "emit_message_read": emit_message_read},
        )

    def _require_project_user(self, user_uuid: object) -> sys_uuid.UUID:
        value = sys_uuid.UUID(str(user_uuid))
        session = contexts.Context().get_session()
        row = session.execute(
            """
            SELECT 1 FROM messenger_project_users
            WHERE project_id = %s AND user_uuid = %s
            FOR KEY SHARE
            """,
            (self.project_uuid, value),
        ).fetchone()
        if row is None:
            raise ra_exceptions.ValidationErrorException()
        return value

    def _ensure_project_user(self, user_uuid: object) -> None:
        value = sys_uuid.UUID(str(user_uuid))
        session = contexts.Context().get_session()
        session.execute(
            """
            INSERT INTO messenger_project_users (project_id, user_uuid)
            SELECT %s, uuid FROM m_workspace_users WHERE uuid = %s
            ON CONFLICT (project_id, user_uuid) DO UPDATE
            SET updated_at = NOW()
            """,
            (self.project_uuid, value),
        )
        session.execute(
            """
            INSERT INTO messenger_folders (
                uuid, project_id, title, background_color_value,
                system_type, created_at, updated_at
            )
            SELECT template.uuid, %s, template.title, 11184810, 'all',
                   template.created_at, template.created_at
            FROM (
                VALUES
                    ('00000000-0000-0000-0000-000000000000'::uuid,
                     'All chats'::varchar,
                     '2000-01-01 00:00:00'::timestamp),
                    ('00000000-0000-0000-0000-000000000001'::uuid,
                     'Personal'::varchar,
                     '2000-01-01 00:00:01'::timestamp),
                    ('00000000-0000-0000-0000-000000000002'::uuid,
                     'Channels'::varchar,
                     '2000-01-01 00:00:02'::timestamp)
            ) AS template(uuid, title, created_at)
            ON CONFLICT (project_id, uuid) DO NOTHING
            """,
            (self.project_uuid,),
        )
        session.execute(
            """
            INSERT INTO messenger_user_folder_bindings (
                uuid, project_id, user_uuid, folder_uuid, rule,
                created_at, updated_at, snapshot_updated_at
            )
            SELECT messenger_uuid_v5(template.folder_uuid, %s),
                   %s, %s, template.folder_uuid, template.rule,
                   template.created_at, template.created_at,
                   template.created_at AT TIME ZONE current_setting('TIMEZONE')
            FROM (
                VALUES
                    ('00000000-0000-0000-0000-000000000000'::uuid,
                     'all_chats'::varchar,
                     '2000-01-01 00:00:00'::timestamp),
                    ('00000000-0000-0000-0000-000000000001'::uuid,
                     'personal'::varchar,
                     '2000-01-01 00:00:01'::timestamp),
                    ('00000000-0000-0000-0000-000000000002'::uuid,
                     'channels'::varchar,
                     '2000-01-01 00:00:02'::timestamp)
            ) AS template(folder_uuid, rule, created_at)
            ON CONFLICT (project_id, user_uuid, folder_uuid) DO NOTHING
            """,
            (str(value), self.project_uuid, value),
        )

    def sync_iam_identity(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        result = super().sync_iam_identity(values)
        self._ensure_project_user(values["user_uuid"])
        return result

    def _message(self, message_uuid: object) -> v2_models.WorkspaceUserMessage:
        exact = v2_models.WorkspaceUserMessage.objects.get_one_or_none(
            filters=self._v2_scope_filters(
                "messages",
                {"uuid": dm_filters.EQ(message_uuid)},
            )
        )
        if exact is not None:
            return exact
        session = contexts.Context().get_session()
        legacy_placements = session.execute(
            """
            SELECT placement.uuid
            FROM messenger_messages AS message
            JOIN messenger_message_placements AS placement
              ON placement.project_id = message.project_id
             AND placement.message_uuid = message.uuid
            WHERE message.project_id = %s AND message.legacy_public_uuid = %s
              AND placement.legacy_public_uuid = message.legacy_public_uuid
            ORDER BY placement.uuid
            LIMIT 2
            """,
            (self.project_uuid, message_uuid),
        ).fetchall()
        if len(legacy_placements) == 1:
            mapped = v2_models.WorkspaceUserMessage.objects.get_one_or_none(
                filters=self._v2_scope_filters(
                    "messages",
                    {"uuid": dm_filters.EQ(legacy_placements[0]["uuid"])},
                )
            )
            if mapped is not None:
                return mapped
        return v2_models.WorkspaceUserMessage.objects.get_one(
            filters=self._v2_scope_filters(
                "messages",
                {"uuid": dm_filters.EQ(message_uuid)},
            )
        )

    def _stream(self, stream_uuid: object) -> v2_models.WorkspaceUserStream:
        return v2_models.WorkspaceUserStream.objects.get_one(
            filters=self._v2_scope_filters(
                "streams",
                {"uuid": dm_filters.EQ(stream_uuid)},
            )
        )

    def _topic(self, topic_uuid: object) -> v2_models.WorkspaceUserTopic:
        return v2_models.WorkspaceUserTopic.objects.get_one(
            filters=self._v2_scope_filters(
                "stream_topics",
                {"uuid": dm_filters.EQ(topic_uuid)},
            )
        )

    def _folder(self, folder_uuid: object) -> v2_models.WorkspaceUserFolder:
        return v2_models.WorkspaceUserFolder.objects.get_one(
            filters=self._v2_scope_filters(
                "folders", {"uuid": dm_filters.EQ(folder_uuid)}
            )
        )

    def _folder_item(self, item_uuid: object) -> v2_models.WorkspaceUserFolderItem:
        return v2_models.WorkspaceUserFolderItem.objects.get_one(
            filters=self._v2_scope_filters(
                "folder_items", {"uuid": dm_filters.EQ(item_uuid)}
            )
        )

    def _create_stream(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        session = contexts.Context().get_session()
        source_name = values.get("source_name", "native")
        source = _simple_source(values.get("source", {"kind": "native"}))
        if source_name != "native":
            raise ra_exceptions.ValidationErrorException()
        direct_user_uuid = values.get("direct_user_uuid")
        direct_user = (
            None if direct_user_uuid is None else sys_uuid.UUID(str(direct_user_uuid))
        )
        if direct_user is not None:
            direct_user = self._require_project_user(direct_user)
        private = bool(values.get("private", False) or direct_user is not None)
        if direct_user is not None and values.get("private") is False:
            raise ra_exceptions.ValidationErrorException()
        if direct_user is not None:
            stream_uuid = helpers.deterministic_direct_stream_uuid(
                self.project_uuid,
                self.user_uuid,
                direct_user,
            )
            session.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (stream_uuid,),
            )
            private_index = ":".join(sorted((str(self.user_uuid), str(direct_user))))
            existing = v2_models.WorkspaceUserStream.objects.get_one_or_none(
                filters=self._v2_scope_filters(
                    "streams", {"uuid": dm_filters.EQ(stream_uuid)}
                )
            )
            if existing is not None:
                if existing.source_name != "native" or not existing.private:
                    raise ra_exceptions.ValidationErrorException()
                return _public(existing, "streams")
            participants = tuple(sorted({self.user_uuid, direct_user}, key=str))
        else:
            stream_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
            session.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (stream_uuid,),
            )
            private_index = None
            participants = (self.user_uuid,)
            existing_row = session.execute(
                """
                SELECT project_id, owner_uuid, source_name, source, private,
                       direct_user_uuid, name, description
                FROM messenger_streams
                WHERE uuid = %s
                """,
                (stream_uuid,),
            ).fetchone()
            if existing_row is not None:
                expected_description = values.get("description")
                if (
                    existing_row["project_id"] != self.project_uuid
                    or existing_row["owner_uuid"] != self.user_uuid
                    or existing_row["source_name"] != source_name
                    or existing_row["source"] != source
                    or existing_row["private"] != private
                    or existing_row["direct_user_uuid"] is not None
                    or existing_row["name"] != values["name"]
                    or existing_row["description"] != expected_description
                ):
                    raise ra_exceptions.ValidationErrorException()
                return _public(self._stream(stream_uuid), "streams")
        topic_uuid = sys_uuid.uuid4()
        now = session.execute("SELECT NOW() AS value", ()).fetchone()["value"]
        session.execute(
            """
            INSERT INTO messenger_streams (
                uuid, project_id, name, description, owner_uuid,
                source_name, source, invite_only, announce,
                direct_user_uuid, private, private_index, color,
                default_topic_uuid, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                stream_uuid,
                self.project_uuid,
                values["name"],
                values.get("description"),
                self.user_uuid,
                source_name,
                json.dumps(source),
                values.get("invite_only", False),
                values.get("announce", False),
                direct_user,
                private,
                private_index,
                values.get("color", base.random_color()),
                topic_uuid,
                now,
                now,
            ),
        )
        for participant in participants:
            session.execute(
                """
                INSERT INTO messenger_stream_bindings (
                    uuid, project_id, stream_uuid, user_uuid, who_uuid,
                    active, membership_generation, role,
                    notification_mode, notification_updated_at,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, true, 1, 'owner',
                          'all_messages', %s, %s, %s)
                """,
                (
                    sys_uuid.uuid4(),
                    self.project_uuid,
                    stream_uuid,
                    participant,
                    self.user_uuid,
                    now,
                    now,
                    now,
                ),
            )
        session.execute(
            """
            INSERT INTO messenger_topics (
                uuid, project_id, stream_uuid, name, color,
                source_name, source, created_at, updated_at
            ) VALUES (%s, %s, %s, 'General Topic', %s,
                      'native', '{"kind":"native"}'::jsonb, %s, %s)
            """,
            (
                topic_uuid,
                self.project_uuid,
                stream_uuid,
                base.random_color(),
                now,
                now,
            ),
        )
        for participant in participants:
            session.execute(
                """
                INSERT INTO messenger_user_topic_bindings (
                    uuid, project_id, user_uuid, topic_uuid,
                    notification_mode, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'default', %s, %s)
                """,
                (
                    sys_uuid.uuid5(topic_uuid, str(participant)),
                    self.project_uuid,
                    participant,
                    topic_uuid,
                    now,
                    now,
                ),
            )
        self._enqueue(
            "delivery_snapshot_event",
            "resource",
            f"{self.project_uuid}:stream:{stream_uuid}",
            {
                "source_kind": "stream.created",
                "resource_kind": "stream",
                "resource_uuid": stream_uuid,
                "recipients": participants,
            },
        )
        self._enqueue(
            "topic_state_projection",
            "topic",
            f"{self.project_uuid}:{topic_uuid}",
            {
                "source_kind": "topic.created",
                "topic_uuid": topic_uuid,
            },
        )
        for participant in participants:
            self._enqueue_folder_projections(
                user_uuid=participant,
                stream_uuid=stream_uuid,
                private=private,
                source_kind="stream.created",
            )
        return _public(self._stream(stream_uuid), "streams")

    def _create_topic(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        session = contexts.Context().get_session()
        stream = self._stream(values["stream_uuid"])
        topic_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        source = _simple_source(values.get("source", {"kind": "native"}))
        now = session.execute("SELECT NOW() AS value", ()).fetchone()["value"]
        session.execute(
            """
            INSERT INTO messenger_topics (
                uuid, project_id, stream_uuid, name, color,
                source_name, source, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, 'native', %s::jsonb, %s, %s)
            """,
            (
                topic_uuid,
                self.project_uuid,
                stream.uuid,
                values["name"],
                values.get("color", base.random_color()),
                json.dumps(source),
                now,
                now,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_user_topic_bindings (
                uuid, project_id, user_uuid, topic_uuid,
                notification_mode, created_at, updated_at
            )
            SELECT messenger_uuid_v5(%s, binding.user_uuid::text),
                   %s, binding.user_uuid, %s, 'default', %s, %s
            FROM messenger_stream_bindings AS binding
            WHERE binding.project_id = %s
              AND binding.stream_uuid = %s
              AND binding.active
            ON CONFLICT (project_id, user_uuid, topic_uuid) DO NOTHING
            """,
            (
                topic_uuid,
                self.project_uuid,
                topic_uuid,
                now,
                now,
                self.project_uuid,
                stream.uuid,
            ),
        )
        self._enqueue(
            "topic_state_projection",
            "topic",
            f"{self.project_uuid}:{topic_uuid}",
            {"source_kind": "topic.created", "topic_uuid": topic_uuid},
        )
        return _public(self._topic(topic_uuid), "stream_topics")

    def _create_folder(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        session = contexts.Context().get_session()
        folder_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
            (f"{self.project_uuid}:{folder_uuid}",),
        )
        existing = v2_models.WorkspaceUserFolder.objects.get_one_or_none(
            filters=self._v2_scope_filters(
                "folders", {"uuid": dm_filters.EQ(folder_uuid)}
            )
        )
        if existing is not None:
            rule = session.execute(
                """
                SELECT rule FROM messenger_user_folder_bindings
                WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
                """,
                (self.project_uuid, self.user_uuid, folder_uuid),
            ).fetchone()["rule"]
            if (
                rule != "custom"
                or existing.title != values["title"]
                or existing.background_color_value
                != values.get("background_color_value")
            ):
                raise ra_exceptions.ValidationErrorException()
            return _public(existing, "folders")
        now = session.execute("SELECT NOW() AS value", ()).fetchone()["value"]
        session.execute(
            """
            INSERT INTO messenger_folders (
                uuid, project_id, title, background_color_value,
                system_type, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 'created', %s, %s)
            """,
            (
                folder_uuid,
                self.project_uuid,
                values["title"],
                values.get("background_color_value"),
                now,
                now,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_user_folder_bindings (
                uuid, project_id, user_uuid, folder_uuid, rule,
                created_at, updated_at, snapshot_updated_at
            ) VALUES (
                messenger_uuid_v5(%s, %s), %s, %s, %s, 'custom', %s, %s, %s
            )
            """,
            (
                folder_uuid,
                str(self.user_uuid),
                self.project_uuid,
                self.user_uuid,
                folder_uuid,
                now,
                now,
                now,
            ),
        )
        self._enqueue_folder_projection(
            user_uuid=self.user_uuid,
            folder_uuid=folder_uuid,
            source_kind="folder.created",
        )
        return _public(self._folder(folder_uuid), "folders")

    def _create_folder_item(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        session = contexts.Context().get_session()
        folder = self._folder(values["folder_uuid"])
        stream = self._stream(values["stream_uuid"])
        rule = session.execute(
            """
            SELECT rule FROM messenger_user_folder_bindings
            WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
            FOR UPDATE
            """,
            (self.project_uuid, self.user_uuid, folder.uuid),
        ).fetchone()["rule"]
        if rule != "custom":
            raise ra_exceptions.ValidationErrorException()
        item_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        now = session.execute("SELECT NOW() AS value", ()).fetchone()["value"]
        inserted = session.execute(
            """
            INSERT INTO messenger_folder_items (
                uuid, project_id, user_uuid, folder_uuid, stream_uuid,
                order_index, pinned_at, chat_type, automatic,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false, %s, %s)
            ON CONFLICT (project_id, user_uuid, folder_uuid, stream_uuid)
            DO NOTHING
            RETURNING uuid
            """,
            (
                item_uuid,
                self.project_uuid,
                self.user_uuid,
                folder.uuid,
                stream.uuid,
                values.get("order_index"),
                values.get("pinned_at"),
                values["chat_type"],
                now,
                now,
            ),
        ).fetchone()
        if inserted is None:
            existing = session.execute(
                """
                SELECT uuid FROM messenger_folder_items
                WHERE project_id = %s AND user_uuid = %s
                  AND folder_uuid = %s AND stream_uuid = %s
                """,
                (self.project_uuid, self.user_uuid, folder.uuid, stream.uuid),
            ).fetchone()
            item_uuid = existing["uuid"]
        self._enqueue_folder_projection(
            user_uuid=self.user_uuid,
            folder_uuid=folder.uuid,
            stream_uuid=stream.uuid,
            item_uuid=item_uuid,
            source_kind="folder_item.created",
        )
        return _public(self._folder_item(item_uuid), "folder_items")

    def create_resource(
        self,
        resource: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        if resource == "folders":
            return self._create_folder(values)
        if resource == "folder_items":
            return self._create_folder_item(values)
        if resource == "streams":
            return self._create_stream(values)
        if resource == "stream_topics":
            provider_target = self._provider_target(
                values["stream_uuid"],
                "topic.create",
            )
            row = self._create_topic(values)
            self._queue_provider_operation(
                operation_kind="topic.create",
                target_type="topic",
                target_uuid=sys_uuid.UUID(str(row["uuid"])),
                stream_uuid=sys_uuid.UUID(str(row["stream_uuid"])),
                payload=row,
                provider_target=provider_target,
            )
            return row
        if resource == "message_reactions":
            message = self._message(values["message_uuid"])
            provider_targets = self._message_provider_targets(
                message,
                "reaction.create",
            )
            row = self._create_reaction(values)
            for provider_target in provider_targets:
                self._queue_provider_operation(
                    operation_kind="reaction.create",
                    target_type="reaction",
                    target_uuid=sys_uuid.UUID(str(row["uuid"])),
                    stream_uuid=message.stream_uuid,
                    payload=row,
                    provider_target=provider_target,
                )
            return row
        return super().create_resource(resource, values)

    def create_message(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        session = contexts.Context().get_session()
        stream = self._stream(values["stream_uuid"])
        provider_targets = self._provider_targets_for_stream(
            stream.uuid,
            "message.create",
        )
        topic_uuid = values.get("topic_uuid") or stream.default_topic_uuid
        if topic_uuid is None:
            raise ra_exceptions.ValidationErrorException()
        topic = self._topic(topic_uuid)
        if topic.stream_uuid != stream.uuid:
            raise ra_exceptions.ValidationErrorException()
        source = _simple_source(values.get("source", {"kind": "native"}))
        canonical_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        placement_uuid = sys_uuid.uuid5(topic.uuid, str(canonical_uuid))
        canonical_payload = resource_projection.simple(values["payload"])
        existing_placement = session.execute(
            """
            SELECT uuid FROM messenger_message_placements
            WHERE project_id = %s AND uuid = %s
            """,
            (self.project_uuid, placement_uuid),
        ).fetchone()
        if existing_placement is not None:
            row = _public(self._message(placement_uuid), "messages")
            self._queue_message_provider_operations(
                "message.create",
                row,
                stream.uuid,
                provider_targets,
            )
            return row
        existing_canonical = session.execute(
            """
            SELECT author_uuid, payload, source_name, source
            FROM messenger_messages
            WHERE project_id = %s AND uuid = %s
            FOR UPDATE
            """,
            (self.project_uuid, canonical_uuid),
        ).fetchone()
        if existing_canonical is not None and (
            existing_canonical["author_uuid"] != self.user_uuid
            or existing_canonical["payload"] != canonical_payload
            or existing_canonical["source_name"] != "native"
            or existing_canonical["source"] != source
        ):
            raise ra_exceptions.ValidationErrorException()
        now = session.execute("SELECT NOW() AS value", ()).fetchone()["value"]
        if existing_canonical is None:
            inserted = session.execute(
                """
                INSERT INTO messenger_messages (
                    uuid, project_id, author_uuid, payload,
                    source_name, source, created_at, updated_at
                ) VALUES (%s, %s, %s, %s::jsonb, 'native', %s::jsonb, %s, %s)
                ON CONFLICT (project_id, uuid) DO NOTHING
                RETURNING uuid
                """,
                (
                    canonical_uuid,
                    self.project_uuid,
                    self.user_uuid,
                    json.dumps(canonical_payload),
                    json.dumps(source),
                    now,
                    now,
                ),
            ).fetchone()
            if inserted is None:
                existing_canonical = session.execute(
                    """
                    SELECT author_uuid, payload, source_name, source
                    FROM messenger_messages
                    WHERE project_id = %s AND uuid = %s
                    FOR UPDATE
                    """,
                    (self.project_uuid, canonical_uuid),
                ).fetchone()
                if existing_canonical is None or (
                    existing_canonical["author_uuid"] != self.user_uuid
                    or existing_canonical["payload"] != canonical_payload
                    or existing_canonical["source_name"] != "native"
                    or existing_canonical["source"] != source
                ):
                    raise ra_exceptions.ValidationErrorException()
        inserted_placement = session.execute(
            """
            INSERT INTO messenger_message_placements (
                uuid, project_id, message_uuid, stream_uuid, topic_uuid,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, message_uuid, stream_uuid, topic_uuid)
            DO NOTHING
            RETURNING uuid
            """,
            (
                placement_uuid,
                self.project_uuid,
                canonical_uuid,
                stream.uuid,
                topic.uuid,
                now,
                now,
            ),
        ).fetchone()
        if inserted_placement is None:
            row = _public(self._message(placement_uuid), "messages")
            self._queue_message_provider_operations(
                "message.create",
                row,
                stream.uuid,
                provider_targets,
            )
            return row
        content = str(canonical_payload.get("content", ""))
        mentioned = f"](urn:user:{str(self.user_uuid).lower()})" in content.lower()
        binding_uuid = sys_uuid.uuid5(placement_uuid, str(self.user_uuid))
        session.execute(
            """
            INSERT INTO messenger_user_message_bindings (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, relation_role, visibility,
                permissions, created_at, updated_at
            )
            SELECT %s, %s, %s, %s, membership_generation,
                   'author', 'visible',
                   '{"read":true,"react":true,"star":true,"pin":true}'::jsonb,
                   %s, %s
            FROM messenger_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
              AND user_uuid = %s AND active
            """,
            (
                binding_uuid,
                self.project_uuid,
                placement_uuid,
                self.user_uuid,
                now,
                now,
                self.project_uuid,
                stream.uuid,
                self.user_uuid,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_user_message_states (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, read_at, mentioned,
                created_at, updated_at
            )
            SELECT %s, %s, %s, %s, membership_generation,
                   %s, %s, %s, %s
            FROM messenger_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
              AND user_uuid = %s AND active
            """,
            (
                binding_uuid,
                self.project_uuid,
                placement_uuid,
                self.user_uuid,
                now,
                mentioned,
                now,
                now,
                self.project_uuid,
                stream.uuid,
                self.user_uuid,
            ),
        )
        self._enqueue(
            "fanout",
            "topic",
            f"{self.project_uuid}:{topic.uuid}",
            {
                "source_kind": "message.created",
                "placement_uuid": placement_uuid,
                "canonical_message_uuid": canonical_uuid,
                "audience_created_before": now,
            },
        )
        self._enqueue(
            "content_mentions",
            "topic",
            f"{self.project_uuid}:{topic.uuid}",
            {
                "source_kind": "message.created",
                "placement_uuid": placement_uuid,
                "message_created_at": now,
            },
        )
        row = _public(self._message(placement_uuid), "messages")
        self._queue_message_provider_operations(
            "message.create",
            row,
            stream.uuid,
            provider_targets,
        )
        return row

    def _queue_message_provider_operations(
        self,
        operation_kind: str,
        row: dict[str, typing.Any],
        stream_uuid: object,
        provider_targets: typing.Iterable[typing.Any],
    ) -> None:
        for provider_target in provider_targets:
            self._queue_provider_operation(
                operation_kind=operation_kind,
                target_type="message",
                target_uuid=sys_uuid.UUID(str(row["uuid"])),
                stream_uuid=stream_uuid,
                payload=row,
                provider_target=provider_target,
            )

    def update_message(
        self,
        message_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        message = self._message(message_uuid)
        if message.author_uuid != self.user_uuid:
            raise ra_exceptions.ValidationErrorException()
        provider_targets = self._message_provider_targets(
            message,
            "message.update",
        )
        session = contexts.Context().get_session()
        session.execute(
            """
            UPDATE messenger_messages
            SET payload = %s::jsonb, updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (
                json.dumps(resource_projection.simple(values["payload"])),
                self.project_uuid,
                message.canonical_message_uuid,
            ),
        )
        placements = session.execute(
            """
            SELECT uuid, topic_uuid
            FROM messenger_message_placements
            WHERE project_id = %s AND message_uuid = %s
            ORDER BY topic_uuid, uuid
            """,
            (self.project_uuid, message.canonical_message_uuid),
        ).fetchall()
        for placement in placements:
            self._enqueue(
                "content_mentions",
                "topic",
                f"{self.project_uuid}:{placement['topic_uuid']}",
                {
                    "source_kind": "message.updated",
                    "placement_uuid": placement["uuid"],
                    "canonical_message_uuid": message.canonical_message_uuid,
                    "message_created_at": message.created_at,
                },
            )
        row = _public(self._message(message_uuid), "messages")
        self._queue_message_provider_operations(
            "message.update",
            row,
            message.stream_uuid,
            provider_targets,
        )
        return row

    def delete_message(
        self,
        message_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        message = self._message(message_uuid)
        if message.author_uuid != self.user_uuid:
            raise ra_exceptions.ValidationErrorException()
        provider_targets = self._message_provider_targets(
            message,
            "message.delete",
        )
        provider_payload = _public(message, "messages")
        session = contexts.Context().get_session()
        placements = session.execute(
            """
            SELECT placement.uuid, placement.stream_uuid, placement.topic_uuid
            FROM messenger_message_placements AS placement
            WHERE placement.project_id = %s AND placement.message_uuid = %s
            ORDER BY placement.uuid
            """,
            (self.project_uuid, message.canonical_message_uuid),
        ).fetchall()
        for placement in placements:
            self._enqueue(
                "delivery_snapshot_event",
                "message",
                f"{self.project_uuid}:{message.canonical_message_uuid}",
                {
                    "source_kind": "message.deleted",
                    "placement": dict(placement),
                    "canonical_message_uuid": message.canonical_message_uuid,
                    "message_created_at": message.created_at,
                    "author_uuid": message.author_uuid,
                    "source_name": message.source_name,
                    "source": resource_projection.simple(message.source),
                },
            )
        session.execute(
            """
            UPDATE messenger_messages
            SET deleted_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (self.project_uuid, message.canonical_message_uuid),
        )
        self._queue_message_provider_operations(
            "message.delete",
            provider_payload,
            message.stream_uuid,
            provider_targets,
        )
        return None

    def _create_reaction(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        message = self._message(values["message_uuid"])
        reaction_uuid = sys_uuid.UUID(str(values.get("uuid") or sys_uuid.uuid4()))
        session = contexts.Context().get_session()
        existing = session.execute(
            """
            SELECT uuid, canonical_message_uuid, placement_uuid,
                   user_uuid, emoji_name
            FROM messenger_message_reaction_facts
            WHERE project_id = %s AND uuid = %s
            """,
            (self.project_uuid, reaction_uuid),
        ).fetchone()
        if existing is not None:
            if (
                existing["canonical_message_uuid"] != message.canonical_message_uuid
                or existing["placement_uuid"] != message.uuid
                or existing["user_uuid"] != self.user_uuid
                or existing["emoji_name"] != values["emoji_name"]
            ):
                raise ra_exceptions.ValidationErrorException()
            return self.get_resource("message_reactions", reaction_uuid)
        inserted = session.execute(
            """
            INSERT INTO messenger_message_reaction_facts (
                uuid, project_id, canonical_message_uuid, placement_uuid,
                user_uuid, emoji_name
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING uuid
            """,
            (
                reaction_uuid,
                self.project_uuid,
                message.canonical_message_uuid,
                message.uuid,
                self.user_uuid,
                values["emoji_name"],
            ),
        ).fetchone()
        if inserted is None:
            reaction_uuid = session.execute(
                """
                SELECT uuid FROM messenger_message_reaction_facts
                WHERE project_id = %s AND canonical_message_uuid = %s
                  AND user_uuid = %s AND emoji_name = %s
                """,
                (
                    self.project_uuid,
                    message.canonical_message_uuid,
                    self.user_uuid,
                    values["emoji_name"],
                ),
            ).fetchone()["uuid"]
            return self.get_resource("message_reactions", reaction_uuid)
        reaction = self.get_resource("message_reactions", reaction_uuid)
        self._enqueue(
            "reaction_snapshot",
            "message",
            f"{self.project_uuid}:{message.canonical_message_uuid}",
            {
                "source_kind": "message_reaction.created",
                "reaction_uuid": reaction_uuid,
                "placement_uuid": message.uuid,
                "reaction": reaction,
                "emit_reaction_event": True,
            },
        )
        return reaction

    def update_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        session = contexts.Context().get_session()
        if resource == "folders":
            folder = self._folder(resource_uuid)
            rule = session.execute(
                """
                SELECT rule FROM messenger_user_folder_bindings
                WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
                FOR UPDATE
                """,
                (self.project_uuid, self.user_uuid, folder.uuid),
            ).fetchone()["rule"]
            if rule != "custom":
                raise ra_exceptions.ValidationErrorException()
            allowed = {"title", "background_color_value"}
            updates = {name: values[name] for name in allowed if name in values}
            if not updates or set(values) - allowed:
                raise ra_exceptions.ValidationErrorException()
            assignments = ", ".join(f'"{name}" = %s' for name in updates)
            session.execute(
                f"UPDATE messenger_folders SET {assignments}, updated_at = NOW() "
                "WHERE project_id = %s AND uuid = %s",
                (*updates.values(), self.project_uuid, folder.uuid),
            )
            self._enqueue_folder_projection(
                user_uuid=self.user_uuid,
                folder_uuid=folder.uuid,
                source_kind="folder.updated",
            )
            return _public(self._folder(folder.uuid), "folders")
        if resource == "streams":
            stream = self._stream(resource_uuid)
            provider_target = self._provider_target(
                stream.uuid,
                "stream.update",
            )
            forbidden = {"source_name", "source", "direct_user_uuid", "private_index"}
            if forbidden.intersection(values):
                raise ra_exceptions.ValidationErrorException()
            allowed = {"name", "description", "invite_only", "announce", "color"}
            updates = {name: values[name] for name in allowed if name in values}
            if not updates:
                raise ra_exceptions.ValidationErrorException()
            assignments = ", ".join(f'"{name}" = %s' for name in updates)
            session.execute(
                f"UPDATE messenger_streams SET {assignments}, updated_at = NOW() "
                "WHERE project_id = %s AND uuid = %s",
                (*updates.values(), self.project_uuid, stream.uuid),
            )
            self._enqueue_stream_snapshot(
                stream.uuid,
                source_kind="stream.updated",
            )
            row = _public(self._stream(stream.uuid), "streams")
            self._queue_provider_operation(
                operation_kind="stream.update",
                target_type="stream",
                target_uuid=stream.uuid,
                stream_uuid=stream.uuid,
                payload=row,
                provider_target=provider_target,
            )
            return row
        if resource == "stream_topics":
            topic = self._topic(resource_uuid)
            provider_target = self._provider_target(
                topic.stream_uuid,
                "topic.update",
            )
            allowed = {"name", "color"}
            updates = {name: values[name] for name in allowed if name in values}
            if not updates or set(values) - allowed:
                raise ra_exceptions.ValidationErrorException()
            assignments = ", ".join(f'"{name}" = %s' for name in updates)
            session.execute(
                f"UPDATE messenger_topics SET {assignments}, updated_at = NOW() "
                "WHERE project_id = %s AND uuid = %s",
                (*updates.values(), self.project_uuid, topic.uuid),
            )
            self._enqueue(
                "topic_state_projection",
                "topic",
                f"{self.project_uuid}:{topic.uuid}",
                {"source_kind": "topic.updated", "topic_uuid": topic.uuid},
            )
            row = _public(self._topic(topic.uuid), "stream_topics")
            self._queue_provider_operation(
                operation_kind="topic.update",
                target_type="topic",
                target_uuid=topic.uuid,
                stream_uuid=topic.stream_uuid,
                payload=row,
                provider_target=provider_target,
            )
            return row
        if resource == "message_reactions":
            reaction = v2_models.WorkspaceMessageReactionView.objects.get_one(
                filters=self._v2_scope_filters(
                    resource, {"uuid": dm_filters.EQ(resource_uuid)}
                )
            )
            if reaction.user_uuid != self.user_uuid:
                raise ra_exceptions.ValidationErrorException()
            old_message = self._message(reaction.message_uuid)
            provider_targets = self._message_provider_targets(
                old_message,
                "reaction.update",
            )
            message = self._message(values.get("message_uuid", reaction.message_uuid))
            session.execute(
                """
                UPDATE messenger_message_reaction_facts
                SET canonical_message_uuid = %s, placement_uuid = %s,
                    emoji_name = %s, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (
                    message.canonical_message_uuid,
                    message.uuid,
                    values.get("emoji_name", reaction.emoji_name),
                    self.project_uuid,
                    reaction.uuid,
                ),
            )
            updated_reaction = self.get_resource(resource, reaction.uuid)
            self._enqueue(
                "reaction_snapshot",
                "message",
                f"{self.project_uuid}:{message.canonical_message_uuid}",
                {
                    "source_kind": "message_reaction.updated",
                    "reaction_uuid": reaction.uuid,
                    "placement_uuid": message.uuid,
                    "reaction": updated_reaction,
                    "old_message_uuid": old_message.uuid,
                    "old_emoji_name": reaction.emoji_name,
                    "old_source_name": old_message.source_name,
                    "old_source": resource_projection.simple(old_message.source),
                    "emit_reaction_event": True,
                },
            )
            if old_message.canonical_message_uuid != message.canonical_message_uuid:
                self._enqueue(
                    "reaction_snapshot",
                    "message",
                    f"{self.project_uuid}:{old_message.canonical_message_uuid}",
                    {
                        "source_kind": "message_reaction.updated_old",
                        "reaction_uuid": reaction.uuid,
                        "placement_uuid": old_message.uuid,
                        "emit_reaction_event": False,
                    },
                )
            provider_payload = dict(updated_reaction)
            provider_payload.update(
                {
                    "previous_message_uuid": str(reaction.message_uuid),
                    "previous_emoji_name": reaction.emoji_name,
                }
            )
            for provider_target in provider_targets:
                self._queue_provider_operation(
                    operation_kind="reaction.update",
                    target_type="reaction",
                    target_uuid=reaction.uuid,
                    stream_uuid=old_message.stream_uuid,
                    payload=provider_payload,
                    provider_target=provider_target,
                )
            return updated_reaction
        if resource == "stream_bindings":
            binding = v2_models.WorkspaceStreamBindingView.objects.get_one(
                filters=self._v2_scope_filters(
                    resource, {"uuid": dm_filters.EQ(resource_uuid)}
                )
            )
            stream = self._stream(binding.stream_uuid)
            if stream.direct_user_uuid is not None:
                raise ra_exceptions.ValidationErrorException()
            allowed = {"role", "notification_mode"}
            updates = {name: values[name] for name in allowed if name in values}
            assignments = ", ".join(f'"{name}" = %s' for name in updates)
            if not assignments:
                raise ra_exceptions.ValidationErrorException()
            session.execute(
                f"UPDATE messenger_stream_bindings SET {assignments}, "
                "notification_updated_at = NOW(), updated_at = NOW() "
                "WHERE project_id = %s AND uuid = %s",
                (*updates.values(), self.project_uuid, binding.uuid),
            )
            self._enqueue(
                "delivery_snapshot_event",
                "resource",
                f"{self.project_uuid}:stream-binding:{binding.uuid}",
                {
                    "source_kind": "stream_binding.updated",
                    "binding_uuid": binding.uuid,
                    "stream_uuid": binding.stream_uuid,
                },
            )
            if "notification_mode" in updates:
                topics = session.execute(
                    """
                    SELECT uuid FROM messenger_topics
                    WHERE project_id = %s AND stream_uuid = %s
                    ORDER BY created_at, uuid
                    """,
                    (self.project_uuid, binding.stream_uuid),
                ).fetchall()
                if topics:
                    self._enqueue(
                        "read_counters",
                        "user-stream",
                        f"{self.project_uuid}:{binding.user_uuid}:"
                        f"{binding.stream_uuid}",
                        {
                            "source_kind": "stream_binding.updated",
                            "user_uuid": binding.user_uuid,
                            "stream_uuid": binding.stream_uuid,
                            "topic_uuid": topics[0]["uuid"],
                        },
                    )
                for topic in topics:
                    self._enqueue(
                        "read_counters",
                        "user-topic",
                        f"{self.project_uuid}:{binding.user_uuid}:{topic['uuid']}",
                        {
                            "source_kind": "stream_binding.updated",
                            "user_uuid": binding.user_uuid,
                            "stream_uuid": binding.stream_uuid,
                            "topic_uuid": topic["uuid"],
                        },
                    )
            return self.get_resource(resource, binding.uuid)
        return super().update_resource(resource, resource_uuid, values)

    def delete_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        session = contexts.Context().get_session()
        if resource == "folder_items":
            item = self._folder_item(resource_uuid)
            deleted = session.execute(
                """
                DELETE FROM messenger_folder_items
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                  AND NOT automatic
                RETURNING uuid
                """,
                (self.project_uuid, self.user_uuid, item.uuid),
            ).fetchone()
            if deleted is None:
                raise ra_exceptions.ValidationErrorException()
            self._enqueue_folder_projection(
                user_uuid=self.user_uuid,
                folder_uuid=item.folder_uuid,
                stream_uuid=item.stream_uuid,
                item_uuid=item.uuid,
                source_kind="folder_item.deleted",
            )
            return None
        if resource == "folders":
            folder = self._folder(resource_uuid)
            binding = session.execute(
                """
                SELECT rule FROM messenger_user_folder_bindings
                WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
                FOR UPDATE
                """,
                (self.project_uuid, self.user_uuid, folder.uuid),
            ).fetchone()
            if binding["rule"] != "custom":
                raise ra_exceptions.ValidationErrorException()
            self._enqueue_folder_projection(
                user_uuid=self.user_uuid,
                folder_uuid=folder.uuid,
                source_kind="folder.deleted",
            )
            session.execute(
                """
                DELETE FROM messenger_user_folder_bindings
                WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
                """,
                (self.project_uuid, self.user_uuid, folder.uuid),
            )
            session.execute(
                """
                DELETE FROM messenger_folders AS folder
                WHERE folder.project_id = %s AND folder.uuid = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM messenger_user_folder_bindings AS binding
                      WHERE binding.project_id = folder.project_id
                        AND binding.folder_uuid = folder.uuid
                  )
                """,
                (self.project_uuid, folder.uuid),
            )
            return None
        if resource == "message_reactions":
            reaction = v2_models.WorkspaceMessageReactionView.objects.get_one(
                filters=self._v2_scope_filters(
                    resource, {"uuid": dm_filters.EQ(resource_uuid)}
                )
            )
            if reaction.user_uuid != self.user_uuid:
                raise ra_exceptions.ValidationErrorException()
            message = self._message(reaction.message_uuid)
            provider_targets = self._message_provider_targets(
                message,
                "reaction.delete",
            )
            provider_payload = _public(reaction, resource)
            canonical = session.execute(
                """
                SELECT canonical_message_uuid
                FROM messenger_message_reaction_facts
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, reaction.uuid),
            ).fetchone()["canonical_message_uuid"]
            session.execute(
                """
                DELETE FROM messenger_message_reaction_facts
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, reaction.uuid),
            )
            self._enqueue(
                "reaction_snapshot",
                "message",
                f"{self.project_uuid}:{canonical}",
                {
                    "source_kind": "message_reaction.deleted",
                    "reaction_uuid": reaction.uuid,
                    "placement_uuid": reaction.message_uuid,
                    "reaction": _public(reaction, resource),
                    "emit_reaction_event": True,
                },
            )
            for provider_target in provider_targets:
                self._queue_provider_operation(
                    operation_kind="reaction.delete",
                    target_type="reaction",
                    target_uuid=reaction.uuid,
                    stream_uuid=message.stream_uuid,
                    payload=provider_payload,
                    provider_target=provider_target,
                )
            return None
        if resource == "stream_bindings":
            binding = v2_models.WorkspaceStreamBindingView.objects.get_one(
                filters=self._v2_scope_filters(
                    resource, {"uuid": dm_filters.EQ(resource_uuid)}
                )
            )
            stream = self._stream(binding.stream_uuid)
            if stream.direct_user_uuid is not None:
                raise ra_exceptions.ValidationErrorException()
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
                provider_target = None
            binding_snapshot = _public(binding, resource)
            generation = session.execute(
                """
                SELECT membership_generation
                FROM messenger_stream_bindings
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, binding.uuid),
            ).fetchone()
            binding_snapshot["membership_generation"] = int(
                generation["membership_generation"]
            )
            self._enqueue(
                "delivery_snapshot_event",
                "resource",
                f"{self.project_uuid}:stream:{stream.uuid}:{binding.user_uuid}",
                {
                    "source_kind": "stream.deleted",
                    "stream_uuid": stream.uuid,
                    "source_name": stream.source_name,
                    "source": resource_projection.simple(stream.source),
                    "recipients": [binding.user_uuid],
                },
            )
            self._enqueue(
                "delivery_snapshot_event",
                "resource",
                f"{self.project_uuid}:stream-binding:{binding.uuid}",
                {
                    "source_kind": "stream_binding.deleted",
                    "binding": binding_snapshot,
                    "stream_uuid": binding.stream_uuid,
                    "exclude_user_uuid": binding.user_uuid,
                },
            )
            self._enqueue_folder_projections(
                user_uuid=binding.user_uuid,
                stream_uuid=stream.uuid,
                private=stream.private,
                source_kind="stream.deleted",
            )
            session.execute(
                """
                UPDATE messenger_stream_bindings
                SET active = false,
                    membership_generation = membership_generation + 1,
                    updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, binding.uuid),
            )
            self._queue_provider_operation(
                operation_kind="membership.remove",
                target_type="stream_binding",
                target_uuid=binding.uuid,
                stream_uuid=binding.stream_uuid,
                payload=binding_snapshot,
                provider_target=provider_target,
            )
            return None
        if resource == "stream_topics":
            topic = self._topic(resource_uuid)
            provider_target = self._provider_target(
                topic.stream_uuid,
                "topic.delete",
            )
            provider_payload = _public(topic, resource)
            self._enqueue(
                "delivery_snapshot_event",
                "topic",
                f"{self.project_uuid}:{topic.uuid}",
                {
                    "source_kind": "topic.deleted",
                    "topic_uuid": topic.uuid,
                    "stream_uuid": topic.stream_uuid,
                    "source_name": topic.source_name,
                    "source": resource_projection.simple(topic.source),
                },
            )
            cleared_default = session.execute(
                """
                UPDATE messenger_streams
                SET default_topic_uuid = NULL, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                  AND default_topic_uuid = %s
                RETURNING uuid
                """,
                (self.project_uuid, topic.stream_uuid, topic.uuid),
            ).fetchone()
            if cleared_default is not None:
                self._enqueue_stream_snapshot(
                    topic.stream_uuid,
                    source_kind="stream.updated",
                )
            session.execute(
                "UPDATE messenger_topics SET deleted_at = NOW() "
                "WHERE project_id = %s AND uuid = %s",
                (self.project_uuid, topic.uuid),
            )
            self._queue_provider_operation(
                operation_kind="topic.delete",
                target_type="topic",
                target_uuid=topic.uuid,
                stream_uuid=topic.stream_uuid,
                payload=provider_payload,
                provider_target=provider_target,
            )
            return None
        if resource == "streams":
            stream = self._stream(resource_uuid)
            if stream.direct_user_uuid is not None:
                raise ra_exceptions.ValidationErrorException()
            provider_target = self._provider_target(
                stream.uuid,
                "stream.delete",
            )
            self._enqueue(
                "delivery_snapshot_event",
                "resource",
                f"{self.project_uuid}:stream:{stream.uuid}",
                {
                    "source_kind": "stream.deleted",
                    "stream_uuid": stream.uuid,
                    "source_name": stream.source_name,
                    "source": resource_projection.simple(stream.source),
                    "all_recipients": True,
                    "private": stream.private,
                },
            )
            session.execute(
                "UPDATE messenger_streams SET deleted_at = NOW() "
                "WHERE project_id = %s AND uuid = %s",
                (self.project_uuid, stream.uuid),
            )
            self._queue_provider_operation(
                operation_kind="stream.delete",
                target_type="stream",
                target_uuid=stream.uuid,
                stream_uuid=stream.uuid,
                payload={"uuid": str(stream.uuid)},
                provider_target=provider_target,
            )
            return None
        return super().delete_resource(resource, resource_uuid)

    def _mark_message_state(
        self,
        message_uuid: object,
        field: str,
        value: bool,
    ) -> dict[str, typing.Any]:
        if field not in {"starred", "pinned"}:
            raise ValueError(field)
        message = self._message(message_uuid)
        contexts.Context().get_session().execute(
            f"""
            UPDATE messenger_user_message_states
            SET {field} = %s, updated_at = NOW()
            WHERE project_id = %s AND placement_uuid = %s AND user_uuid = %s
            """,
            (value, self.project_uuid, message.uuid, self.user_uuid),
        )
        self._enqueue(
            "delivery_snapshot_event",
            "message",
            f"{self.project_uuid}:{message.canonical_message_uuid}",
            {
                "source_kind": "message.updated",
                "placement_uuid": message.uuid,
                "recipient_uuid": self.user_uuid,
            },
        )
        return _public(self._message(message.uuid), "messages")

    def _queue_v2_provider_read_snapshot(
        self,
        *,
        stream_uuid: object,
        topic_uuid: object | None,
        target_type: str,
        target_uuid: object,
        candidate_sql: str,
        candidate_values: typing.Sequence[object],
        provider_candidate_sql: str | None = None,
        provider_candidate_values: typing.Sequence[object] | None = None,
    ) -> bool:
        """Snapshot exact unread placements without materializing them in Python."""
        session = contexts.Context().get_session()
        provider_account_uuids = self._lock_provider_read_accounts_for_stream(
            stream_uuid
        )
        changed = (
            session.execute(
                f"SELECT 1 FROM ({candidate_sql}) AS candidate LIMIT 1",
                candidate_values,
            ).fetchone()
            is not None
        )
        if not provider_account_uuids:
            return changed
        callback = self._provider_read_snapshot_callback(
            provider_account_uuids=provider_account_uuids,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            target_type=target_type,
            target_uuid=target_uuid,
        )
        if callback is not None:
            try:
                callback(
                    session,
                    provider_candidate_sql or candidate_sql,
                    (
                        provider_candidate_values
                        if provider_candidate_values is not None
                        else candidate_values
                    ),
                )
            except (
                ra_exceptions.ValidationErrorException,
                storage_exceptions.RecordNotFound,
            ):
                if changed:
                    raise
        return changed

    def perform_action(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        action: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any] | list[dict[str, typing.Any]]:
        resource_uuid = sys_uuid.UUID(str(resource_uuid))
        session = contexts.Context().get_session()
        if resource == "folder_items" and action in {"pin", "unpin"}:
            item = self._folder_item(resource_uuid)
            session.execute(
                """
                UPDATE messenger_folder_items
                SET pinned_at = CASE WHEN %s THEN NOW() ELSE NULL END,
                    updated_at = NOW()
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                """,
                (
                    action == "pin",
                    self.project_uuid,
                    self.user_uuid,
                    item.uuid,
                ),
            )
            self._enqueue_folder_projection(
                user_uuid=self.user_uuid,
                folder_uuid=item.folder_uuid,
                stream_uuid=item.stream_uuid,
                item_uuid=item.uuid,
                source_kind="folder_item.updated",
            )
            return _public(self._folder_item(item.uuid), "folder_items")
        if resource == "stream_bindings" and action == "add_users":
            stream = self._stream(resource_uuid)
            if stream.direct_user_uuid is not None:
                raise ra_exceptions.ValidationErrorException()
            role_user_uuids = {
                role: [self._require_project_user(value) for value in user_uuids]
                for role, user_uuids in values.items()
            }
            current_participants = set(self._stream_participants(stream.uuid))
            participants = current_participants | {
                user_uuid
                for user_uuids in role_user_uuids.values()
                for user_uuid in user_uuids
            }
            self._validate_stream_participants(stream.uuid, tuple(participants))
            new_participants = participants - current_participants
            if new_participants:
                try:
                    provider_target = self._provider_target(
                        stream.uuid,
                        "membership.add",
                    )
                except (
                    ra_exceptions.ValidationErrorException,
                    storage_exceptions.RecordNotFound,
                ):
                    provider_target = None
            else:
                provider_target = None
            now = session.execute("SELECT NOW() AS value", ()).fetchone()["value"]
            created_binding_uuids = []
            changed_binding_uuids = []
            for role, user_uuids in role_user_uuids.items():
                for user_uuid in user_uuids:
                    existing = session.execute(
                        """
                        SELECT uuid, active, membership_generation
                        FROM messenger_stream_bindings
                        WHERE project_id = %s AND stream_uuid = %s
                          AND user_uuid = %s
                        FOR UPDATE
                        """,
                        (self.project_uuid, stream.uuid, user_uuid),
                    ).fetchone()
                    if existing is None:
                        binding_uuid = sys_uuid.uuid4()
                        membership_generation = 1
                        session.execute(
                            """
                            INSERT INTO messenger_stream_bindings (
                                uuid, project_id, stream_uuid, user_uuid,
                                who_uuid, active, membership_generation, role,
                                notification_mode, notification_updated_at,
                                created_at, updated_at
                            ) VALUES (
                                %s, %s, %s, %s, %s, true, 1, %s,
                                'all_messages', %s, %s, %s
                            )
                            """,
                            (
                                binding_uuid,
                                self.project_uuid,
                                stream.uuid,
                                user_uuid,
                                self.user_uuid,
                                role,
                                now,
                                now,
                                now,
                            ),
                        )
                    else:
                        binding_uuid = existing["uuid"]
                        if existing["active"]:
                            created_binding_uuids.append(binding_uuid)
                            continue
                        membership_generation = existing["membership_generation"] + 1
                        session.execute(
                            """
                            UPDATE messenger_stream_bindings
                            SET active = true,
                                membership_generation = membership_generation + 1,
                                membership_started_at = NOW(),
                                role = %s, who_uuid = %s, updated_at = NOW()
                            WHERE project_id = %s AND uuid = %s
                            """,
                            (
                                role,
                                self.user_uuid,
                                self.project_uuid,
                                binding_uuid,
                            ),
                        )
                    created_binding_uuids.append(binding_uuid)
                    changed_binding_uuids.append(binding_uuid)
                    session.execute(
                        """
                        INSERT INTO messenger_user_topic_bindings (
                            uuid, project_id, user_uuid, topic_uuid,
                            notification_mode, created_at, updated_at
                        )
                        SELECT messenger_uuid_v5(topic.uuid, %s),
                               %s, %s, topic.uuid, 'default', %s, %s
                        FROM messenger_topics AS topic
                        WHERE topic.project_id = %s AND topic.stream_uuid = %s
                        ON CONFLICT (project_id, user_uuid, topic_uuid) DO NOTHING
                        """,
                        (
                            str(user_uuid),
                            self.project_uuid,
                            user_uuid,
                            now,
                            now,
                            self.project_uuid,
                            stream.uuid,
                        ),
                    )
                    self._enqueue(
                        "delivery_snapshot_event",
                        "resource",
                        f"{self.project_uuid}:stream:{stream.uuid}:{user_uuid}",
                        {
                            "source_kind": "stream.created",
                            "resource_kind": "stream",
                            "resource_uuid": stream.uuid,
                            "recipients": [user_uuid],
                        },
                    )
                    for topic in session.execute(
                        """
                        SELECT uuid FROM messenger_topics
                        WHERE project_id = %s AND stream_uuid = %s
                        ORDER BY created_at, uuid
                        """,
                        (self.project_uuid, stream.uuid),
                    ).fetchall():
                        self._enqueue(
                            "topic_state_projection",
                            "topic",
                            f"{self.project_uuid}:{topic['uuid']}:{user_uuid}",
                            {
                                "source_kind": "topic.created",
                                "topic_uuid": topic["uuid"],
                                "recipient_uuid": user_uuid,
                            },
                        )
                        self._enqueue(
                            "topic_membership_policy_rebuild",
                            "topic",
                            f"{self.project_uuid}:{topic['uuid']}",
                            {
                                "source_kind": "stream_binding.created",
                                "user_uuid": user_uuid,
                                "membership_generation": membership_generation,
                                "membership_started_at": now,
                                "stream_uuid": stream.uuid,
                                "topic_uuid": topic["uuid"],
                            },
                        )
                    self._enqueue_folder_projections(
                        user_uuid=user_uuid,
                        stream_uuid=stream.uuid,
                        private=stream.private,
                        source_kind="stream.created",
                    )
            if changed_binding_uuids:
                self._enqueue(
                    "delivery_snapshot_event",
                    "resource",
                    f"{self.project_uuid}:stream-bindings:{stream.uuid}",
                    {
                        "source_kind": "stream_bindings.created",
                        "binding_uuids": changed_binding_uuids,
                        "stream_uuid": stream.uuid,
                        "exclude_binding_uuids": changed_binding_uuids,
                    },
                )
            queued_binding_uuids = set()
            for binding_uuid in changed_binding_uuids:
                if binding_uuid in queued_binding_uuids:
                    continue
                queued_binding_uuids.add(binding_uuid)
                self._queue_provider_operation(
                    operation_kind="membership.add",
                    target_type="stream_binding",
                    target_uuid=binding_uuid,
                    stream_uuid=stream.uuid,
                    payload=self.get_resource("stream_bindings", binding_uuid),
                    provider_target=provider_target,
                )
            return [
                self.get_resource("stream_bindings", binding_uuid)
                for binding_uuid in created_binding_uuids
            ]
        if resource == "messages" and action == "read":
            message = self._message(resource_uuid)
            provider_account_uuids = self._lock_provider_read_accounts_for_stream(
                message.stream_uuid
            )
            changed = session.execute(
                """
                UPDATE messenger_user_message_states
                SET read_at = NOW(), updated_at = NOW()
                WHERE project_id = %s AND placement_uuid = %s AND user_uuid = %s
                  AND read_at IS NULL
                RETURNING placement_uuid
                """,
                (self.project_uuid, message.uuid, self.user_uuid),
            ).fetchone()
            if self._syncs_compact_read_state(session):
                read_state.set_message_read(
                    session,
                    self.project_uuid,
                    self.user_uuid,
                    self._legacy_message_uuid(session, message.uuid),
                    True,
                )
            try:
                provider_targets = self._message_provider_targets(
                    message,
                    "read_state.set",
                    account_locked=bool(provider_account_uuids),
                )
            except (
                ra_exceptions.ValidationErrorException,
                storage_exceptions.RecordNotFound,
            ):
                if changed is not None:
                    raise
                provider_targets = ()
            for provider_target in provider_targets:
                self._queue_provider_read(
                    stream_uuid=message.stream_uuid,
                    topic_uuid=message.topic_uuid,
                    message_uuids=(message.uuid,),
                    target_type="message",
                    target_uuid=resource_uuid,
                    provider_target=provider_target,
                )
            self._enqueue_counter_projections(
                source_kind="message.read",
                placement_uuid=message.uuid,
                user_uuid=self.user_uuid,
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                emit_message_read=changed is not None,
            )
            return _public(self._message(message.uuid), "messages")
        if resource == "messages" and action == "read_up_to":
            message = self._message(resource_uuid)
            candidate_sql = """
                SELECT placement.uuid, canonical.created_at
                FROM messenger_user_message_states AS state
                JOIN messenger_message_placements AS placement
                  ON placement.project_id = state.project_id
                 AND placement.uuid = state.placement_uuid
                JOIN messenger_messages AS canonical
                  ON canonical.project_id = placement.project_id
                 AND canonical.uuid = placement.message_uuid
                JOIN messenger_message_placements AS boundary
                  ON boundary.project_id = placement.project_id
                 AND boundary.uuid = %s
                JOIN messenger_messages AS boundary_message
                  ON boundary_message.project_id = boundary.project_id
                 AND boundary_message.uuid = boundary.message_uuid
                WHERE state.project_id = %s AND state.user_uuid = %s
                  AND (%s::boolean OR state.read_at IS NULL)
                  AND placement.topic_uuid = %s
                  AND (canonical.created_at, placement.uuid)
                      <= (boundary_message.created_at, boundary.uuid)
                ORDER BY canonical.created_at, placement.uuid
            """
            candidate_values = (
                message.uuid,
                self.project_uuid,
                self.user_uuid,
                False,
                message.topic_uuid,
            )
            provider_candidate_values = (
                message.uuid,
                self.project_uuid,
                self.user_uuid,
                True,
                message.topic_uuid,
            )
            changed = self._queue_v2_provider_read_snapshot(
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                target_type="message",
                target_uuid=resource_uuid,
                candidate_sql=candidate_sql,
                candidate_values=candidate_values,
                provider_candidate_values=provider_candidate_values,
            )
            session.execute(
                """
                UPDATE messenger_user_message_states AS state
                SET read_at = COALESCE(state.read_at, NOW()), updated_at = NOW()
                FROM messenger_message_placements AS placement,
                     messenger_messages AS canonical,
                     messenger_message_placements AS boundary,
                     messenger_messages AS boundary_message
                WHERE state.project_id = %s AND state.user_uuid = %s
                  AND placement.project_id = state.project_id
                  AND placement.uuid = state.placement_uuid
                  AND placement.topic_uuid = %s
                  AND canonical.project_id = placement.project_id
                  AND canonical.uuid = placement.message_uuid
                  AND boundary.project_id = %s AND boundary.uuid = %s
                  AND boundary_message.project_id = boundary.project_id
                  AND boundary_message.uuid = boundary.message_uuid
                  AND state.read_at IS NULL
                  AND (canonical.created_at, placement.uuid)
                      <= (boundary_message.created_at, boundary.uuid)
                """,
                (
                    self.project_uuid,
                    self.user_uuid,
                    message.topic_uuid,
                    self.project_uuid,
                    message.uuid,
                ),
            )
            if self._syncs_compact_read_state(session):
                # Compatibility rows can be projected in a different physical
                # order from canonical placements. Reuse the canonical tuple
                # boundary instead of comparing legacy timestamps and UUIDs.
                read_state._bulk_mark_read(
                    session,
                    self.project_uuid,
                    self.user_uuid,
                    """
                    EXISTS (
                        SELECT 1
                        FROM messenger_message_placements AS placement
                        JOIN messenger_messages AS canonical
                          ON canonical.project_id = placement.project_id
                         AND canonical.uuid = placement.message_uuid
                        JOIN messenger_user_message_states AS state
                          ON state.project_id = placement.project_id
                         AND state.placement_uuid = placement.uuid
                         AND state.user_uuid = %s
                        WHERE placement.project_id = message.project_id
                          AND COALESCE(
                              placement.legacy_public_uuid,
                              placement.uuid
                          ) = message.uuid
                          AND placement.topic_uuid = %s
                          AND (canonical.created_at, placement.uuid)
                              <= (%s, %s)
                    )
                    """,
                    (
                        self.user_uuid,
                        message.topic_uuid,
                        message.created_at,
                        message.uuid,
                    ),
                )
            self._enqueue_counter_projections(
                source_kind="messages.read",
                placement_uuid=message.uuid,
                user_uuid=self.user_uuid,
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                emit_message_read=changed,
            )
            return _public(self._message(message.uuid), "messages")
        if resource == "messages" and action in {"star", "unstar"}:
            return self._mark_message_state(
                resource_uuid,
                "starred",
                action == "star",
            )
        if resource == "messages" and action in {"pin", "unpin"}:
            return self._mark_message_state(
                resource_uuid,
                "pinned",
                action == "pin",
            )
        if resource == "streams" and action in {"archive", "unarchive"}:
            stream = self._stream(resource_uuid)
            session.execute(
                """
                UPDATE messenger_streams
                SET is_archived = %s, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (action == "archive", self.project_uuid, stream.uuid),
            )
            self._enqueue_stream_snapshot(
                stream.uuid,
                source_kind="stream.updated",
            )
            self._enqueue(
                "folder_projection",
                "stream-folders",
                f"{self.project_uuid}:{stream.uuid}",
                {
                    "source_kind": "stream.updated",
                    "stream_uuid": stream.uuid,
                    "private": stream.private,
                },
            )
            return _public(self._stream(stream.uuid), "streams")
        if resource == "streams" and action == "read":
            stream = self._stream(resource_uuid)
            candidate_sql = """
                SELECT placement.uuid, canonical.created_at
                FROM messenger_user_message_states AS state
                JOIN messenger_message_placements AS placement
                  ON placement.project_id = state.project_id
                 AND placement.uuid = state.placement_uuid
                JOIN messenger_messages AS canonical
                  ON canonical.project_id = placement.project_id
                 AND canonical.uuid = placement.message_uuid
                WHERE state.project_id = %s AND state.user_uuid = %s
                  AND (%s::boolean OR state.read_at IS NULL)
                  AND placement.stream_uuid = %s
                ORDER BY canonical.created_at, placement.uuid
            """
            stream_read_candidate_values = (
                self.project_uuid,
                self.user_uuid,
                False,
                stream.uuid,
            )
            stream_provider_candidate_values = (
                self.project_uuid,
                self.user_uuid,
                True,
                stream.uuid,
            )
            changed = self._queue_v2_provider_read_snapshot(
                stream_uuid=stream.uuid,
                topic_uuid=None,
                target_type="stream",
                target_uuid=resource_uuid,
                candidate_sql=candidate_sql,
                candidate_values=stream_read_candidate_values,
                provider_candidate_values=stream_provider_candidate_values,
            )
            session.execute(
                """
                UPDATE messenger_user_message_states AS state
                SET read_at = COALESCE(state.read_at, NOW()), updated_at = NOW()
                FROM messenger_message_placements AS placement
                WHERE state.project_id = %s AND state.user_uuid = %s
                  AND placement.project_id = state.project_id
                  AND placement.uuid = state.placement_uuid
                  AND placement.stream_uuid = %s
                  AND state.read_at IS NULL
                """,
                (self.project_uuid, self.user_uuid, stream.uuid),
            )
            if self._syncs_compact_read_state(session):
                read_state.read_stream(
                    session,
                    self.project_uuid,
                    self.user_uuid,
                    stream.uuid,
                    collect_message_rows=False,
                )
            topics = session.execute(
                """
                SELECT uuid FROM messenger_topics
                WHERE project_id = %s AND stream_uuid = %s
                ORDER BY created_at, uuid
                """,
                (self.project_uuid, stream.uuid),
            ).fetchall()
            if topics:
                self._enqueue(
                    "read_counters",
                    "user-stream",
                    f"{self.project_uuid}:{self.user_uuid}:{stream.uuid}",
                    {
                        "source_kind": "stream.read",
                        "user_uuid": self.user_uuid,
                        "stream_uuid": stream.uuid,
                        "topic_uuid": topics[0]["uuid"],
                    },
                )
            for topic in topics:
                self._enqueue(
                    "read_counters",
                    "user-topic",
                    f"{self.project_uuid}:{self.user_uuid}:{topic['uuid']}",
                    {
                        "source_kind": "stream.read",
                        "user_uuid": self.user_uuid,
                        "stream_uuid": stream.uuid,
                        "topic_uuid": topic["uuid"],
                    },
                )
            return _public(self._stream(stream.uuid), "streams")
        if resource == "streams" and action == "notifications":
            stream = self._stream(resource_uuid)
            provider_target = self._provider_target(
                stream.uuid,
                "stream.notification.update",
            )
            session.execute(
                """
                UPDATE messenger_stream_bindings
                SET notification_mode = %s,
                    notification_updated_at = NOW(), updated_at = NOW()
                WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
                  AND active
                """,
                (
                    values["notification_mode"],
                    self.project_uuid,
                    stream.uuid,
                    self.user_uuid,
                ),
            )
            notification = session.execute(
                """
                SELECT notification_mode, notification_updated_at
                FROM messenger_stream_bindings
                WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
                  AND active
                """,
                (self.project_uuid, stream.uuid, self.user_uuid),
            ).fetchone()
            topics = session.execute(
                """
                SELECT uuid FROM messenger_topics
                WHERE project_id = %s AND stream_uuid = %s
                ORDER BY created_at, uuid
                """,
                (self.project_uuid, stream.uuid),
            ).fetchall()
            if topics:
                self._enqueue(
                    "read_counters",
                    "user-stream",
                    f"{self.project_uuid}:{self.user_uuid}:{stream.uuid}",
                    {
                        "source_kind": "stream.updated",
                        "user_uuid": self.user_uuid,
                        "stream_uuid": stream.uuid,
                        "topic_uuid": topics[0]["uuid"],
                    },
                )
            for topic in topics:
                self._enqueue(
                    "read_counters",
                    "user-topic",
                    f"{self.project_uuid}:{self.user_uuid}:{topic['uuid']}",
                    {
                        "source_kind": "stream.updated",
                        "user_uuid": self.user_uuid,
                        "stream_uuid": stream.uuid,
                        "topic_uuid": topic["uuid"],
                    },
                )
            row = _public(self._stream(stream.uuid), "streams")
            self._queue_provider_operation(
                operation_kind="stream.notification.update",
                target_type="stream",
                target_uuid=stream.uuid,
                stream_uuid=stream.uuid,
                payload={
                    "uuid": str(stream.uuid),
                    "stream_uuid": str(stream.uuid),
                    "user_uuid": str(self.user_uuid),
                    "notification_mode": notification["notification_mode"],
                    "notification_updated_at": notification["notification_updated_at"],
                },
                provider_target=provider_target,
            )
            return row
        if resource == "stream_topics" and action == "toggle_done":
            topic = self._topic(resource_uuid)
            session.execute(
                """
                UPDATE messenger_topics
                SET is_done = NOT is_done, version = version + 1,
                    updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, topic.uuid),
            )
            self._enqueue(
                "topic_state_projection",
                "topic",
                f"{self.project_uuid}:{topic.uuid}",
                {"source_kind": "topic.updated", "topic_uuid": topic.uuid},
            )
            return _public(self._topic(topic.uuid), "stream_topics")
        if resource == "stream_topics" and action == "read":
            topic = self._topic(resource_uuid)
            candidate_sql = """
                SELECT placement.uuid, canonical.created_at
                FROM messenger_user_message_states AS state
                JOIN messenger_message_placements AS placement
                  ON placement.project_id = state.project_id
                 AND placement.uuid = state.placement_uuid
                JOIN messenger_messages AS canonical
                  ON canonical.project_id = placement.project_id
                 AND canonical.uuid = placement.message_uuid
                WHERE state.project_id = %s AND state.user_uuid = %s
                  AND (%s::boolean OR state.read_at IS NULL)
                  AND placement.topic_uuid = %s
                ORDER BY canonical.created_at, placement.uuid
            """
            topic_read_candidate_values = (
                self.project_uuid,
                self.user_uuid,
                False,
                topic.uuid,
            )
            topic_provider_candidate_values = (
                self.project_uuid,
                self.user_uuid,
                True,
                topic.uuid,
            )
            changed = self._queue_v2_provider_read_snapshot(
                stream_uuid=topic.stream_uuid,
                topic_uuid=topic.uuid,
                target_type="topic",
                target_uuid=resource_uuid,
                candidate_sql=candidate_sql,
                candidate_values=topic_read_candidate_values,
                provider_candidate_values=topic_provider_candidate_values,
            )
            session.execute(
                """
                UPDATE messenger_user_message_states AS state
                SET read_at = COALESCE(state.read_at, NOW()), updated_at = NOW()
                FROM messenger_message_placements AS placement
                WHERE state.project_id = %s AND state.user_uuid = %s
                  AND placement.project_id = state.project_id
                  AND placement.uuid = state.placement_uuid
                  AND placement.topic_uuid = %s
                  AND state.read_at IS NULL
                """,
                (self.project_uuid, self.user_uuid, topic.uuid),
            )
            if self._syncs_compact_read_state(session):
                read_state.read_topic(
                    session,
                    self.project_uuid,
                    self.user_uuid,
                    topic.stream_uuid,
                    topic.uuid,
                    collect_message_rows=False,
                )
            self._enqueue_counter_projections(
                source_kind="topic.read",
                user_uuid=self.user_uuid,
                stream_uuid=topic.stream_uuid,
                topic_uuid=topic.uuid,
            )
            return _public(self._topic(topic.uuid), "stream_topics")
        if resource == "stream_topics" and action == "set_default":
            topic = self._topic(resource_uuid)
            session.execute(
                """
                UPDATE messenger_streams
                SET default_topic_uuid = %s, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (topic.uuid, self.project_uuid, topic.stream_uuid),
            )
            self._enqueue_stream_snapshot(
                topic.stream_uuid,
                source_kind="stream.updated",
            )
            return _public(self._topic(topic.uuid), "stream_topics")
        if resource == "stream_topics" and action == "set_summary_prompt":
            topic = self._topic(resource_uuid)
            allowed = {
                "summary_system_prompt",
                "summary_reasoning_effort",
                "summary_enabled",
            }
            updates = {name: values[name] for name in allowed if name in values}
            if not updates:
                raise ra_exceptions.ValidationErrorException()
            assignments = ", ".join(f'"{name}" = %s' for name in updates)
            session.execute(
                f"UPDATE messenger_topics SET {assignments}, updated_at = NOW() "
                "WHERE project_id = %s AND uuid = %s",
                (*updates.values(), self.project_uuid, topic.uuid),
            )
            self._enqueue(
                "topic_state_projection",
                "topic",
                f"{self.project_uuid}:{topic.uuid}",
                {"source_kind": "topic.updated", "topic_uuid": topic.uuid},
            )
            return _public(self._topic(topic.uuid), "stream_topics")
        if resource == "stream_topics" and action == "notifications":
            topic = self._topic(resource_uuid)
            provider_target = self._provider_target(
                topic.stream_uuid,
                "topic.notification.update",
            )
            stream = self._stream(topic.stream_uuid)
            helpers._validate_topic_notification_mode(
                stream_notification_mode=stream.notification_mode,
                notification_mode=values["notification_mode"],
            )
            session.execute(
                """
                UPDATE messenger_user_topic_bindings
                SET notification_mode = %s, updated_at = NOW()
                WHERE project_id = %s AND topic_uuid = %s AND user_uuid = %s
                """,
                (
                    values["notification_mode"],
                    self.project_uuid,
                    topic.uuid,
                    self.user_uuid,
                ),
            )
            notification = session.execute(
                """
                SELECT notification_mode, updated_at
                FROM messenger_user_topic_bindings
                WHERE project_id = %s AND topic_uuid = %s AND user_uuid = %s
                """,
                (self.project_uuid, topic.uuid, self.user_uuid),
            ).fetchone()
            self._enqueue_counter_projections(
                source_kind="topic.updated",
                user_uuid=self.user_uuid,
                stream_uuid=topic.stream_uuid,
                topic_uuid=topic.uuid,
            )
            row = _public(self._topic(topic.uuid), "stream_topics")
            self._queue_provider_operation(
                operation_kind="topic.notification.update",
                target_type="topic",
                target_uuid=topic.uuid,
                stream_uuid=topic.stream_uuid,
                payload={
                    "uuid": str(topic.uuid),
                    "stream_uuid": str(topic.stream_uuid),
                    "user_uuid": str(self.user_uuid),
                    "notification_mode": notification["notification_mode"],
                    "notification_updated_at": notification["updated_at"],
                },
                provider_target=provider_target,
            )
            return row
        return super().perform_action(resource, resource_uuid, action, values)


class MessengerV2StoreFactory(sql_canonical_store.SQLCanonicalMessengerStoreFactory):
    """Open v2 request stores while retaining the shared draft/event stores."""

    @contextlib.contextmanager
    def __call__(
        self,
        project_uuid: str | sys_uuid.UUID,
        user_uuid: str | sys_uuid.UUID,
    ) -> typing.Iterator[api_store.MessengerStore]:
        messenger_store = MessengerV2Store(project_uuid, user_uuid)
        messenger_store._ensure_project_user(user_uuid)
        yield typing.cast(
            api_store.MessengerStore,
            messenger_store,
        )
