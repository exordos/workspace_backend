# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import hashlib
import io
import uuid as sys_uuid

from workspace.messenger_api import file_storage


def _register_mail_provider(provider_api, provider_uuid, name):
    response = provider_api.put(
        f"/v1/providers/{provider_uuid}",
        json={
            "name": name,
            "supported_kinds": ["mail"],
            "version": "1.0.0",
        },
    )
    assert response.status_code == 200, response.text


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


def test_provider_blob_upload_is_scoped_and_visible_to_account_owner(
    api,
    workspace_api,
    provider_api,
    db,
    tmp_path,
    monkeypatch,
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    provider_uuid = sys_uuid.uuid4()
    other_provider_uuid = sys_uuid.uuid4()
    _register_mail_provider(provider_api, provider_uuid, "Owner provider")
    _register_mail_provider(provider_api, other_provider_uuid, "Other provider")
    account = _create_mail_account(api, provider_uuid)
    data = b"provider attachment contents"
    digest = hashlib.sha256(data).hexdigest()

    uploaded = provider_api.post(
        f"/v1/providers/{provider_uuid}/blobs/",
        data={
            "external_account_uuid": account["uuid"],
            "name": "report.txt",
            "hash": digest,
        },
        files={"file": ("report.txt", io.BytesIO(data), "text/plain")},
    )
    assert uploaded.status_code in (200, 201), uploaded.text
    payload = uploaded.json()
    assert payload["urn"] == f"urn:file:{payload['uuid']}"
    assert payload["name"] == "report.txt"
    assert payload["content_type"] == "text/plain"
    assert payload["size_bytes"] == len(data)
    assert payload["hash"] == digest
    assert "storage_object_id" not in payload
    assert "external_account_uuid" not in payload

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
    message_uuid = sys_uuid.uuid4()
    message_payload = {
        "external_account_uuid": account["uuid"],
        "provider_external_id": "INBOX:1",
        "folder_urn": f"urn:mail-folder:{folder_uuid}",
        "from_address": "sender@example.com",
        "to_addresses": ["cassi@example.com"],
        "subject": "Provider attachment",
        "body_text": "The attachment is a Workspace URN.",
        "attachments": [
            {
                "urn": payload["urn"],
                "name": payload["name"],
                "content_type": payload["content_type"],
                "content_id": "report-content-id",
                "size_bytes": payload["size_bytes"],
                "hash": payload["hash"],
            },
        ],
    }
    message = provider_api.put(
        f"/v1/providers/{provider_uuid}/mail/messages/{message_uuid}",
        json=message_payload,
    )
    assert message.status_code == 200, message.text
    assert message.json()["attachments"] == [
        {
            "urn": payload["urn"],
            "name": "report.txt",
            "content_type": "text/plain",
            "content_id": "report-content-id",
            "size_bytes": len(data),
            "hash": digest,
        },
    ]

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT updated_at FROM m_mail_messages WHERE uuid = %s",
            (message_uuid,),
        )
        initial_updated_at = cursor.fetchone()[0]
    initial_ui_message = workspace_api.get(f"/v1/mail/messages/{message_uuid}")
    assert initial_ui_message.status_code == 200, initial_ui_message.text
    event_count = len(workspace_api.get("/v1/events/").json())
    repeated = provider_api.put(
        f"/v1/providers/{provider_uuid}/mail/messages/{message_uuid}",
        json=message_payload,
    )
    assert repeated.status_code == 200, repeated.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT updated_at FROM m_mail_messages WHERE uuid = %s",
            (message_uuid,),
        )
        repeated_updated_at = cursor.fetchone()[0]
    repeated_ui_message = workspace_api.get(f"/v1/mail/messages/{message_uuid}")
    assert repeated_updated_at == initial_updated_at
    assert (
        repeated_ui_message.json()["delivery"]["updated_at"]
        == (initial_ui_message.json()["delivery"]["updated_at"])
    )
    assert len(workspace_api.get("/v1/events/").json()) == event_count

    changed_attachment_payload = {
        **message_payload,
        "attachments": [
            {
                **message_payload["attachments"][0],
                "content_id": "changed-content-id",
            },
        ],
    }
    changed = provider_api.put(
        f"/v1/providers/{provider_uuid}/mail/messages/{message_uuid}",
        json=changed_attachment_payload,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["attachments"][0]["content_id"] == "changed-content-id"
    assert len(workspace_api.get("/v1/events/").json()) == event_count + 1

    ui_attachments = workspace_api.get(
        "/v1/mail/attachments/",
        params={"message_uuid": str(message_uuid)},
    )
    assert ui_attachments.status_code == 200, ui_attachments.text
    assert [item["uuid"] for item in ui_attachments.json()] == [payload["uuid"]]

    ui_data = b"attachment uploaded by the UI"
    ui_uploaded = workspace_api.post(
        "/v1/mail/attachments/",
        data={"message_uuid": str(message_uuid)},
        files={"file": ("ui.txt", io.BytesIO(ui_data), "text/plain")},
    )
    assert ui_uploaded.status_code in (200, 201), ui_uploaded.text
    ui_blob_uuid = ui_uploaded.json()["uuid"]
    provider_ui_download = provider_api.get(
        f"/v1/providers/{provider_uuid}/blobs/{ui_blob_uuid}/actions/download",
    )
    assert provider_ui_download.status_code == 200, provider_ui_download.text
    assert provider_ui_download.content == ui_data
    commands = provider_api.get(
        f"/v1/providers/{provider_uuid}/mail/commands/",
    )
    assert commands.status_code == 200, commands.text
    attachment_command = commands.json()[-1]
    assert attachment_command["operation"] == "message.update"
    assert {item["urn"] for item in attachment_command["payload"]["attachments"]} == {
        payload["urn"],
        f"urn:file:{ui_blob_uuid}",
    }

    provider_download = provider_api.get(
        f"/v1/providers/{provider_uuid}/blobs/{payload['uuid']}/actions/download",
    )
    assert provider_download.status_code == 200, provider_download.text
    assert provider_download.content == data

    ui_download = api.get(
        f"/v1/files/{payload['uuid']}/actions/download",
    )
    assert ui_download.status_code == 200, ui_download.text
    assert ui_download.content == data

    cross_provider = provider_api.get(
        f"/v1/providers/{other_provider_uuid}/blobs/{payload['uuid']}",
    )
    assert cross_provider.status_code == 404


def test_provider_blob_rejects_wrong_hash(
    api,
    provider_api,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    provider_uuid = sys_uuid.uuid4()
    _register_mail_provider(provider_api, provider_uuid, "Mail provider")
    account = _create_mail_account(api, provider_uuid)

    response = provider_api.post(
        f"/v1/providers/{provider_uuid}/blobs/",
        data={
            "external_account_uuid": account["uuid"],
            "hash": "0" * 64,
        },
        files={"file": ("report.txt", io.BytesIO(b"actual"), "text/plain")},
    )
    assert response.status_code == 400
