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
EVENT_RETENTION = datetime.timedelta(days=7)


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
) -> int:
    """Prune only the seven-day event suffix and advance durable watermarks."""
    cutoff = now - EVENT_RETENTION
    session.execute(
        """
        INSERT INTO "m_workspace_event_cursors" (
            "project_id", "user_uuid", "current_epoch_version",
            "pruned_through_epoch_version"
        )
        SELECT
            "project_id", "user_uuid", MAX("epoch_version"), MAX("epoch_version")
        FROM "m_workspace_events"
        WHERE "created_at" < %s
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
        """,
        (cutoff,),
    )
    session.execute(
        """
        UPDATE "m_workspace_event_audience_snapshots_v1" AS audience
        SET
            "current_epoch_version" = GREATEST(
                audience."current_epoch_version", expired."epoch_version"
            ),
            "pruned_through_epoch_version" = GREATEST(
                audience."pruned_through_epoch_version", expired."epoch_version"
            )
        FROM (
            SELECT "audience_snapshot_uuid", MAX("epoch_version") AS "epoch_version"
            FROM "m_workspace_broadcast_message_events_v1"
            WHERE "created_at" < %s
            GROUP BY "audience_snapshot_uuid"
        ) AS expired
        WHERE audience."uuid" = expired."audience_snapshot_uuid"
        """,
        (cutoff,),
    )
    result = session.execute(
        """
        WITH deleted_recipient_events AS (
            DELETE FROM "m_workspace_events"
            WHERE "created_at" < %s
            RETURNING 1
        ), deleted_broadcast_events AS (
            DELETE FROM "m_workspace_broadcast_message_events_v1"
            WHERE "created_at" < %s
            RETURNING 1
        )
        SELECT
            (SELECT COUNT(*) FROM deleted_recipient_events)
            + (SELECT COUNT(*) FROM deleted_broadcast_events) AS "count"
        """,
        (cutoff, cutoff),
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
            audience."current_epoch_version",
            audience."pruned_through_epoch_version"
        FROM "m_workspace_event_audience_snapshots_v1" AS audience
        JOIN "m_workspace_event_audience_members_v1" AS member
          ON member."audience_snapshot_uuid" = audience."uuid"
        WHERE NOT EXISTS (
            SELECT 1
            FROM "m_workspace_broadcast_message_events_v1" AS event
            WHERE event."audience_snapshot_uuid" = audience."uuid"
        )
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
        rows = RESOURCE_MODELS[resource].objects.get_all(
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

    def _is_direct_stream(self, stream_uuid: object) -> bool:
        session = contexts.Context().get_session()
        row = session.execute(
            """
            SELECT "private_index" IS NOT NULL AS "is_direct"
            FROM "m_workspace_streams"
            WHERE "project_id" = %s AND "uuid" = %s
            """,
            (self.project_uuid, stream_uuid),
        ).fetchone()
        if row is None:
            raise ra_exceptions.ValidationErrorException()
        return row["is_direct"]

    def _validate_stream_participants(
        self,
        stream_uuid: object,
        participants: collections.abc.Iterable[object],
    ) -> None:
        if not self._is_direct_stream(stream_uuid):
            return
        if len(set(participants)) != 2:
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
        required_capability = provider_data._required_capability(operation_kind)
        if required_capability is None:
            raise ra_exceptions.ValidationErrorException()
        try:
            account, _chat, bridge = provider_data.resolve_provider_target(
                session,
                project_id=self.project_uuid,
                owner_user_uuid=self.user_uuid,
                external_account_uuid=stream.external_account_uuid,
                stream_uuid=stream_uuid,
                capability_name=required_capability,
            )
        except provider_data.ProviderUnavailableError as exc:
            raise ra_exceptions.ValidationErrorException() from exc
        return account, bridge

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
            self._queue_provider_operation(
                operation_kind="reaction.update",
                target_type="reaction",
                target_uuid=row.uuid,
                stream_uuid=message.stream_uuid,
                payload=_public_dict(row, resource),
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
            unread_messages = helpers._get_unread_workspace_user_messages(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                stream_uuid=resource_uuid,
            )
            provider_target = (
                self._provider_target(resource_uuid, "read_state.set")
                if unread_messages
                else _PROVIDER_TARGET_UNSET
            )
            row = helpers.read_workspace_user_stream_messages(
                self.project_uuid, self.user_uuid, resource_uuid
            )
            self._queue_provider_read(
                stream_uuid=resource_uuid,
                topic_uuid=None,
                message_uuids=[message.uuid for message in unread_messages],
                target_type="stream",
                target_uuid=resource_uuid,
                provider_target=provider_target,
            )
        elif resource == "stream_bindings" and action == "add_users":
            role_user_uuids = {
                role: [sys_uuid.UUID(str(value)) for value in user_uuids]
                for role, user_uuids in values.items()
            }
            participants = set(self._stream_participants(resource_uuid))
            participants.update(
                user_uuid
                for user_uuids in role_user_uuids.values()
                for user_uuid in user_uuids
            )
            self._validate_stream_participants(resource_uuid, tuple(participants))
            row = helpers.get_or_create_workspace_stream_bindings(
                self.project_uuid,
                resource_uuid,
                self.user_uuid,
                role_user_uuids,
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
        elif resource == "stream_topics" and action == "read":
            topic = helpers.get_workspace_user_stream_topic(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
            )
            unread_messages = helpers._get_unread_workspace_user_messages(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                stream_uuid=topic.stream_uuid,
                topic_uuid=resource_uuid,
            )
            provider_target = (
                self._provider_target(topic.stream_uuid, "read_state.set")
                if unread_messages
                else _PROVIDER_TARGET_UNSET
            )
            row = helpers.read_workspace_user_stream_topic_messages(
                self.project_uuid, self.user_uuid, resource_uuid
            )
            self._queue_provider_read(
                stream_uuid=topic.stream_uuid,
                topic_uuid=resource_uuid,
                message_uuids=[message.uuid for message in unread_messages],
                target_type="topic",
                target_uuid=resource_uuid,
                provider_target=provider_target,
            )
        elif resource == "messages" and action == "read":
            message = helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
            )
            provider_target = (
                self._provider_target(message.stream_uuid, "read_state.set")
                if not message.read
                else _PROVIDER_TARGET_UNSET
            )
            row = helpers.read_workspace_user_message(
                self.project_uuid, self.user_uuid, resource_uuid
            )
            self._queue_provider_read(
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                message_uuids=[] if message.read else [message.uuid],
                target_type="message",
                target_uuid=resource_uuid,
                provider_target=provider_target,
            )
        elif resource == "messages" and action == "read_up_to":
            message = helpers.get_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
            )
            unread_messages = helpers._get_unread_workspace_user_messages(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                created_at=message.created_at,
                boundary_uuid=message.uuid,
            )
            provider_target = (
                self._provider_target(message.stream_uuid, "read_state.set")
                if unread_messages
                else _PROVIDER_TARGET_UNSET
            )
            row = helpers.read_workspace_user_topic_messages_to_message(
                self.project_uuid, self.user_uuid, resource_uuid
            )
            self._queue_provider_read(
                stream_uuid=message.stream_uuid,
                topic_uuid=message.topic_uuid,
                message_uuids=[message.uuid for message in unread_messages],
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
        scoped_filters = {
            name: value for name, value in filters.items() if name != "epoch_version"
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
