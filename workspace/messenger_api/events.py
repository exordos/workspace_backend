#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import uuid as sys_uuid

import webob
from restalchemy.api import contexts as ra_contexts
from restalchemy.api import packers as ra_packers
from restalchemy.api import resources as ra_resources
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import engines

from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import models
from workspace.provider_api import payloads as provider_payloads


EVENTS_CHANNEL = "workspace_events"
MESSAGE_OBJECT_TYPE = "message"
MESSAGE_REACTION_OBJECT_TYPE = "message_reaction"
STREAM_OBJECT_TYPE = "stream"
STREAM_BINDING_OBJECT_TYPE = "stream_binding"
TOPIC_OBJECT_TYPE = "topic"
USER_OBJECT_TYPE = "user"
FOLDER_OBJECT_TYPE = "folder"
FOLDER_ITEM_OBJECT_TYPE = "folder_item"
CREATED_ACTION = "created"
UPDATED_ACTION = "updated"
DELETED_ACTION = "deleted"
READ_ACTION = "read"
MESSAGE_CREATED_EVENT = event_payloads.MessageCreatedEventPayload.KIND
MESSAGE_UPDATED_EVENT = event_payloads.MessageUpdatedEventPayload.KIND
MESSAGE_READ_EVENT = event_payloads.MessageReadEventPayload.KIND
MESSAGES_READ_EVENT = event_payloads.MessagesReadEventPayload.KIND
MESSAGE_DELETED_EVENT = event_payloads.MessageDeletedEventPayload.KIND
MESSAGE_REACTION_CREATED_EVENT = event_payloads.MessageReactionCreatedEventPayload.KIND
MESSAGE_REACTION_UPDATED_EVENT = event_payloads.MessageReactionUpdatedEventPayload.KIND
MESSAGE_REACTION_DELETED_EVENT = event_payloads.MessageReactionDeletedEventPayload.KIND
STREAM_CREATED_EVENT = event_payloads.StreamCreatedEventPayload.KIND
STREAM_UPDATED_EVENT = event_payloads.StreamUpdatedEventPayload.KIND
STREAM_READ_EVENT = event_payloads.StreamReadEventPayload.KIND
STREAM_DELETED_EVENT = event_payloads.StreamDeletedEventPayload.KIND
STREAM_BINDINGS_CREATED_EVENT = event_payloads.StreamBindingsCreatedEventPayload.KIND
USER_UPDATED_EVENT = event_payloads.UserUpdatedEventPayload.KIND
TOPIC_CREATED_EVENT = event_payloads.TopicCreatedEventPayload.KIND
TOPIC_UPDATED_EVENT = event_payloads.TopicUpdatedEventPayload.KIND
TOPIC_READ_EVENT = event_payloads.TopicReadEventPayload.KIND
TOPIC_DELETED_EVENT = event_payloads.TopicDeletedEventPayload.KIND
FOLDER_CREATED_EVENT = event_payloads.FolderCreatedEventPayload.KIND
FOLDER_UPDATED_EVENT = event_payloads.FolderUpdatedEventPayload.KIND
FOLDER_DELETED_EVENT = event_payloads.FolderDeletedEventPayload.KIND
FOLDER_ITEM_DELETED_EVENT = event_payloads.FolderItemDeletedEventPayload.KIND
EVENT_METADATA = {
    MESSAGE_CREATED_EVENT: (MESSAGE_OBJECT_TYPE, CREATED_ACTION),
    MESSAGE_UPDATED_EVENT: (MESSAGE_OBJECT_TYPE, UPDATED_ACTION),
    MESSAGE_READ_EVENT: (MESSAGE_OBJECT_TYPE, READ_ACTION),
    MESSAGES_READ_EVENT: (MESSAGE_OBJECT_TYPE, READ_ACTION),
    MESSAGE_DELETED_EVENT: (MESSAGE_OBJECT_TYPE, DELETED_ACTION),
    MESSAGE_REACTION_CREATED_EVENT: (
        MESSAGE_REACTION_OBJECT_TYPE,
        CREATED_ACTION,
    ),
    MESSAGE_REACTION_UPDATED_EVENT: (
        MESSAGE_REACTION_OBJECT_TYPE,
        UPDATED_ACTION,
    ),
    MESSAGE_REACTION_DELETED_EVENT: (
        MESSAGE_REACTION_OBJECT_TYPE,
        DELETED_ACTION,
    ),
    STREAM_CREATED_EVENT: (STREAM_OBJECT_TYPE, CREATED_ACTION),
    STREAM_UPDATED_EVENT: (STREAM_OBJECT_TYPE, UPDATED_ACTION),
    STREAM_READ_EVENT: (STREAM_OBJECT_TYPE, READ_ACTION),
    STREAM_DELETED_EVENT: (STREAM_OBJECT_TYPE, DELETED_ACTION),
    STREAM_BINDINGS_CREATED_EVENT: (
        STREAM_BINDING_OBJECT_TYPE,
        CREATED_ACTION,
    ),
    USER_UPDATED_EVENT: (USER_OBJECT_TYPE, UPDATED_ACTION),
    TOPIC_CREATED_EVENT: (TOPIC_OBJECT_TYPE, CREATED_ACTION),
    TOPIC_UPDATED_EVENT: (TOPIC_OBJECT_TYPE, UPDATED_ACTION),
    TOPIC_READ_EVENT: (TOPIC_OBJECT_TYPE, READ_ACTION),
    TOPIC_DELETED_EVENT: (TOPIC_OBJECT_TYPE, DELETED_ACTION),
    FOLDER_CREATED_EVENT: (FOLDER_OBJECT_TYPE, CREATED_ACTION),
    FOLDER_UPDATED_EVENT: (FOLDER_OBJECT_TYPE, UPDATED_ACTION),
    FOLDER_DELETED_EVENT: (FOLDER_OBJECT_TYPE, DELETED_ACTION),
    FOLDER_ITEM_DELETED_EVENT: (FOLDER_ITEM_OBJECT_TYPE, DELETED_ACTION),
}
WORKSPACE_EVENT_RESOURCE = ra_resources.ResourceByRAModel(
    model_class=models.WorkspaceVisibleEvent,
    convert_underscore=False,
    process_filters=True,
)
DEFAULT_EVENTS_LIMIT = 100
MAX_EVENTS_LIMIT = 500
WORKSPACE_USER_MESSAGE_FIELDS = tuple(models.WorkspaceUserMessage.properties.properties)
WORKSPACE_USER_STREAM_FIELDS = tuple(
    name
    for name in models.WorkspaceUserStream.properties.properties
    if name != "private_index"
)
WORKSPACE_STREAM_BINDING_FIELDS = tuple(
    models.WorkspaceStreamBinding.properties.properties
)
WORKSPACE_USER_TOPIC_FIELDS = tuple(models.WorkspaceUserTopic.properties.properties)
WORKSPACE_USER_FOLDER_FIELDS = tuple(models.UserFolder.properties.properties)
WORKSPACE_USER_FIELDS = tuple(models.WorkspaceUser.properties.properties)


