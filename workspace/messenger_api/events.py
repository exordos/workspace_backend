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

from restalchemy.common import exceptions as ra_exc
from restalchemy.storage.sql import engines

from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import models


EVENTS_CHANNEL = "workspace_events"
MESSAGE_CREATED_EVENT = event_payloads.MessageCreatedEventPayload.KIND
MESSAGE_UPDATED_EVENT = event_payloads.MessageUpdatedEventPayload.KIND
MESSAGES_READ_EVENT = event_payloads.MessagesReadEventPayload.KIND
MESSAGE_DELETED_EVENT = event_payloads.MessageDeletedEventPayload.KIND
STREAM_CREATED_EVENT = event_payloads.StreamCreatedEventPayload.KIND
STREAM_UPDATED_EVENT = event_payloads.StreamUpdatedEventPayload.KIND
STREAM_DELETED_EVENT = event_payloads.StreamDeletedEventPayload.KIND
STREAM_BINDINGS_CREATED_EVENT = (
    event_payloads.StreamBindingsCreatedEventPayload.KIND
)
USER_UPDATED_EVENT = event_payloads.UserUpdatedEventPayload.KIND
TOPIC_CREATED_EVENT = event_payloads.TopicCreatedEventPayload.KIND
TOPIC_UPDATED_EVENT = event_payloads.TopicUpdatedEventPayload.KIND
TOPIC_DELETED_EVENT = event_payloads.TopicDeletedEventPayload.KIND
FOLDER_CREATED_EVENT = event_payloads.FolderCreatedEventPayload.KIND
FOLDER_UPDATED_EVENT = event_payloads.FolderUpdatedEventPayload.KIND
FOLDER_DELETED_EVENT = event_payloads.FolderDeletedEventPayload.KIND
FOLDER_ITEM_DELETED_EVENT = (
    event_payloads.FolderItemDeletedEventPayload.KIND
)
FOLDER_EVENTS = (
    FOLDER_CREATED_EVENT,
    FOLDER_UPDATED_EVENT,
)
TOPIC_EVENTS = (
    TOPIC_CREATED_EVENT,
    TOPIC_UPDATED_EVENT,
)
DEFAULT_EVENTS_LIMIT = 100
MAX_EVENTS_LIMIT = 500
WORKSPACE_USER_MESSAGE_FIELDS = tuple(
    models.WorkspaceUserMessage.properties.properties
)
WORKSPACE_USER_STREAM_FIELDS = tuple(
    name for name in models.WorkspaceUserStream.properties.properties
    if name != "private_index"
)
WORKSPACE_STREAM_BINDING_FIELDS = tuple(
    models.WorkspaceStreamBinding.properties.properties
)
WORKSPACE_USER_TOPIC_FIELDS = tuple(
    models.WorkspaceUserTopic.properties.properties
)
WORKSPACE_USER_FOLDER_FIELDS = tuple(
    models.UserFolder.properties.properties
)
WORKSPACE_USER_FIELDS = tuple(
    models.WorkspaceUser.properties.properties
)


def _to_uuid_string(value):
    return str(value).lower()


def _event_payload_value(name, value):
    if value is None:
        return None
    if name in ("created_at", "updated_at", "last_ping_at"):
        value = event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.from_simple_type(
            value
        )
        return event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(value)
    if name == "uuid" or name.endswith("uuid") or name == "project_id":
        return _to_uuid_string(value)
    return value


def _event_payload_get(event_payload, name):
    if hasattr(event_payload, "get"):
        return event_payload.get(name)
    try:
        return event_payload[name]
    except KeyError:
        return None


def _message_from_event_payload(event_payload):
    result = {}
    for name in WORKSPACE_USER_MESSAGE_FIELDS:
        value = _event_payload_get(event_payload, name)
        if name == "reactions" and value is None:
            value = {}
        if value is None:
            continue
        result[name] = _event_payload_value(name, value)
    return result


def _folder_from_event_payload(event_payload):
    return {
        name: _event_payload_value(name, event_payload[name])
        for name in WORKSPACE_USER_FOLDER_FIELDS
    }


def _stream_from_event_payload(event_payload):
    return {
        name: _event_payload_value(name, _event_payload_get(event_payload, name))
        for name in WORKSPACE_USER_STREAM_FIELDS
    }


