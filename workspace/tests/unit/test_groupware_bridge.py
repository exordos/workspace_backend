# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import email.message
import types
import unittest.mock as mock
import uuid

import icalendar
import requests

from workspace.services.groupware_bridge import agents
from workspace.services.groupware_bridge import calendar
from workspace.services.groupware_bridge import mail


def _dav_response(url, content):
    response = requests.Response()
    response.status_code = 207
    response.url = url
    response._content = content.encode("utf-8")
    return response


def test_imap_list_parser_preserves_remote_path_and_decodes_display_name():
    row = b'(\\HasNoChildren) "/" "~peter/mail/&U,BTFw-/&ZeVnLIqe-"'

    path, delimiter, special_use, display_path = mail._parse_list_row(row)

    assert path == "~peter/mail/&U,BTFw-/&ZeVnLIqe-"
    assert delimiter == "/"
    assert special_use is None
    assert display_path == "~peter/mail/\u53f0\u5317/\u65e5\u672c\u8a9e"


def test_mime_helpers_decode_bodies_attachments_and_invalid_date():
    message = email.message.EmailMessage()
    message["From"] = "CASSI <cassi@example.com>"
    message["To"] = "Recipient <recipient@example.com>"
    message["Date"] = "not a mail date"
    message.set_content("Plain body")
    message.add_alternative("<p>HTML body</p>", subtype="html")
    message.add_attachment(
        b"attachment-data",
        maintype="application",
        subtype="octet-stream",
        filename="report.bin",
    )

    body_html, body_text, attachments = mail._body_parts(message)

    assert body_text == "Plain body\n"
    assert body_html == "<p>HTML body</p>\n"
    assert attachments == [
        (
            "report.bin",
            "application/octet-stream",
            None,
            b"attachment-data",
        ),
    ]
    assert mail._message_addresses(message, "To") == [
        "Recipient <recipient@example.com>",
    ]
    parsed_date = mail._message_date(message)
    assert parsed_date.tzinfo is datetime.timezone.utc


def test_imap_fetch_flags_parses_uids_and_ignores_unrelated_rows():
    client = mail.ImapClient(types.SimpleNamespace())
    client.connection = mock.Mock()
    client.connection.select.return_value = ("OK", [b"2"])
    client.connection.uid.return_value = (
        "OK",
        [
            b"1 (UID 41 FLAGS (\\Seen \\Flagged))",
            (b"2 (UID 42 FLAGS ())", b"ignored"),
            b")",
        ],
    )

    assert client.fetch_flags("INBOX") == {
        41: frozenset({b"\\Seen", b"\\Flagged"}),
        42: frozenset(),
    }


def test_smtp_client_uses_tls_auth_and_closes_connection():
    settings = types.SimpleNamespace(
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_security="tls",
        credentials=types.SimpleNamespace(
            username="cassi@example.com",
            password="application-password",
        ),
    )
    message = email.message.EmailMessage()
    message["From"] = "cassi@example.com"
    message["To"] = "recipient@example.com"
    message["Subject"] = "Transport test"
    message.set_content("Body")
    connection = mock.Mock()

    with (
        mock.patch.object(mail.ssl, "create_default_context") as context_factory,
        mock.patch.object(mail.smtplib, "SMTP_SSL", return_value=connection) as smtp,
    ):
        mail.SmtpClient(settings).send(message)

    smtp.assert_called_once_with(
        "smtp.example.com",
        465,
        context=context_factory.return_value,
        timeout=mail.SMTP_TIMEOUT_SECONDS,
    )
    connection.login.assert_called_once_with(
        "cassi@example.com",
        "application-password",
    )
    connection.send_message.assert_called_once_with(message)
    connection.quit.assert_called_once_with()


def test_smtp_client_uses_timeout_for_starttls_connection():
    settings = types.SimpleNamespace(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_security="starttls",
        credentials=types.SimpleNamespace(
            username="cassi@example.com",
            password="application-password",
        ),
    )
    message = email.message.EmailMessage()
    connection = mock.Mock()

    with (
        mock.patch.object(mail.ssl, "create_default_context") as context_factory,
        mock.patch.object(mail.smtplib, "SMTP", return_value=connection) as smtp,
    ):
        mail.SmtpClient(settings).send(message)

    smtp.assert_called_once_with(
        "smtp.example.com",
        587,
        timeout=mail.SMTP_TIMEOUT_SECONDS,
    )
    connection.starttls.assert_called_once_with(
        context=context_factory.return_value,
    )
    connection.send_message.assert_called_once_with(message)
    connection.quit.assert_called_once_with()


