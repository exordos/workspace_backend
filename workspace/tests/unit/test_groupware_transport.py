# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import datetime
import email.message
import imaplib
import socket
import ssl
import uuid
import zoneinfo
from unittest import mock

import pytest
import requests

from workspace.services import groupware_calendar
from workspace.services import groupware_content
from workspace.services import groupware_mail
from workspace.services import groupware_network


def test_mailbox_preserves_wire_names_and_displays_modified_utf7():
    client = mock.Mock()
    client.list.return_value = (
        "OK",
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\Junk) "/" "&BCEEPwQwBDw-"',
            (b'() "/" {16}', b"Reports &- US"),
            b'(\\NoSelect) "/" "Public"',
            b'() "/" "Quoted \\"folder\\""',
        ],
    )

    folders = groupware_mail.Mailbox(client).folders()

    assert [(folder.name, folder.kind) for folder in folders] == [
        ("INBOX", "inbox"),
        ("Спам", "spam"),
        ("Reports & US", "custom"),
        ('Quoted "folder"', "custom"),
    ]
    assert folders[1].mailbox == b"&BCEEPwQwBDw-"
    assert folders[3].mailbox == b'Quoted "folder"'


def test_mailbox_reads_without_marking_seen_and_ignores_old_wildcard_match():
    client = mock.Mock()
    client.select.return_value = "OK", [b"1"]
    client.response.return_value = "UIDVALIDITY", [b"200"]
    client.uid.side_effect = [
        ("OK", [b"7"]),
        ("OK", [(b"2 (BODY[] {8}", b"new mail"), b" UID 7 FLAGS (\\Flagged))"]),
        ("OK", [None]),
    ]
    mailbox = groupware_mail.Mailbox(client)
    folder = groupware_mail.Folder(b"Inbox", "Inbox", "inbox")

    assert mailbox.select(folder) == 200
    assert mailbox.uids(8) == []
    assert mailbox.fetch(7) == groupware_mail.Message(7, b"new mail", False, True)
    assert mailbox.fetch(9) is None
    client.select.assert_called_once_with('"Inbox"', readonly=True)
    client.uid.assert_any_call(
        "FETCH",
        "7",
        f"(UID FLAGS BODY.PEEK[]<0.{groupware_mail.MAX_MESSAGE_BYTES + 1}>)",
    )


def test_mailbox_skips_messages_beyond_the_download_limit(monkeypatch):
    monkeypatch.setattr(groupware_mail, "MAX_MESSAGE_BYTES", 8)
    client = mock.Mock()
    client.uid.return_value = "OK", [(b"1 (UID 7 BODY[]<0>", b"some bytes"), b")"]
    with pytest.raises(groupware_mail.MessageTooLarge) as failure:
        groupware_mail.Mailbox(client).fetch(7)
    assert failure.value.downloaded_bytes == len(b"some bytes")


def test_mailbox_rejects_more_folders_than_the_limit(monkeypatch):
    monkeypatch.setattr(groupware_mail, "MAX_MAIL_FOLDERS", 1)
    client = mock.Mock()
    client.list.return_value = "OK", [b'() "/" "Inbox"', b'() "/" "Sent"']
    with pytest.raises(imaplib.IMAP4.error, match="folder count"):
        groupware_mail.Mailbox(client).folders()


@pytest.mark.parametrize("command_budget, session_budget", ((4, 10), (10, 4)))
def test_imap_response_budget_is_checked_before_reading_the_body(
    monkeypatch, command_budget, session_budget
):
    connection = object.__new__(groupware_mail._IMAPConnection)
    connection._remaining_bytes = command_budget
    connection._remaining_lines = 5
    connection._remaining_session_bytes = session_budget
    connection._remaining_session_lines = 10
    stopped = mock.Mock()
    read = mock.Mock(return_value=b"body")
    monkeypatch.setattr(groupware_mail._IMAPConnection, "shutdown", stopped)
    monkeypatch.setattr(imaplib.IMAP4, "read", read)
    assert connection.read(4) == b"body"
    with pytest.raises(imaplib.IMAP4.abort, match="response exceeds"):
        connection.read(1)
    read.assert_called_once_with(4)
    stopped.assert_called_once_with()


