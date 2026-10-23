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

import datetime
import hashlib
import json
import typing
import uuid as sys_uuid

import webob
from restalchemy.api import contexts as ra_contexts
from restalchemy.api import packers as ra_packers
from restalchemy.api import resources as ra_resources
from restalchemy.common import exceptions as ra_exc
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from workspace.common import mutation_journal_opts
from workspace.messenger_api import publication
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models


EVENTS_CHANNEL = "workspace_events"
MESSAGE_OBJECT_TYPE = "message"
MESSAGE_REACTION_OBJECT_TYPE = "message_reaction"
STREAM_OBJECT_TYPE = "stream"
STREAM_BINDING_OBJECT_TYPE = "stream_binding"
TOPIC_OBJECT_TYPE = "topic"
USER_OBJECT_TYPE = "user"
FOLDER_OBJECT_TYPE = "folder"
FOLDER_ITEM_OBJECT_TYPE = "folder_item"
FILE_OBJECT_TYPE = "file"
EXTERNAL_ACCOUNT_OBJECT_TYPE = "external_account"


class ProjectEventLockUnavailableError(RuntimeError):
    """Raised when a nonblocking event writer cannot serialize a project."""


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
STREAM_BINDING_UPDATED_EVENT = event_payloads.StreamBindingUpdatedEventPayload.KIND
STREAM_BINDING_DELETED_EVENT = event_payloads.StreamBindingDeletedEventPayload.KIND
USER_UPDATED_EVENT = event_payloads.UserUpdatedEventPayload.KIND
TOPIC_CREATED_EVENT = event_payloads.TopicCreatedEventPayload.KIND
TOPIC_UPDATED_EVENT = event_payloads.TopicUpdatedEventPayload.KIND
TOPIC_READ_EVENT = event_payloads.TopicReadEventPayload.KIND
TOPIC_DELETED_EVENT = event_payloads.TopicDeletedEventPayload.KIND
FOLDER_CREATED_EVENT = event_payloads.FolderCreatedEventPayload.KIND
FOLDER_UPDATED_EVENT = event_payloads.FolderUpdatedEventPayload.KIND
FOLDER_DELETED_EVENT = event_payloads.FolderDeletedEventPayload.KIND
FOLDER_ITEM_DELETED_EVENT = event_payloads.FolderItemDeletedEventPayload.KIND
FILE_CREATED_EVENT = event_payloads.FileCreatedEventPayload.KIND
FILE_UPDATED_EVENT = event_payloads.FileUpdatedEventPayload.KIND
FILE_DELETED_EVENT = event_payloads.FileDeletedEventPayload.KIND
EXTERNAL_ACCOUNT_CREATED_EVENT = event_payloads.ExternalAccountCreatedEventPayload.KIND
EXTERNAL_ACCOUNT_UPDATED_EVENT = event_payloads.ExternalAccountUpdatedEventPayload.KIND
EXTERNAL_ACCOUNT_DELETED_EVENT = event_payloads.ExternalAccountDeletedEventPayload.KIND
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
    STREAM_BINDING_UPDATED_EVENT: (
        STREAM_BINDING_OBJECT_TYPE,
        UPDATED_ACTION,
    ),
    STREAM_BINDING_DELETED_EVENT: (
        STREAM_BINDING_OBJECT_TYPE,
        DELETED_ACTION,
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
    FILE_CREATED_EVENT: (FILE_OBJECT_TYPE, CREATED_ACTION),
    FILE_UPDATED_EVENT: (FILE_OBJECT_TYPE, UPDATED_ACTION),
    FILE_DELETED_EVENT: (FILE_OBJECT_TYPE, DELETED_ACTION),
    EXTERNAL_ACCOUNT_CREATED_EVENT: (EXTERNAL_ACCOUNT_OBJECT_TYPE, CREATED_ACTION),
    EXTERNAL_ACCOUNT_UPDATED_EVENT: (EXTERNAL_ACCOUNT_OBJECT_TYPE, UPDATED_ACTION),
    EXTERNAL_ACCOUNT_DELETED_EVENT: (EXTERNAL_ACCOUNT_OBJECT_TYPE, DELETED_ACTION),
}
WORKSPACE_EVENT_RESOURCE = ra_resources.ResourceByRAModel(
    model_class=models.WorkspaceVisibleEvent,
    convert_underscore=False,
    process_filters=True,
)
DEFAULT_EVENTS_LIMIT = 100
MESSAGE_EVENT_MAX_AGE = datetime.timedelta(weeks=2)
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
WORKSPACE_FILE_EVENT_FIELDS = (
    "uuid",
    "project_id",
    "user_uuid",
    "stream_uuid",
    "name",
    "description",
    "content_type",
    "size_bytes",
    "hash",
    "created_at",
    "updated_at",
)