def _topic_from_event_payload(event_payload):
    result = {}
    for name in WORKSPACE_USER_TOPIC_FIELDS:
        value = _event_payload_get(event_payload, name)
        if name == "notification_mode" and value is None:
            value = models.WorkspaceTopicNotificationMode.DEFAULT.value
        result[name] = _event_payload_value(name, value)
    return result


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


def _stream_bindings_from_event_payload(event_payload):
    return [
        _stream_binding_snapshot_from_mapping(stream_binding)
        for stream_binding in event_payload["stream_bindings"]
    ]


def _deleted_stream_from_event_payload(event_payload):
    return {
        "uuid": _event_payload_value("uuid", event_payload["uuid"]),
    }


def _deleted_topic_from_event_payload(event_payload):
    return {
        "uuid": _event_payload_value("uuid", event_payload["uuid"]),
        "stream_uuid": _event_payload_value(
            "stream_uuid",
            event_payload["stream_uuid"],
        ),
    }


def _deleted_message_from_event_payload(event_payload):
    return {
        "uuid": _event_payload_value("uuid", event_payload["uuid"]),
        "stream_uuid": _event_payload_value(
            "stream_uuid",
            event_payload["stream_uuid"],
        ),
        "topic_uuid": _event_payload_value(
            "topic_uuid",
            event_payload["topic_uuid"],
        ),
    }


def _read_message_uuids_from_event_payload(event_payload):
    return [
        _event_payload_value("uuid", message_uuid)
        for message_uuid in event_payload["message_uuids"]
    ]


def _deleted_folder_from_event_payload(event_payload):
    return {
        "uuid": _event_payload_value("uuid", event_payload["uuid"]),
    }


def _deleted_folder_item_from_event_payload(event_payload):
    return {
        "uuid": _event_payload_value("uuid", event_payload["uuid"]),
    }


def event_row_to_messenger_event(row):
    payload = row["payload"]
    if payload["kind"] == MESSAGE_CREATED_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "message",
            "message": _message_from_event_payload(payload),
        }
    if payload["kind"] == MESSAGE_UPDATED_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "message",
            "kind": payload["kind"],
            "message": _message_from_event_payload(payload),
        }
    if payload["kind"] == MESSAGES_READ_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "message",
            "kind": payload["kind"],
            "message_uuids": _read_message_uuids_from_event_payload(payload),
        }
    if payload["kind"] == MESSAGE_DELETED_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "message",
            "kind": payload["kind"],
            "message": _deleted_message_from_event_payload(payload),
        }
    if payload["kind"] in (STREAM_CREATED_EVENT, STREAM_UPDATED_EVENT):
        return {
            "epoch_version": row["epoch_version"],
            "type": "stream",
            "kind": payload["kind"],
            "stream": _stream_from_event_payload(payload),
        }
    if payload["kind"] == STREAM_DELETED_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "stream",
            "kind": payload["kind"],
            "stream": _deleted_stream_from_event_payload(payload),
        }
    if payload["kind"] in TOPIC_EVENTS:
        return {
            "epoch_version": row["epoch_version"],
            "type": "topic",
            "kind": payload["kind"],
            "topic": _topic_from_event_payload(payload),
        }
    if payload["kind"] == TOPIC_DELETED_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "topic",
            "kind": payload["kind"],
            "topic": _deleted_topic_from_event_payload(payload),
        }
    if payload["kind"] == STREAM_BINDINGS_CREATED_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "stream_binding",
            "kind": payload["kind"],
            "stream_uuid": _event_payload_value(
                "stream_uuid",
                payload["stream_uuid"],
            ),
            "stream_bindings": _stream_bindings_from_event_payload(payload),
        }
    if payload["kind"] == USER_UPDATED_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "user",
            "kind": payload["kind"],
            "user": _user_from_event_payload(payload),
        }
    if payload["kind"] == FOLDER_DELETED_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "folder",
            "kind": payload["kind"],
            "folder": _deleted_folder_from_event_payload(payload),
        }
    if payload["kind"] == FOLDER_ITEM_DELETED_EVENT:
        return {
            "epoch_version": row["epoch_version"],
            "type": "folder_item",
            "kind": payload["kind"],
            "folder_item": _deleted_folder_item_from_event_payload(payload),
        }
    if payload["kind"] in FOLDER_EVENTS:
        return {
            "epoch_version": row["epoch_version"],
            "type": "folder",
            "kind": payload["kind"],
            "folder": _folder_from_event_payload(payload),
        }
    raise ra_exc.ValidationErrorException()


