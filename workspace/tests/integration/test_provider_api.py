# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid


def _register_mail_provider(provider_api, provider_uuid, name="Mail provider"):
    response = provider_api.put(
        f"/v1/providers/{provider_uuid}",
        json={
            "name": name,
            "supported_kinds": ["mail"],
            "version": "1.0.0",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_mail_account(api, provider_uuid):
    response = api.post(
        "/v1/external_accounts/",
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
    assert response.status_code in (200, 201), response.text
    return response.json()


def _register_calendar_provider(provider_api, provider_uuid):
    response = provider_api.put(
        f"/v1/providers/{provider_uuid}",
        json={
            "name": "Calendar provider",
            "supported_kinds": ["calendar"],
            "version": "1.0.0",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_calendar_account(api, provider_uuid):
    response = api.post(
        "/v1/external_accounts/",
        json={
            "provider_uuid": str(provider_uuid),
            "server_url": "https://calendar.example.com",
            "account_settings": {
                "kind": "calendar",
                "credentials": {
                    "kind": "calendar",
                    "username": "cassi@example.com",
                    "password": "application-password",
                },
            },
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def _register_messenger_provider(provider_api, provider_uuid, name="Messenger"):
    response = provider_api.put(
        f"/v1/providers/{provider_uuid}",
        json={
            "name": name,
            "supported_kinds": ["zulip"],
            "version": "1.0.0",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_messenger_account(api, provider_uuid):
    response = api.post(
        "/v1/external_accounts/",
        json={
            "provider_uuid": str(provider_uuid),
            "server_url": "https://chat.example.com",
            "account_settings": {
                "kind": "zulip",
                "credentials": {
                    "kind": "zulip",
                    "login": "cassi@example.com",
                    "token": "provider-token",
                },
            },
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def _bind_workspace_api(workspace_api, messenger_api):
    workspace_api.user_uuid = messenger_api.user_uuid
    workspace_api.project_id = messenger_api.project_id
    return workspace_api


def test_mail_provider_vertical_slice(api, workspace_api, provider_api):
    ui_api = _bind_workspace_api(workspace_api, api)
    provider_uuid = sys_uuid.uuid4()
    provider = _register_mail_provider(provider_api, provider_uuid)
    assert provider["uuid"] == str(provider_uuid)

    account = _create_mail_account(api, provider_uuid)
    assert account["access_status"] == "pending"
    assert account["account_settings"]["credentials"] is None

    provider_account = provider_api.get(
        f"/v1/providers/{provider_uuid}/external_accounts/{account['uuid']}",
    )
    assert provider_account.status_code == 200, provider_account.text
    assert (
        provider_account.json()["settings"]["credentials"]["password"]
        == "application-password"
    )
    assert (
        provider_account.json()["settings"]["server_url"] == "https://mail.example.com"
    )
    account_status = provider_api.post(
        f"/v1/providers/{provider_uuid}/external_accounts/"
        f"{account['uuid']}/actions/status/invoke",
        json={"status": "confirmed"},
    )
    assert account_status.status_code == 200, account_status.text
    assert account_status.json()["status"] == "confirmed"

    folder_uuid = sys_uuid.uuid4()
    folder = provider_api.put(
        f"/v1/providers/{provider_uuid}/mail/folders/{folder_uuid}",
        json={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "INBOX",
            "path": "INBOX",
            "name": "Inbox",
            "special_use": "inbox",
        },
    )
    assert folder.status_code == 200, folder.text
    assert folder.json()["uuid"] == str(folder_uuid)
    assert folder.json()["urn"] == f"urn:mail-folder:{folder_uuid}"
    assert folder.json()["provider_external_id"] == "INBOX"

    message_uuid = sys_uuid.uuid4()
    message = provider_api.put(
        f"/v1/providers/{provider_uuid}/mail/messages/{message_uuid}",
        json={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "INBOX:42",
            "folder_urn": f"urn:mail-folder:{folder_uuid}",
            "from_address": "sender@example.com",
            "to_addresses": ["cassi@example.com"],
            "subject": "Provider delivery",
            "body_text": "Inbound message",
        },
    )
    assert message.status_code == 200, message.text
    assert message.json()["to_addresses"] == ["cassi@example.com"]

    matching_messages = provider_api.get(
        f"/v1/providers/{provider_uuid}/mail/messages/",
        params={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "INBOX:42",
        },
    )
    assert matching_messages.status_code == 200, matching_messages.text
    assert [item["uuid"] for item in matching_messages.json()] == [
        str(message_uuid),
    ]

    ui_message = ui_api.get(f"/v1/mail/messages/{message_uuid}")
    assert ui_message.status_code == 200, ui_message.text
    assert ui_message.json()["provider"] == {
        "uuid": str(provider_uuid),
        "name": "Mail provider",
        "kind": "mail",
    }
    assert ui_message.json()["delivery"]["status"] == "delivered"
    assert "provider_external_id" not in ui_message.json()
    assert "sync_status" not in ui_message.json()

    updated = ui_api.put(
        f"/v1/mail/messages/{message_uuid}",
        json={"subject": "Changed in Workspace"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["delivery"]["status"] == "pending"

    commands = provider_api.get(
        f"/v1/providers/{provider_uuid}/mail/commands/",
        params={"status": "pending"},
    )
    assert commands.status_code == 200, commands.text
    assert len(commands.json()) == 1
    command = commands.json()[0]
    assert command["entity_urn"] == f"urn:mail-message:{message_uuid}"
    assert command["operation"] == "message.update"
    assert command["payload"]["folder_urn"] == f"urn:mail-folder:{folder_uuid}"
    assert "folder_uuid" not in command["payload"]

    result = provider_api.post(
        f"/v1/providers/{provider_uuid}/mail/commands/"
        f"{command['uuid']}/actions/result/invoke",
        json={"status": "delivered"},
    )
    assert result.status_code == 200, result.text
    delivered = ui_api.get(f"/v1/mail/messages/{message_uuid}")
    assert delivered.json()["delivery"]["status"] == "delivered"

    inbound_update = provider_api.put(
        f"/v1/providers/{provider_uuid}/mail/messages/{message_uuid}",
        json={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "INBOX:42",
            "folder_urn": f"urn:mail-folder:{folder_uuid}",
            "from_address": "sender@example.com",
            "to_addresses": ["cassi@example.com"],
            "subject": "Confirmed remotely",
            "body_text": "Inbound message",
        },
    )
    assert inbound_update.status_code == 200, inbound_update.text
    all_commands = provider_api.get(
        f"/v1/providers/{provider_uuid}/mail/commands/",
    )
    assert len(all_commands.json()) == 1

    events = ui_api.get("/v1/events/").json()
    assert events[-1]["payload"]["provider"]["uuid"] == str(provider_uuid)
    assert events[-1]["payload"]["delivery"]["status"] == "delivered"


def test_provider_namespace_prevents_cross_provider_read(api, provider_api):
    owner_uuid = sys_uuid.uuid4()
    other_uuid = sys_uuid.uuid4()
    _register_mail_provider(provider_api, owner_uuid, "Owner")
    _register_mail_provider(provider_api, other_uuid, "Other")
    account = _create_mail_account(api, owner_uuid)
    response = provider_api.get(
        f"/v1/providers/{other_uuid}/external_accounts/{account['uuid']}",
    )
    assert response.status_code == 404


def test_calendar_provider_vertical_slice(api, workspace_api, provider_api):
    ui_api = _bind_workspace_api(workspace_api, api)
    provider_uuid = sys_uuid.uuid4()
    _register_calendar_provider(provider_api, provider_uuid)
    account = _create_calendar_account(api, provider_uuid)

    calendar_uuid = sys_uuid.uuid4()
    calendar = provider_api.put(
        f"/v1/providers/{provider_uuid}/calendar/calendars/{calendar_uuid}",
        json={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "primary",
            "name": "Primary",
            "color": "#3366ff",
        },
    )
    matching_calendars = provider_api.get(
        f"/v1/providers/{provider_uuid}/calendar/calendars/",
        params={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "primary",
        },
    )
    assert matching_calendars.status_code == 200, matching_calendars.text
    assert [item["uuid"] for item in matching_calendars.json()] == [
        str(calendar_uuid),
    ]
    assert calendar.status_code == 200, calendar.text
    assert calendar.json()["urn"] == f"urn:calendar:{calendar_uuid}"
    assert "ctag" not in calendar.json()
    assert "sync_token" not in calendar.json()
    assert "source" not in calendar.json()

    event_uuid = sys_uuid.uuid4()
    event = provider_api.put(
        f"/v1/providers/{provider_uuid}/calendar/events/{event_uuid}",
        json={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "remote-event-42",
            "calendar_urn": f"urn:calendar:{calendar_uuid}",
            "uid": "remote-event-42@example.com",
            "summary": "Inbound calendar event",
            "starts_at": "2026-07-16T12:00:00Z",
            "ends_at": "2026-07-16T13:00:00Z",
            "attendees": [{"email": "reviewer@example.com"}],
        },
    )
    assert event.status_code == 200, event.text
    assert event.json()["urn"] == f"urn:calendar-event:{event_uuid}"
    assert event.json()["calendar_urn"] == f"urn:calendar:{calendar_uuid}"
    assert "ics" not in event.json()
    assert "etag" not in event.json()
    assert "source" not in event.json()

    calendars = provider_api.get(
        f"/v1/providers/{provider_uuid}/calendar/calendars/",
    )
    assert calendars.status_code == 200, calendars.text
    assert [item["uuid"] for item in calendars.json()] == [str(calendar_uuid)]
    provider_events = provider_api.get(
        f"/v1/providers/{provider_uuid}/calendar/events/",
    )
    assert provider_events.status_code == 200, provider_events.text
    assert [item["uuid"] for item in provider_events.json()] == [str(event_uuid)]

    assert (
        provider_api.get(
            f"/v1/providers/{provider_uuid}/calendar/commands/",
        ).json()
        == []
    )

    ui_event = ui_api.get(f"/v1/calendar/events/{event_uuid}")
    assert ui_event.status_code == 200, ui_event.text
    assert ui_event.json()["provider"] == {
        "uuid": str(provider_uuid),
        "name": "Calendar provider",
        "kind": "calendar",
    }
    assert ui_event.json()["delivery"]["status"] == "delivered"
    for field in ("provider_external_id", "ics", "etag", "source", "sync_status"):
        assert field not in ui_event.json()

    updated = ui_api.put(
        f"/v1/calendar/events/{event_uuid}",
        json={"summary": "Changed in Workspace"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["delivery"]["status"] == "pending"

    commands = provider_api.get(
        f"/v1/providers/{provider_uuid}/calendar/commands/",
        params={"status": "pending"},
    )
    assert commands.status_code == 200, commands.text
    assert len(commands.json()) == 1
    command = commands.json()[0]
    assert command["domain"] == "calendar"
    assert command["operation"] == "event.update"
    assert command["entity_urn"] == f"urn:calendar-event:{event_uuid}"
    assert command["payload"]["calendar_urn"] == f"urn:calendar:{calendar_uuid}"
    assert "calendar_uuid" not in command["payload"]
    for field in ("ics", "etag", "source", "sync_status"):
        assert field not in command["payload"]

    result = provider_api.post(
        f"/v1/providers/{provider_uuid}/calendar/commands/"
        f"{command['uuid']}/actions/result/invoke",
        json={"status": "delivered", "provider_external_id": "remote-event-42"},
    )
    assert result.status_code == 200, result.text
    delivered = ui_api.get(f"/v1/calendar/events/{event_uuid}")
    assert delivered.json()["delivery"]["status"] == "delivered"

    calendar_update = ui_api.put(
        f"/v1/calendar/calendars/{calendar_uuid}",
        json={"name": "Primary calendar"},
    )
    assert calendar_update.status_code == 200, calendar_update.text
    assert calendar_update.json()["delivery"]["status"] == "pending"

    moved = ui_api.post(
        f"/v1/calendar/events/{event_uuid}/actions/move/invoke",
        json={"calendar_uuid": str(calendar_uuid)},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["delivery"]["status"] == "pending"

    deleted = ui_api.delete(f"/v1/calendar/events/{event_uuid}")
    assert deleted.status_code in (200, 204), deleted.text

    local_calendar = ui_api.post(
        "/v1/calendar/calendars/",
        json={
            "external_user_uuid": account["uuid"],
            "name": "Created in Workspace",
        },
    )
    assert local_calendar.status_code in (200, 201), local_calendar.text
    assert local_calendar.json()["delivery"]["status"] == "pending"

    all_commands = provider_api.get(
        f"/v1/providers/{provider_uuid}/calendar/commands/",
    )
    assert all_commands.status_code == 200, all_commands.text
    assert [item["operation"] for item in all_commands.json()] == [
        "event.update",
        "calendar.update",
        "event.move",
        "event.delete",
        "calendar.create",
    ]

    inbound_delete = provider_api.delete(
        f"/v1/providers/{provider_uuid}/calendar/calendars/"
        f"{local_calendar.json()['uuid']}",
    )
    assert inbound_delete.status_code in (200, 204), inbound_delete.text
    assert (
        len(
            provider_api.get(
                f"/v1/providers/{provider_uuid}/calendar/commands/",
            ).json(),
        )
        == 5
    )

    events = ui_api.get("/v1/events/").json()
    assert events[-1]["payload"]["uuid"] == local_calendar.json()["uuid"]
    assert events[-1]["payload"]["delivery"]["status"] == "delivered"
    assert events[-1]["payload"]["provider"]["uuid"] == str(provider_uuid)


def test_messenger_provider_vertical_slice(api, workspace_api, provider_api, db):
    ui_api = _bind_workspace_api(workspace_api, api)
    provider_uuid = sys_uuid.uuid4()
    _register_messenger_provider(provider_api, provider_uuid)
    account = _create_messenger_account(api, provider_uuid)
    account_status = provider_api.post(
        f"/v1/providers/{provider_uuid}/external_accounts/"
        f"{account['uuid']}/actions/status/invoke",
        json={"status": "confirmed"},
    )
    assert account_status.status_code == 200, account_status.text

    author_uuid = sys_uuid.uuid4()
    author = provider_api.put(
        f"/v1/providers/{provider_uuid}/messenger/users/{author_uuid}",
        json={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "user:42",
            "username": "Remote Author",
            "email": "author@example.com",
        },
    )
    assert author.status_code == 200, author.text
    assert author.json()["urn"] == f"urn:messenger-user:{author_uuid}"

    stream_uuid = sys_uuid.uuid4()
    stream = provider_api.put(
        f"/v1/providers/{provider_uuid}/messenger/streams/{stream_uuid}",
        json={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "7",
            "owner_urn": f"urn:messenger-user:{author_uuid}",
            "name": "Provider stream",
            "description": "Delivered by a provider",
        },
    )
    assert stream.status_code == 200, stream.text
    assert stream.json()["urn"] == f"urn:messenger-stream:{stream_uuid}"
    stream_event = next(
        item
        for item in reversed(ui_api.get("/v1/events/").json())
        if item["payload"]["kind"] == "stream.created"
        and item["payload"]["uuid"] == str(stream_uuid)
    )
    assert stream_event["payload"]["provider"]["uuid"] == str(provider_uuid)
    assert stream_event["payload"]["delivery"]["status"] == "delivered"

    topic_uuid = sys_uuid.uuid4()
    topic = provider_api.put(
        f"/v1/providers/{provider_uuid}/messenger/topics/{topic_uuid}",
        json={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "topic:general",
            "stream_urn": f"urn:messenger-stream:{stream_uuid}",
            "name": "General",
        },
    )
    assert topic.status_code == 200, topic.text
    assert topic.json()["stream_urn"] == f"urn:messenger-stream:{stream_uuid}"
    topic_event = next(
        item
        for item in reversed(ui_api.get("/v1/events/").json())
        if item["payload"]["kind"] == "topic.created"
        and item["payload"]["uuid"] == str(topic_uuid)
    )
    assert topic_event["payload"]["provider"]["uuid"] == str(provider_uuid)
    assert topic_event["payload"]["delivery"]["status"] == "delivered"

    message_uuid = sys_uuid.uuid4()
    message_payload = {
        "external_account_uuid": account["uuid"],
        "provider_external_id": "100",
        "stream_urn": f"urn:messenger-stream:{stream_uuid}",
        "topic_urn": f"urn:messenger-topic:{topic_uuid}",
        "author_urn": f"urn:messenger-user:{author_uuid}",
        "payload": {"kind": "markdown", "content": "Inbound message"},
        "created_at": "2026-07-14T23:45:54.000000Z",
    }
    message = provider_api.put(
        f"/v1/providers/{provider_uuid}/messenger/messages/{message_uuid}",
        json=message_payload,
    )
    assert message.status_code == 200, message.text
    assert message.json()["urn"] == f"urn:messenger-message:{message_uuid}"
    assert message.json()["created_at"] == "2026-07-14T23:45:54.000000Z"

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT updated_at FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        initial_updated_at = cursor.fetchone()[0]
    initial_ui_message = api.get(f"/v1/messages/{message_uuid}")
    assert initial_ui_message.status_code == 200, initial_ui_message.text
    event_count = len(ui_api.get("/v1/events/").json())
    repeated = provider_api.put(
        f"/v1/providers/{provider_uuid}/messenger/messages/{message_uuid}",
        json=message_payload,
    )
    assert repeated.status_code == 200, repeated.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT updated_at FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        repeated_updated_at = cursor.fetchone()[0]
    repeated_ui_message = api.get(f"/v1/messages/{message_uuid}")
    assert repeated_updated_at == initial_updated_at
    assert (
        repeated_ui_message.json()["delivery"]["updated_at"]
        == (initial_ui_message.json()["delivery"]["updated_at"])
    )
    assert len(ui_api.get("/v1/events/").json()) == event_count

    changed_message_payload = {
        **message_payload,
        "payload": {"kind": "markdown", "content": "Inbound message changed"},
    }
    changed = provider_api.put(
        f"/v1/providers/{provider_uuid}/messenger/messages/{message_uuid}",
        json=changed_message_payload,
    )
    assert changed.status_code == 200, changed.text
    assert len(ui_api.get("/v1/events/").json()) > event_count

    reaction_uuid = sys_uuid.uuid4()
    reaction = provider_api.put(
        f"/v1/providers/{provider_uuid}/messenger/reactions/{reaction_uuid}",
        json={
            "external_account_uuid": account["uuid"],
            "provider_external_id": "5",
            "message_urn": f"urn:messenger-message:{message_uuid}",
            "author_urn": f"urn:messenger-user:{author_uuid}",
            "emoji_name": "thumbs_up",
        },
    )
    assert reaction.status_code == 200, reaction.text
    assert reaction.json()["message_urn"] == (f"urn:messenger-message:{message_uuid}")

    commands = provider_api.get(
        f"/v1/providers/{provider_uuid}/messenger/commands/",
    )
    assert commands.status_code == 200, commands.text
    assert commands.json() == []

    ui_message = api.get(f"/v1/messages/{message_uuid}")
    assert ui_message.status_code == 200, ui_message.text
    assert ui_message.json()["provider"] == {
        "uuid": str(provider_uuid),
        "name": "Messenger",
        "kind": "zulip",
    }
    assert ui_message.json()["delivery"]["status"] == "delivered"

    outbound = api.post(
        "/v1/messages/",
        json={
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(topic_uuid),
            "payload": {"kind": "markdown", "content": "UI outbound"},
        },
    )
    assert outbound.status_code in (200, 201), outbound.text
    outbound_uuid = outbound.json()["uuid"]
    assert outbound.json()["delivery"]["status"] == "pending"

    commands = provider_api.get(
        f"/v1/providers/{provider_uuid}/messenger/commands/",
    )
    assert commands.status_code == 200, commands.text
    assert len(commands.json()) == 1
    command = commands.json()[0]
    assert command["operation"] == "message.create"
    assert command["entity_urn"] == f"urn:messenger-message:{outbound_uuid}"
    assert command["payload"]["topic_urn"] == f"urn:messenger-topic:{topic_uuid}"

    result = provider_api.post(
        f"/v1/providers/{provider_uuid}/messenger/commands/"
        f"{command['uuid']}/actions/result/invoke",
        json={"status": "delivered"},
    )
    assert result.status_code == 200, result.text
    assert (
        api.get(
            f"/v1/messages/{outbound_uuid}",
        ).json()["delivery"]["status"]
        == "delivered"
    )

    other_provider_uuid = sys_uuid.uuid4()
    _register_messenger_provider(provider_api, other_provider_uuid, "Other")
    denied = provider_api.get(
        f"/v1/providers/{other_provider_uuid}/messenger/messages/{outbound_uuid}",
    )
    assert denied.status_code == 404

    events = ui_api.get("/v1/events/").json()
    assert events[-1]["payload"]["uuid"] == outbound_uuid
    assert events[-1]["payload"]["provider"]["uuid"] == str(provider_uuid)
    assert events[-1]["payload"]["delivery"]["status"] == "delivered"

    updated_message = api.put(
        f"/v1/messages/{outbound_uuid}",
        json={"payload": {"kind": "markdown", "content": "UI edited"}},
    )
    assert updated_message.status_code == 200, updated_message.text
    assert updated_message.json()["delivery"]["status"] == "pending"

    local_reaction = api.post(
        "/v1/message_reactions/",
        json={"message_uuid": outbound_uuid, "emoji_name": "eyes"},
    )
    assert local_reaction.status_code in (200, 201), local_reaction.text
    reaction_uuid = local_reaction.json()["uuid"]
    assert local_reaction.json()["provider"]["uuid"] == str(provider_uuid)
    assert local_reaction.json()["delivery"]["status"] == "pending"
    reaction_event = next(
        item
        for item in reversed(ui_api.get("/v1/events/").json())
        if item["payload"]["kind"] == "message_reaction.created"
        and item["payload"]["uuid"] == reaction_uuid
    )
    assert reaction_event["payload"]["provider"]["uuid"] == str(provider_uuid)
    assert reaction_event["payload"]["delivery"]["status"] == "pending"

    updated_reaction = api.put(
        f"/v1/message_reactions/{reaction_uuid}",
        json={"message_uuid": outbound_uuid, "emoji_name": "thumbs_up"},
    )
    assert updated_reaction.status_code == 200, updated_reaction.text
    assert updated_reaction.json()["delivery"]["status"] == "pending"

    deleted_reaction = api.delete(f"/v1/message_reactions/{reaction_uuid}")
    assert deleted_reaction.status_code in (200, 204), deleted_reaction.text

    updated_stream = api.put(
        f"/v1/streams/{stream_uuid}",
        json={
            "name": "Provider stream renamed",
            "description": "Updated in Workspace",
        },
    )
    assert updated_stream.status_code == 200, updated_stream.text
    assert updated_stream.json()["delivery"]["status"] == "pending"

    stream_notifications = api.post(
        f"/v1/streams/{stream_uuid}/actions/notifications/invoke",
        json={"notification_mode": "mentions_only"},
    )
    assert stream_notifications.status_code == 200, stream_notifications.text
    assert stream_notifications.json()["notification_mode"] == "mentions_only"

    updated_topic = api.put(
        f"/v1/stream_topics/{topic_uuid}",
        json={"name": "Provider topic renamed"},
    )
    assert updated_topic.status_code == 200, updated_topic.text
    assert updated_topic.json()["delivery"]["status"] == "pending"

    topic_notifications = api.post(
        f"/v1/stream_topics/{topic_uuid}/actions/notifications/invoke",
        json={"notification_mode": "follow"},
    )
    assert topic_notifications.status_code == 200, topic_notifications.text
    assert topic_notifications.json()["notification_mode"] == "follow"

    deleted_message = api.delete(f"/v1/messages/{outbound_uuid}")
    assert deleted_message.status_code in (200, 204), deleted_message.text

    commands = provider_api.get(
        f"/v1/providers/{provider_uuid}/messenger/commands/",
    )
    assert commands.status_code == 200, commands.text
    command_payloads = commands.json()
    assert [item["operation"] for item in command_payloads] == [
        "message.create",
        "message.update",
        "reaction.create",
        "reaction.update",
        "reaction.delete",
        "stream.update",
        "topic.update",
        "message.delete",
    ]
    assert command_payloads[1]["payload"]["payload"] == {
        "kind": "markdown",
        "content": "UI edited",
    }
    assert command_payloads[3]["payload"]["emoji_name"] == "thumbs_up"
    assert command_payloads[5]["payload"]["name"] == "Provider stream renamed"
    assert command_payloads[6]["payload"]["name"] == "Provider topic renamed"

    native_stream = api.post(
        "/v1/streams/",
        json={
            "name": "Native stream",
            "description": "Local only",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert native_stream.status_code in (200, 201), native_stream.text
    native_stream_uuid = native_stream.json()["uuid"]
    native_topic_uuid = native_stream.json()["default_topic_uuid"]
    assert native_stream.json()["provider"] is None
    assert native_stream.json()["delivery"] is None

    native_message = api.post(
        "/v1/messages/",
        json={
            "stream_uuid": native_stream_uuid,
            "topic_uuid": native_topic_uuid,
            "payload": {"kind": "markdown", "content": "Native message"},
        },
    )
    assert native_message.status_code in (200, 201), native_message.text
    native_message_uuid = native_message.json()["uuid"]
    assert native_message.json()["provider"] is None
    assert native_message.json()["delivery"] is None

    native_message_update = api.put(
        f"/v1/messages/{native_message_uuid}",
        json={"payload": {"kind": "markdown", "content": "Native edited"}},
    )
    assert native_message_update.status_code == 200, native_message_update.text

    native_reaction = api.post(
        "/v1/message_reactions/",
        json={"message_uuid": native_message_uuid, "emoji_name": "eyes"},
    )
    assert native_reaction.status_code in (200, 201), native_reaction.text
    native_reaction_uuid = native_reaction.json()["uuid"]
    assert native_reaction.json()["provider"] is None
    assert native_reaction.json()["delivery"] is None

    native_reaction_update = api.put(
        f"/v1/message_reactions/{native_reaction_uuid}",
        json={"message_uuid": native_message_uuid, "emoji_name": "thumbs_up"},
    )
    assert native_reaction_update.status_code == 200, native_reaction_update.text
    native_reaction_delete = api.delete(
        f"/v1/message_reactions/{native_reaction_uuid}",
    )
    assert native_reaction_delete.status_code in (200, 204)

    native_stream_update = api.put(
        f"/v1/streams/{native_stream_uuid}",
        json={"name": "Native renamed", "description": "Still local"},
    )
    assert native_stream_update.status_code == 200, native_stream_update.text
    native_topic_update = api.put(
        f"/v1/stream_topics/{native_topic_uuid}",
        json={"name": "Native topic renamed"},
    )
    assert native_topic_update.status_code == 200, native_topic_update.text
    native_message_delete = api.delete(f"/v1/messages/{native_message_uuid}")
    assert native_message_delete.status_code in (200, 204)

    commands_after_native_mutations = provider_api.get(
        f"/v1/providers/{provider_uuid}/messenger/commands/",
    )
    assert commands_after_native_mutations.status_code == 200
    assert commands_after_native_mutations.json() == command_payloads

    for deleted_command in (command_payloads[4], command_payloads[7]):
        terminal_result = provider_api.post(
            f"/v1/providers/{provider_uuid}/messenger/commands/"
            f"{deleted_command['uuid']}/actions/result/invoke",
            json={"status": "delivered"},
        )
        assert terminal_result.status_code == 200, terminal_result.text

    terminal_events = ui_api.get("/v1/events/").json()
    assert terminal_events[-1]["payload"]["uuid"] == outbound_uuid
    assert terminal_events[-1]["payload"]["delivery"]["status"] == "delivered"


def test_messenger_provider_shares_realm_entities_across_external_accounts(
    api,
    provider_api,
    db,
):
    provider_uuid = sys_uuid.uuid4()
    _register_messenger_provider(provider_api, provider_uuid)
    project_id = api.project_id
    second_user_uuid = sys_uuid.uuid4()

    first_account = _create_messenger_account(api, provider_uuid)
    second_response = api.post(
        "/v1/external_accounts/",
        user=second_user_uuid,
        project=project_id,
        json={
            "provider_uuid": str(provider_uuid),
            "server_url": "https://chat.example.com",
            "account_settings": {
                "kind": "zulip",
                "credentials": {
                    "kind": "zulip",
                    "login": "second@example.com",
                    "token": "second-provider-token",
                },
            },
        },
    )
    assert second_response.status_code in (200, 201), second_response.text
    second_account = second_response.json()
    for account in (first_account, second_account):
        confirmed = provider_api.post(
            f"/v1/providers/{provider_uuid}/external_accounts/"
            f"{account['uuid']}/actions/status/invoke",
            json={"status": "confirmed"},
        )
        assert confirmed.status_code == 200, confirmed.text

    author_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    for account in (first_account, second_account):
        user = provider_api.put(
            f"/v1/providers/{provider_uuid}/messenger/users/{author_uuid}",
            json={
                "external_account_uuid": account["uuid"],
                "provider_external_id": "42",
                "username": "shared-author@example.com",
                "email": "shared-author@example.com",
            },
        )
        assert user.status_code == 200, user.text
        stream = provider_api.put(
            f"/v1/providers/{provider_uuid}/messenger/streams/{stream_uuid}",
            json={
                "external_account_uuid": account["uuid"],
                "provider_external_id": "7",
                "owner_urn": f"urn:messenger-user:{author_uuid}",
                "name": "Shared stream",
                "description": "One realm entity",
            },
        )
        assert stream.status_code == 200, stream.text
        topic = provider_api.put(
            f"/v1/providers/{provider_uuid}/messenger/topics/{topic_uuid}",
            json={
                "external_account_uuid": account["uuid"],
                "provider_external_id": "7:general",
                "stream_urn": f"urn:messenger-stream:{stream_uuid}",
                "name": "General",
            },
        )
        assert topic.status_code == 200, topic.text
        message = provider_api.put(
            f"/v1/providers/{provider_uuid}/messenger/messages/{message_uuid}",
            json={
                "external_account_uuid": account["uuid"],
                "provider_external_id": "100",
                "stream_urn": f"urn:messenger-stream:{stream_uuid}",
                "topic_urn": f"urn:messenger-topic:{topic_uuid}",
                "author_urn": f"urn:messenger-user:{author_uuid}",
                "payload": {"kind": "markdown", "content": "Shared message"},
                "created_at": "2026-07-15T12:00:00.000000Z",
            },
        )
        assert message.status_code == 200, message.text

    flags = provider_api.post(
        f"/v1/providers/{provider_uuid}/messenger/messages/{message_uuid}"
        "/actions/flags/invoke",
        json={
            "external_account_uuid": first_account["uuid"],
            "read": True,
            "starred": True,
        },
    )
    assert flags.status_code == 200, flags.text

    for user_uuid, expected_flags in (
        (api.user_uuid, (True, True)),
        (second_user_uuid, (False, False)),
    ):
        visible = api.get(
            f"/v1/messages/{message_uuid}",
            user=user_uuid,
            project=project_id,
        )
        assert visible.status_code == 200, visible.text
        assert visible.json()["uuid"] == str(message_uuid)
        assert (visible.json()["read"], visible.json()["starred"]) == expected_flags

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_stream_bindings "
            "WHERE project_id = %s AND stream_uuid = %s "
            "AND user_uuid IN (%s, %s)",
            (project_id, stream_uuid, api.user_uuid, second_user_uuid),
        )
        assert cursor.fetchone()[0] == 2


def test_groupware_provider_upserts_are_idempotent(
    api, workspace_api, provider_api, db
):
    ui_api = _bind_workspace_api(workspace_api, api)
    provider_uuid = sys_uuid.uuid4()
    registered = provider_api.put(
        f"/v1/providers/{provider_uuid}",
        json={
            "name": "Groupware provider",
            "supported_kinds": ["mail", "calendar"],
            "version": "1.0.0",
        },
    )
    assert registered.status_code == 200, registered.text
    mail_account = _create_mail_account(api, provider_uuid)
    calendar_account = _create_calendar_account(api, provider_uuid)

    folder_uuid = sys_uuid.uuid4()
    folder_payload = {
        "external_account_uuid": mail_account["uuid"],
        "provider_external_id": "INBOX",
        "path": "INBOX",
        "name": "Inbox",
        "special_use": "inbox",
    }
    message_uuid = sys_uuid.uuid4()
    message_payload = {
        "external_account_uuid": mail_account["uuid"],
        "provider_external_id": "INBOX:10",
        "folder_urn": f"urn:mail-folder:{folder_uuid}",
        "from_address": "sender@example.com",
        "to_addresses": ["cassi@example.com"],
        "subject": "Stable mail",
        "body_text": "No changes",
    }
    calendar_uuid = sys_uuid.uuid4()
    calendar_payload = {
        "external_account_uuid": calendar_account["uuid"],
        "provider_external_id": "primary",
        "name": "Primary",
        "color": "#3366ff",
    }
    event_uuid = sys_uuid.uuid4()
    event_payload = {
        "external_account_uuid": calendar_account["uuid"],
        "provider_external_id": "event:10",
        "calendar_urn": f"urn:calendar:{calendar_uuid}",
        "uid": "event-10@example.com",
        "summary": "Stable event",
        "starts_at": "2026-07-17T12:00:00Z",
        "ends_at": "2026-07-17T13:00:00Z",
    }
    resources = (
        (
            f"/v1/providers/{provider_uuid}/mail/folders/{folder_uuid}",
            folder_payload,
            f"/v1/mail/folders/{folder_uuid}",
            "SELECT updated_at FROM m_mail_folders WHERE uuid = %s",
        ),
        (
            f"/v1/providers/{provider_uuid}/mail/messages/{message_uuid}",
            message_payload,
            f"/v1/mail/messages/{message_uuid}",
            "SELECT updated_at FROM m_mail_messages WHERE uuid = %s",
        ),
        (
            f"/v1/providers/{provider_uuid}/calendar/calendars/{calendar_uuid}",
            calendar_payload,
            f"/v1/calendar/calendars/{calendar_uuid}",
            "SELECT updated_at FROM m_calendars WHERE uuid = %s",
        ),
        (
            f"/v1/providers/{provider_uuid}/calendar/events/{event_uuid}",
            event_payload,
            f"/v1/calendar/events/{event_uuid}",
            "SELECT updated_at FROM m_calendar_events WHERE uuid = %s",
        ),
    )

    for service_path, payload, _ui_path, _updated_at_query in resources:
        created = provider_api.put(service_path, json=payload)
        assert created.status_code == 200, created.text
    persisted_event = ui_api.get(f"/v1/calendar/events/{event_uuid}")
    assert persisted_event.status_code == 200, persisted_event.text
    assert persisted_event.json()["starts_at"] == "2026-07-17T12:00:00.000000Z"
    assert persisted_event.json()["ends_at"] == "2026-07-17T13:00:00.000000Z"

    event_count = len(ui_api.get("/v1/events/").json())
    for service_path, payload, ui_path, updated_at_query in resources:
        before_ui = ui_api.get(ui_path)
        assert before_ui.status_code == 200, before_ui.text
        with db.cursor() as cursor:
            cursor.execute(updated_at_query, (before_ui.json()["uuid"],))
            before_updated_at = cursor.fetchone()[0]
        repeated = provider_api.put(service_path, json=payload)
        assert repeated.status_code == 200, repeated.text
        with db.cursor() as cursor:
            cursor.execute(updated_at_query, (repeated.json()["uuid"],))
            after_updated_at = cursor.fetchone()[0]
        after_ui = ui_api.get(ui_path)
        assert (
            after_ui.json()["delivery"]["updated_at"]
            == (before_ui.json()["delivery"]["updated_at"])
        )
        assert len(ui_api.get("/v1/events/").json()) == event_count
        assert after_updated_at == before_updated_at
    assert len(ui_api.get("/v1/events/").json()) == event_count

    changed_payloads = (
        {**folder_payload, "name": "Inbox changed"},
        {**message_payload, "subject": "Changed mail"},
        {**calendar_payload, "name": "Primary changed"},
        {**event_payload, "summary": "Changed event"},
    )
    for index, (
        (service_path, _payload, ui_path, updated_at_query),
        changed,
    ) in enumerate(
        zip(resources, changed_payloads, strict=True),
        start=1,
    ):
        before_ui = ui_api.get(ui_path)
        with db.cursor() as cursor:
            cursor.execute(updated_at_query, (before_ui.json()["uuid"],))
            before_updated_at = cursor.fetchone()[0]
        response = provider_api.put(service_path, json=changed)
        assert response.status_code == 200, response.text
        with db.cursor() as cursor:
            cursor.execute(updated_at_query, (response.json()["uuid"],))
            after_updated_at = cursor.fetchone()[0]
        after_ui = ui_api.get(ui_path)
        assert after_updated_at != before_updated_at
        assert (
            after_ui.json()["delivery"]["updated_at"]
            != (before_ui.json()["delivery"]["updated_at"])
        )
        assert len(ui_api.get("/v1/events/").json()) == event_count + index
