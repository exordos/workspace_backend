# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Canonical-mail-first store with a rebuildable RESTAlchemy SQL projection."""

import contextlib
import copy
import dataclasses
import datetime
import enum
import threading
import typing
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.storage import exceptions as storage_exceptions

from workspace.messenger_api import exceptions as messenger_exc
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models
from workspace.messenger_mail import repository as mail_repository
from workspace.messenger_mail import runtime as mail_runtime


RESOURCE_MODELS = {
    "folders": models.UserFolder,
    "folder_items": models.UserFolderItem,
    "streams": models.WorkspaceUserStream,
    "stream_bindings": models.WorkspaceStreamBinding,
    "stream_topics": models.WorkspaceUserTopic,
    "messages": models.WorkspaceUserMessage,
    "message_reactions": models.WorkspaceMessageReactions,
    "files": models.WorkspaceFile,
    "users": models.WorkspaceUser,
}
USER_SCOPED_RESOURCES = {
    "folders",
    "folder_items",
    "streams",
    "stream_topics",
    "messages",
}
EXTENSION_RESOURCES = {
    "streams",
    "stream_topics",
    "messages",
    "message_reactions",
}
EVENT_NAMESPACE = sys_uuid.UUID("798900cd-aef4-5277-9989-d4aac5fbfc8a")


@dataclasses.dataclass
class _CanonicalEventState:
    cursor: mail_repository.EpochCursor | None = None
    event_uuids: set[sys_uuid.UUID] = dataclasses.field(default_factory=set)