def test_mail_import_adopts_pending_message_with_same_message_id():
    account = types.SimpleNamespace(
        uuid=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_uuid=uuid.uuid4(),
    )
    folder = types.SimpleNamespace(uuid=uuid.uuid4(), path="Sent")
    pending = types.SimpleNamespace(
        external_uid=None,
        message_id="<message@example.com>",
        update_dm=mock.Mock(),
        update=mock.Mock(),
    )
    manager = mock.Mock()
    manager.get_one_or_none.return_value = None
    manager.get_all.return_value = [pending]
    raw_message = (
        b"From: cassi@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Message-ID: <message@example.com>\r\n"
        b"Subject: Test\r\n\r\nBody"
    )

    with (
        mock.patch.object(mail.models.MailMessage, "objects", manager),
        mock.patch.object(mail.workspace_events, "create_groupware_event") as event,
    ):
        result = mail.MailSynchronizer()._import_message(
            account,
            folder,
            42,
            frozenset({b"\\Seen"}),
            raw_message,
        )

    assert result is pending
    pending.update_dm.assert_called_once()
    values = pending.update_dm.call_args.kwargs["values"]
    assert values["external_uid"] == 42
    assert values["source"] == {"folder_path": "Sent", "external_uid": 42}
    assert values["seen"] is True
    pending.update.assert_called_once_with()
    event.assert_called_once()


def test_calendar_roundtrip_handles_single_attendee_and_alarm():
    source = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
SUMMARY:Architecture review
DTSTART:20260714T120000Z
DTEND:20260714T130000Z
ATTENDEE;CN=Reviewer;PARTSTAT=ACCEPTED:mailto:reviewer@example.com
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
END:VALARM
END:VEVENT
END:VCALENDAR
"""

    values = calendar.parse_ics(source)

    assert values["uid"] == "event-1"
    assert values["attendees"] == [
        {
            "email": "reviewer@example.com",
            "name": "Reviewer",
            "status": "ACCEPTED",
        },
    ]
    assert values["alarms"] == [
        {"action": "DISPLAY", "trigger": "-PT15M"},
    ]

    event = types.SimpleNamespace(
        uid=values["uid"],
        summary=values["summary"],
        starts_at=values["starts_at"],
        ends_at=values["ends_at"],
        description=None,
        location=None,
        recurrence=None,
        attendees=values["attendees"],
        alarms=values["alarms"],
    )
    generated = icalendar.Calendar.from_ical(calendar.build_ics(event))
    generated_event = next(
        component for component in generated.walk() if component.name == "VEVENT"
    )
    assert str(generated_event["uid"]) == "event-1"
    assert str(generated_event["attendee"]) == "mailto:reviewer@example.com"
    generated_alarm = next(
        component
        for component in generated_event.subcomponents
        if component.name == "VALARM"
    )
    assert generated_alarm["trigger"].to_ical() == b"-PT15M"


def test_calendar_import_matches_non_recurring_event_with_null_recurrence_id():
    account = types.SimpleNamespace(
        uuid=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_uuid=uuid.uuid4(),
    )
    calendar_model = types.SimpleNamespace(uuid=uuid.uuid4())
    existing = types.SimpleNamespace(
        recurrence_id=None,
        sync_status="pending",
    )
    manager = mock.Mock()
    manager.get_all.return_value = [existing]
    remote = {
        "href": "https://calendar.example.com/calendars/cassi/event-1.ics",
        "etag": '"event-1"',
        "ics": """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
