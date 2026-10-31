# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import io
import uuid as sys_uuid

from workspace.messenger_api import file_storage
from workspace.tests.integration import conftest


MAIL_FOLDERS = "/v1/mail/folders/"
MAIL_MESSAGES = "/v1/mail/messages/"
FILES = "/v1/messenger/files/"
EVENTS = "/v1/events/"
EXTERNAL_ACCOUNTS = "/v1/messenger/external_accounts/"
EXTERNAL_ACCOUNT_PERMISSIONS = tuple(
    f"workspace.external_account.{action}"
    for action in ("read", "create", "update", "delete")
)


def _folder(api, **values):
    response = api.post(
        MAIL_FOLDERS,
        json={"path": "Drafts", "name": "Drafts", **values},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _message(api, folder_uuid, **values):
    response = api.post(
        MAIL_MESSAGES,
        json={
            "folder_uuid": folder_uuid,
            "to_addresses": ["recipient@example.test"],
            "subject": "Workspace mail",
            "body_text": "Stored before transport.",
            **values,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_mail_draft_roundtrip_is_project_and_owner_scoped(workspace_api):
    draft_folder = _folder(workspace_api)
    archive_folder = _folder(workspace_api, path="Archive", name="Archive")
    message = _message(workspace_api, draft_folder["uuid"])

    assert message["draft"] is True
    assert message["delivery_status"] == "draft"
    assert message.get("external_account_uuid") is None
    assert message["source_name"] == "native"

    updated = workspace_api.put(
        MAIL_MESSAGES + message["uuid"],
        json={"subject": "Updated subject", "flagged": True},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["subject"] == "Updated subject"
    assert updated.json()["flagged"] is True

    moved = workspace_api.post(
        MAIL_MESSAGES + message["uuid"] + "/actions/move/invoke",
        json={"folder_uuid": archive_folder["uuid"]},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder_uuid"] == archive_folder["uuid"]

    other_user = sys_uuid.uuid4()
    assert (
        workspace_api.get(MAIL_MESSAGES + message["uuid"], user=other_user).status_code
        == 404
    )
    assert workspace_api.get(MAIL_MESSAGES).json() == [moved.json()]

    events = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert events.status_code == 200, events.text
    mail_kinds = [
        item["payload"]["kind"]
        for item in events.json()
        if item["payload"]["kind"].startswith("mail.")
    ]
    assert mail_kinds == [
        "mail.folder.created",
        "mail.folder.created",
        "mail.message.created",
        "mail.message.updated",
        "mail.message.updated",
    ]


def test_send_creates_one_durable_intent_in_the_request_transaction(
    workspace_api,
    db,
):
    account_uuid = str(sys_uuid.uuid4())
    account = workspace_api.post(
        EXTERNAL_ACCOUNTS,
        permissions=EXTERNAL_ACCOUNT_PERMISSIONS,
        json={
            "uuid": account_uuid,
            "provider": "mail",
            "settings": {"email": "cassi@example.test"},
            "status": "active",
        },
    )
    assert account.status_code == 201, account.text
    folder = _folder(workspace_api, external_account_uuid=account_uuid)
    message = _message(workspace_api, folder["uuid"])
    assert message["from_address"] == "cassi@example.test"
    change_folder_account = workspace_api.put(
        MAIL_FOLDERS + folder["uuid"],
        json={"external_account_uuid": None},
    )
    assert change_folder_account.status_code == 400, change_folder_account.text

    sent = workspace_api.post(
        MAIL_MESSAGES + message["uuid"] + "/actions/send/invoke",
        json={},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["draft"] is False
    assert sent.json()["delivery_status"] == "queued"
    assert sent.json().get("sent_at") is None

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT project_id, external_account_uuid, status, payload
            FROM mail_outgoing_intents WHERE message_uuid = %s
            """,
            (message["uuid"],),
        )
        intent = cursor.fetchone()
    assert str(intent[0]) == workspace_api.project_id
    assert str(intent[1]) == account_uuid
    assert intent[2] == "new"
    assert intent[3]["message_uuid"] == message["uuid"]
    assert intent[3]["to_addresses"] == ["recipient@example.test"]

    repeated = workspace_api.post(
        MAIL_MESSAGES + message["uuid"] + "/actions/send/invoke",
        json={},
    )
    assert repeated.status_code == 400, repeated.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM mail_outgoing_intents WHERE message_uuid = %s",
            (message["uuid"],),
        )
        assert cursor.fetchone()[0] == 1

    delete = workspace_api.delete(MAIL_MESSAGES + message["uuid"])
    assert delete.status_code == 400, delete.text

    senderless_account_uuid = str(sys_uuid.uuid4())
    senderless_account = workspace_api.post(
        EXTERNAL_ACCOUNTS,
        permissions=EXTERNAL_ACCOUNT_PERMISSIONS,
        json={
            "uuid": senderless_account_uuid,
            "provider": "mail",
            "settings": {},
            "status": "active",
        },
    )
    assert senderless_account.status_code == 201, senderless_account.text
    senderless_folder = _folder(
        workspace_api,
        external_account_uuid=senderless_account_uuid,
        path="Senderless",
        name="Senderless",
    )
    senderless_message = _message(workspace_api, senderless_folder["uuid"])
    senderless_send = workspace_api.post(
        MAIL_MESSAGES + senderless_message["uuid"] + "/actions/send/invoke",
        json={},
    )
    assert senderless_send.status_code == 400, senderless_send.text


def test_owner_file_urn_is_private_and_can_be_attached_to_a_draft(
    workspace_api,
    db,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    upload = workspace_api.post(
        FILES,
        data={"acl": '{"mode":"owner"}'},
        files={"file": ("private.txt", io.BytesIO(b"private"), "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    file = upload.json()
    file_urn = f"urn:file:{file['uuid']}"
    metadata = file_storage.get_workspace_file_storage().read_metadata(
        sys_uuid.UUID(file["uuid"])
    )
    assert metadata.acl_mode == "owner"
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT project_id, user_uuid, viewer_user_uuid
            FROM m_workspace_visible_files_v1
            WHERE uuid = %s
            """,
            (file["uuid"],),
        )
        visible_file = cursor.fetchone()
    assert str(visible_file[0]) == workspace_api.project_id
    assert str(visible_file[1]) == workspace_api.user_uuid
    assert str(visible_file[2]) == workspace_api.user_uuid

    other_user = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, other_user, f"user-{other_user}")
    assert workspace_api.get(FILES + file["uuid"], user=other_user).status_code == 404

    folder = _folder(workspace_api)
    message = _message(workspace_api, folder["uuid"], attachment_urns=[file_urn])
    assert message["attachment_urns"] == [file_urn]

    other_folder_response = workspace_api.post(
        MAIL_FOLDERS,
        user=other_user,
        json={"path": "Other drafts", "name": "Other drafts"},
    )
    assert other_folder_response.status_code == 201, other_folder_response.text
    other_folder = other_folder_response.json()
    forbidden = workspace_api.post(
        MAIL_MESSAGES,
        user=other_user,
        json={
            "folder_uuid": other_folder["uuid"],
            "body_text": "No access",
            "attachment_urns": [file_urn],
        },
    )
    assert forbidden.status_code == 400, forbidden.text
