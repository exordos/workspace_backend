# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import dataclasses
import datetime
import unittest.mock
import uuid as sys_uuid

import pytest

from workspace.messenger_mail import codec
from workspace.messenger_mail import protocol


def _envelope(markdown="Hello, **messenger**"):
    return codec.MessengerEnvelope(
        from_address="author@workspace.internal",
        to_addresses=(
            "member@workspace.internal",
            "observer@workspace.internal",
        ),
        project_uuid=sys_uuid.UUID("10000000-0000-0000-0000-000000000001"),
        message_uuid=sys_uuid.UUID("20000000-0000-0000-0000-000000000002"),
        stream_uuid=sys_uuid.UUID("30000000-0000-0000-0000-000000000003"),
        topic_uuid=sys_uuid.UUID("40000000-0000-0000-0000-000000000004"),
        author_uuid=sys_uuid.UUID("50000000-0000-0000-0000-000000000005"),
        operation_uuid=sys_uuid.UUID("60000000-0000-0000-0000-000000000006"),
        markdown=markdown,
        sent_at=datetime.datetime(
            2026,
            7,
            15,
            10,
            30,
            tzinfo=datetime.timezone.utc,
        ),
    )


def test_codec_round_trip_preserves_utf8_markdown_and_workspace_urns():
    envelope = _envelope(
        "Привет! [report](urn:file:70000000-0000-0000-0000-000000000007)\n"
        "urn:image:80000000-0000-0000-0000-000000000008"
    )

    message = codec.build_message(envelope)
    parsed = codec.parse_message(message.as_bytes())

    assert parsed == envelope
    assert message.get_content_type() == "text/markdown"
    assert message.get_content_charset() == "utf-8"
    assert not message.is_multipart()
    assert list(message.iter_attachments()) == []


def test_codec_sets_required_headers_and_deterministic_message_id():
    envelope = _envelope()

    first = codec.build_message(envelope)
    second = codec.build_message(envelope)

    assert first["Message-ID"] == second["Message-ID"]
    assert first["Message-ID"] == (
        "<20000000-0000-0000-0000-000000000002@messenger.workspace.invalid>"
    )
    assert first[codec.HEADER_SCHEMA_VERSION] == "1"
    assert first[codec.HEADER_PROJECT_UUID] == str(envelope.project_uuid)
    assert first[codec.HEADER_MESSAGE_UUID] == str(envelope.message_uuid)
    assert first[codec.HEADER_STREAM_UUID] == str(envelope.stream_uuid)
    assert first[codec.HEADER_TOPIC_UUID] == str(envelope.topic_uuid)
    assert first[codec.HEADER_AUTHOR_UUID] == str(envelope.author_uuid)
    assert first[codec.HEADER_OPERATION_UUID] == str(envelope.operation_uuid)
    assert first[codec.HEADER_SOURCE_NAME] == "native"
    assert first[codec.HEADER_SOURCE] == '{"kind":"native"}'


def test_codec_preserves_future_source_provenance_without_exposing_a_new_api():
    envelope = dataclasses.replace(
        _envelope(),
        source_name="future-provider",
        source={"kind": "future-provider", "external_id": "message-42"},
    )

    parsed = codec.parse_message(codec.build_message(envelope).as_bytes())

    assert parsed.source_name == "future-provider"
    assert parsed.source == {
        "kind": "future-provider",
        "external_id": "message-42",
    }


def test_codec_rejects_mime_attachments():
    message = codec.build_message(_envelope())
    message.add_attachment(
        b"binary data",
        maintype="application",
        subtype="octet-stream",
        filename="report.bin",
    )

    with pytest.raises(
        codec.InvalidMessengerMessage,
        match="multipart messages are not supported",
    ):
        codec.parse_message(message.as_bytes())


def test_codec_rejects_non_markdown_body():
    message = codec.build_message(_envelope())
    message.set_type("text/plain")

    with pytest.raises(
        codec.InvalidMessengerMessage,
        match="must be UTF-8 markdown",
    ):
        codec.parse_message(message.as_bytes())


class FakeImapConnection:
    def __init__(self):
        self.responses = {
            "UIDVALIDITY": ("UIDVALIDITY", [b"91"]),
            "UIDNEXT": ("UIDNEXT", [b"42"]),
            "HIGHESTMODSEQ": ("HIGHESTMODSEQ", [b"1201"]),
            "APPENDUID": ("APPENDUID", [b"91 43"]),
        }
        self.uid_calls = []
        self.append_result = ("OK", [b"APPEND completed"])
        self.created = []

    def select(self, path, readonly=True):
        assert path == "Workspace/Events"
        assert readonly is True
        return "OK", [b"41"]

    def response(self, name):
        return self.responses[name]

    def uid(self, *args):
        self.uid_calls.append(args)
        if args[0] == "search":
            return "OK", [b"40 41"]
        if args[0] == "fetch":
            uid = int(args[1])
            return "OK", [
                (
                    f"1 (UID {uid} FLAGS (\\Seen $WorkspacePinned) RFC822 {{7}}".encode(),
                    b"message",
                ),
                b")",
            ]
        return "OK", [b"stored"]

    def append(self, path, flags, date_time, raw_message):
        assert path == "Workspace/Events"
        assert flags == "(\\Seen $WorkspacePinned)"
        assert date_time is None
        assert raw_message == b"message"
        return self.append_result

    def create(self, path):
        self.created.append(path)
        return "OK", [b"created"]