def _to_uuid_string(value):
    return str(value).lower()


def _model_to_event_payload_value(value):
    return {
        name: _event_payload_value(name, prop.value)
        for name, prop in value.properties.items()
    }


def _event_payload_value(name, value):
    if value is None:
        return None
    if name in ("created_at", "updated_at", "last_ping_at"):
        value = event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.from_simple_type(value)
        return event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(value)
    if isinstance(value, sys_uuid.UUID):
        return _to_uuid_string(value)
    if name == "uuid" or name.endswith("uuid") or name == "project_id":
        return _to_uuid_string(value)
    if hasattr(value, "properties") and hasattr(value.properties, "items"):
        return _model_to_event_payload_value(value)
    if isinstance(value, list):
        return [_event_payload_value(name, item) for item in value]
    if isinstance(value, dict):
        return {
            item_name: _event_payload_value(item_name, item_value)
            for item_name, item_value in value.items()
        }
    return value


def _event_payload_get(event_payload, name):
    if hasattr(event_payload, "get"):
        return event_payload.get(name)
    try:
        return event_payload[name]
    except KeyError:
        return None


def _message_from_event_payload(event_payload, session=None):
    result = {}
    for name in WORKSPACE_USER_MESSAGE_FIELDS:
        value = _event_payload_get(event_payload, name)
        if name == "reactions" and value is None:
            value = {}
        if value is None:
            continue
        result[name] = _event_payload_value(name, value)
    message = models.WorkspaceMessage.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(_event_payload_get(event_payload, "uuid")),
            "project_id": dm_filters.EQ(
                _event_payload_get(event_payload, "project_id"),
            ),
        },
        session=session,
    )
    return provider_payloads.add_provider_delivery_payload(
        result,
        message,
        session=session,
    )