def _fetch_one(session, statement, values):
    result = session.execute(statement, values)
    return result.fetchone()


def _fetch_all(session, statement, values):
    result = session.execute(statement, values)
    return list(result.fetchall())


def create_message_events(project_id, message, recipients, session=None):
    if not recipients:
        return []

    event_uuids = [str(sys_uuid.uuid4()) for _ in recipients]
    recipient_uuids = [str(recipient_uuid) for recipient_uuid in recipients]
    engine = engines.engine_factory.get_engine()
    with engine.session_manager(session=session) as s:
        rows = _fetch_all(
            s,
            """
            WITH recipients AS (
                SELECT *
                FROM unnest(%s::uuid[], %s::uuid[]) AS r(
                    event_uuid,
                    user_uuid
                )
            ),
            inserted AS (
                INSERT INTO m_workspace_events
                    (uuid, project_id, user_uuid, payload, created_at,
                     updated_at)
                SELECT
                    r.event_uuid,
                    um.project_id,
                    um.user_uuid,
                    to_jsonb(um) || jsonb_build_object(
                        'kind', %s::text,
                        'created_at', to_char(
                            um.created_at,
                            'YYYY-MM-DD HH24:MI:SS.US'
                        ),
                        'updated_at', to_char(
                            um.updated_at,
                            'YYYY-MM-DD HH24:MI:SS.US'
                        )
                    ),
                    NOW(),
                    NOW()
                FROM recipients AS r
                JOIN m_workspace_user_messages_view AS um
                    ON  um.uuid = %s
                    AND um.project_id = %s
                    AND um.user_uuid = r.user_uuid
                ORDER BY um.user_uuid
                RETURNING epoch_version
            )
            SELECT epoch_version
            FROM inserted
            ORDER BY epoch_version
            """,
            (
                event_uuids,
                recipient_uuids,
                MESSAGE_CREATED_EVENT,
                str(message.uuid),
                str(project_id),
            ),
        )

    if len(rows) != len(recipients):
        raise ra_exc.ValidationErrorException()
    return [row["epoch_version"] for row in rows]


def create_message_updated_event(message, session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=message.project_id,
        user_uuid=message.user_uuid,
        payload=event_payloads.MessageUpdatedEventPayload(
            **dict(message)
        ),
    )
    return event.insert(session=session)


def create_messages_read_event(project_id, user_uuid, message_uuids,
                               session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        payload=event_payloads.MessagesReadEventPayload(
            project_id=project_id,
            message_uuids=[
                str(message_uuid) for message_uuid in message_uuids
            ],
        ),
    )
    return event.insert(session=session)


def create_message_deleted_event(project_id, user_uuid, message_uuid,
                                 stream_uuid, topic_uuid, session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        payload=event_payloads.MessageDeletedEventPayload(
            uuid=message_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
        ),
    )
    return event.insert(session=session)


def create_folder_event(folder, session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=folder.project_id,
        user_uuid=folder.user_uuid,
        payload=event_payloads.FolderCreatedEventPayload(
            **dict(folder)
        ),
    )
    return event.insert(session=session)


def create_stream_event(stream, session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=stream.project_id,
        user_uuid=stream.user_uuid,
        payload=event_payloads.StreamCreatedEventPayload(
            **dict(stream)
        ),
    )
    return event.insert(session=session)


def create_stream_updated_event(stream, session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=stream.project_id,
        user_uuid=stream.user_uuid,
        payload=event_payloads.StreamUpdatedEventPayload(
            **dict(stream)
        ),
    )
    return event.insert(session=session)


def create_topic_event(topic, session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=topic.project_id,
        user_uuid=topic.user_uuid,
        payload=event_payloads.TopicCreatedEventPayload(
            **dict(topic)
        ),
    )
    return event.insert(session=session)


