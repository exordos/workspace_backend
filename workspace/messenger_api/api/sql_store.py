# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Canonical-mail-first store with a rebuildable RESTAlchemy SQL projection."""

import contextlib
import copy
import dataclasses
import datetime
import threading
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.storage import exceptions as storage_exceptions

from workspace.messenger_api import exceptions as messenger_exc
from workspace.messenger_api import file_storage
from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models
from workspace.messenger_mail import repository as mail_repository
from workspace.messenger_mail import runtime as mail_runtime
from workspace.messenger_mail import external_bridge_data_plane


RESOURCE_MODELS = resource_projection.RESOURCE_MODELS
USER_SCOPED_RESOURCES = resource_projection.USER_SCOPED_RESOURCES
EXTENSION_RESOURCES = resource_projection.EXTENSION_RESOURCES
EXTENSION_CANONICAL_MODELS = resource_projection.EXTENSION_CANONICAL_MODELS
EVENT_NAMESPACE = sys_uuid.UUID("798900cd-aef4-5277-9989-d4aac5fbfc8a")


@dataclasses.dataclass
class _CanonicalEventState:
    cursor: mail_repository.EpochCursor | None = None
    event_uuids: set[sys_uuid.UUID] = dataclasses.field(default_factory=set)


_CANONICAL_NOT_PROVIDED = resource_projection.CANONICAL_NOT_PROVIDED


def _simple(value: typing.Any) -> typing.Any:
    return resource_projection.simple(value)


def _as_dict(
    value: typing.Any,
    resource: typing.Any = None,
    canonical: typing.Any = _CANONICAL_NOT_PROVIDED,
) -> typing.Any:
    return resource_projection.as_dict(value, resource, canonical=canonical)


def _plain_values(values: typing.Any) -> typing.Any:
    return resource_projection.plain_values(values)


class SQLDraftStore:
    """PostgreSQL-only draft storage that does not initialize mail services."""

    def __init__(
        self,
        project_uuid: sys_uuid.UUID,
        user_uuid: sys_uuid.UUID,
    ) -> None:
        self.project_uuid = project_uuid
        self.user_uuid = user_uuid

    def get_draft(self, draft_uuid: object) -> typing.Any:
        row = helpers.get_workspace_draft(
            self.project_uuid,
            self.user_uuid,
            draft_uuid,
        )
        return _as_dict(row, "drafts")

    def filter_draft_page(
        self,
        filters: typing.Any,
        marker_uuid: object,
        sort_direction: typing.Any,
        limit: typing.Any,
    ) -> typing.Any:
        allowed_filters = {"stream_uuid", "topic_uuid"}
        if not set(filters).issubset(allowed_filters):
            raise ra_exceptions.ValidationErrorException()
        where = ['"project_id" = %s', '"user_uuid" = %s']
        params = [self.project_uuid, self.user_uuid]
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
            _as_dict(rows_by_uuid[draft_uuid], "drafts") for draft_uuid in draft_uuids
        ]

    @staticmethod
    def _public_draft(row: typing.Any) -> typing.Any:
        return _as_dict(row, "drafts")

    def create_draft(self, values: typing.Any) -> typing.Any:
        draft, created = helpers.create_workspace_draft(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            draft_uuid=values["uuid"],
            stream_uuid=values["stream_uuid"],
            topic_uuid=values["topic_uuid"],
            payload=values["payload"],
        )
        return _as_dict(draft, "drafts"), created

    def update_draft(
        self,
        draft_uuid: object,
        payload: typing.Any,
        expected_revision: typing.Any,
    ) -> typing.Any:
        draft, updated = helpers.update_workspace_draft(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            draft_uuid=draft_uuid,
            payload=payload,
            expected_revision=expected_revision,
        )
        if not updated:
            raise messenger_exc.DraftPreconditionFailedError(self._public_draft(draft))
        return _as_dict(draft, "drafts")

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
            raise messenger_exc.DraftPreconditionFailedError(self._public_draft(draft))
        return None