def _folder_from_event_payload(event_payload):
    return {
        name: _event_payload_value(name, _event_payload_get(event_payload, name))
        for name in WORKSPACE_USER_FOLDER_FIELDS
    }


def _stream_from_event_payload(event_payload, session=None):
    result = {
        name: _event_payload_value(name, _event_payload_get(event_payload, name))
        for name in WORKSPACE_USER_STREAM_FIELDS
    }
    stream = models.WorkspaceStream.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(_event_payload_get(event_payload, "uuid")),
            "project_id": dm_filters.EQ(
                _event_payload_get(event_payload, "project_id"),
            ),
        },
        session=session,
    )
    return provider_payloads.add_provider_delivery_payload(
        result,
        stream,
        session=session,
    )


def _topic_from_event_payload(event_payload, session=None):
    result = {}
    for name in WORKSPACE_USER_TOPIC_FIELDS:
        value = _event_payload_get(event_payload, name)
        if name == "notification_mode" and value is None:
            value = models.WorkspaceTopicNotificationMode.DEFAULT.value
        result[name] = _event_payload_value(name, value)
    topic = models.WorkspaceStreamTopic.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(_event_payload_get(event_payload, "uuid")),
            "project_id": dm_filters.EQ(
                _event_payload_get(event_payload, "project_id"),
            ),
        },
        session=session,
    )
    return provider_payloads.add_provider_delivery_payload(
        result,
        topic,
        session=session,
    )


def _stream_binding_snapshot_from_mapping(value):
    return {
        name: _event_payload_value(name, _event_payload_get(value, name))
        for name in WORKSPACE_STREAM_BINDING_FIELDS
    }


def _user_from_event_payload(event_payload):
    return {
        name: _event_payload_value(name, _event_payload_get(event_payload, name))
        for name in WORKSPACE_USER_FIELDS
    }


def _payload_with_kind(kind, payload):
    result = {"kind": kind}
    result.update(payload)
    return result


def _event_metadata_for_kind(kind):
    try:
        return EVENT_METADATA[kind]
    except KeyError:
        raise ra_exc.ValidationErrorException()


def _event_row_get(row, name, default=None):
    if hasattr(row, name):
        return getattr(row, name)
    try:
        return row[name]
    except (KeyError, TypeError):
        return default


def _event_row_uuid(value, default=None):
    if value is None:
        return default
    if isinstance(value, sys_uuid.UUID):
        return value
    return sys_uuid.UUID(str(value))


def _workspace_event_from_row(row):
    if isinstance(row, models.WorkspaceEvent):
        return row
    payload = row["payload"]
    object_type, action = _event_metadata_for_kind(payload["kind"])
    schema_version = _event_row_get(row, "schema_version")
    row_object_type = _event_row_get(row, "object_type")
    row_action = _event_row_get(row, "action")
    values = {
        "schema_version": (schema_version or models.WORKSPACE_EVENT_SCHEMA_VERSION),
        "uuid": _event_row_uuid(_event_row_get(row, "uuid"), sys_uuid.uuid4()),
        "epoch_version": _event_row_get(row, "epoch_version"),
        "project_id": _event_row_uuid(
            _event_row_get(row, "project_id") or payload.get("project_id"),
            sys_uuid.uuid4(),
        ),
        "user_uuid": _event_row_uuid(
            _event_row_get(row, "user_uuid") or payload.get("user_uuid"),
            sys_uuid.uuid4(),
        ),
        "object_type": row_object_type or object_type,
        "action": row_action or action,
        "payload": payload,
    }
    created_at = _event_row_get(row, "created_at")
    updated_at = _event_row_get(row, "updated_at")
    if created_at is not None:
        values["created_at"] = created_at
    if updated_at is not None:
        values["updated_at"] = updated_at
    return models.WorkspaceEvent(**values)


