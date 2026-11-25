# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import contextlib
import datetime
import email.message
import smtplib
import uuid
from unittest import mock

import pytest
from cryptography import exceptions
from cryptography.hazmat.primitives.ciphers import aead
from oslo_config import cfg
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.storage.sql import engines

from workspace.common import groupware_opts
from workspace.mail_api.dm import models as mail_models
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.services import groupware_caldav_write
from workspace.services import groupware_calendar
from workspace.services import groupware_content
from workspace.services import groupware_credentials
from workspace.services import groupware_delivery
from workspace.services import groupware_mail
from workspace.services import groupware_network
from workspace.services import groupware_projection
from workspace.services import groupware_sync


ACCOUNT_PERMISSIONS = ("workspace.external_account.create",)


def _account(api, address):
    response = api.post(
        "/v1/external_accounts/",
        json={
            "provider": "google",
            "settings": {
                "email": address,
                "smtp": {"host": "smtp.example.test", "port": 465},
            },
            "status": "active",
        },
        permissions=ACCOUNT_PERMISSIONS,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["uuid"])


def _event(calendar, title):
    return groupware_calendar.Event(
        calendar.path + "same.ics",
        '"1"',
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:event\r\n"
        "DTSTART:20261125T120000Z\r\nDURATION:PT45M\r\n"
        f"SUMMARY:{title}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
    )