def _to_uuid_string(value: object) -> str:
    return str(value).lower()


def _model_to_event_payload_value(value: typing.Any) -> typing.Any:
    return {
        name: _event_payload_value(name, prop.value)
        for name, prop in value.properties.items()
    }


def _event_payload_value(name: str, value: object) -> object:
    if value is None:
        return None
    if name in ("created_at", "updated_at", "last_ping_at"):
        value = event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.from_simple_type(value)
        return event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(value)
    if isinstance(value, sys_uuid.UUID):
        return _to_uuid_string(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat().replace("+00:00", "Z")
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


def _event_payload_get(event_payload: typing.Any, name: typing.Any) -> typing.Any:
    if hasattr(event_payload, "get"):
        return event_payload.get(name)
    if hasattr(event_payload, name):
        return getattr(event_payload, name)
    try:
        return event_payload[name]
    except (KeyError, TypeError):
        return None


def _message_from_event_payload(
    event_payload: typing.Any, session: typing.Any = None
) -> typing.Any:
    result = {}
    for name in WORKSPACE_USER_MESSAGE_FIELDS:
        value = _event_payload_get(event_payload, name)
        if name in {"reactions", "reaction_users"} and value is None:
            value = {}
        if value is None:
            continue
        result[name] = _event_payload_value(name, value)
    return result


def is_recent_message(created_at: datetime.datetime) -> bool:
    created_at = event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.from_simple_type(
        created_at
    )
    return (
        created_at
        >= datetime.datetime.now(datetime.timezone.utc) - MESSAGE_EVENT_MAX_AGE
    )


def message_snapshot_changed(before: typing.Any, after: typing.Any) -> bool:
    previous = _message_from_event_payload(before)
    current = _message_from_event_payload(after)
    # Synchronization timestamps alone do not change the client-visible state.
    previous.pop("updated_at", None)
    current.pop("updated_at", None)
    return previous != current


def _folder_from_event_payload(event_payload: typing.Any) -> typing.Any:
    return {
        name: _event_payload_value(name, _event_payload_get(event_payload, name))
        for name in WORKSPACE_USER_FOLDER_FIELDS
    }


def _stream_from_event_payload(
    event_payload: typing.Any, session: typing.Any = None
) -> typing.Any:
    result = {
        name: _event_payload_value(name, _event_payload_get(event_payload, name))
        for name in WORKSPACE_USER_STREAM_FIELDS
    }
    return result


def _topic_from_event_payload(
    event_payload: typing.Any, session: typing.Any = None
) -> typing.Any:
    result = {}
    for name in WORKSPACE_USER_TOPIC_FIELDS:
        value = _event_payload_get(event_payload, name)
        if name == "notification_mode" and value is None:
            value = models.WorkspaceTopicNotificationMode.DEFAULT.value
        result[name] = _event_payload_value(name, value)
    return result


def _stream_binding_snapshot_from_mapping(value: typing.Any) -> typing.Any:
    return {
        name: _event_payload_value(name, _event_payload_get(value, name))
        for name in WORKSPACE_STREAM_BINDING_FIELDS
    }


def _user_from_event_payload(event_payload: typing.Any) -> typing.Any:
    return {
        name: _event_payload_value(name, _event_payload_get(event_payload, name))
        for name in WORKSPACE_USER_FIELDS
    }


def _file_from_event_payload(event_payload: typing.Any) -> typing.Any:
    return {
        name: _event_payload_value(name, _event_payload_get(event_payload, name))
        for name in WORKSPACE_FILE_EVENT_FIELDS
    }


def _payload_with_kind(
    kind: str,
    payload: dict[str, object],
) -> dict[str, object]:
    result: dict[str, object] = {"kind": kind}
    result.update(payload)
    return result


def _event_metadata_for_kind(kind: str) -> tuple[str, str]:
    try:
        return EVENT_METADATA[kind]
    except KeyError:
        raise ra_exc.ValidationErrorException()


def _event_row_get(
    row: typing.Any, name: typing.Any, default: typing.Any = None
) -> typing.Any:
    if hasattr(row, name):
        return getattr(row, name)
    try:
        return row[name]
    except (KeyError, TypeError):
        return default


def _event_row_uuid(
    value: object,
    default: sys_uuid.UUID | None = None,
) -> sys_uuid.UUID | None:
    if value is None:
        return default
    if isinstance(value, sys_uuid.UUID):
        return value
    return sys_uuid.UUID(str(value))


def _event_row_datetime(value: object) -> object:
    parsed = value
    if isinstance(parsed, str):
        parsed = datetime.datetime.fromisoformat(parsed)
    if not isinstance(parsed, datetime.datetime):
        return parsed
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _workspace_event_from_row(row: typing.Any) -> typing.Any:
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
        values["created_at"] = _event_row_datetime(created_at)
    if updated_at is not None:
        values["updated_at"] = _event_row_datetime(updated_at)
    return models.WorkspaceEvent(**values)


_PACKER_REQUEST = None


def _get_packer_request() -> typing.Any:
    global _PACKER_REQUEST
    if _PACKER_REQUEST is None:
        request = webob.Request.blank("/")
        request.api_context = ra_contexts.RequestContext(request)
        _PACKER_REQUEST = request
    return _PACKER_REQUEST


def pack_workspace_event(event: typing.Any, request: typing.Any = None) -> typing.Any:
    request = request or _get_packer_request()
    return ra_packers.BaseResourcePacker(
        WORKSPACE_EVENT_RESOURCE,
        request,
    ).pack(event)


def event_row_to_messenger_event(row: typing.Any) -> typing.Any:
    return pack_workspace_event(_workspace_event_from_row(row))


def _create_workspace_event(
    project_id: object,
    user_uuid: object,
    kind: typing.Any,
    payload: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    if mutation_journal_opts.enabled():
        pending = publication.enqueue(
            session or contexts.Context().get_session(),
            project_id,
            payload.get("uuid", user_uuid),
            [user_uuid],
            kind,
            payload,
        )
        return pending[0]
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


def create_message_events(
    project_id: object,
    message: typing.Any,
    recipients: typing.Any,
    session: typing.Any = None,
    compact: typing.Any = False,
) -> typing.Any:
    if not recipients:
        return []
    if not compact:
        user_messages = models.WorkspaceUserMessage.objects.get_all(
            filters={
                "uuid": dm_filters.EQ(message.uuid),
                "project_id": dm_filters.EQ(project_id),
                "user_uuid": dm_filters.In(recipients),
            },
            order_by={"user_uuid": "asc"},
            session=session,
        )
        return sorted(
            _create_workspace_event(
                project_id=user_message.project_id,
                user_uuid=user_message.user_uuid,
                kind=MESSAGE_CREATED_EVENT,
                payload=_message_from_event_payload(user_message, session=session),
                session=session,
            )
            for user_message in user_messages
        )
    recipients = sorted({sys_uuid.UUID(str(value)) for value in recipients}, key=str)
    author_uuid = sys_uuid.UUID(str(message.user_uuid))
    content = message_payloads.markdown_content(message.payload)
    normalized_content = str(content or "").lower()
    shared = {
        "uuid": message.uuid,
        "stream_uuid": message.stream_uuid,
        "author_uuid": author_uuid,
        "topic_uuid": message.topic_uuid,
        "payload": message.payload,
        "created_at": message.created_at,
        "updated_at": message.updated_at,
        "project_id": project_id,
        "pinned": False,
        "starred": False,
        "reactions": {},
        "source_name": message.source_name,
        "status": message.status,
        "provider_uuid": message.provider_uuid,
        "provider_identity": message.provider_identity,
        "reaction_users": getattr(message, "reaction_users", {}) or {},
    }
    user_messages = [
        {
            **shared,
            "user_uuid": recipient_uuid,
            "read": recipient_uuid == author_uuid,
            "is_own": recipient_uuid == author_uuid,
            "mentioned": (
                f"](urn:user:{str(recipient_uuid).lower()})" in normalized_content
            ),
        }
        for recipient_uuid in recipients
    ]
    return create_compact_message_events(
        project_id,
        user_messages,
        session=session,
    )


def create_compact_message_events(
    project_id: object,
    user_messages: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    """Publish persisted recipient message snapshots as one broadcast event."""
    user_messages = list(user_messages)
    if not user_messages:
        return []
    return create_resource_broadcast_event(
        project_id,
        _event_payload_get(user_messages[0], "uuid"),
        MESSAGE_CREATED_EVENT,
        user_messages,
        _message_from_event_payload,
        session=session,
    )


def _split_common_recipient_payloads(
    resources: typing.Any, payload_factory: typing.Any, session: typing.Any = None
) -> typing.Any:
    """Deduplicate identical resource fields without changing event payloads."""
    payloads = {
        str(_event_payload_get(resource, "user_uuid")): payload_factory(
            resource,
            session=session,
        )
        for resource in resources
    }
    for payload in payloads.values():
        # The visibility view injects the scoped recipient and must not retain
        # one copy of the same UUID in every override.
        payload.pop("user_uuid", None)
    return _compress_recipient_payloads(payloads)


def _compress_recipient_payloads(
    payloads: dict[str, dict[str, typing.Any]],
) -> tuple[dict[str, typing.Any], dict[str, dict[str, typing.Any]]]:
    """Split complete per-recipient payloads into a common body and overrides."""
    if not payloads:
        return {}, {}
    values = list(payloads.values())
    common = {}
    for key in set.intersection(*(set(value) for value in values)):
        variants: dict[str, list[typing.Any]] = {}
        for payload in values:
            value = payload[key]
            fingerprint = json.dumps(value, sort_keys=True, default=str)
            variants.setdefault(fingerprint, [value, 0])[1] += 1
        # Use the modal value as the base payload. A typical message therefore
        # stores only the author's read/is_own exception, not 300 false flags.
        common[key] = max(variants.values(), key=lambda item: item[1])[0]
    overrides = {
        user_uuid: {
            key: value
            for key, value in payload.items()
            if key not in common or common[key] != value
        }
        for user_uuid, payload in payloads.items()
    }
    return common, {key: value for key, value in overrides.items() if value}


def create_resource_broadcast_event(
    project_id: object,
    entity_uuid: object,
    kind: typing.Any,
    resources: typing.Any,
    payload_factory: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    prepared = prepare_resource_broadcast_event(
        project_id,
        entity_uuid,
        kind,
        resources,
        payload_factory,
        session=session,
    )
    return create_prepared_resource_broadcast_events(
        [prepared] if prepared is not None else [],
        session=session,
    )


def prepare_resource_broadcast_event(
    project_id: object,
    entity_uuid: object,
    kind: typing.Any,
    resources: typing.Any,
    payload_factory: typing.Any,
    session: typing.Any = None,
) -> dict[str, typing.Any] | None:
    """Serialize recipient views before the project event lock is acquired."""
    resources = list(resources)
    session = session or contexts.Context().get_session()
    if not resources:
        return None
    missing_cache = object()
    previous_cache = getattr(
        session,
        "_workspace_event_metadata_cache",
        missing_cache,
    )
    if previous_cache is missing_cache:
        session._workspace_event_metadata_cache = {}
    try:
        common, recipient_payloads = _split_common_recipient_payloads(
            resources,
            payload_factory,
            session=session,
        )
    finally:
        if previous_cache is missing_cache:
            delattr(session, "_workspace_event_metadata_cache")
        else:
            session._workspace_event_metadata_cache = previous_cache
    return {
        "project_id": project_id,
        "entity_uuid": entity_uuid,
        "recipients": [
            _event_payload_get(resource, "user_uuid") for resource in resources
        ],
        "kind": kind,
        "payload": common,
        "recipient_payloads": recipient_payloads,
    }


def create_prepared_resource_broadcast_events(
    prepared_events: typing.Iterable[dict[str, typing.Any]],
    session: typing.Any = None,
    wait_for_project_lock: bool = True,
) -> list[int]:
    """Persist an already-serialized bounded event batch in causal order."""
    prepared_events = list(prepared_events)
    if not prepared_events:
        return []
    session = session or contexts.Context().get_session()
    return _persist_prepared_resource_broadcast_events(
        prepared_events,
        session=session,
        wait_for_project_lock=wait_for_project_lock,
    )


def _persist_prepared_resource_broadcast_events(
    prepared_events: typing.Iterable[dict[str, typing.Any]],
    session: typing.Any,
    wait_for_project_lock: bool = True,
) -> list[int]:
    epochs = []
    for event in prepared_events:
        epochs.extend(
            create_broadcast_event(
                event["project_id"],
                event["entity_uuid"],
                event["recipients"],
                event["kind"],
                event["payload"],
                recipient_payloads=event["recipient_payloads"],
                session=session,
                wait_for_project_lock=wait_for_project_lock,
            )
        )
    return epochs


def create_broadcast_event(
    project_id: object,
    entity_uuid: object,
    recipients: typing.Any,
    kind: typing.Any,
    payload: typing.Any,
    recipient_payloads: typing.Any = None,
    session: typing.Any = None,
    event_uuid: object = None,
    created_at: typing.Any = None,
    wait_for_project_lock: bool = True,
    owned_publication: bool = False,
) -> typing.Any:
    if not recipients:
        return []
    session = session or contexts.Context().get_session()
    recipients = sorted({sys_uuid.UUID(str(value)) for value in recipients}, key=str)
    if mutation_journal_opts.enabled() and not owned_publication:
        return publication.enqueue(
            session,
            project_id,
            entity_uuid,
            recipients,
            kind,
            payload,
            recipient_payloads,
        )
    # WebSocket cursors assume that lower epoch versions become visible before
    # higher versions. Serialize broadcast and direct event writers with the
    # same per-project transaction lock so commit order preserves that invariant.
    if owned_publication:
        pass
    elif wait_for_project_lock:
        session.execute(
            """
            SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))
            """,
            (project_id,),
        )
    else:
        lock_acquired = session.execute(
            """
            SELECT pg_try_advisory_xact_lock(
                hashtextextended(%s::text, 0)
            ) AS locked
            """,
            (project_id,),
        ).fetchone()["locked"]
        if not lock_acquired:
            raise ProjectEventLockUnavailableError(str(project_id))
    membership_digest = hashlib.sha256(
        "\n".join(str(value) for value in recipients).encode("ascii")
    ).hexdigest()
    audience_snapshot_uuid = sys_uuid.uuid5(
        sys_uuid.UUID(str(project_id)),
        membership_digest,
    )
    session.execute(
        """
        INSERT INTO m_workspace_event_audience_snapshots_v1 (
            uuid, project_id, membership_digest
        ) VALUES (%s, %s, %s)
        ON CONFLICT (project_id, membership_digest) DO NOTHING
        """,
        (audience_snapshot_uuid, project_id, membership_digest),
    )
    session.execute(
        """
        INSERT INTO m_workspace_event_audience_members_v1 (
            audience_snapshot_uuid, user_uuid
        )
        SELECT %s, members.user_uuid
        FROM unnest(%s::uuid[]) AS members(user_uuid)
        ON CONFLICT (audience_snapshot_uuid, user_uuid) DO NOTHING
        """,
        (audience_snapshot_uuid, recipients),
    )
    object_type, action = _event_metadata_for_kind(kind)
    event_uuid = event_uuid or sys_uuid.uuid4()
    epoch_version = session.execute(
        """
        INSERT INTO m_workspace_broadcast_message_events_v1 (
            uuid, project_id, entity_uuid, audience_snapshot_uuid,
            schema_version, object_type, action, payload
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s::jsonb
        )
        RETURNING epoch_version
        """,
        (
            event_uuid,
            project_id,
            entity_uuid,
            audience_snapshot_uuid,
            models.WORKSPACE_EVENT_SCHEMA_VERSION,
            object_type,
            action,
            json.dumps(_payload_with_kind(kind, payload), sort_keys=True),
        ),
    ).fetchone()["epoch_version"]
    if created_at is not None:
        session.execute(
            """
            UPDATE m_workspace_broadcast_message_events_v1
            SET created_at = %s, updated_at = %s
            WHERE uuid = %s
            """,
            (created_at, created_at, event_uuid),
        )
    if recipient_payloads:
        session.execute(
            """
            INSERT INTO m_workspace_event_recipient_payloads_v1 (
                event_uuid, user_uuid, payload
            )
            SELECT %s, overrides.key::uuid, overrides.value
            FROM jsonb_each(%s::jsonb) AS overrides(key, value)
            """,
            (event_uuid, json.dumps(recipient_payloads, sort_keys=True)),
        )
    session.execute(
        """
        UPDATE m_workspace_event_audience_snapshots_v1
        SET current_epoch_version = GREATEST(current_epoch_version, %s)
        WHERE uuid = %s
        """,
        (epoch_version, audience_snapshot_uuid),
    )
    return [epoch_version]


def create_message_updated_events(
    project_id: object,
    user_messages: typing.Any,
    session: typing.Any = None,
    compact: typing.Any = False,
) -> typing.Any:
    if not compact:
        return [
            create_message_updated_event(message, session=session)
            for message in user_messages
        ]
    user_messages = list(user_messages)
    if not user_messages:
        return []
    return create_resource_broadcast_event(
        project_id,
        _event_payload_get(user_messages[0], "uuid"),
        MESSAGE_UPDATED_EVENT,
        user_messages,
        _message_from_event_payload,
        session=session,
    )


def create_stream_updated_events(
    project_id: object,
    streams: typing.Any,
    session: typing.Any = None,
    compact: typing.Any = False,
) -> typing.Any:
    if not compact:
        return [
            create_stream_updated_event(stream, session=session) for stream in streams
        ]
    streams = list(streams)
    if not streams:
        return []
    return create_resource_broadcast_event(
        project_id,
        _event_payload_get(streams[0], "uuid"),
        STREAM_UPDATED_EVENT,
        streams,
        _stream_from_event_payload,
        session=session,
    )


def create_stream_events(
    project_id: object,
    streams: typing.Any,
    session: typing.Any = None,
    compact: typing.Any = False,
) -> typing.Any:
    if not compact:
        return [create_stream_event(stream, session=session) for stream in streams]
    streams = list(streams)
    if not streams:
        return []
    return create_resource_broadcast_event(
        project_id,
        _event_payload_get(streams[0], "uuid"),
        STREAM_CREATED_EVENT,
        streams,
        _stream_from_event_payload,
        session=session,
    )


def create_topic_events(
    project_id: object,
    topics: typing.Any,
    session: typing.Any = None,
    compact: typing.Any = False,
) -> typing.Any:
    if not compact:
        return [create_topic_event(topic, session=session) for topic in topics]
    topics = list(topics)
    if not topics:
        return []
    return create_resource_broadcast_event(
        project_id,
        _event_payload_get(topics[0], "uuid"),
        TOPIC_CREATED_EVENT,
        topics,
        _topic_from_event_payload,
        session=session,
    )


def create_topic_updated_events(
    project_id: object,
    topics: typing.Any,
    session: typing.Any = None,
    compact: typing.Any = False,
) -> typing.Any:
    if not compact:
        return [create_topic_updated_event(topic, session=session) for topic in topics]
    topics = list(topics)
    if not topics:
        return []
    return create_resource_broadcast_event(
        project_id,
        _event_payload_get(topics[0], "uuid"),
        TOPIC_UPDATED_EVENT,
        topics,
        _topic_from_event_payload,
        session=session,
    )


def create_folder_updated_events(
    project_id: object,
    folders: typing.Any,
    entity_uuid: object,
    session: typing.Any = None,
    compact: typing.Any = False,
) -> typing.Any:
    if not compact:
        return [
            create_folder_updated_event(folder, session=session) for folder in folders
        ]
    folders = list(folders)
    if not folders:
        return []
    return create_resource_broadcast_event(
        project_id,
        entity_uuid,
        FOLDER_UPDATED_EVENT,
        folders,
        lambda folder, session=None: _folder_from_event_payload(folder),
        session=session,
    )


def create_message_deleted_events(
    project_id: object,
    recipients: typing.Any,
    message_uuid: object,
    stream_uuid: object,
    topic_uuid: object,
    author_uuid: object,
    source_name: typing.Any,
    provider_uuid: typing.Any,
    provider_identity: typing.Any,
    session: typing.Any = None,
    compact: typing.Any = False,
) -> typing.Any:
    values = {
        "uuid": _event_payload_value("uuid", message_uuid),
        "stream_uuid": _event_payload_value("stream_uuid", stream_uuid),
        "topic_uuid": _event_payload_value("topic_uuid", topic_uuid),
        "author_uuid": _event_payload_value("author_uuid", author_uuid),
        "source_name": _event_payload_value("source_name", source_name),
        "provider_uuid": _event_payload_value("provider_uuid", provider_uuid),
        "provider_identity": _event_payload_value(
            "provider_identity", provider_identity
        ),
    }
    if compact:
        return create_broadcast_event(
            project_id,
            message_uuid,
            recipients,
            MESSAGE_DELETED_EVENT,
            values,
            session=session,
        )
    return [
        create_message_deleted_event(
            project_id,
            recipient,
            message_uuid,
            stream_uuid,
            topic_uuid,
            author_uuid,
            source_name,
            provider_uuid,
            provider_identity,
            session=session,
        )
        for recipient in recipients
    ]


def create_message_updated_event(
    message: typing.Any, session: typing.Any = None
) -> typing.Any:
    return _create_workspace_event(
        project_id=message.project_id,
        user_uuid=message.user_uuid,
        kind=MESSAGE_UPDATED_EVENT,
        payload=_message_from_event_payload(message, session=session),
        session=session,
    )


def create_message_read_event(
    message: typing.Any, session: typing.Any = None
) -> typing.Any:
    return _create_workspace_event(
        project_id=message.project_id,
        user_uuid=message.user_uuid,
        kind=MESSAGE_READ_EVENT,
        payload=_message_from_event_payload(message, session=session),
        session=session,
    )


def create_message_deleted_event(
    project_id: object,
    user_uuid: object,
    message_uuid: object,
    stream_uuid: object,
    topic_uuid: object,
    author_uuid: object,
    source_name: typing.Any,
    provider_uuid: typing.Any,
    provider_identity: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
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
            "provider_uuid": _event_payload_value("provider_uuid", provider_uuid),
            "provider_identity": _event_payload_value(
                "provider_identity", provider_identity
            ),
        },
        session=session,
    )


def _message_reaction_event_payload(
    reaction: typing.Any,
    message: typing.Any,
    session: typing.Any = None,
    **values: typing.Any,
) -> typing.Any:
    del session
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
        "provider_uuid": _event_payload_value("provider_uuid", message.provider_uuid),
        "provider_identity": _event_payload_value(
            "provider_identity", message.provider_identity
        ),
    }
    payload.update(values)
    return payload


def create_message_reaction_created_event(
    reaction: typing.Any, message: typing.Any, session: typing.Any = None
) -> typing.Any:
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
    reaction: typing.Any,
    message: typing.Any,
    old_message: typing.Any,
    old_emoji_name: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
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
            old_provider_uuid=_event_payload_value(
                "provider_uuid", old_message.provider_uuid
            ),
            old_provider_identity=_event_payload_value(
                "provider_identity", old_message.provider_identity
            ),
        ),
        session=session,
    )