def test_imap_select_search_and_fetch_uid_state():
    connection = FakeImapConnection()
    client = protocol.ImapClient(
        protocol.ImapSettings(
            "imap.internal",
            143,
            protocol.Credentials("service", "secret"),
        )
    )
    client.connection = connection

    metadata = client.select("Workspace/Events")
    uids = client.search("UID 40:*")
    messages = client.fetch(uids)

    assert metadata == protocol.MailboxMetadata(91, 42, 1201)
    assert [message.uid for message in messages] == [40, 41]
    assert messages[0].flags == frozenset({"\\Seen", "$WorkspacePinned"})
    assert messages[0].raw_message == b"message"


def test_imap_append_parses_appenduid_response_code_and_serializes_keywords():
    connection = FakeImapConnection()
    client = protocol.ImapClient(
        protocol.ImapSettings(
            "imap.internal",
            143,
            protocol.Credentials("service", "secret"),
        )
    )
    client.connection = connection

    result = client.append(
        "Workspace/Events",
        b"message",
        flags=("\\Seen",),
        keywords=("$WorkspacePinned",),
    )

    assert result == protocol.AppendUid(91, 43)


def test_imap_append_prefers_tagged_appenduid():
    connection = FakeImapConnection()
    connection.append_result = (
        "OK",
        [b"[APPENDUID 101 202] APPEND completed"],
    )
    client = protocol.ImapClient(
        protocol.ImapSettings(
            "imap.internal",
            143,
            protocol.Credentials("service", "secret"),
        )
    )
    client.connection = connection

    result = client.append(
        "Workspace/Events",
        b"message",
        flags=("\\Seen",),
        keywords=("$WorkspacePinned",),
    )

    assert result == protocol.AppendUid(101, 202)


def test_imap_create_mailbox_and_store_flags_and_keywords():
    connection = FakeImapConnection()
    client = protocol.ImapClient(
        protocol.ImapSettings(
            "imap.internal",
            143,
            protocol.Credentials("service", "secret"),
        )
    )
    client.connection = connection

    client.create_mailbox("Workspace/State")
    client.store_flags(
        41,
        flags=("\\Seen", "\\Flagged"),
        keywords=("$WorkspacePinned",),
        mode="add",
    )

    assert connection.created == ["Workspace/State"]
    assert connection.uid_calls == [
        (
            "store",
            "41",
            "+FLAGS",
            "(\\Seen \\Flagged $WorkspacePinned)",
        )
    ]


class FakeEnsureMailboxConnection:
    def __init__(self, select_result, create_result=("OK", [b"created"])):
        self.select_result = select_result
        self.create_result = create_result
        self.create_calls = []

    def select(self, path, readonly=True):
        assert readonly is True
        return self.select_result

    def create(self, path):
        self.create_calls.append(path)
        return self.create_result


def test_imap_ensure_mailbox_creates_only_after_trycreate():
    connection = FakeEnsureMailboxConnection(
        ("NO", [b"[TRYCREATE] Mailbox does not exist"])
    )
    client = protocol.ImapClient(
        protocol.ImapSettings(
            "imap.internal",
            143,
            protocol.Credentials("service", "secret"),
        )
    )
    client.connection = connection

    assert client.ensure_mailbox("Workspace/State") is True
    assert connection.create_calls == ["Workspace/State"]


def test_imap_ensure_mailbox_treats_alreadyexists_as_safe_create_race():
    connection = FakeEnsureMailboxConnection(
        ("NO", [b"[NONEXISTENT] Mailbox does not exist"]),
        ("NO", [b"[ALREADYEXISTS] Mailbox already exists"]),
    )
    client = protocol.ImapClient(
        protocol.ImapSettings(
            "imap.internal",
            143,
            protocol.Credentials("service", "secret"),
        )
    )
    client.connection = connection

    assert client.ensure_mailbox("Workspace/State") is False
    assert connection.create_calls == ["Workspace/State"]


def test_imap_ensure_mailbox_does_not_mask_non_creation_errors():
    connection = FakeEnsureMailboxConnection(
        ("NO", [b"[AUTHENTICATIONFAILED] Access denied"])
    )
    client = protocol.ImapClient(
        protocol.ImapSettings(
            "imap.internal",
            143,
            protocol.Credentials("service", "secret"),
        )
    )
    client.connection = connection

    with pytest.raises(protocol.imaplib.IMAP4.error, match="Unable to select"):
        client.ensure_mailbox("Workspace/State")

    assert connection.create_calls == []