class SQLProjectedMessengerStore:
    """Writes canonical IMAP/SMTP state before updating the SQL projection."""

    def __init__(
        self,
        project_uuid: sys_uuid.UUID,
        user_uuid: sys_uuid.UUID,
        mail_service: typing.Any,
        canonical_event_states: typing.Any = None,
        bridge_config: typing.Any = None,
    ) -> None:
        self.project_uuid = project_uuid
        self.user_uuid = user_uuid
        self.mail_service = mail_service
        self.bridge_config = bridge_config
        self._projection_event_epoch = self._latest_projection_event_epoch()
        self._canonical_event_states = (
            {} if canonical_event_states is None else canonical_event_states
        )

    def _latest_projection_event_epoch(self) -> int:
        events = models.WorkspaceEvent.objects.get_all(
            filters={"project_id": dm_filters.EQ(self.project_uuid)},
            order_by={"epoch_version": "desc"},
            limit=1,
        )
        return 0 if not events else events[0].epoch_version

    def _known_event_uuids(
        self,
        user_uuid: sys_uuid.UUID,
    ) -> set[sys_uuid.UUID]:
        state = self._canonical_event_states.setdefault(
            user_uuid,
            _CanonicalEventState(),
        )
        current = self.mail_service.repository.current_epoch(user_uuid)
        cursor = state.cursor
        if (
            cursor is None
            or cursor.uid_validity != current.uid_validity
            or cursor.epoch_version > current.epoch_version
        ):
            state.event_uuids.clear()
            cursor = mail_repository.EpochCursor(current.uid_validity, 0)
        events = self.mail_service.repository.events_after(user_uuid, cursor)
        state.event_uuids.update(event.record.event_uuid for event in events)
        state.cursor = current
        return state.event_uuids

    def _sync_projection_events(self) -> None:
        rows = models.WorkspaceEvent.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(self.project_uuid),
                "epoch_version": dm_filters.GT(self._projection_event_epoch),
            },
            order_by={"epoch_version": "asc"},
        )
        for row in rows:
            known = self._known_event_uuids(row.user_uuid)
            if row.uuid not in known:
                self.mail_service.repository.append_event(
                    mail_repository.EventRecord(
                        project_uuid=self.project_uuid,
                        event_uuid=row.uuid,
                        operation_uuid=sys_uuid.uuid5(
                            EVENT_NAMESPACE,
                            f"projection-event:{row.uuid}",
                        ),
                        user_uuid=row.user_uuid,
                        object_type=row.object_type,
                        action=row.action,
                        payload=_simple(row.payload),
                        occurred_at=row.created_at,
                        schema_version=row.schema_version,
                    )
                )
                known.add(row.uuid)
            self._projection_event_epoch = max(
                self._projection_event_epoch,
                row.epoch_version,
            )

    def _scope_filters(self, resource: typing.Any, filters: typing.Any) -> typing.Any:
        result = filters.copy()
        model = RESOURCE_MODELS[resource]
        properties = model.properties.properties
        if "project_id" in properties:
            result["project_id"] = dm_filters.EQ(self.project_uuid)
        if resource in USER_SCOPED_RESOURCES:
            result["user_uuid"] = dm_filters.EQ(self.user_uuid)
        if resource == "message_reactions":
            result["message_uuid"] = dm_filters.In(
                helpers.get_workspace_user_message_uuids(
                    self.project_uuid,
                    self.user_uuid,
                )
            )
        return result

    def sync_iam_identity(self, values: typing.Any) -> typing.Any:
        row = models.WorkspaceUser.sync_iam_identity(**values)
        return _as_dict(row, "users")

    def filter_resources(
        self,
        resource: typing.Any,
        filters: typing.Any,
        order_by: typing.Any = None,
        limit: typing.Any = None,
    ) -> typing.Any:
        rows = RESOURCE_MODELS[resource].objects.get_all(
            filters=self._scope_filters(resource, filters),
            order_by=order_by,
            limit=limit,
        )
        if resource == "files":
            rows = [row for row in rows if self._can_read_file(row)]
        return [_as_dict(row, resource) for row in rows]

    def filter_message_page(
        self,
        filters: typing.Any,
        marker_uuid: object,
        sort_direction: typing.Any,
        limit: typing.Any,
    ) -> typing.Any:
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
        return [_as_dict(row, "messages") for row in rows]

    def filter_draft_page(
        self,
        filters: typing.Any,
        marker_uuid: object,
        sort_direction: typing.Any,
        limit: typing.Any,
    ) -> typing.Any:
        allowed_filters = {"stream_uuid", "topic_uuid"}
        if not set(filters).issubset(allowed_filters):
            raise ra_exceptions.ValidationErrorException()
        where = ['"project_id" = %s', '"user_uuid" = %s']
        params = [self.project_uuid, self.user_uuid]
        scoped_filters = {}
        for name, clause in filters.items():
            if not isinstance(clause, dm_filters.EQ):
                raise ra_exceptions.ValidationErrorException()
            where.append(f'"{name}" = %s')
            params.append(clause.value)
            scoped_filters[name] = clause
        scoped_filters = self._scope_filters("drafts", scoped_filters)
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
            _as_dict(rows_by_uuid[draft_uuid], "drafts") for draft_uuid in draft_uuids
        ]

    def get_resource(self, resource: typing.Any, resource_uuid: object) -> typing.Any:
        if resource == "folder_items":
            row = helpers.get_workspace_user_folder_item(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
            )
        elif resource == "files":
            row = helpers.get_workspace_file_by_uuid(resource_uuid)
            if not self._can_read_file(row):
                return helpers.get_workspace_owned_file(
                    self.project_uuid,
                    self.user_uuid,
                    resource_uuid,
                )
        else:
            model = RESOURCE_MODELS[resource]
            row = model.objects.get_one(
                filters=self._scope_filters(
                    resource,
                    {model.get_id_property_name(): dm_filters.EQ(resource_uuid)},
                )
            )
        return _as_dict(row, resource)

    def _record(
        self,
        operation: typing.Any,
        entity_uuid: object,
        payload: typing.Any,
    ) -> typing.Any:
        return mail_repository.OperationRecord(
            project_uuid=self.project_uuid,
            operation_uuid=sys_uuid.uuid4(),
            actor_uuid=self.user_uuid,
            operation=operation,
            entity_uuid=typing.cast(sys_uuid.UUID, entity_uuid),
            payload=typing.cast(dict, _simple(payload)),
            occurred_at=datetime.datetime.now(datetime.timezone.utc),
        )

    def _append(
        self,
        operation: typing.Any,
        entity_uuid: object,
        payload: typing.Any,
    ) -> typing.Any:
        payload = self._canonical_snapshot(operation, entity_uuid, payload)
        payload.setdefault(
            "_event_recipient_uuids",
            [str(value) for value in self._event_recipients(operation, payload)],
        )
        record = self._record(operation, entity_uuid, payload)
        self.mail_service.repository.append_operation(record)
        return record

    def _queue_bridge_operation(
        self,
        record: typing.Any,
        operation_kind: typing.Any,
        entity_uuid: sys_uuid.UUID,
        payload: typing.Any,
        target_type: typing.Any,
        *,
        target_stream_uuid: sys_uuid.UUID | None = None,
        provider_entity_id: typing.Any = None,
        provider_revision: typing.Any = None,
    ) -> typing.Any:
        if self.bridge_config is None:
            return None
        return external_bridge_data_plane.queue_workspace_operation(
            contexts.Context().get_session(),
            project_uuid=self.project_uuid,
            owner_user_uuid=self.user_uuid,
            operation_uuid=record.operation_uuid,
            operation_kind=operation_kind,
            entity_uuid=entity_uuid,
            payload=typing.cast(dict, _simple(payload)),
            target_type=target_type,
            target_stream_uuid=target_stream_uuid,
            provider_entity_id=provider_entity_id,
            provider_revision=provider_revision,
            realm_uuid=self.bridge_config.realm_uuid,
            bridge_instance_uuid=self.bridge_config.bridge_instance_uuid,
            identity_generation=self.bridge_config.identity_generation,
            enrollment_secret=self.bridge_config.enrollment_secret,
            now=record.occurred_at,
        )

    def _preflight_bridge_operation(
        self,
        operation_kind: typing.Any,
        target_stream_uuid: sys_uuid.UUID,
    ) -> typing.Any:
        if self.bridge_config is None:
            return False
        try:
            target = external_bridge_data_plane.validate_workspace_operation(
                contexts.Context().get_session(),
                project_uuid=self.project_uuid,
                owner_user_uuid=self.user_uuid,
                operation_kind=operation_kind,
                target_stream_uuid=target_stream_uuid,
            )
        except ValueError as exc:
            raise ra_exceptions.ValidationErrorException() from exc
        return target is not None

    def create_resource(self, resource: typing.Any, values: typing.Any) -> typing.Any:
        values = _plain_values(values)
        entity_uuid = values.get("uuid") or sys_uuid.uuid4()
        values["uuid"] = entity_uuid
        if resource == "folders":
            self._append("folder.create", entity_uuid, values)
            row = helpers.create_workspace_user_folder(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                **self._projection_values(values),
            )
        elif resource == "folder_items":
            self._append("folder_item.create", entity_uuid, values)
            row = helpers.create_workspace_user_folder_item(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                **self._projection_values(values),
            )
        elif resource == "streams":
            default_topic_uuid = sys_uuid.uuid4()
            peer_uuid = values.get("direct_user_uuid")
            participants = (
                (self.user_uuid,)
                if peer_uuid is None
                else (
                    self.user_uuid,
                    sys_uuid.UUID(str(peer_uuid)),
                )
            )
            binding_uuids = {
                str(participant_uuid): sys_uuid.uuid4()
                for participant_uuid in participants
            }
            canonical_values = values.copy()
            canonical_values["kind"] = "direct" if peer_uuid is not None else "stream"
            canonical_values["default_topic_uuid"] = str(default_topic_uuid)
            canonical_values["_event_recipient_uuids"] = [
                str(participant_uuid) for participant_uuid in participants
            ]
            self._append("stream.create", entity_uuid, canonical_values)
            for participant_uuid in participants:
                binding_uuid = binding_uuids[str(participant_uuid)]
                self._append(
                    "binding.create",
                    binding_uuid,
                    {
                        "stream_uuid": str(entity_uuid),
                        "user_uuid": str(participant_uuid),
                        "who_uuid": str(self.user_uuid),
                        "role": models.WorkspaceStreamRole.OWNER.value,
                        "notification_mode": (
                            models.WorkspaceStreamNotificationMode.ALL_MESSAGES.value
                        ),
                    },
                )
            default_topic_name = values.get("default_topic_name", "General Topic")
            self._append(
                "topic.create",
                default_topic_uuid,
                {
                    "stream_uuid": str(entity_uuid),
                    "name": default_topic_name,
                    "source_name": values.get("source_name", "native"),
                    "source": values.get("source", {"kind": "native"}),
                },
            )
            projection_values = self._projection_values(values)
            projection_values.update(
                {
                    "canonical_default_topic_uuid": default_topic_uuid,
                    "canonical_binding_uuids": binding_uuids,
                }
            )
            row = helpers.get_or_create_workspace_user_stream(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                **projection_values,
            )
        elif resource == "stream_topics":
            self._append("topic.create", entity_uuid, values)
            row = helpers.create_workspace_user_stream_topic(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                values=self._projection_values(values),
            )
            if self.bridge_config is not None:
                try:
                    external_bridge_data_plane.ensure_topic_projection_mapping(
                        contexts.Context().get_session(),
                        project_uuid=self.project_uuid,
                        owner_user_uuid=self.user_uuid,
                        stream_uuid=row.stream_uuid,
                        topic_uuid=row.uuid,
                        topic_name=row.name,
                        bridge_instance_uuid=(self.bridge_config.bridge_instance_uuid),
                    )
                except ValueError as exc:
                    raise ra_exceptions.ValidationErrorException() from exc
        elif resource == "message_reactions":
            self._append("reaction.create", entity_uuid, values)
            row = helpers.create_workspace_message_reaction(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                **self._projection_values(values),
            )
        elif resource == "files":
            self._append("file.create", entity_uuid, values)
            row = helpers.create_workspace_file(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                **self._projection_values(values),
            )
        else:
            raise ValueError(f"Unsupported Messenger create resource {resource}")
        self._sync_projection_events()
        return _as_dict(row, resource)

    def update_resource(
        self,
        resource: typing.Any,
        resource_uuid: sys_uuid.UUID,
        values: typing.Any,
    ) -> typing.Any:
        values = _plain_values(values)
        if resource == "streams":
            self._preflight_bridge_operation("stream.upsert", resource_uuid)
        elif resource == "stream_topics":
            topic = self.mail_service.repository.projection.topics[
                sys_uuid.UUID(str(resource_uuid))
            ]
            self._preflight_bridge_operation(
                "topic.upsert",
                topic["stream_uuid"],
            )
        if resource == "stream_bindings":
            binding = self.mail_service.repository.projection.bindings.get(
                sys_uuid.UUID(str(resource_uuid))
            )
            if binding is not None:
                stream_uuid = sys_uuid.UUID(binding["stream_uuid"])
                if (
                    self._is_direct_stream(stream_uuid)
                    and values.get("role", "owner") != "owner"
                ):
                    raise ra_exceptions.ValidationErrorException()
        operation = {
            "folders": "folder.update",
            "streams": "stream.update",
            "stream_topics": "topic.update",
            "message_reactions": "reaction.update",
        }.get(resource)
        record: typing.Any = None
        if operation is not None:
            record = self._append(operation, resource_uuid, values)
        if resource == "folders":
            row = helpers.update_workspace_user_folder(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                **self._projection_values(values),
            )
        elif resource == "streams":
            row = helpers.update_workspace_user_stream(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                self._projection_values(values),
            )
            provider = record.payload.get("provider") or {}
            self._queue_bridge_operation(
                record,
                "stream.upsert",
                row.uuid,
                {
                    "name": row.name,
                    "description": row.description or "",
                    "private": row.private,
                    "chat_kind": (
                        "personal_dm"
                        if self._is_direct_stream(row.uuid)
                        else "group_dm"
                        if row.private
                        else "channel"
                    ),
                    "participant_uuids": [
                        str(value) for value in self._stream_participants(row.uuid)
                    ],
                    "default_topic_uuid": (
                        None
                        if row.default_topic_uuid is None
                        else str(row.default_topic_uuid)
                    ),
                },
                "stream",
                target_stream_uuid=row.uuid,
                provider_entity_id=provider.get("external_id"),
                provider_revision=provider.get("revision"),
            )
        elif resource == "stream_topics":
            row = helpers.update_workspace_user_stream_topic(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                self._projection_values(values),
            )
            provider = record.payload.get("provider") or {}
            self._queue_bridge_operation(
                record,
                "topic.upsert",
                row.uuid,
                {"stream_uuid": str(row.stream_uuid), "name": row.name},
                "topic",
                provider_entity_id=provider.get("external_id"),
                provider_revision=provider.get("revision"),
            )
        elif resource == "message_reactions":
            row = helpers.update_workspace_message_reaction(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                self._projection_values(values),
            )
        elif resource == "files":
            self._append("file.update", resource_uuid, values)
            row = helpers.update_workspace_file(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                self._projection_values(values),
            )
        elif resource == "stream_bindings":
            row = models.WorkspaceStreamBinding.objects.get_one(
                filters=self._scope_filters(
                    resource,
                    {"uuid": dm_filters.EQ(resource_uuid)},
                )
            )
            self._append("binding.update", resource_uuid, values)
            row.update_dm(values=self._projection_values(values))
            row.update()
            helpers.create_workspace_stream_binding_updated_events(row)
        else:
            raise ValueError(f"Unsupported Messenger update resource {resource}")
        self._sync_projection_events()
        return _as_dict(row, resource)

    def delete_resource(
        self,
        resource: typing.Any,
        resource_uuid: sys_uuid.UUID,
    ) -> typing.Any:
        external_target = False
        target_stream_uuid = None
        if resource == "streams":
            target_stream_uuid = resource_uuid
            external_target = self._preflight_bridge_operation(
                "stream.delete",
                target_stream_uuid,
            )
        elif resource == "stream_topics":
            topic = self.mail_service.repository.projection.topics[
                sys_uuid.UUID(str(resource_uuid))
            ]
            target_stream_uuid = topic["stream_uuid"]
            external_target = self._preflight_bridge_operation(
                "topic.delete",
                target_stream_uuid,
            )
        if resource == "stream_bindings":
            binding = self.mail_service.repository.projection.bindings.get(
                sys_uuid.UUID(str(resource_uuid))
            )
            if binding is not None:
                stream_uuid = sys_uuid.UUID(binding["stream_uuid"])
                remaining = tuple(
                    participant
                    for participant in self._stream_participants(stream_uuid)
                    if participant != sys_uuid.UUID(binding["user_uuid"])
                )
                self._validate_stream_participants(stream_uuid, remaining)
        operation = {
            "folders": "folder.delete",
            "folder_items": "folder_item.delete",
            "streams": "stream.delete",
            "stream_bindings": "binding.delete",
            "stream_topics": "topic.delete",
            "message_reactions": "reaction.delete",
        }.get(resource)
        record: typing.Any = None
        if operation is not None:
            record = self._append(operation, resource_uuid, {})
        if resource == "folders":
            row = typing.cast(
                typing.Any,
                helpers.delete_workspace_user_folder(
                    self.project_uuid, self.user_uuid, resource_uuid
                ),
            )
        elif resource == "folder_items":
            row = typing.cast(
                typing.Any,
                helpers.delete_workspace_user_folder_item(
                    self.project_uuid, self.user_uuid, resource_uuid
                ),
            )
        elif resource == "streams":
            row = typing.cast(
                typing.Any,
                helpers.delete_workspace_user_stream(
                    self.project_uuid, self.user_uuid, resource_uuid
                ),
            )
        elif resource == "stream_bindings":
            row = typing.cast(
                typing.Any,
                helpers.delete_workspace_stream_binding(
                    self.project_uuid, resource_uuid
                ),
            )
        elif resource == "stream_topics":
            row = typing.cast(
                typing.Any,
                helpers.delete_workspace_user_stream_topic(
                    self.project_uuid, self.user_uuid, resource_uuid
                ),
            )
        elif resource == "message_reactions":
            row = typing.cast(
                typing.Any,
                helpers.delete_workspace_message_reaction(
                    self.project_uuid, self.user_uuid, resource_uuid
                ),
            )
        elif resource == "files":
            file = helpers.get_workspace_owned_file(
                self.project_uuid, self.user_uuid, resource_uuid
            )
            self._append("file.delete", resource_uuid, _as_dict(file))
            row = typing.cast(
                typing.Any,
                helpers.delete_workspace_file(
                    self.project_uuid, self.user_uuid, resource_uuid
                ),
            )
        else:
            raise ValueError(f"Unsupported Messenger delete resource {resource}")
        if external_target:
            provider = record.payload.get("provider") or {}
            self._queue_bridge_operation(
                record,
                "stream.delete" if resource == "streams" else "topic.delete",
                resource_uuid,
                (
                    {"stream_uuid": str(resource_uuid)}
                    if resource == "streams"
                    else {
                        "stream_uuid": str(target_stream_uuid),
                        "topic_uuid": str(resource_uuid),
                    }
                ),
                "stream" if resource == "streams" else "topic",
                target_stream_uuid=target_stream_uuid,
                provider_entity_id=provider.get("external_id"),
                provider_revision=provider.get("revision"),
            )
        self._sync_projection_events()
        return None if row is None else _as_dict(row, resource)

    def create_message(self, values: typing.Any) -> typing.Any:
        values = _plain_values(values)
        message_uuid = values.get("uuid") or sys_uuid.uuid4()
        values["uuid"] = message_uuid
        stream_uuid = sys_uuid.UUID(str(values["stream_uuid"]))
        try:
            self._binding_uuid(stream_uuid, self.user_uuid)
        except mail_repository.InvalidJournalRecord as exc:
            raise ra_exceptions.ValidationErrorException() from exc
        if values.get("topic_uuid") is None:
            stream = self.mail_service.repository.projection.streams.get(stream_uuid)
            default_topic_uuid = (
                None if stream is None else stream.get("default_topic_uuid")
            )
            if default_topic_uuid is None:
                raise messenger_exc.StreamDefaultTopicNotConfiguredError()
            values["topic_uuid"] = sys_uuid.UUID(str(default_topic_uuid))
        values["_event_recipient_uuids"] = [
            str(value) for value in self._stream_participants(stream_uuid)
        ]
        self._preflight_bridge_operation("message.create", stream_uuid)
        record_values = values.copy()
        record_values["author_uuid"] = str(self.user_uuid)
        record = self._record("message.create", message_uuid, record_values)
        self.mail_service.deliver_message(record)
        projection_values = values.copy()
        projection_values["created_at"] = record.occurred_at
        try:
            row = helpers.get_workspace_user_message(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                message_uuid=message_uuid,
            )
        except storage_exceptions.RecordNotFound:
            row = helpers.create_workspace_user_message(
                project_id=self.project_uuid,
                user_uuid=self.user_uuid,
                enforce_visibility=True,
                **self._projection_values(projection_values),
            )
        if self.bridge_config is not None:
            session = contexts.Context().get_session()
            external_bridge_data_plane.queue_message_create(
                session,
                project_uuid=self.project_uuid,
                owner_user_uuid=self.user_uuid,
                message={
                    **values,
                    "uuid": message_uuid,
                    "payload": _simple(values["payload"]),
                },
                realm_uuid=self.bridge_config.realm_uuid,
                bridge_instance_uuid=self.bridge_config.bridge_instance_uuid,
                identity_generation=self.bridge_config.identity_generation,
                enrollment_secret=self.bridge_config.enrollment_secret,
                now=record.occurred_at,
            )
        self._sync_projection_events()
        return _as_dict(row, "messages")

    def update_message(
        self,
        message_uuid: sys_uuid.UUID,
        values: typing.Any,
    ) -> typing.Any:
        values = _plain_values(values)
        current = self.mail_service.repository.projection.messages[message_uuid]
        self._preflight_bridge_operation(
            "message.update",
            current["stream_uuid"],
        )
        record = self._append("message.update", message_uuid, values)
        row = helpers.update_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
            self._projection_values(values),
        )
        provider = record.payload.get("provider") or {}
        self._queue_bridge_operation(
            record,
            "message.update",
            row.uuid,
            {
                "stream_uuid": str(row.stream_uuid),
                "topic_uuid": str(row.topic_uuid),
                "author_uuid": str(row.author_uuid),
                "payload": _simple(row.payload),
            },
            "message",
            provider_entity_id=provider.get("external_id"),
            provider_revision=provider.get("revision"),
        )
        self._sync_projection_events()
        return _as_dict(row, "messages")

    def delete_message(self, message_uuid: sys_uuid.UUID) -> typing.Any:
        message = self.mail_service.repository.projection.messages[message_uuid]
        self._preflight_bridge_operation(
            "message.delete",
            message["stream_uuid"],
        )
        record = self._record(
            "message.delete",
            message_uuid,
            {
                "_event_recipient_uuids": [
                    str(value)
                    for value in self._stream_participants(message["stream_uuid"])
                ]
            },
        )
        self.mail_service.delete_message(record)
        provider = message.get("provider") or {}
        self._queue_bridge_operation(
            record,
            "message.delete",
            message_uuid,
            {
                "stream_uuid": message["stream_uuid"],
                "topic_uuid": message["topic_uuid"],
                "author_uuid": message["author_uuid"],
            },
            "message",
            provider_entity_id=provider.get("external_id"),
            provider_revision=provider.get("revision"),
        )
        row = typing.cast(
            typing.Any,
            helpers.delete_workspace_user_message(
                self.project_uuid,
                self.user_uuid,
                message_uuid,
            ),
        )
        self._sync_projection_events()
        return None if row is None else _as_dict(row, "messages")

    @staticmethod
    def _public_draft(row: typing.Any) -> typing.Any:
        return _as_dict(row, "drafts")

    def create_draft(self, values: typing.Any) -> typing.Any:
        draft, created = helpers.create_workspace_draft(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            draft_uuid=values["uuid"],
            stream_uuid=values["stream_uuid"],
            topic_uuid=values["topic_uuid"],
            payload=values["payload"],
        )
        return _as_dict(draft, "drafts"), created

    def update_draft(
        self,
        draft_uuid: object,
        payload: typing.Any,
        expected_revision: typing.Any,
    ) -> typing.Any:
        draft, updated = helpers.update_workspace_draft(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            draft_uuid=draft_uuid,
            payload=payload,
            expected_revision=expected_revision,
        )
        if not updated:
            raise messenger_exc.DraftPreconditionFailedError(self._public_draft(draft))
        return _as_dict(draft, "drafts")

    def delete_draft(
        self, draft_uuid: object, expected_revision: typing.Any
    ) -> typing.Any:
        draft, deleted = helpers.delete_workspace_draft(
            project_id=self.project_uuid,
            user_uuid=self.user_uuid,
            draft_uuid=draft_uuid,
            expected_revision=expected_revision,
        )
        if not deleted:
            raise messenger_exc.DraftPreconditionFailedError(self._public_draft(draft))
        return None

    def perform_action(
        self,
        resource: typing.Any,
        resource_uuid: object,
        action: typing.Any,
        values: typing.Any,
    ) -> typing.Any:
        resource_uuid = sys_uuid.UUID(str(resource_uuid))
        if resource == "folder_items" and action in {"pin", "unpin"}:
            self._append(f"folder_item.{action}", resource_uuid, values)
            function = (
                helpers.pin_workspace_user_folder_item
                if action == "pin"
                else helpers.unpin_workspace_user_folder_item
            )
            row = function(self.project_uuid, self.user_uuid, resource_uuid)
        elif resource == "streams" and action in {"archive", "unarchive"}:
            archived = action == "archive"
            self._append("stream.update", resource_uuid, {"is_archived": archived})
            row = helpers.update_workspace_user_stream(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                {"is_archived": archived},
            )
        elif resource == "streams" and action == "notifications":
            binding_uuid = self._binding_uuid(resource_uuid, self.user_uuid)
            self._append("binding.update", binding_uuid, values)
            row = helpers.update_workspace_user_stream_notifications(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                values["notification_mode"],
            )
        elif resource == "streams" and action == "read":
            self._preflight_bridge_operation("read_state.set", resource_uuid)
            stream_messages = [
                (message_uuid, message)
                for message_uuid, message in (
                    self.mail_service.repository.projection.messages.items()
                )
                if message.get("stream_uuid") == str(resource_uuid)
            ]
            stream_messages.sort(
                key=lambda item: (item[1].get("created_at", ""), str(item[0]))
            )
            message_uuids = [message_uuid for message_uuid, _message in stream_messages]
            for message_uuid in message_uuids:
                self._append(
                    "message.read",
                    message_uuid,
                    {"user_uuid": str(self.user_uuid)},
                )
            if message_uuids:
                bridge_record = self._record(
                    "message.read",
                    resource_uuid,
                    {"user_uuid": str(self.user_uuid)},
                )
                self._queue_bridge_operation(
                    bridge_record,
                    "read_state.set",
                    resource_uuid,
                    {
                        "stream_uuid": str(resource_uuid),
                        "topic_uuid": None,
                        "reader_uuid": str(self.user_uuid),
                        "message_uuids": [str(value) for value in message_uuids],
                        "read": True,
                    },
                    "stream",
                )
            row = helpers.read_workspace_user_stream_messages(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "stream_bindings" and action == "add_users":
            values = {
                role: [sys_uuid.UUID(str(value)) for value in user_uuids]
                for role, user_uuids in values.items()
            }
            participants = set(self._stream_participants(resource_uuid))
            participants.update(
                user_uuid for user_uuids in values.values() for user_uuid in user_uuids
            )
            self._validate_stream_participants(
                resource_uuid,
                tuple(sorted(participants, key=str)),
            )
            binding_uuids = {}
            existing_bindings = self.mail_service.repository.projection.bindings
            for role, user_uuids in values.items():
                for value in user_uuids:
                    target_uuid = sys_uuid.UUID(str(value))
                    existing_uuid = next(
                        (
                            binding_uuid
                            for binding_uuid, binding in existing_bindings.items()
                            if binding.get("stream_uuid") == str(resource_uuid)
                            and binding.get("user_uuid") == str(target_uuid)
                        ),
                        None,
                    )
                    binding_uuid = existing_uuid or sys_uuid.uuid4()
                    binding_uuids[str(target_uuid)] = binding_uuid
                    if existing_uuid is None:
                        self._append(
                            "binding.create",
                            binding_uuid,
                            {
                                "stream_uuid": str(resource_uuid),
                                "user_uuid": str(target_uuid),
                                "who_uuid": str(self.user_uuid),
                                "role": role,
                                "notification_mode": (
                                    models.WorkspaceStreamNotificationMode.ALL_MESSAGES.value
                                ),
                            },
                        )
            row = helpers.get_or_create_workspace_stream_bindings(
                self.project_uuid,
                resource_uuid,
                self.user_uuid,
                values,
                binding_uuids=binding_uuids,
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
            topic = self.mail_service.repository.projection.topics[resource_uuid]
            self._preflight_bridge_operation(
                "read_state.set",
                topic["stream_uuid"],
            )
            topic_messages = [
                (message_uuid, message)
                for message_uuid, message in (
                    self.mail_service.repository.projection.messages.items()
                )
                if message.get("topic_uuid") == str(resource_uuid)
            ]
            topic_messages.sort(
                key=lambda item: (item[1].get("created_at", ""), str(item[0]))
            )
            if topic_messages:
                bridge_record = self._record(
                    "message.read",
                    topic_messages[-1][0],
                    {"user_uuid": str(self.user_uuid)},
                )
                self._queue_bridge_operation(
                    bridge_record,
                    "read_state.set",
                    topic_messages[-1][0],
                    {
                        "stream_uuid": topic["stream_uuid"],
                        "topic_uuid": str(resource_uuid),
                        "reader_uuid": str(self.user_uuid),
                        "message_uuids": [
                            str(message_uuid)
                            for message_uuid, _message in topic_messages
                        ],
                        "read": True,
                    },
                    "message",
                )
            row = helpers.read_workspace_user_stream_topic_messages(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "messages" and action == "read":
            message = self.mail_service.repository.projection.messages[resource_uuid]
            self._preflight_bridge_operation(
                "read_state.set",
                message["stream_uuid"],
            )
            record = self._append(
                "message.read",
                resource_uuid,
                {"user_uuid": str(self.user_uuid)},
            )
            self._queue_bridge_operation(
                record,
                "read_state.set",
                resource_uuid,
                {
                    "stream_uuid": message["stream_uuid"],
                    "topic_uuid": message["topic_uuid"],
                    "reader_uuid": str(self.user_uuid),
                    "message_uuids": [str(resource_uuid)],
                    "read": True,
                },
                "message",
            )
            row = helpers.read_workspace_user_message(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "messages" and action == "read_up_to":
            target = self.mail_service.repository.projection.messages[resource_uuid]
            self._preflight_bridge_operation(
                "read_state.set",
                target["stream_uuid"],
            )
            target_created_at = target.get("created_at", "")
            target_boundary = (
                target_created_at,
                sys_uuid.UUID(str(resource_uuid)),
            )
            selected_messages = sorted(
                (
                    (
                        message.get("created_at", ""),
                        sys_uuid.UUID(str(message_uuid)),
                    )
                    for message_uuid, message in (
                        self.mail_service.repository.projection.messages.items()
                    )
                    if message.get("topic_uuid") == target.get("topic_uuid")
                    and (
                        message.get("created_at", ""),
                        sys_uuid.UUID(str(message_uuid)),
                    )
                    <= target_boundary
                ),
                key=lambda item: item,
            )
            for _created_at, message_uuid in selected_messages:
                self._append(
                    "message.read",
                    message_uuid,
                    {"user_uuid": str(self.user_uuid)},
                )
            bridge_record = self._record(
                "message.read",
                resource_uuid,
                {"user_uuid": str(self.user_uuid)},
            )
            self._queue_bridge_operation(
                bridge_record,
                "read_state.set",
                resource_uuid,
                {
                    "stream_uuid": target["stream_uuid"],
                    "topic_uuid": target["topic_uuid"],
                    "reader_uuid": str(self.user_uuid),
                    "message_uuids": [
                        str(message_uuid)
                        for _created_at, message_uuid in selected_messages
                    ],
                    "read": True,
                },
                "message",
            )
            row = helpers.read_workspace_user_topic_messages_to_message(
                self.project_uuid, self.user_uuid, resource_uuid
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
            user = helpers.get_workspace_own_user(
                resource_uuid,
                self.user_uuid,
            )
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
            user = helpers.get_workspace_own_user(
                resource_uuid,
                self.user_uuid,
            )
            old_avatar = user.avatar
            avatar = (
                models.build_workspace_user_gravatar_avatar(user.email)
                if user.email
                else models.build_workspace_user_default_avatar(user.uuid)
            )
            row = helpers.update_workspace_user_avatar(
                resource_uuid,
                self.user_uuid,
                avatar,
            )
            self._delete_replaced_avatar_file(old_avatar)
        else:
            raise ValueError(f"Unsupported Messenger action {resource}.{action}")
        self._sync_projection_events()
        if isinstance(row, list):
            return [_as_dict(item, resource) for item in row]
        return _as_dict(row, resource)

    @staticmethod
    def _after_epoch_version(filters: typing.Any) -> typing.Any:
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
        self, after: typing.Any, epoch_generation: typing.Any
    ) -> typing.Any:
        state = self.mail_service.repository.event_cursor_state(self.user_uuid)
        reason = None
        if after > 0 and epoch_generation is None:
            reason = "epoch_generation_required"
        elif (
            epoch_generation is not None and epoch_generation != state.epoch_generation
        ):
            reason = "epoch_generation_changed"
        elif after > state.current_epoch_version:
            reason = "future_epoch"
        elif after < state.minimum_epoch_version - 1:
            reason = "epoch_pruned"
        if reason is not None:
            raise messenger_exc.EventsCursorExpiredError(
                reason=reason,
                epoch_generation=state.epoch_generation,
                current_epoch_version=state.current_epoch_version,
                minimum_epoch_version=state.minimum_epoch_version,
            )
        return state

    def events_after(
        self,
        filters: typing.Any,
        order_by: typing.Any = None,
        epoch_generation: typing.Any = None,
        limit: typing.Any = None,
    ) -> typing.Any:
        del order_by
        after, clauses = self._after_epoch_version(filters)
        state = self._validate_event_cursor(after, epoch_generation)
        events = self.mail_service.repository.events_after(
            self.user_uuid,
            mail_repository.EpochCursor(int(state.epoch_generation), after),
            limit=limit,
        )
        result = [event.as_dict() for event in events]
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

    def current_epoch(self) -> typing.Any:
        return self.event_cursor()["current_epoch_version"]

    def event_cursor(self) -> typing.Any:
        return self.mail_service.repository.event_cursor_state(self.user_uuid).as_dict()

    def replay_operations(
        self, projector: typing.Any, after_uid: typing.Any = 0
    ) -> typing.Any:
        replay = self.mail_service.repository.read_operations(after_uid=after_uid)
        for entry in replay.entries:
            projector(entry.record)
        return replay

    def rebuild_sql_projection(self) -> typing.Any:
        projection = self.mail_service.repository.rebuild()
        collections: tuple[tuple[typing.Any, typing.Any], ...] = (
            (models.WorkspaceStream, projection.streams),
            (models.WorkspaceStreamBinding, projection.bindings),
            (models.WorkspaceStreamTopic, projection.topics),
            (models.WorkspaceMessage, projection.messages),
            (models.WorkspaceMessageReactions, projection.reactions),
            (models.Folder, projection.folders),
            (models.FolderItem, projection.folder_items),
        )
        if hasattr(projection, "files"):
            collections += ((models.WorkspaceFile, projection.files),)
        for model, values in collections:
            for entity_uuid, payload in values.items():
                self._upsert_projection_row(model, entity_uuid, payload)
        for (user_uuid, message_uuid), state in projection.message_states.items():
            self._upsert_projection_row(
                models.WorkspaceUserMessageFlags,
                message_uuid,
                {"user_uuid": user_uuid, **state},
                user_uuid=user_uuid,
            )
        return projection

    def _upsert_projection_row(
        self,
        model: typing.Any,
        entity_uuid: object,
        payload: typing.Any,
        user_uuid: object = None,
    ) -> typing.Any:
        values = {
            name: value
            for name, value in payload.items()
            if name in model.properties.properties and not name.startswith("_")
        }
        values.update({"uuid": entity_uuid, "project_id": self.project_uuid})
        if user_uuid is not None:
            values["user_uuid"] = user_uuid
        filters = {
            "uuid": dm_filters.EQ(entity_uuid),
            "project_id": dm_filters.EQ(self.project_uuid),
        }
        if user_uuid is not None:
            filters["user_uuid"] = dm_filters.EQ(user_uuid)
        row = model.objects.get_one_or_none(filters=filters)
        if row is None:
            row = model(**values)
            row.insert()
        else:
            values.pop("uuid", None)
            values.pop("project_id", None)
            values.pop("user_uuid", None)
            row.update_dm(values=values)
            row.update()
        return row

    def _binding_uuid(self, stream_uuid: object, user_uuid: object) -> typing.Any:
        for (
            binding_uuid,
            binding,
        ) in self.mail_service.repository.projection.bindings.items():
            if binding.get("stream_uuid") == str(stream_uuid) and binding.get(
                "user_uuid"
            ) == str(user_uuid):
                return binding_uuid
        raise mail_repository.InvalidJournalRecord(
            "User is not an ordinary stream participant"
        )

    def _is_direct_stream(self, stream_uuid: object) -> typing.Any:
        stream = self.mail_service.repository.projection.streams.get(
            sys_uuid.UUID(str(stream_uuid)),
            {},
        )
        return stream.get("kind") == "direct"

    def _validate_stream_participants(
        self, stream_uuid: object, participants: typing.Any
    ) -> None:
        try:
            self.mail_service.validate_stream_participants(
                sys_uuid.UUID(str(stream_uuid)),
                tuple(sorted(participants, key=str)),
            )
        except mail_repository.InvalidJournalRecord as exc:
            raise ra_exceptions.ValidationErrorException() from exc

    def _can_read_file(self, file: typing.Any) -> typing.Any:
        if file.user_uuid == self.user_uuid:
            return True
        if file.stream_uuid is None:
            try:
                metadata = file_storage.read_workspace_file_metadata(
                    file_uuid=file.uuid,
                    storage_type=file.storage_type,
                )
            except Exception:
                return False
            return (
                metadata.acl_mode == "public"
                and metadata.uuid == file.uuid
                and metadata.owner_uuid == file.user_uuid
            )
        try:
            self._binding_uuid(file.stream_uuid, self.user_uuid)
        except mail_repository.InvalidJournalRecord:
            return False
        return True

    def _delete_replaced_avatar_file(self, avatar: typing.Any) -> None:
        if not avatar.startswith(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX):
            return
        helpers.delete_workspace_avatar_file(
            self.user_uuid,
            sys_uuid.UUID(avatar[len(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX) :]),
        )

    def _event_recipients(
        self, operation: typing.Any, payload: typing.Any
    ) -> typing.Any:
        if "_event_recipient_uuids" in payload:
            return tuple(
                sys_uuid.UUID(value) for value in payload["_event_recipient_uuids"]
            )
        if operation.startswith("folder"):
            return (sys_uuid.UUID(str(payload.get("user_uuid", self.user_uuid))),)
        stream_uuid = payload.get("stream_uuid")
        if operation.startswith("stream") and stream_uuid is None:
            stream_uuid = payload.get("uuid")
        if operation.startswith("binding"):
            target = payload.get("user_uuid")
            recipients = {self.user_uuid}
            if target is not None:
                recipients.add(sys_uuid.UUID(str(target)))
            if stream_uuid is not None:
                recipients.update(self._stream_participants(stream_uuid))
            return tuple(recipients)
        if operation.startswith("topic") and stream_uuid is None:
            topic_uuid = payload.get("uuid")
            if topic_uuid is not None:
                topic = self.mail_service.repository.projection.topics.get(
                    sys_uuid.UUID(str(topic_uuid)),
                    {},
                )
                stream_uuid = topic.get("stream_uuid")
        if operation.startswith("message"):
            recipients = payload.get("_recipient_uuids")
            if recipients:
                return tuple(sys_uuid.UUID(value) for value in recipients)
            if stream_uuid is None:
                message_uuid = payload.get("uuid")
                if message_uuid is not None:
                    message = self.mail_service.repository.projection.messages.get(
                        sys_uuid.UUID(str(message_uuid)),
                        {},
                    )
                    stream_uuid = message.get("stream_uuid")
        if operation.startswith("reaction") and stream_uuid is None:
            message_uuid = payload.get("message_uuid")
            if message_uuid is not None:
                message = self.mail_service.repository.projection.messages.get(
                    sys_uuid.UUID(str(message_uuid)),
                    {},
                )
                stream_uuid = message.get("stream_uuid")
        if stream_uuid is not None:
            return self._stream_participants(stream_uuid)
        return (self.user_uuid,)

    def _canonical_snapshot(
        self,
        operation: typing.Any,
        entity_uuid: object,
        payload: typing.Any,
    ) -> typing.Any:
        collection_name = {
            "stream": "streams",
            "binding": "bindings",
            "topic": "topics",
            "message": "messages",
            "reaction": "reactions",
            "folder": "folders",
            "folder_item": "folder_items",
            "file": "files",
        }.get(operation.split(".", 1)[0])
        result = {}
        collection = getattr(
            self.mail_service.repository.projection,
            collection_name or "",
            {},
        )
        if entity_uuid in collection:
            result.update(copy.deepcopy(collection[entity_uuid]))
        result.update(copy.deepcopy(payload))
        result["uuid"] = str(entity_uuid)
        return result

    def _stream_participants(self, stream_uuid: object) -> typing.Any:
        return tuple(
            sys_uuid.UUID(binding["user_uuid"])
            for binding in self.mail_service.repository.projection.bindings.values()
            if binding.get("stream_uuid") == str(stream_uuid)
        )

    @staticmethod
    def _projection_values(values: typing.Any) -> typing.Any:
        return resource_projection.projection_values(values)


@dataclasses.dataclass
class _ProjectProjectionState:
    lock: typing.Any = dataclasses.field(default_factory=threading.RLock)
    projection: mail_repository.Projection | None = None
    canonical_event_states: dict[sys_uuid.UUID, _CanonicalEventState] = (
        dataclasses.field(default_factory=dict)
    )


class MailEventStore:
    """Read one user's event journal without loading the project state journal."""

    def __init__(
        self,
        project_uuid: object,
        user_uuid: object,
        repository: typing.Any,
    ) -> None:
        self.project_uuid = project_uuid
        self.user_uuid = user_uuid
        self.repository = repository

    @staticmethod
    def _after_epoch_version(filters: typing.Any) -> typing.Any:
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
        self, after: typing.Any, epoch_generation: typing.Any
    ) -> typing.Any:
        state = self.repository.event_cursor_state(self.user_uuid)
        reason = None
        if after > 0 and epoch_generation is None:
            reason = "epoch_generation_required"
        elif (
            epoch_generation is not None and epoch_generation != state.epoch_generation
        ):
            reason = "epoch_generation_changed"
        elif after > state.current_epoch_version:
            reason = "future_epoch"
        elif after < state.minimum_epoch_version - 1:
            reason = "epoch_pruned"
        if reason is not None:
            raise messenger_exc.EventsCursorExpiredError(
                reason=reason,
                epoch_generation=state.epoch_generation,
                current_epoch_version=state.current_epoch_version,
                minimum_epoch_version=state.minimum_epoch_version,
            )
        return state

    def events_after(
        self,
        filters: typing.Any,
        order_by: typing.Any = None,
        epoch_generation: typing.Any = None,
        limit: typing.Any = None,
    ) -> typing.Any:
        del order_by
        after, clauses = self._after_epoch_version(filters)
        state = self._validate_event_cursor(after, epoch_generation)
        events = self.repository.events_after(
            self.user_uuid,
            mail_repository.EpochCursor(int(state.epoch_generation), after),
            limit=limit,
        )
        result = [event.as_dict() for event in events]
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

    def current_epoch(self) -> typing.Any:
        return self.event_cursor()["current_epoch_version"]

    def event_cursor(self) -> typing.Any:
        return self.repository.event_cursor_state(self.user_uuid).as_dict()


class SQLProjectedMessengerStoreFactory:
    def __init__(
        self,
        runtime_factory: mail_runtime.RuntimeFactory,
        bridge_config: typing.Any = None,
    ) -> None:
        self.runtime_factory = runtime_factory
        self.bridge_config = bridge_config
        self._states: dict[sys_uuid.UUID, _ProjectProjectionState] = {}
        self._states_lock: typing.Any = threading.Lock()

    def _state(self, project_uuid: sys_uuid.UUID) -> _ProjectProjectionState:
        with self._states_lock:
            return self._states.setdefault(project_uuid, _ProjectProjectionState())

    def move_stream_projection(
        self,
        *,
        chat_uuid: object,
        revision: typing.Any,
        owner_uuid: object,
        stream_uuid: object,
        old_project_uuid: object,
        new_project_uuid: object = None,
        write_new: typing.Any = True,
        write_old: typing.Any = True,
    ) -> None:
        """Preserve the transitional mail journal during a project move."""
        if stream_uuid is None or old_project_uuid is None:
            return
        stream_uuid = sys_uuid.UUID(str(stream_uuid))
        old_project_uuid = sys_uuid.UUID(str(old_project_uuid))
        owner_uuid = sys_uuid.UUID(str(owner_uuid))
        occurred_at = datetime.datetime.now(datetime.timezone.utc)
        with self(old_project_uuid, owner_uuid) as old_store:
            projection = copy.deepcopy(old_store.mail_service.repository.projection)
        stream = projection.streams.get(stream_uuid)
        if stream is None:
            return
        message_uuids = {
            message_uuid
            for message_uuid, value in projection.messages.items()
            if value.get("stream_uuid") == str(stream_uuid)
        }

        def record(
            project_uuid: object,
            operation: typing.Any,
            entity_uuid: object,
            payload: typing.Any,
            suffix: typing.Any,
            when: typing.Any = None,
        ) -> typing.Any:
            return mail_repository.OperationRecord(
                project_uuid=typing.cast(sys_uuid.UUID, project_uuid),
                operation_uuid=sys_uuid.uuid5(
                    sys_uuid.UUID(str(chat_uuid)),
                    f"projection:{revision}:{project_uuid}:{suffix}:{entity_uuid}",
                ),
                actor_uuid=owner_uuid,
                operation=operation,
                entity_uuid=sys_uuid.UUID(str(entity_uuid)),
                payload=payload,
                occurred_at=when or occurred_at,
            )

        if new_project_uuid is not None and write_new:
            new_project_uuid = sys_uuid.UUID(str(new_project_uuid))
            operations = []
            stream_payload = copy.deepcopy(stream)
            stream_payload.pop("uuid", None)
            operations.append(
                record(
                    new_project_uuid,
                    "stream.create",
                    stream_uuid,
                    stream_payload,
                    "stream-create",
                )
            )
            for collection, operation, reference in (
                (projection.bindings, "binding.create", "stream_uuid"),
                (projection.topics, "topic.create", "stream_uuid"),
                (projection.files, "file.create", "stream_uuid"),
            ):
                for entity_uuid, value in collection.items():
                    if value.get(reference) != str(stream_uuid):
                        continue
                    payload = copy.deepcopy(value)
                    payload.pop("uuid", None)
                    operations.append(
                        record(
                            new_project_uuid,
                            operation,
                            entity_uuid,
                            payload,
                            operation,
                        )
                    )
            for message_uuid, value in projection.messages.items():
                if value.get("stream_uuid") != str(stream_uuid):
                    continue
                payload = copy.deepcopy(value)
                payload.pop("uuid", None)
                created_at = payload.pop("created_at", occurred_at)
                if isinstance(created_at, str):
                    created_at = datetime.datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                operations.append(
                    record(
                        new_project_uuid,
                        "message.create",
                        message_uuid,
                        payload,
                        "message-create",
                        when=created_at,
                    )
                )
            for reaction_uuid, value in projection.reactions.items():
                if sys_uuid.UUID(value["message_uuid"]) not in message_uuids:
                    continue
                payload = copy.deepcopy(value)
                payload.pop("uuid", None)
                operations.append(
                    record(
                        new_project_uuid,
                        "reaction.create",
                        reaction_uuid,
                        payload,
                        "reaction-create",
                    )
                )
            for (user_uuid, message_uuid), value in projection.message_states.items():
                if message_uuid not in message_uuids:
                    continue
                operations.append(
                    record(
                        new_project_uuid,
                        "message.state",
                        message_uuid,
                        {
                            "user_uuid": str(user_uuid),
                            "message_uuid": str(message_uuid),
                            **value,
                        },
                        f"message-state:{user_uuid}",
                    )
                )
            with self(new_project_uuid, owner_uuid) as new_store:
                for operation in operations:
                    new_store.mail_service.repository.append_operation(operation)
        if write_old:
            with self(old_project_uuid, owner_uuid) as old_store:
                repository = old_store.mail_service.repository
                for file_uuid, value in sorted(
                    projection.files.items(), key=lambda item: str(item[0])
                ):
                    if value.get("stream_uuid") != str(stream_uuid):
                        continue
                    repository.append_operation(
                        record(
                            old_project_uuid,
                            "file.delete",
                            file_uuid,
                            {},
                            "file-delete",
                        )
                    )
                for message_uuid in sorted(message_uuids, key=str):
                    repository.append_operation(
                        record(
                            old_project_uuid,
                            "message.delete",
                            message_uuid,
                            {},
                            "message-delete",
                        )
                    )
                repository.append_operation(
                    record(
                        old_project_uuid,
                        "stream.delete",
                        stream_uuid,
                        {},
                        "stream-delete",
                    )
                )

    @contextlib.contextmanager
    def draft_store(
        self, project_uuid: sys_uuid.UUID, user_uuid: sys_uuid.UUID
    ) -> typing.Iterator[typing.Any]:
        yield api_store.guard_api_store(
            project_uuid,
            typing.cast(
                api_store.MessengerStore,
                SQLDraftStore(project_uuid, user_uuid),
            ),
        )

    @contextlib.contextmanager
    def event_store(
        self, project_uuid: sys_uuid.UUID, user_uuid: sys_uuid.UUID
    ) -> typing.Iterator[typing.Any]:
        with self.runtime_factory.messenger_service(project_uuid) as service:
            yield MailEventStore(project_uuid, user_uuid, service.repository)

    @contextlib.contextmanager
    def __call__(
        self,
        project_uuid: sys_uuid.UUID,
        user_uuid: sys_uuid.UUID,
    ) -> typing.Iterator[SQLProjectedMessengerStore]:
        state = self._state(project_uuid)
        with state.lock:
            try:
                with self.runtime_factory.messenger_service(project_uuid) as service:
                    if state.projection is not None:
                        service.repository.projection = state.projection
                    state.projection = service.repository.refresh()
                    projected_store = SQLProjectedMessengerStore(
                        project_uuid,
                        user_uuid,
                        service,
                        canonical_event_states=state.canonical_event_states,
                        bridge_config=self.bridge_config,
                    )
                    yield typing.cast(
                        SQLProjectedMessengerStore,
                        api_store.guard_api_store(
                            project_uuid,
                            typing.cast(
                                api_store.MessengerStore,
                                projected_store,
                            ),
                        ),
                    )
                    state.projection = service.repository.projection
            except Exception:
                # A canonical write can succeed before its SQL projection fails.
                # Discard process-local state so the retry starts from IMAP rather
                # than trusting a possibly half-completed request snapshot.
                state.projection = None
                state.canonical_event_states.clear()
                raise