def create_message_reaction_deleted_event(
    reaction: typing.Any, message: typing.Any, session: typing.Any = None
) -> typing.Any:
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


def create_folder_event(folder: typing.Any, session: typing.Any = None) -> typing.Any:
    return _create_workspace_event(
        project_id=folder.project_id,
        user_uuid=folder.user_uuid,
        kind=FOLDER_CREATED_EVENT,
        payload=_folder_from_event_payload(folder),
        session=session,
    )


def create_stream_event(stream: typing.Any, session: typing.Any = None) -> typing.Any:
    return _create_workspace_event(
        project_id=stream.project_id,
        user_uuid=stream.user_uuid,
        kind=STREAM_CREATED_EVENT,
        payload=_stream_from_event_payload(stream, session=session),
        session=session,
    )


def create_stream_updated_event(
    stream: typing.Any, session: typing.Any = None
) -> typing.Any:
    return _create_workspace_event(
        project_id=stream.project_id,
        user_uuid=stream.user_uuid,
        kind=STREAM_UPDATED_EVENT,
        payload=_stream_from_event_payload(stream, session=session),
        session=session,
    )


def create_stream_read_event(
    stream: typing.Any, session: typing.Any = None
) -> typing.Any:
    return _create_workspace_event(
        project_id=stream.project_id,
        user_uuid=stream.user_uuid,
        kind=STREAM_READ_EVENT,
        payload=_stream_from_event_payload(stream, session=session),
        session=session,
    )


