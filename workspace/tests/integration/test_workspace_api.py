# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

from workspace.tests.integration import conftest


def test_only_workspace_v1_contract_is_exposed(workspace_api):
    api = workspace_api
    assert api.get("/v1/").status_code == 200
    assert api.get("/v1/messenger/").status_code == 200
    assert api.get("/v1/mail/").status_code == 200
    assert api.get("/v1/calendar/").status_code == 200
    assert api.get("/v1/events/").status_code == 200
    assert api.get("/v1/epoch/").status_code == 200
    assert api.get("/v1/messages/").status_code >= 400
    assert api.get("/v1/external_accounts/").status_code >= 400
    assert api.get("/v1/messenger/events/").status_code >= 400


def test_mail_external_user_and_local_message_roundtrip(
    api, workspace_api, provider_api, db
):
    messenger_api = api
    api = workspace_api
    conftest.seed_workspace_user(
        db,
        api.user_uuid,
        f"user-{api.user_uuid}",
    )
    provider_uuid = sys_uuid.uuid4()
    registered = provider_api.put(
        f"/v1/providers/{provider_uuid}",
        json={
            "name": "Mail provider",
            "supported_kinds": ["mail"],
            "version": "1.0.0",
        },
    )
    assert registered.status_code == 200, registered.text
    external_user = messenger_api.post(
        "/v1/external_accounts/",
        user=api.user_uuid,
        project=api.project_id,
        json={
            "provider_uuid": str(provider_uuid),
            "server_url": "https://mail.example.com",
            "account_settings": {
                "kind": "mail",
                "credentials": {
                    "kind": "mail",
                    "username": "cassi@example.com",
                    "password": "application-password",
                },
                "email": "cassi@example.com",
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "imap_security": "tls",
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_security": "tls",
            },
        },
    )
    assert external_user.status_code in (200, 201), external_user.text
    external_user_payload = external_user.json()
    assert external_user_payload["account_type"] == "mail"
    assert external_user_payload["access_status"] == "pending"
    assert external_user_payload["account_settings"]["credentials"] is None

    folder = api.post(
        "/v1/mail/folders/",
        json={
            "external_user_uuid": external_user_payload["uuid"],
            "path": "INBOX",
            "name": "Inbox",
            "special_use": "inbox",
        },
    )
    assert folder.status_code in (200, 201), folder.text

    message = api.post(
        "/v1/mail/messages/",
        json={
            "folder_uuid": folder.json()["uuid"],
            "to_addresses": ["recipient@example.com"],
            "subject": "Local engine",
            "body_text": "Stored before transport",
            "draft": True,
        },
    )
    assert message.status_code in (200, 201), message.text
    assert message.json()["from_address"] == "cassi@example.com"
    assert message.json()["to_addresses"] == ["recipient@example.com"]
    assert message.json()["provider"]["uuid"] == str(provider_uuid)
    assert message.json()["delivery"]["status"] == "pending"
    assert "sync_status" not in message.json()

    commands = provider_api.get(
        f"/v1/providers/{provider_uuid}/mail/commands/",
        params={"status": "pending"},
    )
    message_command = next(
        item for item in commands.json() if item["operation"] == "message.create"
    )
    assert message_command["payload"]["folder_urn"] == (
        f"urn:mail-folder:{folder.json()['uuid']}"
    )
    assert "folder_uuid" not in message_command["payload"]

    sent = api.post(
        f"/v1/mail/messages/{message.json()['uuid']}/actions/send/invoke",
        json={},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["draft"] is False
    assert sent.json()["delivery"]["status"] == "pending"

    events = api.get("/v1/events/")
    assert events.status_code == 200, events.text
    kinds = [item["payload"]["kind"] for item in events.json()]
    assert kinds == [
        "mail.folder.created",
        "mail.message.created",
        "mail.message.updated",
    ]


def test_calendar_local_event_roundtrip(workspace_api, db):
    api = workspace_api
    conftest.seed_workspace_user(
        db,
        api.user_uuid,
        f"user-{api.user_uuid}",
    )
    calendar = api.post(
        "/v1/calendar/calendars/",
        json={"name": "Personal", "color": "#3366ff"},
    )
    assert calendar.status_code in (200, 201), calendar.text

    event = api.post(
        "/v1/calendar/events/",
        json={
            "calendar_uuid": calendar.json()["uuid"],
            "uid": str(sys_uuid.uuid4()),
            "summary": "Workspace API review",
            "starts_at": "2026-07-14T12:00:00+00:00",
            "ends_at": "2026-07-14T13:00:00+00:00",
            "attendees": [{"email": "reviewer@example.com"}],
        },
    )
    assert event.status_code in (200, 201), event.text
    assert event.json()["summary"] == "Workspace API review"
    assert event.json()["attendees"] == [{"email": "reviewer@example.com"}]
    assert event.json()["alarms"] == []
    assert "sync_status" not in event.json()
    assert "ics" not in event.json()
    assert "etag" not in event.json()

    listed = api.get(
        "/v1/calendar/events/",
        params={"calendar_uuid": calendar.json()["uuid"]},
    )
    assert listed.status_code == 200, listed.text
    assert [item["uuid"] for item in listed.json()] == [event.json()["uuid"]]
    assert listed.json()[0]["attendees"] == [{"email": "reviewer@example.com"}]

    events = api.get("/v1/events/")
    assert events.status_code == 200, events.text
    kinds = [item["payload"]["kind"] for item in events.json()]
    assert kinds == [
        "calendar.calendar.created",
        "calendar.event.created",
    ]
    assert events.json()[-1]["payload"]["attendees"] == [
        {"email": "reviewer@example.com"},
    ]