def test_two_mailboxes_and_multiple_calendars_are_visible_and_keep_distinct_ids(
    workspace_api, tmp_path, monkeypatch
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    work = _account(workspace_api, "first@example.test")
    home = _account(workspace_api, "second@example.test")
    inbox = groupware_mail.Folder(b"INBOX", "Inbox", "inbox")
    source = email.message.EmailMessage()
    source["From"] = "sender@example.test"
    source["Subject"] = "sample"
    source.set_content("hello")
    source.add_attachment(
        b"attachment", "application", "octet-stream", filename="sample.bin"
    )
    mail = groupware_mail.Message(3, source.as_bytes(), False, True)
    first = groupware_calendar.Calendar("/calendars/first/", "First", True)
    second = groupware_calendar.Calendar("/calendars/second/", "Second", False)

    engine = engines.engine_factory.get_engine()
    for _ in range(2):
        with engine.session_manager() as session:
            account = external_models.ExternalAccount.objects.get_one(
                filters={"uuid": work}, session=session
            )
            projection = groupware_projection.AccountProjection(session, account)
            folder = projection.mail_folder(inbox)
            first_message = projection.mail_message(folder, 42, mail)
            for calendar in (first, second):
                projection.calendar_event(
                    projection.calendar(calendar),
                    _event(calendar, calendar.name),
                    datetime.timezone.utc,
                )

            account = external_models.ExternalAccount.objects.get_one(
                filters={"uuid": home}, session=session
            )
            projection = groupware_projection.AccountProjection(session, account)
            folder = projection.mail_folder(inbox)
            second_message = projection.mail_message(folder, 42, mail)
            projection.calendar_event(
                projection.calendar(first),
                _event(first, "Other"),
                datetime.timezone.utc,
            )

            assert first_message.uuid != second_message.uuid

    messages = workspace_api.get("/v1/mail/messages/").json()
    assert len(messages) == 2
    assert {item["external_account_uuid"] for item in messages} == {
        str(work),
        str(home),
    }
    assert {item["subject"] for item in messages} == {"sample"}
    assert all("provider_identity" not in item for item in messages)
    attachments = {
        item["attachment_urns"][0].removeprefix("urn:file:") for item in messages
    }
    assert len(attachments) == 2
    other_user = uuid.uuid4()
    for attachment in attachments:
        assert workspace_api.get(f"/v1/messenger/files/{attachment}").status_code == 200
        assert (
            workspace_api.get(
                f"/v1/messenger/files/{attachment}", user=other_user
            ).status_code
            == 404
        )
        assert file_storage.read_workspace_file(uuid.UUID(attachment)) == b"attachment"
    calendars = workspace_api.get("/v1/calendar/calendars/").json()
    assert len(calendars) == 3
    assert sum(item["external_account_uuid"] == str(work) for item in calendars) == 2
    events = workspace_api.get("/v1/calendar/events/").json()
    assert {item["payload"]["title"] for item in events} == {"First", "Second", "Other"}
    assert workspace_api.get("/v1/calendar/events/", user=other_user).json() == []


def test_credentials_are_encrypted_and_bound_to_the_account_and_service(
    workspace_api, db
):
    first = _account(workspace_api, "first@example.test")
    second = _account(workspace_api, "second@example.test")
    store = groupware_credentials.CredentialStore(
        aead.AESGCM.generate_key(bit_length=256)
    )
    engine = engines.engine_factory.get_engine()
    password = "fixture-application-password"

    with engine.session_manager() as session:
        store.save(session, first, "mail", password)
        assert store.load(session, first, "mail") == password
        assert store.load(session, first, "calendar") is None
        assert store.load(session, second, "mail") is None

    encrypted = db.execute(
        "SELECT encrypted_password FROM groupware_account_credentials "
        "WHERE external_account_uuid = %s AND service = 'mail'",
        (first,),
    ).fetchone()[0]
    assert password.encode() not in encrypted

    db.execute(
        "INSERT INTO groupware_account_credentials (external_account_uuid, service, encrypted_password) VALUES (%s, 'calendar', %s)",
        (second, encrypted),
    )
    with engine.session_manager() as session:
        with pytest.raises(exceptions.InvalidTag):
            store.load(session, second, "calendar")
        store.remove(session, first)
        assert store.load(session, first, "mail") is None

    removed = workspace_api.delete(
        f"/v1/external_accounts/{second}",
        permissions=("workspace.external_account.delete",),
    )
    assert removed.status_code == 204, removed.text
    assert not db.execute(
        "SELECT 1 FROM groupware_account_credentials WHERE external_account_uuid = %s",
        (second,),
    ).fetchone()


@pytest.mark.parametrize("delivery", ["accepted", "disconnected", "interrupted"])
def test_outbox_sends_from_the_selected_account_once(
    workspace_api, db, monkeypatch, delivery
):
    accepted = delivery == "accepted"
    other = _account(workspace_api, "other@example.test")
    sender = "sender@example.test"
    receiver = "receiver@example.test"
    bcc = "blind@example.test"
    account_uuid = _account(workspace_api, sender)
    sent_folder = workspace_api.post(
        "/v1/mail/folders/",
        json={
            "name": "Sent",
            "kind": "sent",
            "external_account_uuid": str(account_uuid),
        },
    ).json()
    drafts = workspace_api.post(
        "/v1/mail/folders/",
        json={
            "name": "Drafts",
            "kind": "drafts",
            "external_account_uuid": str(account_uuid),
        },
    ).json()
    draft = workspace_api.post(
        "/v1/mail/drafts/",
        json={
            "folder_uuid": drafts["uuid"],
            "subject": "queued",
            "to_addresses": [receiver],
            "bcc_addresses": [bcc],
            "payload": {
                "kind": "multipart",
                "parts": [{"kind": "text", "content": "body"}],
            },
        },
    ).json()
    queued = workspace_api.post(
        f"/v1/mail/drafts/{draft['uuid']}/actions/send/invoke", json={}
    )
    assert queued.status_code == 200, queued.text
    engine = engines.engine_factory.get_engine()
    store = groupware_credentials.CredentialStore(
        aead.AESGCM.generate_key(bit_length=256)
    )
    with engine.session_manager() as session:
        store.save(session, other, "mail", "other-fixture-password")
        store.save(session, account_uuid, "mail", "sender-fixture-password")
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": account_uuid}, session=session
        )
        projection = groupware_projection.AccountProjection(session, account)
        provider_sent = projection.mail_folder(
            groupware_mail.Folder(b"ProviderSent", "Provider Sent", "sent")
        )
        assert sent_folder["uuid"] == str(provider_sent.uuid)
        assert (
            projection.mail_folder(
                groupware_mail.Folder(b"ProviderSent", "Provider Sent", "sent")
            ).uuid
            == provider_sent.uuid
        )
        alternate = projection.mail_folder(
            groupware_mail.Folder(b"OlderSent", "Older Sent", "sent")
        )
        assert alternate.kind == "custom"

    transport = mock.Mock()
    transport.send_message.return_value = {}
    if delivery == "disconnected":
        transport.send_message.side_effect = smtplib.SMTPServerDisconnected(
            "provider details"
        )
    if delivery == "interrupted":
        db.execute(
            """
            UPDATE mail_outgoing_intents SET status = 'checking', attempts = 1, updated_at = now() - interval '11 minutes'
            WHERE message_uuid = %s
            """,
            (draft["uuid"],),
        )
    opened = mock.Mock(return_value=contextlib.nullcontext(transport))
    monkeypatch.setattr(groupware_mail, "open_smtp", opened)
    outbox = groupware_delivery.Sender(engine, store)
    assert outbox.send_next(other) is False
    assert outbox.send_next(account_uuid) is (delivery != "interrupted")
    assert outbox.send_next(account_uuid) is False

    if delivery == "interrupted":
        opened.assert_not_called()
    else:
        opened.assert_called_once_with(
            groupware_network.Endpoint("smtp.example.test", 465),
            sender,
            "sender-fixture-password",
        )
        transport.send_message.assert_called_once()
        mime, envelope_sender, recipients = transport.send_message.call_args.args
        assert mime["From"] == envelope_sender == sender
        assert mime["Bcc"] is None
        assert recipients == [receiver, bcc]
        if accepted:
            sent = workspace_api.get(f"/v1/mail/messages/{draft['uuid']}").json()
            assert sent["folder_uuid"] == str(provider_sent.uuid)
            with engine.session_manager() as session:
                account = external_models.ExternalAccount.objects.get_one(
                    filters={"uuid": account_uuid}, session=session
                )
                folder = mail_models.MailFolder.objects.get_one(
                    filters={"uuid": provider_sent.uuid}, session=session
                )
                projection = groupware_projection.AccountProjection(session, account)
                copy = groupware_mail.Message(10, mime.as_bytes(), True, False)
                for _ in range(2):
                    projected = projection.mail_message(folder, 51, copy)
                    assert str(projected.uuid) == draft["uuid"]
    response = workspace_api.get(f"/v1/mail/messages/{draft['uuid']}")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["delivery_status"] == ("sent" if accepted else "failed")
    assert bool(result.get("sent_at")) is accepted
    assert result["folder_uuid"] == (
        str(provider_sent.uuid) if accepted else drafts["uuid"]
    )
    row = db.execute(
        "SELECT status, attempts, error FROM mail_outgoing_intents WHERE message_uuid = %s",
        (draft["uuid"],),
    ).fetchone()
    assert row[:2] == ("done" if accepted else "error", 1)
    assert "provider details" not in str(row[2])