def create_topic_updated_event(topic, session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=topic.project_id,
        user_uuid=topic.user_uuid,
        payload=event_payloads.TopicUpdatedEventPayload(
            **dict(topic)
        ),
    )
    return event.insert(session=session)


def create_topic_deleted_event(project_id, user_uuid, topic_uuid, stream_uuid,
                               session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        payload=event_payloads.TopicDeletedEventPayload(
            uuid=topic_uuid,
            stream_uuid=stream_uuid,
        ),
    )
    return event.insert(session=session)


def create_stream_deleted_event(project_id, user_uuid, stream_uuid,
                                session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        payload=event_payloads.StreamDeletedEventPayload(
            uuid=stream_uuid,
        ),
    )
    return event.insert(session=session)


def create_stream_bindings_created_event(bindings, user_uuid, session=None):
    binding = bindings[0]
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=binding.project_id,
        user_uuid=user_uuid,
        payload=event_payloads.StreamBindingsCreatedEventPayload(
            project_id=binding.project_id,
            stream_uuid=binding.stream_uuid,
            stream_bindings=[
                _stream_binding_snapshot_from_mapping(stream_binding)
                for stream_binding in bindings
            ],
        ),
    )
    return event.insert(session=session)


def create_user_updated_events(user, project_id, recipient_user_uuids,
                               session=None):
    result = []
    payload = event_payloads.UserUpdatedEventPayload(**dict(user))
    for recipient_user_uuid in recipient_user_uuids:
        event_uuid = sys_uuid.uuid4()
        event = models.WorkspaceEvent(
            uuid=event_uuid,
            project_id=project_id,
            user_uuid=recipient_user_uuid,
            payload=payload,
        )
        result.append(event.insert(session=session))
    return result


def create_folder_updated_event(folder, session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=folder.project_id,
        user_uuid=folder.user_uuid,
        payload=event_payloads.FolderUpdatedEventPayload(
            **dict(folder)
        ),
    )
    return event.insert(session=session)


def create_folder_deleted_event(project_id, user_uuid, folder_uuid,
                                session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        payload=event_payloads.FolderDeletedEventPayload(
            uuid=folder_uuid,
        ),
    )
    return event.insert(session=session)


def create_folder_item_deleted_event(project_id, user_uuid, item_uuid,
                                     session=None):
    event_uuid = sys_uuid.uuid4()
    event = models.WorkspaceEvent(
        uuid=event_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        payload=event_payloads.FolderItemDeletedEventPayload(
            uuid=item_uuid,
        ),
    )
    return event.insert(session=session)


def _event_rows_statement(where_clause):
    return f"""
        SELECT
            e.epoch_version,
            e.user_uuid,
            e.payload
        FROM m_workspace_events AS e
        WHERE {where_clause}
        ORDER BY e.epoch_version ASC
        LIMIT %s
    """


def get_events_after(project_id, user_uuid, after_epoch_version=0,
                     limit=DEFAULT_EVENTS_LIMIT, session=None):
    engine = engines.engine_factory.get_engine()
    with engine.session_manager(session=session) as s:
        rows = _fetch_all(
            s,
            _event_rows_statement(
                """
                e.project_id = %s
                AND e.user_uuid = %s
                AND e.epoch_version > %s
                """
            ),
            (str(project_id), str(user_uuid), after_epoch_version, limit),
        )
    return [event_row_to_messenger_event(row) for row in rows]


def get_event_for_user(project_id, user_uuid, epoch_version, session=None):
    engine = engines.engine_factory.get_engine()
    with engine.session_manager(session=session) as s:
        row = _fetch_one(
            s,
            _event_rows_statement(
                """
                e.project_id = %s
                AND e.user_uuid = %s
                AND e.epoch_version = %s
                """
            ),
            (str(project_id), str(user_uuid), epoch_version, 1),
        )
    return None if row is None else event_row_to_messenger_event(row)


def get_current_epoch_version(project_id, user_uuid, session=None):
    engine = engines.engine_factory.get_engine()
    with engine.session_manager(session=session) as s:
        row = _fetch_one(
            s,
            """
            SELECT COALESCE(MAX(epoch_version), 0) AS epoch_version
            FROM m_workspace_events
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