def create_topic_event(topic: typing.Any, session: typing.Any = None) -> typing.Any:
    return _create_workspace_event(
        project_id=topic.project_id,
        user_uuid=topic.user_uuid,
        kind=TOPIC_CREATED_EVENT,
        payload=_topic_from_event_payload(topic, session=session),
        session=session,
    )


def create_topic_updated_event(
    topic: typing.Any, session: typing.Any = None
) -> typing.Any:
    return _create_workspace_event(
        project_id=topic.project_id,
        user_uuid=topic.user_uuid,
        kind=TOPIC_UPDATED_EVENT,
        payload=_topic_from_event_payload(topic, session=session),
        session=session,
    )


def create_topic_read_event(
    topic: typing.Any, session: typing.Any = None
) -> typing.Any:
    return _create_workspace_event(
        project_id=topic.project_id,
        user_uuid=topic.user_uuid,
        kind=TOPIC_READ_EVENT,
        payload=_topic_from_event_payload(topic, session=session),
        session=session,
    )


def create_topic_deleted_event(
    project_id: object,
    user_uuid: object,
    topic_uuid: object,
    stream_uuid: object,
    source_name: typing.Any,
    provider_uuid: typing.Any,
    provider_identity: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=TOPIC_DELETED_EVENT,
        payload={
            "uuid": _event_payload_value("uuid", topic_uuid),
            "stream_uuid": _event_payload_value("stream_uuid", stream_uuid),
            "source_name": _event_payload_value("source_name", source_name),
            "provider_uuid": _event_payload_value("provider_uuid", provider_uuid),
            "provider_identity": _event_payload_value(
                "provider_identity", provider_identity
            ),
        },
        session=session,
    )