def test_mailbox_does_not_accept_an_unexpected_message_uid():
    client = mock.Mock()
    client.uid.return_value = "OK", [(b"1 (UID 8 BODY[] {4}", b"mail"), b")"]

    with pytest.raises(imaplib.IMAP4.error, match="Unexpected mail message UID"):
        groupware_mail.Mailbox(client).fetch(7)


def test_smtp_exposes_partial_recipient_refusal_without_provider_error_text():
    client = mock.Mock()
    client.send_message.return_value = {"refused@example.org": (550, b"rejected")}
    outgoing = email.message.EmailMessage()
    recipients = ["accepted@example.org", "refused@example.org"]

    assert groupware_mail.send(client, outgoing, "sender@example.net", recipients) == {
        "refused@example.org"
    }
    client.send_message.assert_called_once_with(
        outgoing, "sender@example.net", recipients
    )


CALDAV_ORIGIN = "https://calendar.example.test"


def _response(url, code, body="", **headers):
    response = requests.Response()
    response.url = url
    response.status_code = code
    response.headers.update(headers)
    response._content = body.encode()
    return response


def _xml(*items):
    return (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        + "".join(
            f"<d:response><d:href>{path}</d:href><d:propstat><d:prop>{prop}"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status>"
            "</d:propstat></d:response>"
            for path, prop in items
        )
        + "</d:multistatus>"
    )


def test_caldav_preserves_propfind_on_redirect_and_discovers_all_calendars():
    session = mock.Mock()
    origin = CALDAV_ORIGIN
    session.request.side_effect = [
        _response(origin + "/principals/", 301, Location="/principals/me/"),
        _response(
            origin + "/principals/me/",
            207,
            _xml(
                (
                    "/principals/me/",
                    "<d:current-user-principal><d:href>.</d:href></d:current-user-principal>",
                )
            ),
        ),
        _response(
            origin + "/principals/me/",
            207,
            _xml(
                (
                    "/principals/me/",
                    "<c:calendar-home-set><d:href>/home/me/</d:href></c:calendar-home-set>",
                )
            ),
        ),
        _response(
            origin + "/home/me/",
            207,
            _xml(
                ("/home/me/", "<d:resourcetype><d:collection/></d:resourcetype>"),
                (
                    "work/",
                    "<d:resourcetype><c:calendar/></d:resourcetype><d:displayname>Work</d:displayname>"
                    "<d:current-user-privilege-set><d:privilege><d:write/></d:privilege></d:current-user-privilege-set>",
                ),
                (
                    "shared/",
                    "<d:resourcetype><c:calendar/></d:resourcetype><d:displayname>Shared</d:displayname>",
                ),
                (
                    "tasks/",
                    "<d:resourcetype><c:calendar/></d:resourcetype><c:supported-calendar-component-set>"
                    '<c:comp name="VTODO"/></c:supported-calendar-component-set>',
                ),
            ),
        ),
    ]

    calendars = groupware_calendar.Client(
        session, origin + "/principals/", "person@example.org", "test"
    ).calendars()

    assert calendars == [
        groupware_calendar.Calendar("/home/me/work/", "Work", True),
        groupware_calendar.Calendar("/home/me/shared/", "Shared", False),
    ]
    assert session.request.call_args_list[0].args == (
        "PROPFIND",
        origin + "/principals/",
    )
    assert session.request.call_args_list[1].args == (
        "PROPFIND",
        origin + "/principals/me/",
    )
    assert all(
        call.kwargs["allow_redirects"] is False
        for call in session.request.call_args_list
    )


@pytest.mark.parametrize("location", ["https://other.test/me", "//other.test/me"])
def test_caldav_does_not_forward_credentials_to_a_different_origin(location):
    session = mock.Mock()
    session.request.return_value = _response(
        CALDAV_ORIGIN + "/principals/", 301, Location=location
    )

    with pytest.raises(groupware_calendar.CalendarError, match="Unexpected.+address"):
        groupware_calendar.Client(
            session, CALDAV_ORIGIN + "/principals/", "person@example.org", "test"
        ).calendars()

    assert session.request.call_count == 1