_PACKER_REQUEST = None


def _get_packer_request():
    global _PACKER_REQUEST
    if _PACKER_REQUEST is None:
        request = webob.Request.blank("/")
        request.api_context = ra_contexts.RequestContext(request)
        _PACKER_REQUEST = request
    return _PACKER_REQUEST


def pack_workspace_event(event, request=None):
    request = request or _get_packer_request()
    return ra_packers.BaseResourcePacker(
        WORKSPACE_EVENT_RESOURCE,
        request,
    ).pack(event)


def event_row_to_messenger_event(row):
    return pack_workspace_event(_workspace_event_from_row(row))


def _fetch_one(session, statement, values):
    result = session.execute(statement, values)
    return result.fetchone()


def _create_workspace_event(project_id, user_uuid, kind, payload, session=None):
    object_type, action = _event_metadata_for_kind(kind)
    event = models.WorkspaceEvent(
        schema_version=models.WORKSPACE_EVENT_SCHEMA_VERSION,
        uuid=sys_uuid.uuid4(),
        project_id=project_id,
        user_uuid=user_uuid,
        object_type=object_type,
        action=action,
        payload=_payload_with_kind(kind, payload),
    )
    return event.insert(session=session)


def create_message_events(project_id, message, recipients, session=None):
    if not recipients:
        return []

    user_messages = models.WorkspaceUserMessage.objects.get_all(
        filters={
            "uuid": dm_filters.EQ(message.uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.In(recipients),
        },
        order_by={"user_uuid": "asc"},
        session=session,
    )

    result = []
    for user_message in user_messages:
        epoch_version = _create_workspace_event(
            project_id=user_message.project_id,
            user_uuid=user_message.user_uuid,
            kind=MESSAGE_CREATED_EVENT,
            payload=_message_from_event_payload(user_message, session=session),
            session=session,
        )
        result.append(epoch_version)
    return sorted(result)


def create_message_updated_event(message, session=None):
    return _create_workspace_event(
        project_id=message.project_id,
        user_uuid=message.user_uuid,
        kind=MESSAGE_UPDATED_EVENT,
        payload=_message_from_event_payload(message, session=session),
        session=session,
    )


def create_message_read_event(message, session=None):
    return _create_workspace_event(
        project_id=message.project_id,
        user_uuid=message.user_uuid,
        kind=MESSAGE_READ_EVENT,
        payload=_message_from_event_payload(message, session=session),
        session=session,
    )


def create_messages_read_event(project_id, user_uuid, message_uuids, session=None):
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=MESSAGES_READ_EVENT,
        payload={
            "project_id": _event_payload_value("project_id", project_id),
            "message_uuids": [
                _event_payload_value("uuid", message_uuid)
                for message_uuid in message_uuids
            ],
        },
        session=session,
    )


def create_message_deleted_event(
    project_id,
    user_uuid,
    message_uuid,
    stream_uuid,
    topic_uuid,
    author_uuid,
    source_name,
    source,
    session=None,
):
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=MESSAGE_DELETED_EVENT,
        payload={
            "uuid": _event_payload_value("uuid", message_uuid),
            "stream_uuid": _event_payload_value("stream_uuid", stream_uuid),
            "topic_uuid": _event_payload_value("topic_uuid", topic_uuid),
            "author_uuid": _event_payload_value("author_uuid", author_uuid),
            "source_name": _event_payload_value("source_name", source_name),
            "source": _event_payload_value("source", source),
        },
        session=session,
    )