class FakeDeleteConnection:
    def __init__(self):
        self.uid_calls = []
        self.expunge_called = False

    def select(self, path, readonly=True):
        assert path == "INBOX"
        assert readonly is False
        return "OK", [b"2"]

    def response(self, name):
        assert name in {"UIDVALIDITY", "UIDNEXT", "HIGHESTMODSEQ"}
        return name, [b"1"]

    def uid(self, *args):
        self.uid_calls.append(args)
        if args[0] == "search":
            return "OK", [b"7 9"]
        return "OK", [b"done"]

    def expunge(self):
        self.expunge_called = True
        raise AssertionError("global EXPUNGE must never be used")


def test_imap_delete_by_message_id_uses_uid_expunge_only_for_matches():
    connection = FakeDeleteConnection()
    client = protocol.ImapClient(
        protocol.ImapSettings(
            "imap.internal",
            143,
            protocol.Credentials("service", "secret"),
        )
    )
    client.connection = connection
    message_id = "<20000000-0000-0000-0000-000000000002@messenger.workspace.invalid>"

    assert client.delete_by_message_id("INBOX", message_id) == [7, 9]
    assert connection.uid_calls == [
        ("search", None, f'HEADER Message-ID "{message_id}"'),
        ("store", "7", "+FLAGS", "(\\Deleted)"),
        ("store", "9", "+FLAGS", "(\\Deleted)"),
        ("expunge", "7,9"),
    ]
    assert connection.expunge_called is False


def test_imap_delete_by_message_id_rejects_search_injection():
    connection = FakeDeleteConnection()
    client = protocol.ImapClient(
        protocol.ImapSettings(
            "imap.internal",
            143,
            protocol.Credentials("service", "secret"),
        )
    )
    client.connection = connection

    with pytest.raises(ValueError, match="Invalid Message-ID"):
        client.delete_by_message_id("INBOX", "<valid@example>\r\nUID 1:*")

    assert connection.uid_calls == []


def test_imap_context_uses_timeout_starttls_and_login(monkeypatch):
    connection = unittest.mock.Mock()
    constructor = unittest.mock.Mock(return_value=connection)
    ssl_context = object()
    monkeypatch.setattr(protocol.imaplib, "IMAP4", constructor)
    monkeypatch.setattr(
        protocol.ssl,
        "create_default_context",
        unittest.mock.Mock(return_value=ssl_context),
    )
    settings = protocol.ImapSettings(
        "imap.internal",
        1143,
        protocol.Credentials("service", "secret"),
        security="starttls",
        timeout=2.5,
    )

    with protocol.ImapClient(settings):
        pass

    constructor.assert_called_once_with("imap.internal", 1143, timeout=2.5)
    connection.starttls.assert_called_once_with(ssl_context=ssl_context)
    connection.login.assert_called_once_with("service", "secret")
    connection.logout.assert_called_once_with()


@pytest.mark.parametrize(
    ("security", "constructor_name", "uses_starttls"),
    [
        ("plain", "SMTP", False),
        ("starttls", "SMTP", True),
        ("tls", "SMTP_SSL", False),
    ],
)
def test_smtp_security_timeout_optional_auth_and_context_cleanup(
    monkeypatch,
    security,
    constructor_name,
    uses_starttls,
):
    connection = unittest.mock.Mock()
    constructor = unittest.mock.Mock(return_value=connection)
    monkeypatch.setattr(protocol.smtplib, constructor_name, constructor)
    ssl_context = object()
    monkeypatch.setattr(
        protocol.ssl,
        "create_default_context",
        unittest.mock.Mock(return_value=ssl_context),
    )
    credentials = None
    if security != "plain":
        credentials = protocol.Credentials("service", "secret")
    settings = protocol.SmtpSettings(
        "smtp.internal",
        2525,
        security=security,
        credentials=credentials,
        timeout=3.5,
    )
    message = codec.build_message(_envelope())

    with protocol.SmtpClient(settings) as client:
        client.send(message)

    expected = unittest.mock.call("smtp.internal", 2525, timeout=3.5)
    if security == "tls":
        expected = unittest.mock.call(
            "smtp.internal",
            2525,
            timeout=3.5,
            context=ssl_context,
        )
    assert constructor.call_args == expected
    assert connection.starttls.called is uses_starttls
    if uses_starttls:
        connection.starttls.assert_called_once_with(context=ssl_context)
    if credentials is None:
        connection.login.assert_not_called()
    else:
        connection.login.assert_called_once_with("service", "secret")
    connection.send_message.assert_called_once_with(message)
    connection.quit.assert_called_once_with()


def test_smtp_send_without_context_still_closes_connection(monkeypatch):
    connection = unittest.mock.Mock()
    monkeypatch.setattr(
        protocol.smtplib,
        "SMTP",
        unittest.mock.Mock(return_value=connection),
    )
    client = protocol.SmtpClient(
        protocol.SmtpSettings("smtp.internal", 25, timeout=1.0)
    )
    message = codec.build_message(_envelope())

    client.send(message)

    connection.send_message.assert_called_once_with(message)
    connection.quit.assert_called_once_with()
    assert client.connection is None