def create_stream_deleted_event(
    project_id: object,
    user_uuid: object,
    stream_uuid: object,
    source_name: typing.Any,
    provider_uuid: typing.Any,
    provider_identity: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=STREAM_DELETED_EVENT,
        payload={
            "uuid": _event_payload_value("uuid", stream_uuid),
            "source_name": _event_payload_value("source_name", source_name),
            "provider_uuid": _event_payload_value("provider_uuid", provider_uuid),
            "provider_identity": _event_payload_value(
                "provider_identity", provider_identity
            ),
        },
        session=session,
    )


def create_stream_bindings_created_event(
    bindings: typing.Any, user_uuid: object, session: typing.Any = None
) -> typing.Any:
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


def create_stream_binding_updated_events(
    binding: typing.Any, recipient_user_uuids: typing.Any, session: typing.Any = None
) -> typing.Any:
    payload = _stream_binding_snapshot_from_mapping(binding)
    return [
        _create_workspace_event(
            project_id=binding.project_id,
            user_uuid=recipient_user_uuid,
            kind=STREAM_BINDING_UPDATED_EVENT,
            payload=payload,
            session=session,
        )
        for recipient_user_uuid in recipient_user_uuids
    ]


def create_stream_binding_deleted_events(
    binding: typing.Any,
    recipient_user_uuids: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    payload = {
        "uuid": _event_payload_value("uuid", binding.uuid),
        "stream_uuid": _event_payload_value("stream_uuid", binding.stream_uuid),
        "user_uuid": _event_payload_value("user_uuid", binding.user_uuid),
    }
    return [
        _create_workspace_event(
            project_id=binding.project_id,
            user_uuid=recipient_user_uuid,
            kind=STREAM_BINDING_DELETED_EVENT,
            payload=payload,
            session=session,
        )
        for recipient_user_uuid in recipient_user_uuids
    ]


def create_user_updated_events(
    user: typing.Any,
    project_id: object,
    recipient_user_uuids: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    payload = _user_from_event_payload(user)
    return create_broadcast_event(
        project_id,
        user.uuid,
        recipient_user_uuids,
        USER_UPDATED_EVENT,
        payload,
        session=session,
    )


def create_file_events(
    file: typing.Any,
    recipient_user_uuids: typing.Any,
    kind: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    payload = _file_from_event_payload(file)
    return [
        _create_workspace_event(
            project_id=file.project_id,
            user_uuid=recipient_user_uuid,
            kind=kind,
            payload=payload,
            session=session,
        )
        for recipient_user_uuid in recipient_user_uuids
    ]


def create_file_created_events(
    file: typing.Any, recipient_user_uuids: typing.Any, session: typing.Any = None
) -> typing.Any:
    return create_file_events(
        file,
        recipient_user_uuids,
        FILE_CREATED_EVENT,
        session=session,
    )


def create_file_updated_events(
    file: typing.Any, recipient_user_uuids: typing.Any, session: typing.Any = None
) -> typing.Any:
    return create_file_events(
        file,
        recipient_user_uuids,
        FILE_UPDATED_EVENT,
        session=session,
    )


def create_file_deleted_events(
    project_id: object,
    stream_uuid: object,
    file_uuid: object,
    recipient_user_uuids: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    return [
        _create_workspace_event(
            project_id=project_id,
            user_uuid=recipient_user_uuid,
            kind=FILE_DELETED_EVENT,
            payload={
                "uuid": _event_payload_value("uuid", file_uuid),
                "stream_uuid": _event_payload_value("stream_uuid", stream_uuid),
            },
            session=session,
        )
        for recipient_user_uuid in recipient_user_uuids
    ]


def create_folder_updated_event(
    folder: typing.Any, session: typing.Any = None
) -> typing.Any:
    return _create_workspace_event(
        project_id=folder.project_id,
        user_uuid=folder.user_uuid,
        kind=FOLDER_UPDATED_EVENT,
        payload=_folder_from_event_payload(folder),
        session=session,
    )


def create_folder_deleted_event(
    project_id: object,
    user_uuid: object,
    folder_uuid: object,
    session: typing.Any = None,
) -> typing.Any:
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=FOLDER_DELETED_EVENT,
        payload={"uuid": _event_payload_value("uuid", folder_uuid)},
        session=session,
    )


def create_folder_item_deleted_event(
    project_id: object,
    user_uuid: object,
    item_uuid: object,
    session: typing.Any = None,
) -> typing.Any:
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=FOLDER_ITEM_DELETED_EVENT,
        payload={"uuid": _event_payload_value("uuid", item_uuid)},
        session=session,
    )


def _external_snapshot(resource: typing.Any, hidden_fields: typing.Any) -> typing.Any:
    result = {}
    for name, prop in resource.properties.items():
        if name in hidden_fields:
            continue
        result[name] = _event_payload_value(name, prop.value)
    return result


def create_external_resource_event(
    project_id: object,
    user_uuid: object,
    resource: typing.Any,
    kind: typing.Any,
    hidden_fields: typing.Any = (),
    session: typing.Any = None,
) -> typing.Any:
    return _create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=kind,
        payload={
            "uuid": _event_payload_value("uuid", resource.uuid),
            "snapshot": _external_snapshot(resource, hidden_fields),
        },
        session=session,
    )


def get_events_after(
    project_id: object,
    user_uuid: object,
    after_epoch_version: typing.Any = 0,
    limit: typing.Any = DEFAULT_EVENTS_LIMIT,
    epoch_generation: typing.Any = None,
    session: typing.Any = None,
) -> typing.Any:
    del session
    with api_store.open_event_store(
        typing.cast(sys_uuid.UUID, project_id),
        typing.cast(sys_uuid.UUID, user_uuid),
    ) as store:
        filters = {"epoch_version": dm_filters.GT(after_epoch_version)}
        order_by = {"epoch_version": "asc"}
        if epoch_generation is None:
            events = store.events_after(filters, order_by, limit=limit)
        else:
            events = store.events_after(
                filters,
                order_by,
                epoch_generation=epoch_generation,
                limit=limit,
            )
    return events


def get_event_cursor(
    project_id: object, user_uuid: object, session: typing.Any = None
) -> typing.Any:
    del session
    with api_store.open_event_store(
        typing.cast(sys_uuid.UUID, project_id),
        typing.cast(sys_uuid.UUID, user_uuid),
    ) as store:
        return store.event_cursor()


def normalize_epoch_version(
    value: str | bytes | bytearray | int | float | None,
    default: int = 0,
) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ra_exc.ParseError(value=value)
    if parsed < 0:
        raise ra_exc.ParseError(value=value)
    return parsed