def _simple(value):
    if isinstance(value, sys_uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {name: _simple(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_simple(item) for item in value]
    if hasattr(value, "properties") and hasattr(value.properties, "items"):
        return {
            name: _simple(prop.value)
            for name, prop in value.properties.items()
            if prop.value is not None
        }
    return value


def _as_dict(value, resource=None):
    result = _simple(value)
    if not isinstance(result, dict):
        raise TypeError("Messenger projection rows must serialize to dictionaries")
    if resource in EXTENSION_RESOURCES:
        result.setdefault("provider", None)
        result.setdefault("delivery", None)
    return result


def _plain_values(values):
    result = {
        name: value
        for name, value in values.items()
        if name not in {"provider", "delivery"}
    }
    return result


class SQLProjectedMessengerStore:
    """Writes canonical IMAP/SMTP state before updating the SQL projection."""

    def __init__(
        self,
        project_uuid: sys_uuid.UUID,
        user_uuid: sys_uuid.UUID,
        mail_service,
        canonical_event_states=None,
    ):
        self.project_uuid = project_uuid
        self.user_uuid = user_uuid
        self.mail_service = mail_service
        self._projection_event_epoch = self._latest_projection_event_epoch()
        self._canonical_event_states = (
            {} if canonical_event_states is None else canonical_event_states
        )

    def _latest_projection_event_epoch(self):
        events = models.WorkspaceEvent.objects.get_all(
            filters={"project_id": dm_filters.EQ(self.project_uuid)},
            order_by={"epoch_version": "desc"},
            limit=1,
        )
        return 0 if not events else events[0].epoch_version

    def _known_event_uuids(self, user_uuid):
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

    def _sync_projection_events(self):
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

    def _scope_filters(self, resource, filters):
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

    def sync_iam_identity(self, values):
        row = models.WorkspaceUser.sync_iam_identity(**values)
        return _as_dict(row, "users")

    def filter_resources(self, resource, filters, order_by=None):
        rows = RESOURCE_MODELS[resource].objects.get_all(
            filters=self._scope_filters(resource, filters),
            order_by=order_by,
        )
        if resource == "files":
            rows = [row for row in rows if self._can_read_file(row)]
        return [_as_dict(row, resource) for row in rows]

    def filter_message_page(
        self,
        filters,
        marker_uuid,
        sort_direction,
        limit,
    ):
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

    def get_resource(self, resource, resource_uuid):
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

    def _record(self, operation, entity_uuid, payload):
        return mail_repository.OperationRecord(
            project_uuid=self.project_uuid,
            operation_uuid=sys_uuid.uuid4(),
            actor_uuid=self.user_uuid,
            operation=operation,
            entity_uuid=entity_uuid,
            payload=typing.cast(dict, _simple(payload)),
            occurred_at=datetime.datetime.now(datetime.timezone.utc),
        )

    def _append(self, operation, entity_uuid, payload):
        payload = self._canonical_snapshot(operation, entity_uuid, payload)
        payload.setdefault(
            "_event_recipient_uuids",
            [str(value) for value in self._event_recipients(operation, payload)],
        )
        record = self._record(operation, entity_uuid, payload)
        self.mail_service.repository.append_operation(record)
        return record

    def create_resource(self, resource, values):
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

    def update_resource(self, resource, resource_uuid, values):
        values = _plain_values(values)
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
        if operation is not None:
            self._append(operation, resource_uuid, values)
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
        elif resource == "stream_topics":
            row = helpers.update_workspace_user_stream_topic(
                self.project_uuid,
                self.user_uuid,
                resource_uuid,
                self._projection_values(values),
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

    def delete_resource(self, resource, resource_uuid):
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
        if operation is not None:
            self._append(operation, resource_uuid, {})
        if resource == "folders":
            row = helpers.delete_workspace_user_folder(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "folder_items":
            row = helpers.delete_workspace_user_folder_item(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "streams":
            row = helpers.delete_workspace_user_stream(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "stream_bindings":
            row = helpers.delete_workspace_stream_binding(
                self.project_uuid, resource_uuid
            )
        elif resource == "stream_topics":
            row = helpers.delete_workspace_user_stream_topic(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "message_reactions":
            row = helpers.delete_workspace_message_reaction(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "files":
            file = helpers.get_workspace_owned_file(
                self.project_uuid, self.user_uuid, resource_uuid
            )
            self._append("file.delete", resource_uuid, _as_dict(file))
            row = helpers.delete_workspace_file(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        else:
            raise ValueError(f"Unsupported Messenger delete resource {resource}")
        self._sync_projection_events()
        return None if row is None else _as_dict(row, resource)

    def create_message(self, values):
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
        self._sync_projection_events()
        return _as_dict(row, "messages")

    def update_message(self, message_uuid, values):
        values = _plain_values(values)
        self._append("message.update", message_uuid, values)
        row = helpers.update_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
            self._projection_values(values),
        )
        self._sync_projection_events()
        return _as_dict(row, "messages")

    def delete_message(self, message_uuid):
        message = self.mail_service.repository.projection.messages[message_uuid]
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
        row = helpers.delete_workspace_user_message(
            self.project_uuid,
            self.user_uuid,
            message_uuid,
        )
        self._sync_projection_events()
        return None if row is None else _as_dict(row, "messages")

    def perform_action(self, resource, resource_uuid, action, values):
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
            for (
                message_uuid,
                message,
            ) in self.mail_service.repository.projection.messages.items():
                if message.get("stream_uuid") == str(resource_uuid):
                    self._append(
                        "message.read",
                        message_uuid,
                        {"user_uuid": str(self.user_uuid)},
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
            row = helpers.read_workspace_user_stream_topic_messages(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "messages" and action == "read":
            self._append(
                "message.read",
                resource_uuid,
                {"user_uuid": str(self.user_uuid)},
            )
            row = helpers.read_workspace_user_message(
                self.project_uuid, self.user_uuid, resource_uuid
            )
        elif resource == "messages" and action == "read_up_to":
            target = self.mail_service.repository.projection.messages[resource_uuid]
            target_created_at = target.get("created_at", "")
            for (
                message_uuid,
                message,
            ) in self.mail_service.repository.projection.messages.items():
                if (
                    message.get("topic_uuid") == target.get("topic_uuid")
                    and message.get("created_at", "") <= target_created_at
                ):
                    self._append(
                        "message.read",
                        message_uuid,
                        {"user_uuid": str(self.user_uuid)},
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
    def _after_epoch_version(filters):
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

    def _validate_event_cursor(self, after, epoch_generation):
        state = self.mail_service.repository.event_cursor_state(self.user_uuid)
        reason = None
        if after > 0 and epoch_generation is None:
            reason = "epoch_generation_required"
        elif (
            epoch_generation is not None
            and epoch_generation != state.epoch_generation
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

    def events_after(self, filters, order_by=None, epoch_generation=None):
        del order_by
        after, clauses = self._after_epoch_version(filters)
        state = self._validate_event_cursor(after, epoch_generation)
        events = self.mail_service.repository.events_after(
            self.user_uuid,
            mail_repository.EpochCursor(int(state.epoch_generation), after),
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

    def current_epoch(self):
        return self.event_cursor()["current_epoch_version"]

    def event_cursor(self):
        return self.mail_service.repository.event_cursor_state(
            self.user_uuid
        ).as_dict()

    def replay_operations(self, projector, after_uid=0):
        replay = self.mail_service.repository.read_operations(after_uid=after_uid)
        for entry in replay.entries:
            projector(entry.record)
        return replay

    def rebuild_sql_projection(self):
        projection = self.mail_service.repository.rebuild()
        collections = (
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
        model,
        entity_uuid,
        payload,
        user_uuid=None,
    ):
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

    def _binding_uuid(self, stream_uuid, user_uuid):
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

    def _is_direct_stream(self, stream_uuid):
        stream = self.mail_service.repository.projection.streams.get(
            sys_uuid.UUID(str(stream_uuid)),
            {},
        )
        return stream.get("kind") == "direct"

    def _validate_stream_participants(self, stream_uuid, participants):
        try:
            self.mail_service.validate_stream_participants(
                sys_uuid.UUID(str(stream_uuid)),
                tuple(sorted(participants, key=str)),
            )
        except mail_repository.InvalidJournalRecord as exc:
            raise ra_exceptions.ValidationErrorException() from exc

    def _can_read_file(self, file):
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

    def _delete_replaced_avatar_file(self, avatar):
        if not avatar.startswith(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX):
            return
        helpers.delete_workspace_avatar_file(
            self.user_uuid,
            sys_uuid.UUID(avatar[len(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX) :]),
        )

    def _event_recipients(self, operation, payload):
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

    def _canonical_snapshot(self, operation, entity_uuid, payload):
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

    def _stream_participants(self, stream_uuid):
        return tuple(
            sys_uuid.UUID(binding["user_uuid"])
            for binding in self.mail_service.repository.projection.bindings.values()
            if binding.get("stream_uuid") == str(stream_uuid)
        )

    @staticmethod
    def _projection_values(values):
        result = values.copy()
        for name in ("project_id", "user_uuid", "provider", "delivery"):
            result.pop(name, None)
        source = result.get("source")
        if isinstance(source, dict):
            kind = source.get("kind", result.get("source_name", "native"))
            if kind == models.SourceName.NATIVE.value:
                result["source"] = models.NativeSource()
            else:
                source_values = source.copy()
                source_values.pop("kind", None)
                result["source"] = models.ZulipSource(**source_values)
        return result


@dataclasses.dataclass
class _ProjectProjectionState:
    lock: typing.Any = dataclasses.field(default_factory=threading.RLock)
    projection: mail_repository.Projection | None = None
    canonical_event_states: dict[sys_uuid.UUID, _CanonicalEventState] = (
        dataclasses.field(default_factory=dict)
    )


class SQLProjectedMessengerStoreFactory:
    def __init__(self, runtime_factory: mail_runtime.RuntimeFactory):
        self.runtime_factory = runtime_factory
        self._states: dict[sys_uuid.UUID, _ProjectProjectionState] = {}
        self._states_lock: typing.Any = threading.Lock()

    def _state(self, project_uuid: sys_uuid.UUID) -> _ProjectProjectionState:
        with self._states_lock:
            return self._states.setdefault(project_uuid, _ProjectProjectionState())

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
                    yield SQLProjectedMessengerStore(
                        project_uuid,
                        user_uuid,
                        service,
                        canonical_event_states=state.canonical_event_states,
                    )
                    state.projection = service.repository.projection
            except Exception:
                # A canonical write can succeed before its SQL projection fails.
                # Discard process-local state so the retry starts from IMAP rather
                # than trusting a possibly half-completed request snapshot.
                state.projection = None
                state.canonical_event_states.clear()
                raise