SUMMARY:Architecture review
DTSTART:20260714T120000Z
DTEND:20260714T130000Z
END:VEVENT
END:VCALENDAR
""",
    }

    with mock.patch.object(calendar.models.CalendarEvent, "objects", manager):
        result = calendar.CalendarSynchronizer._upsert_event(
            account,
            calendar_model,
            remote,
        )

    assert result is existing
    filters = manager.get_all.call_args.kwargs["filters"]
    assert set(filters) == {"calendar_uuid", "uid"}
    assert filters["calendar_uuid"].value == calendar_model.uuid
    assert filters["uid"].value == "event-1"


def test_caldav_discovers_calendar_home_from_well_known_url():
    account = types.SimpleNamespace(
        server_url="https://calendar.example.com/",
        account_settings=types.SimpleNamespace(
            credentials=types.SimpleNamespace(username="cassi", password="secret"),
        ),
    )
    discovery = _dav_response(
        "https://calendar.example.com/principals/cassi/",
        """<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
        <d:response><d:propstat><d:prop>
        <d:current-user-principal><d:href>/principals/cassi/</d:href></d:current-user-principal>
        <c:calendar-home-set><d:href>/calendars/cassi/</d:href></c:calendar-home-set>
        </d:prop></d:propstat></d:response></d:multistatus>""",
    )
    listing = _dav_response(
        "https://calendar.example.com/calendars/cassi/",
        """<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
        <d:response><d:href>/calendars/cassi/work/</d:href><d:propstat><d:prop>
        <d:displayname>Work</d:displayname>
        <d:resourcetype><c:calendar/></d:resourcetype>
        </d:prop></d:propstat></d:response></d:multistatus>""",
    )

    with mock.patch.object(
        calendar.requests,
        "request",
        side_effect=[discovery, listing],
    ) as request:
        result = calendar.CalDavClient(account).calendars()

    assert request.call_args_list[0].args[:2] == (
        "PROPFIND",
        "https://calendar.example.com/.well-known/caldav",
    )
    assert request.call_args_list[0].kwargs["headers"]["Depth"] == "0"
    assert request.call_args_list[1].args[:2] == (
        "PROPFIND",
        "https://calendar.example.com/calendars/cassi/",
    )
    assert request.call_args_list[1].kwargs["headers"]["Depth"] == "1"
    assert result == [
        {
            "href": "https://calendar.example.com/calendars/cassi/work/",
            "name": "Work",
            "color": None,
            "ctag": None,
            "sync_token": None,
        },
    ]


def test_caldav_follows_current_user_principal_to_calendar_home():
    account = types.SimpleNamespace(
        server_url="https://calendar.example.com/dav/",
        account_settings=types.SimpleNamespace(
            credentials=types.SimpleNamespace(username="cassi", password="secret"),
        ),
    )
    discovery = _dav_response(
        "https://calendar.example.com/dav/",
        """<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop>
        <d:current-user-principal><d:href>/principals/cassi/</d:href></d:current-user-principal>
        </d:prop></d:propstat></d:response></d:multistatus>""",
    )
    principal = _dav_response(
        "https://calendar.example.com/principals/cassi/",
        """<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
        <d:response><d:propstat><d:prop>
        <c:calendar-home-set><d:href>/calendars/cassi/</d:href></c:calendar-home-set>
        </d:prop></d:propstat></d:response></d:multistatus>""",
    )
    listing = _dav_response(
        "https://calendar.example.com/calendars/cassi/",
        '<d:multistatus xmlns:d="DAV:"/>',
    )

    with mock.patch.object(
        calendar.requests,
        "request",
        side_effect=[discovery, principal, listing],
    ) as request:
        assert calendar.CalDavClient(account).calendars() == []

    assert request.call_args_list[0].args[1] == "https://calendar.example.com/dav/"
    assert request.call_args_list[1].args[1] == (
        "https://calendar.example.com/principals/cassi/"
    )
    assert request.call_args_list[2].args[1] == (
        "https://calendar.example.com/calendars/cassi/"
    )


def test_groupware_worker_marks_unchecked_account_after_success():
    settings = types.SimpleNamespace(
        credentials=types.SimpleNamespace(username="cassi", password="secret"),
    )
    account = types.SimpleNamespace(
        account_settings=settings,
        update_dm=mock.Mock(),
        update=mock.Mock(),
    )
    account.update_dm.side_effect = lambda values: account.__dict__.update(values)
    synchronizer = mock.Mock()
    worker = agents.WorkspaceGroupwareBridgeWorker(
        mail_synchronizer=synchronizer,
        calendar_synchronizer=mock.Mock(),
    )

    worker._sync_account(account, synchronizer)

    synchronizer.sync.assert_called_once_with(account)
    assert account.access_status == "confirmed"
    assert account.status == "active"
    assert account.access_last_error is None


def test_groupware_worker_does_not_connect_without_credentials():
    account = types.SimpleNamespace(
        account_settings=types.SimpleNamespace(credentials=None),
        update_dm=mock.Mock(),
        update=mock.Mock(),
    )
    account.update_dm.side_effect = lambda values: account.__dict__.update(values)
    synchronizer = mock.Mock()
    worker = agents.WorkspaceGroupwareBridgeWorker(
        mail_synchronizer=synchronizer,
        calendar_synchronizer=mock.Mock(),
    )

    worker._sync_account(account, synchronizer)

    synchronizer.sync.assert_not_called()
    assert account.access_status == "missing_credentials"