def _message_reaction_event_payload(
    reaction,
    message,
    session=None,
    **values,
):
    payload = {
        "uuid": _event_payload_value("uuid", reaction.uuid),
        "project_id": _event_payload_value("project_id", reaction.project_id),
        "message_uuid": _event_payload_value(
            "message_uuid",
            reaction.message_uuid,
        ),
        "user_uuid": _event_payload_value("user_uuid", reaction.user_uuid),
        "emoji_name": _event_payload_value(
            "emoji_name",
            reaction.emoji_name,
        ),
        "source_name": _event_payload_value(
            "source_name",
            message.source_name,
        ),
        "source": _event_payload_value("source", message.source),
    }
    payload.update(values)
    return provider_payloads.add_provider_delivery_payload(
        payload,
        reaction,
        session=session,
    )


def create_message_reaction_created_event(reaction, message, session=None):
    return _create_workspace_event(
        project_id=reaction.project_id,
        user_uuid=reaction.user_uuid,
        kind=MESSAGE_REACTION_CREATED_EVENT,
        payload=_message_reaction_event_payload(
            reaction,
            message,
            session=session,
        ),
        session=session,
    )


def create_message_reaction_updated_event(
    reaction,
    message,
    old_message,
    old_emoji_name,
    session=None,
):
    return _create_workspace_event(
        project_id=reaction.project_id,
        user_uuid=reaction.user_uuid,
        kind=MESSAGE_REACTION_UPDATED_EVENT,
        payload=_message_reaction_event_payload(
            reaction,
            message,
            session=session,
            old_message_uuid=_event_payload_value(
                "message_uuid",
                old_message.uuid,
            ),
            old_emoji_name=_event_payload_value(
                "emoji_name",
                old_emoji_name,
            ),
            old_source_name=_event_payload_value(
                "source_name",
                old_message.source_name,
            ),
            old_source=_event_payload_value("source", old_message.source),
        ),
        session=session,
    )


def create_message_reaction_deleted_event(reaction, message, session=None):
    return _create_workspace_event(
        project_id=reaction.project_id,
        user_uuid=reaction.user_uuid,
        kind=MESSAGE_REACTION_DELETED_EVENT,
        payload=_message_reaction_event_payload(
            reaction,
            message,
            session=session,
        ),
        session=session,
    )


def create_folder_event(folder, session=None):
    return _create_workspace_event(
        project_id=folder.project_id,
        user_uuid=folder.user_uuid,
        kind=FOLDER_CREATED_EVENT,
        payload=_folder_from_event_payload(folder),
        session=session,
    )


def create_stream_event(stream, session=None):
    return _create_workspace_event(
        project_id=stream.project_id,
        user_uuid=stream.user_uuid,
        kind=STREAM_CREATED_EVENT,
        payload=_stream_from_event_payload(stream, session=session),
        session=session,
    )


def create_stream_updated_event(stream, session=None):
    return _create_workspace_event(
        project_id=stream.project_id,
        user_uuid=stream.user_uuid,
        kind=STREAM_UPDATED_EVENT,
        payload=_stream_from_event_payload(stream, session=session),
        session=session,
    )


def create_stream_read_event(stream, session=None):
    return _create_workspace_event(
        project_id=stream.project_id,
        user_uuid=stream.user_uuid,
        kind=STREAM_READ_EVENT,
        payload=_stream_from_event_payload(stream, session=session),
        session=session,
    )


def create_topic_event(topic, session=None):
    return _create_workspace_event(
        project_id=topic.project_id,
        user_uuid=topic.user_uuid,
        kind=TOPIC_CREATED_EVENT,
        payload=_topic_from_event_payload(topic, session=session),
        session=session,
    )


def create_topic_updated_event(topic, session=None):
    return _create_workspace_event(
        project_id=topic.project_id,
        user_uuid=topic.user_uuid,
        kind=TOPIC_UPDATED_EVENT,
        payload=_topic_from_event_payload(topic, session=session),
        session=session,
    )


def create_topic_read_event(topic, session=None):
    return _create_workspace_event(
        project_id=topic.project_id,
        user_uuid=topic.user_uuid,
        kind=TOPIC_READ_EVENT,
        payload=_topic_from_event_payload(topic, session=session),
        session=session,
    )


