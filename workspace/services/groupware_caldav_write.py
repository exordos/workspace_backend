# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import contextlib
import datetime
import http
import typing
import uuid

import requests
from restalchemy.common import exceptions

from workspace.calendar_api import models
from workspace.services import groupware_calendar
from workspace.services import groupware_content
from workspace.services import groupware_credentials


_CALDAV_ERRORS = (
    groupware_calendar.CalendarError,
    requests.RequestException,
    ValueError,
    OSError,
)


def created_event_uuid(remote: groupware_calendar.Event) -> uuid.UUID | None:
    name = remote.path.rsplit("/", 1)[-1]
    if not name.startswith("workspace-") or not name.endswith(".ics"):
        return None
    try:
        event_uuid = uuid.UUID(name[len("workspace-") : -len(".ics")])
        _, item = groupware_content.editable_event(remote.data)
    except ValueError:
        return None
    if (
        name == f"workspace-{event_uuid}.ics"
        and str(item.get("UID")) == f"workspace-{event_uuid}"
    ):
        return event_uuid
    return None


def binding(session: typing.Any, calendar_uuid: uuid.UUID) -> typing.Any:
    return session.execute(
        "SELECT remote_path FROM groupware_calendar_bindings WHERE calendar_uuid = %s",
        (calendar_uuid,),
    ).fetchone()


def write_event_binding(
    session: typing.Any, event: models.CalendarEvent, remote: groupware_calendar.Event
) -> None:
    session.execute(
        """
        INSERT INTO groupware_calendar_event_bindings (event_uuid, calendar_uuid, remote_path, etag, data)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (event_uuid) DO UPDATE SET etag = excluded.etag, data = excluded.data
        """,
        (event.uuid, event.calendar_uuid, remote.path, remote.etag, remote.data),
    )


@contextlib.contextmanager
def _client(
    session: typing.Any, calendar: models.Calendar
) -> typing.Iterator[groupware_calendar.Client]:
    row = session.execute(
        """
        SELECT settings FROM m_external_accounts_v2
        WHERE uuid = %s AND project_id = %s AND owner_user_uuid = %s
          AND provider = 'caldav' AND NOT disabled AND status = 'active'
        """,
        (calendar.external_account_uuid, calendar.project_id, calendar.owner_user_uuid),
    ).fetchone()
    store = groupware_credentials.configured_store()
    if row is None or store is None:
        raise exceptions.ValidationErrorException()
    password = store.load(session, calendar.external_account_uuid, "calendar")
    if password is None:
        raise exceptions.ValidationErrorException()
    settings = row["settings"]
    try:
        with requests.Session() as http_session:
            yield groupware_calendar.Client(
                http_session, settings["url"], settings["username"], password
            )
    except _CALDAV_ERRORS:
        raise exceptions.ValidationErrorException() from None


def create(
    session: typing.Any, calendar: models.Calendar, event: models.CalendarEvent
) -> None:
    location = binding(session, calendar.uuid)
    if location is None:
        return
    with _client(session, calendar) as client:
        path = location["remote_path"].rstrip("/") + f"/workspace-{event.uuid}.ics"
        data = groupware_content.compose_event(event.uuid, event.payload)
        try:
            tag = client.put(path, data, etag=None)
        except (
            groupware_calendar.CalendarHTTPError,
            requests.RequestException,
            OSError,
        ) as error:
            if isinstance(error, groupware_calendar.CalendarHTTPError) and (
                error.status != http.HTTPStatus.PRECONDITION_FAILED
            ):
                raise
            remote = client.get(path)
            parsed = groupware_content.parse_event(remote.data, datetime.timezone.utc)
            if (
                created_event_uuid(remote) != event.uuid
                or parsed is None
                or models.EVENT_PAYLOAD_TYPE.to_simple_type(parsed)
                != models.EVENT_PAYLOAD_TYPE.to_simple_type(event.payload)
            ):
                raise error
        else:
            # Acknowledged writes remain successful if the provider omits the ETag
            # and a follow-up read fails. The next sync fills in the missing ETag.
            remote = groupware_calendar.Event(path, tag or "", data)
            if not tag:
                with contextlib.suppress(*_CALDAV_ERRORS):
                    remote = client.get(path)
    write_event_binding(session, event, remote)


def _event_binding(session: typing.Any, event: models.CalendarEvent) -> typing.Any:
    row = session.execute(
        """
        SELECT remote_path, etag, data FROM groupware_calendar_event_bindings
        WHERE event_uuid = %s AND calendar_uuid = %s
        """,
        (event.uuid, event.calendar_uuid),
    ).fetchone()
    if row is None or not row["etag"]:
        raise exceptions.ValidationErrorException()
    return row


def update(
    session: typing.Any, calendar: models.Calendar, event: models.CalendarEvent
) -> None:
    if binding(session, calendar.uuid) is None:
        return
    source = _event_binding(session, event)
    with _client(session, calendar) as client:
        path = source["remote_path"]
        data = groupware_content.compose_event(
            event.uuid, event.payload, source["data"]
        )
        tag = client.put(path, data, etag=source["etag"])
        remote = (
            groupware_calendar.Event(path, tag, data)
            if tag is not None
            else client.get(path)
        )
    write_event_binding(session, event, remote)


def delete(
    session: typing.Any, calendar: models.Calendar, event: models.CalendarEvent
) -> None:
    if binding(session, calendar.uuid) is None:
        return
    source = _event_binding(session, event)
    with _client(session, calendar) as client:
        groupware_content.editable_event(source["data"])
        client.delete(source["remote_path"], etag=source["etag"])
