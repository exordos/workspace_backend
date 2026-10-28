# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import base64
import uuid

import pytest

from workspace.messenger_api.dm import external_models
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_outgoing


outgoing = test_zulip_outgoing.outgoing
FILES = "/v1/messenger/files/"
CONTENT = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


def upload(api, stream):
    response = api.post(
        FILES,
        data={"stream_uuid": str(stream)},
        files={"file": ("local.png", CONTENT, "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()["uuid"]


def readable(api, file, user):
    response = api.get(FILES + file, user=user)
    assert response.status_code == 200, response.text
    assert response.json()["uuid"] == file
    response = api.get(FILES + file + "/actions/download", user=user)
    assert response.status_code == 200, response.text
    assert response.content == CONTENT
    assert file in {item["uuid"] for item in api.get(FILES, user=user).json()}


def hidden(api, file, user, **kwargs):
    for suffix in ("", "/actions/download"):
        response = api.get(FILES + file + suffix, user=user, **kwargs)
        assert response.status_code == 404, response.text
    assert file not in {
        item["uuid"] for item in api.get(FILES, user=user, **kwargs).json()
    }


@pytest.mark.parametrize("kind", ["zulip", "native", "direct"])
def test_local_file_uses_workspace_membership_without_running_either_sync_worker(
    outgoing, db, tmp_path, monkeypatch, kind
):
    api, account, channel, *_ = outgoing
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    peer, outsider = uuid.uuid4(), uuid.uuid4()
    if kind == "zulip":
        stream = channel.stream_uuid
        conftest.seed_user_stream_binding(db, api.project_id, stream, peer)
    elif kind == "native":
        stream = conftest.seed_user_stream(db, api.project_id, api.user_uuid, "Group")
        conftest.seed_user_stream_binding(db, api.project_id, stream, peer)
    else:
        conftest.seed_workspace_user(db, peer, "direct-peer")
        db.execute(
            "SELECT messenger_v2_register_project_user(%s,%s)", (api.project_id, peer)
        )
        response = api.post(
            "/v1/messenger/streams/",
            json={"name": "Direct", "direct_user_uuid": str(peer)},
        )
        assert response.status_code == 201, response.text
        stream = response.json()["uuid"]

    file = upload(api, stream)
    assert db.execute(
        "SELECT source_name,provider_identity FROM m_workspace_files WHERE uuid=%s",
        (file,),
    ).fetchone() == ("zulip" if kind == "zulip" else "native", None)
    assert not db.execute(
        "SELECT 1 FROM zulip_entity_links WHERE entity_type='file' AND entity_uuid=%s",
        (file,),
    ).fetchall()
    assert not db.execute(
        "SELECT 1 FROM m_external_accounts_v2 WHERE owner_user_uuid=%s", (peer,)
    ).fetchall()
    readable(api, file, api.user_uuid)
    readable(api, file, peer)
    hidden(api, file, outsider)
    hidden(api, file, peer, project=uuid.uuid4())

    # A provider receipt cannot grant access to a Workspace outsider.
    linked = external_models.ExternalAccount(
        project_id=api.project_id,
        owner_user_uuid=outsider,
        provider="zulip",
        external_account_id="8",
        status="active",
        settings={
            "server_url": "https://zulip.example.com",
            "email": "peer@example.com",
            "api_key": "test-key",
        },
    )
    linked.insert()
    try:
        db.execute(
            "INSERT INTO zulip_entity_links(project_id,entity_type,entity_uuid,provider_uuid,external_account_uuid,provider_identity,data) "
            "VALUES (%s,'file',%s,%s,%s,'/user_uploads/1/local.png','{\"account_identity\":\"8\"}')",
            (api.project_id, file, account.provider_uuid, linked.uuid),
        )
        hidden(api, file, outsider)
        db.execute(
            "UPDATE messenger_stream_bindings SET active=false WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
            (api.project_id, stream, peer),
        )
        hidden(api, file, peer)
        db.execute(
            "UPDATE messenger_stream_bindings SET active=true WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
            (api.project_id, stream, peer),
        )
        readable(api, file, peer)
        db.execute(
            "UPDATE messenger_streams SET deleted_at=NOW() WHERE uuid=%s", (stream,)
        )
        hidden(api, file, peer)
    finally:
        linked.delete()


@pytest.mark.parametrize("mode", ["owner", "public"])
def test_owner_and_public_files_ignore_provider_receipts(
    outgoing, db, tmp_path, monkeypatch, mode
):
    api, _account, channel, *_ = outgoing
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    file = upload(api, channel.stream_uuid)
    db.execute(
        "UPDATE m_workspace_files SET acl_mode=%s,provider_identity='/user_uploads/1/local.png' WHERE uuid=%s",
        (mode, file),
    )
    readable(api, file, api.user_uuid)
    other = uuid.uuid4()
    if mode == "owner":
        hidden(api, file, other)
        hidden(api, file, api.user_uuid, project=uuid.uuid4())
    else:
        readable(api, file, other)