def create_topic_deleted_event(
    project_id, user_uuid, topic_uuid, stream_uuid, source_name, source, session=None
):
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=TOPIC_DELETED_EVENT,
        payload={
            "uuid": _event_payload_value("uuid", topic_uuid),
            "stream_uuid": _event_payload_value("stream_uuid", stream_uuid),
            "source_name": _event_payload_value("source_name", source_name),
            "source": _event_payload_value("source", source),
        },
        session=session,
    )


def create_stream_deleted_event(
    project_id, user_uuid, stream_uuid, source_name, source, session=None
):
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=STREAM_DELETED_EVENT,
        payload={
            "uuid": _event_payload_value("uuid", stream_uuid),
            "source_name": _event_payload_value("source_name", source_name),
            "source": _event_payload_value("source", source),
        },
        session=session,
    )


def create_stream_bindings_created_event(bindings, user_uuid, session=None):
    binding = bindings[0]
    return _create_workspace_event(
        project_id=binding.project_id,
        user_uuid=user_uuid,
        kind=STREAM_BINDINGS_CREATED_EVENT,
        payload={
            "uuid": _event_payload_value("uuid", binding.stream_uuid),
            "items": [
                _stream_binding_snapshot_from_mapping(stream_binding)
                for stream_binding in bindings
            ],
        },
        session=session,
    )


def create_user_updated_events(user, project_id, recipient_user_uuids, session=None):
    result = []
    payload = _user_from_event_payload(user)
    for recipient_user_uuid in recipient_user_uuids:
        result.append(
            _create_workspace_event(
                project_id=project_id,
                user_uuid=recipient_user_uuid,
                kind=USER_UPDATED_EVENT,
                payload=payload,
                session=session,
            )
        )
    return result


def create_folder_updated_event(folder, session=None):
    return _create_workspace_event(
        project_id=folder.project_id,
        user_uuid=folder.user_uuid,
        kind=FOLDER_UPDATED_EVENT,
        payload=_folder_from_event_payload(folder),
        session=session,
    )


def create_folder_deleted_event(project_id, user_uuid, folder_uuid, session=None):
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=FOLDER_DELETED_EVENT,
        payload={"uuid": _event_payload_value("uuid", folder_uuid)},
        session=session,
    )


def create_folder_item_deleted_event(project_id, user_uuid, item_uuid, session=None):
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=FOLDER_ITEM_DELETED_EVENT,
        payload={"uuid": _event_payload_value("uuid", item_uuid)},
        session=session,
    )


def get_events_after(
    project_id,
    user_uuid,
    after_epoch_version=0,
    limit=DEFAULT_EVENTS_LIMIT,
    session=None,
):
    events = models.WorkspaceVisibleEvent.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
            "epoch_version": dm_filters.GT(after_epoch_version),
        },
        order_by={"epoch_version": "asc"},
        limit=limit,
        session=session,
    )
    return [pack_workspace_event(event) for event in events]


def get_event_for_user(project_id, user_uuid, epoch_version, session=None):
    events = models.WorkspaceVisibleEvent.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
            "epoch_version": dm_filters.EQ(epoch_version),
        },
        limit=1,
        session=session,
    )
    return None if not events else pack_workspace_event(events[0])


def get_current_epoch_version(project_id, user_uuid, session=None):
    engine = engines.engine_factory.get_engine()
    with engine.session_manager(session=session) as s:
        row = _fetch_one(
            s,
            """
            SELECT COALESCE(MAX(epoch_version), 0) AS epoch_version
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
            """,
            (str(project_id), str(user_uuid)),
        )
    return row["epoch_version"] if row is not None else 0


def normalize_epoch_version(value, default=0):
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ra_exc.ParseError(value=value)
    if parsed < 0:
        raise ra_exc.ParseError(value=value)
    return parsed


def normalize_events_limit(value, default=DEFAULT_EVENTS_LIMIT):
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ra_exc.ParseError(value=value)
    if parsed < 1:
        raise ra_exc.ParseError(value=value)
    return min(parsed, MAX_EVENTS_LIMIT)