def test_caldav_queries_a_calendar_and_guards_writes_with_an_etag():
    session = mock.Mock()
    path = "/home/me/work/meeting.ics"
    origin = CALDAV_ORIGIN
    session.request.side_effect = [
        _response(
            origin + "/home/me/work/",
            207,
            _xml(
                (
                    path,
                    '<d:getetag>"1"</d:getetag><c:calendar-data>event</c:calendar-data>',
                )
            ),
        ),
        _response(origin + path, 204, ETag='"2"'),
        _response(origin + path, 412),
    ]
    calendar = groupware_calendar.Calendar("/home/me/work/", "Work", True)
    client = groupware_calendar.Client(
        session, CALDAV_ORIGIN + "/principals/", "person@example.org", "test"
    )
    start = datetime.datetime(2026, 11, 25, 12, tzinfo=datetime.timezone.utc)
    end = start + datetime.timedelta(days=1)

    assert client.events(calendar, start, end) == [
        groupware_calendar.Event(path, '"1"', "event")
    ]
    assert client.put(path, "modified event", etag='"1"') == '"2"'
    with pytest.raises(groupware_calendar.CalendarHTTPError) as error:
        client.delete(path, etag='"1"')

    assert error.value.status == 412
    assert (
        b'start="20261125T120000Z"' in session.request.call_args_list[0].kwargs["data"]
    )
    assert session.request.call_args_list[1].kwargs["headers"]["If-Match"] == '"1"'
    assert session.request.call_args_list[2].kwargs["headers"]["If-Match"] == '"1"'


def test_caldav_empty_calendar_report_can_include_collection_etag():
    session = mock.Mock()
    path = "/home/me/empty/"
    session.request.return_value = _response(
        CALDAV_ORIGIN + path,
        207,
        _xml((path, '<d:getetag>"collection"</d:getetag>')),
    )
    calendar = groupware_calendar.Calendar(path, "Empty", True)
    start = datetime.datetime(2026, 11, 25, tzinfo=datetime.timezone.utc)

    assert (
        groupware_calendar.Client(
            session, CALDAV_ORIGIN + "/principals/", "person@example.org", "test"
        ).events(calendar, start, start + datetime.timedelta(days=1))
        == []
    )


def test_multipart_mail_preserves_text_html_and_attachment_bytes():
    fixture = email.message.EmailMessage()
    fixture["From"] = "Sender <sender@example.org>"
    fixture["To"] = "Receiver <receiver@example.net>"
    fixture["Date"] = "Wed, 25 Nov 2026 09:30:00 +0300"
    fixture["Subject"] = "Отчёт"
    fixture.set_content("Plain body")
    fixture.add_alternative("<p>HTML body</p>", subtype="html")
    fixture.add_attachment(b"%PDF-data", "application", "pdf", filename="report.pdf")

    parsed = groupware_content.parse_mail(fixture.as_bytes())
    assert parsed.from_address == "sender@example.org"
    assert parsed.to_addresses == ["receiver@example.net"]
    assert parsed.sent_at == datetime.datetime(
        2026, 11, 25, 6, 30, tzinfo=datetime.timezone.utc
    )
    assert parsed.subject == "Отчёт"
    assert [part.KIND for part in parsed.payload.parts] == ["text", "html"]
    assert parsed.attachments == [
        groupware_content.Attachment("report.pdf", "application/pdf", b"%PDF-data")
    ]

    sent = groupware_content.compose_mail(
        uuid.UUID("00112233-4455-4677-8899-aabbccddeeff"),
        parsed.from_address,
        parsed.to_addresses,
        [],
        parsed.subject,
        parsed.payload,
        parsed.attachments,
    )
    assert sent["Message-ID"] == "<00112233-4455-4677-8899-aabbccddeeff@example.org>"
    assert sent["Bcc"] is None
    assert (
        groupware_content.parse_mail(sent.as_bytes()).attachments == parsed.attachments
    )