@pytest.fixture
def groupware_store(workspace_api, db):
    groupware_opts.register_opts()
    key = aead.AESGCM.generate_key(bit_length=256)
    cfg.CONF.set_override(
        "secret_encryption_key", key.hex(), group=groupware_opts.DOMAIN
    )
    try:
        yield groupware_credentials.CredentialStore(key)
    finally:
        cfg.CONF.clear_override("secret_encryption_key", group=groupware_opts.DOMAIN)
        db.execute(
            """
            DELETE FROM groupware_account_credentials credentials USING m_external_accounts_v2 account
            WHERE credentials.external_account_uuid = account.uuid
              AND account.owner_user_uuid = %s AND account.project_id = %s
            """,
            (workspace_api.user_uuid, workspace_api.project_id),
        )


@pytest.mark.parametrize("smtp_available", (True, False))
def test_groupware_connect_sync_disconnect_and_reconnect_api(
    workspace_api, db, groupware_store, monkeypatch, smtp_available
):
    permissions = tuple(
        f"workspace.external_account.{action}"
        for action in ("create", "read", "update")
    )
    secret = "fixture-provider-password"
    mail = workspace_api.post(
        "/v1/external_accounts/",
        permissions=permissions,
        json={
            "provider": "imap_smtp",
            "settings": {
                "email": "mail@example.test",
                "imap": {"host": "imap.example.test", "port": 993},
                "smtp": {"host": "smtp.example.test", "port": 587, "tls": "starttls"},
                "password": secret,
            },
        },
    )
    assert mail.status_code == 201, mail.text
    calendar = workspace_api.post(
        "/v1/external_accounts/",
        permissions=permissions,
        json={
            "provider": "caldav",
            "settings": {
                "url": "https://calendar.example.test/dav/",
                "username": "calendar-login",
                "password": secret,
            },
        },
    )
    assert calendar.status_code == 201, calendar.text
    assert mail.json()["capabilities"] == ["mail"]
    assert calendar.json()["capabilities"] == ["calendar"]
    assert mail.json()["status"] == calendar.json()["status"] == "new"
    assert all(
        secret not in response.text and "password" not in response.json()["settings"]
        for response in (mail, calendar)
    )

    imap = mock.Mock()
    imap.folders.return_value = [groupware_mail.Folder(b"Inbox", "Inbox", "inbox")]
    imap.select.return_value = 52
    imap.uids.return_value = [1]
    imap.fetch.return_value = groupware_mail.Message(
        1, b"Subject: private\r\n\r\nbody", True, False
    )
    imap_open = mock.Mock(return_value=contextlib.nullcontext(imap))
    smtp_open = mock.Mock(return_value=contextlib.nullcontext(mock.Mock()))
    if not smtp_available:
        smtp_open.side_effect = smtplib.SMTPServerDisconnected("fixture")
    monkeypatch.setattr(groupware_mail, "open_imap", imap_open)
    monkeypatch.setattr(groupware_mail, "open_smtp", smtp_open)
    caldav = mock.Mock()
    calendar_source = groupware_calendar.Calendar("/dav/home/", "Personal", True)
    caldav.calendars.return_value = [calendar_source]
    caldav.events.return_value = [_event(calendar_source, "Imported")]
    monkeypatch.setattr(groupware_calendar, "Client", mock.Mock(return_value=caldav))
    engine = engines.engine_factory.get_engine()
    sync = groupware_sync.Synchronizer(engine, groupware_store, interval=300)
    assert sync.sync_next() is True
    assert sync.sync_next() is True
    assert sync.sync_next() is False
    imap_open.assert_called_once_with(
        groupware_network.Endpoint("imap.example.test", 993),
        "mail@example.test",
        secret,
    )
    smtp_open.assert_called_once_with(
        groupware_network.Endpoint("smtp.example.test", 587, "starttls"),
        "mail@example.test",
        secret,
    )
    assert [
        entry["subject"] for entry in workspace_api.get("/v1/mail/messages/").json()
    ] == ["private"]
    assert [
        entry["payload"]["title"]
        for entry in workspace_api.get("/v1/calendar/events/").json()
    ] == ["Imported"]

    mail_uuid = mail.json()["uuid"]
    mail_path = f"/v1/external_accounts/{mail_uuid}"
    mail_state = workspace_api.get(mail_path, permissions=permissions).json()
    assert mail_state["status"] == ("active" if smtp_available else "error")
    if not smtp_available:
        assert "SMTP" in mail_state["error_description"]
    assert (
        workspace_api.delete(
            mail_path, permissions=("workspace.external_account.delete",)
        ).status_code
        == 400
    )
    forbidden = workspace_api.put(
        mail_path,
        permissions=permissions,
        json={"settings": {"email": "another@example.test"}},
    )
    assert forbidden.status_code == 400
    disabled = workspace_api.put(
        mail_path, permissions=permissions, json={"disabled": True}
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["disabled"]
    assert not db.execute(
        "SELECT 1 FROM groupware_account_credentials WHERE external_account_uuid = %s",
        (mail_uuid,),
    ).fetchone()
    assert len(workspace_api.get("/v1/mail/messages/").json()) == 1
    assert (
        workspace_api.put(
            mail_path, permissions=permissions, json={"disabled": False}
        ).status_code
        == 400
    )
    reconnected = workspace_api.put(
        mail_path,
        permissions=permissions,
        json={"disabled": False, "settings": {"password": "fixture-replacement"}},
    )
    assert reconnected.status_code == 200, reconnected.text
    assert not reconnected.json()["disabled"]
    assert "fixture-replacement" not in reconnected.text
    events = workspace_api.get("/v1/events/", params={"page_limit": 100})
    assert events.status_code == 200
    assert secret not in events.text and "fixture-replacement" not in events.text


@pytest.mark.parametrize("oversized", (False, True))
def test_mail_import_resumes_after_a_bounded_batch(
    workspace_api, db, groupware_store, monkeypatch, oversized
):
    created = workspace_api.post(
        "/v1/external_accounts/",
        permissions=ACCOUNT_PERMISSIONS,
        json={
            "provider": "imap_smtp",
            "settings": {
                "email": "mail@example.test",
                "imap": {"host": "imap.example.test", "port": 993},
                "smtp": {"host": "smtp.example.test", "port": 465},
                "password": "fixture",
            },
        },
    )
    assert created.status_code == 201
    source = b"Subject: imported\r\n\r\nbody"
    monkeypatch.setattr(groupware_mail, "MAX_MESSAGE_BYTES", len(source))
    mailbox = mock.Mock()
    mailbox.folders.return_value = [groupware_mail.Folder(b"Inbox", "Inbox", "inbox")]
    mailbox.select.return_value = 40
    if oversized:
        amount = len(source) + 1
        monkeypatch.setattr(groupware_sync, "MAIL_BYTES_PER_SYNC", 2 * amount)
        mailbox.uids.side_effect = {1: [1, 2, 3], 3: [3]}.__getitem__
        mailbox.fetch.side_effect = [
            groupware_mail.MessageTooLarge(amount),
            groupware_mail.MessageTooLarge(amount),
            groupware_mail.Message(3, source, False, False),
        ]
    else:
        mailbox.uids.side_effect = {1: [1, 2], 2: [2]}.__getitem__
        mailbox.fetch.side_effect = lambda uid: groupware_mail.Message(
            uid, source, False, False
        )
    monkeypatch.setattr(
        groupware_mail, "open_imap", lambda *args: contextlib.nullcontext(mailbox)
    )
    monkeypatch.setattr(
        groupware_mail, "open_smtp", lambda *args: contextlib.nullcontext()
    )
    sync = groupware_sync.Synchronizer(
        engines.engine_factory.get_engine(), groupware_store, interval=300
    )
    account_uuid = created.json()["uuid"]
    progress = """
        SELECT last_uid FROM groupware_mailbox_progress
        JOIN m_mail_folders ON m_mail_folders.uuid = groupware_mailbox_progress.folder_uuid
        WHERE m_mail_folders.external_account_uuid = %s
    """
    assert sync.sync_next()
    assert len(workspace_api.get("/v1/mail/messages/").json()) == (
        0 if oversized else 1
    )
    assert db.execute(progress, (account_uuid,)).fetchone() == (2 if oversized else 1,)
    db.execute(
        "UPDATE groupware_account_credentials SET next_sync_at = now() WHERE external_account_uuid = %s",
        (account_uuid,),
    )
    assert sync.sync_next()
    assert len(workspace_api.get("/v1/mail/messages/").json()) == (
        1 if oversized else 2
    )
    assert db.execute(progress, (account_uuid,)).fetchone() == (3 if oversized else 2,)


@pytest.mark.parametrize(
    "create_result", ("etag", "read_failed", "rolled_back", "imported")
)
def test_calendar_api_writes_provider_events_and_protects_against_conflicts(
    workspace_api, db, groupware_store, monkeypatch, create_result
):
    created_account = workspace_api.post(
        "/v1/external_accounts/",
        permissions=ACCOUNT_PERMISSIONS,
        json={
            "provider": "caldav",
            "settings": {
                "url": "https://calendar.example.test/remote.php/dav/",
                "username": "login",
                "password": "fixture",
            },
        },
    )
    assert created_account.status_code == 201, created_account.text
    client = mock.Mock()
    source = groupware_calendar.Calendar(
        "/remote.php/dav/calendars/login/test/", "Personal", True
    )
    server = {}

    def put(path, data, *, etag):
        old = server.get(path)
        if (old.etag if old is not None else None) != etag:
            raise groupware_calendar.CalendarHTTPError(412)
        tag = f'"{uuid.uuid4()}"'
        server[path] = groupware_calendar.Event(path, tag, data)
        if etag is None and create_result == "read_failed":
            return None
        return tag

    def delete(path, *, etag):
        assert server[path].etag == etag
        del server[path]

    client.calendars.return_value = [source]
    client.events.side_effect = lambda *args: list(server.values())
    client.get.side_effect = lambda path: server[path]
    client.put.side_effect = put
    client.delete.side_effect = delete
    monkeypatch.setattr(groupware_calendar, "Client", mock.Mock(return_value=client))
    sync = groupware_sync.Synchronizer(
        engines.engine_factory.get_engine(), groupware_store, interval=300
    )
    assert sync.sync_next()
    (calendar,) = workspace_api.get("/v1/calendar/calendars/").json()
    assert calendar["read_only"] is False
    event_payload = {
        "kind": "event",
        "title": "Created in Workspace",
        "starts_at": "2026-11-25T13:00:00Z",
        "ends_at": "2026-11-25T13:15:00Z",
    }
    request = {"calendar_uuid": calendar["uuid"], "payload": event_payload}
    if create_result in ("rolled_back", "imported"):
        request["uuid"] = str(uuid.uuid4())
        with monkeypatch.context() as patch:
            patch.setattr(
                groupware_caldav_write,
                "write_event_binding",
                mock.Mock(side_effect=ra_exceptions.ValidationErrorException),
            )
            failed = workspace_api.post("/v1/calendar/events/", json=request)
        assert failed.status_code == 400
        assert len(server) == 1
        assert workspace_api.get("/v1/calendar/events/").json() == []
        if create_result == "imported":
            db.execute(
                "UPDATE groupware_account_credentials SET next_sync_at = now() WHERE external_account_uuid = %s",
                (created_account.json()["uuid"],),
            )
            assert sync.sync_next()
    if create_result == "read_failed":
        client.get.side_effect = groupware_calendar.CalendarError(
            "fixture read failure"
        )
    event = workspace_api.post("/v1/calendar/events/", json=request)
    assert event.status_code == 201, event.text
    if "uuid" in request:
        assert event.json()["uuid"] == request["uuid"]
        replay = workspace_api.post("/v1/calendar/events/", json=request)
        assert replay.status_code == 201
        assert replay.json() == event.json()
    event_path = f"/v1/calendar/events/{event.json()['uuid']}"
    (remote_path,) = server
    assert (
        groupware_content.parse_event(
            server[remote_path].data, datetime.timezone.utc
        ).title
        == event_payload["title"]
    )
    if create_result == "read_failed":
        assert workspace_api.delete(event_path).status_code == 400
        db.execute(
            "UPDATE groupware_account_credentials SET next_sync_at = now() WHERE external_account_uuid = %s",
            (created_account.json()["uuid"],),
        )
        assert sync.sync_next()
    updated = workspace_api.put(
        event_path, json={"payload": {**event_payload, "title": "Edited in Workspace"}}
    )
    assert updated.status_code == 200, updated.text
    assert (
        groupware_content.parse_event(
            server[remote_path].data, datetime.timezone.utc
        ).title
        == "Edited in Workspace"
    )

    remote = server[remote_path]
    server[remote_path] = groupware_calendar.Event(
        remote_path,
        '"changed"',
        remote.data.replace("Edited in Workspace", "Edited at provider"),
    )
    conflict = workspace_api.put(
        event_path, json={"payload": {**event_payload, "title": "Conflict"}}
    )
    assert conflict.status_code == 400
    assert (
        workspace_api.get(event_path).json()["payload"]["title"]
        == "Edited in Workspace"
    )
    db.execute(
        "UPDATE groupware_account_credentials SET next_sync_at = now() WHERE external_account_uuid = %s",
        (created_account.json()["uuid"],),
    )
    assert sync.sync_next()
    assert (
        workspace_api.get(event_path).json()["payload"]["title"] == "Edited at provider"
    )
    assert len(workspace_api.get("/v1/calendar/events/").json()) == 1
    assert workspace_api.delete(event_path).status_code == 204
    assert workspace_api.get(event_path).status_code == 404
    assert not server
