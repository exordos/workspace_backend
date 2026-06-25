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
DEFAULT_EVENTS_LIMIT = 100
MAX_EVENTS_LIMIT = 500
WORKSPACE_USER_MESSAGE_FIELDS = tuple(
    models.WorkspaceUserMessage.properties.properties
)
WORKSPACE_USER_FOLDER_FIELDS = tuple(
    models.UserFolder.properties.properties
)


def _to_uuid_string(value):
    return str(value).lower()


def _event_payload_value(name, value):
    if value is None:
        return None
    if name in ("created_at", "updated_at"):
        value = event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.from_simple_type(
            value
        )
        return event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(value)
    if name == "uuid" or name.endswith("uuid") or name == "project_id":
        return _to_uuid_string(value)
    return value


def _message_from_event_payload(event_payload):
    result = {}
    for name in WORKSPACE_USER_MESSAGE_FIELDS:
        value = event_payload[name]
        if value is None:
            continue
        result[name] = _event_payload_value(name, value)
    return result


def _folder_from_event_payload(event_payload):
    return {
        name: _event_payload_value(name, event_payload[name])
        for name in WORKSPACE_USER_FOLDER_FIELDS
    }


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