def test_event_projects_floating_and_all_day_dates_in_calendar_timezone():
    prefix = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:work-event\r\n"
    suffix = "\r\nSUMMARY:Встреча\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    timezone = zoneinfo.ZoneInfo("Europe/Moscow")
    timed = groupware_content.parse_event(
        prefix + "DTSTART:20261125T120000\r\nDURATION:PT45M" + suffix, timezone
    )
    all_day = groupware_content.parse_event(
        prefix + "DTSTART;VALUE=DATE:20261125" + suffix, timezone
    )

    assert timed.title == "Встреча"
    assert timed.starts_at == datetime.datetime(
        2026, 11, 25, 9, tzinfo=datetime.timezone.utc
    )
    assert timed.ends_at - timed.starts_at == datetime.timedelta(minutes=45)
    assert all_day.starts_at == datetime.datetime(
        2026, 11, 24, 21, tzinfo=datetime.timezone.utc
    )
    assert all_day.ends_at - all_day.starts_at == datetime.timedelta(days=1)


def test_recurring_event_is_not_written_as_a_single_event():
    data = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:weekly-event\r\n"
        "DTSTART:20261125T120000Z\r\nDURATION:PT45M\r\nRRULE:FREQ=WEEKLY\r\n"
        "SUMMARY:Weekly\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    assert groupware_content.parse_event(data, datetime.timezone.utc) is None


def _address(ip, port, family=socket.AF_INET):
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)


@pytest.mark.parametrize(
    "invalid",
    [
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.1",
        "::1",
        "::ffff:192.168.0.1",
        "64:ff9b::a9fe:a9fe",
    ],
)
def test_external_protocols_reject_mixed_public_and_internal_dns(monkeypatch, invalid):
    lookup = mock.Mock(return_value=[_address("8.8.8.8", 993), _address(invalid, 993)])
    dial = mock.Mock()
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(socket, "create_connection", dial)

    endpoint = groupware_network.Endpoint("mixed.example.test", 993)
    with pytest.raises(groupware_network.EndpointError):
        endpoint.connect(10, ssl.create_default_context())

    dial.assert_not_called()


def test_protocol_connect_pins_the_validated_ip_and_keeps_tls_hostname(monkeypatch):
    lookup = mock.Mock(return_value=[_address("8.8.8.8", 993)])
    dial = mock.Mock()
    context = mock.Mock()
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(socket, "create_connection", dial)

    result = groupware_network.Endpoint("mail.example.test", 993).connect(10, context)

    dial.assert_called_once_with(("8.8.8.8", 993), 10)
    context.wrap_socket.assert_called_once_with(
        dial.return_value, server_hostname="mail.example.test"
    )
    assert result is context.wrap_socket.return_value


def test_caldav_pins_address_and_keeps_http_host_and_tls_hostname(monkeypatch):
    lookup = mock.Mock(return_value=[_address("8.8.8.8", 8443)])
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    request = requests.Request(
        "PROPFIND", "https://dav.example.test:8443/principal/"
    ).prepare()

    pool = groupware_network.HTTPSAdapter().get_connection_with_tls_context(
        request, True
    )

    assert (pool.scheme, pool.host, pool.port) == ("https", "8.8.8.8", 8443)
    assert pool.conn_kw["server_hostname"] == pool.assert_hostname == "dav.example.test"
    assert request.headers["Host"] == "dav.example.test:8443"


def test_starttls_is_completed_before_authenticating_for_both_protocols(monkeypatch):
    endpoint = groupware_network.Endpoint("mail.example.test", 1587, "starttls")
    smtp = mock.Mock()
    imap = mock.MagicMock()
    imap.__enter__.return_value = imap
    monkeypatch.setattr(groupware_mail, "_SMTPConnection", mock.Mock(return_value=smtp))
    monkeypatch.setattr(groupware_mail, "_IMAPConnection", mock.Mock(return_value=imap))

    with groupware_mail.open_imap(endpoint, "login", "password"):
        pass
    with groupware_mail.open_smtp(endpoint, "login", "password"):
        pass

    assert [call[0] for call in smtp.mock_calls] == ["starttls", "login", "close"]
    assert [call[0] for call in imap.mock_calls] == [
        "__enter__",
        "starttls",
        "login",
        "__exit__",
    ]
