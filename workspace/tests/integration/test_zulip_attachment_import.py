# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import base64
import urllib.parse
from unittest import mock

import pytest

from workspace.bridges.zulip import outgoing_operations
from workspace.tests.integration import test_zulip_history as history_tests
from workspace.tests.integration import test_zulip_incoming as incoming_tests


incoming = incoming_tests.incoming


def temporary_url(path):
    token = base64.b16encode(f"{path}:1:example-signature".encode()).decode()
    return (
        f"/user_uploads/temporary/{token}/{urllib.parse.quote(path.rsplit('/', 1)[-1])}"
    )


@pytest.mark.parametrize("direction", ["realtime", "history"])
@pytest.mark.parametrize("status", [200, 403, 404])
def test_expired_file_link_uses_current_account_access(
    incoming, tmp_path, monkeypatch, direction, status
):
    api, db, account, http, _ = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    path = "1/key/file.pdf"
    original = "/user_uploads/" + path
    temporary = temporary_url(path)
    content = f"Text survives [file]({temporary})"
    downloaded = []

    async def download(selected, url):
        downloaded.append(url)
        assert selected["uuid"] == account["uuid"]
        if url != original:
            raise outgoing_operations.ApiError(400, {"msg": "Invalid token"})
        if status != 200:
            raise outgoing_operations.ApiError(status, {"msg": "Unavailable"})
        return b"attachment bytes", "application/pdf"

    if direction == "realtime":
        http.download = mock.AsyncMock(side_effect=download)
        incoming_tests.apply(incoming, incoming_tests.event(content))
        incoming_tests.apply(incoming, incoming_tests.event(content))
    else:
        history_tests.request(db, account)

        async def run():
            async with history_tests.worker(
                incoming, {101: history_tests.message(content=content)}
            ) as (service, _):
                service.http.download = mock.AsyncMock(side_effect=download)
                await history_tests.drain(service, account)
                assert not await service.db.sql("SELECT 1 FROM zulip_history_intents")

        asyncio.run(run())
    incoming_tests.drain()
    response = api.get("/v1/messenger/messages/")
    assert response.status_code == 200, response.text
    messages = response.json()
    assert len(messages) == 1
    text = messages[0]["payload"]["content"]
    assert text.startswith("Text survives ")
    assert temporary not in text
    files = api.get("/v1/messenger/files/")
    assert files.status_code == 200, files.text
    if status == 200:
        assert len(files.json()) == 1
        file = files.json()[0]
        assert file["provider_identity"] == original
        response = api.get(f"/v1/messenger/files/{file['uuid']}/actions/download")
        assert response.status_code == 200, response.text
        assert response.content == b"attachment bytes"
        assert "urn:file:" in text
        assert downloaded == [original]
    else:
        assert files.json() == []
        assert ("access denied" if status == 403 else "file deleted") in text
        assert downloaded and set(downloaded) == {original}


def test_long_upload_identity_survives_storage_api_and_replay(
    incoming, tmp_path, monkeypatch
):
    api, db, account, http, _ = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    path = "/user_uploads/1/ab/" + "k" * 24 + "/" + "f" * 249 + ".pdf"
    assert len(path) == 297
    content = f"[long file]({path})"
    http.download = mock.AsyncMock(return_value=(b"long path file", "application/pdf"))
    incoming_tests.apply(incoming, incoming_tests.event(content))
    first = api.get("/v1/messenger/files/")
    assert first.status_code == 200, first.text
    file = first.json()[0]
    assert file["provider_identity"] == path
    assert db.execute(
        "SELECT provider_identity FROM zulip_entity_links "
        "WHERE entity_type='file' AND entity_uuid=%s",
        (file["uuid"],),
    ).fetchall() == [(path,)]
    incoming_tests.apply(incoming, incoming_tests.event(content))
    incoming_tests.drain()
    assert api.get("/v1/messenger/files/").json() == [file]
    response = api.get(f"/v1/messenger/files/{file['uuid']}/actions/download")
    assert response.status_code == 200, response.text
    assert response.content == b"long path file"
    http.download.assert_awaited_once_with(account, path)


def test_old_temporary_file_identity_keeps_its_uuid_when_reimported(
    incoming, tmp_path, monkeypatch
):
    api, db, account, http, _ = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    original = "/user_uploads/1/key/file.pdf"
    temporary = temporary_url("1/key/file.pdf")
    http.download = mock.AsyncMock(return_value=(b"existing file", "application/pdf"))
    incoming_tests.apply(incoming, incoming_tests.event(f"[file]({original})"))
    file = api.get("/v1/messenger/files/").json()[0]
    db.execute(
        "UPDATE m_workspace_files SET provider_identity=%s WHERE uuid=%s",
        (temporary, file["uuid"]),
    )
    db.execute(
        "UPDATE zulip_entity_links SET provider_identity=%s "
        "WHERE entity_type='file' AND entity_uuid=%s",
        (temporary, file["uuid"]),
    )
    incoming_tests.apply(incoming, incoming_tests.event(f"[file]({temporary})", 102))
    incoming_tests.apply(incoming, incoming_tests.event(f"[file]({original})", 103))
    files = api.get("/v1/messenger/files/").json()
    assert len(files) == 1
    assert files[0]["uuid"] == file["uuid"]
    assert files[0]["provider_identity"] == original
    assert db.execute(
        "SELECT provider_identity FROM zulip_entity_links "
        "WHERE entity_type='file' AND entity_uuid=%s",
        (file["uuid"],),
    ).fetchall() == [(original,)]
    http.download.assert_awaited_once_with(account, original)


def test_malformed_temporary_link_does_not_block_the_message(
    incoming, tmp_path, monkeypatch
):
    api, _db, _account, http, _ = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    http.download = mock.AsyncMock(
        side_effect=outgoing_operations.ApiError(400, {"msg": "Invalid token"})
    )
    incoming_tests.apply(
        incoming,
        incoming_tests.event("Text [file](/user_uploads/temporary/not-hex/file.pdf)"),
    )
    incoming_tests.drain()
    message = api.get("/v1/messenger/messages/").json()[0]
    assert message["payload"]["content"] == "Text file (access denied)"
    http.download.assert_not_awaited()


@pytest.mark.parametrize("direction", ["realtime", "history"])
@pytest.mark.parametrize("form", ["regular", "temporary", "unicode"])
@pytest.mark.parametrize("size", [2048, 2049])
def test_upload_path_byte_limit_keeps_message_synchronization(
    incoming, tmp_path, monkeypatch, direction, form, size
):
    api, db, account, http, _ = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    prefix, suffix = "/user_uploads/1/", "/file.pdf"
    remaining = size - len(prefix + suffix)
    middle = (
        "я" * (remaining // 2) + "x" * (remaining % 2)
        if form == "unicode"
        else "x" * remaining
    )
    path = prefix + middle + suffix
    assert len(path.encode("utf-8")) == size
    target = (
        temporary_url(path.removeprefix("/user_uploads/"))
        if form == "temporary"
        else path
    )
    content = f"Text survives [file]({target})"
    download = mock.AsyncMock(return_value=(b"bounded path bytes", "application/pdf"))
    if direction == "realtime":
        http.download = download
        incoming_tests.apply(incoming, incoming_tests.event(content))
        incoming_tests.apply(incoming, incoming_tests.event(content))
    else:
        history_tests.request(db, account)

        async def run():
            async with history_tests.worker(
                incoming, {101: history_tests.message(content=content)}
            ) as (service, _):
                service.http.download = download
                await history_tests.drain(service, account)
                assert not await service.db.sql("SELECT 1 FROM zulip_history_intents")

        asyncio.run(run())
    incoming_tests.drain()
    response = api.get("/v1/messenger/messages/")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    text = response.json()[0]["payload"]["content"]
    files = api.get("/v1/messenger/files/")
    assert files.status_code == 200, files.text
    if size == 2048:
        assert len(files.json()) == 1
        file = files.json()[0]
        assert file["provider_identity"] == path
        assert "urn:file:" in text
        response = api.get(f"/v1/messenger/files/{file['uuid']}/actions/download")
        assert response.status_code == 200 and response.content == b"bounded path bytes"
        download.assert_awaited_once()
        selected, downloaded_path = download.await_args.args
        assert downloaded_path == path
        for key in (
            "uuid",
            "project_id",
            "provider_uuid",
            "owner_user_uuid",
            "settings",
        ):
            assert selected[key] == account[key]
    else:
        assert files.json() == []
        assert text == "Text survives file (file path too long)"
        download.assert_not_awaited()


@pytest.mark.parametrize("legacy_identity", [False, True])
def test_personal_delivery_of_temporary_attachment_links_the_late_recipient(
    incoming, tmp_path, monkeypatch, legacy_identity
):
    api, db, account, http, _ = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    original = "/user_uploads/1/shared/file.pdf"
    temporary = temporary_url("1/shared/file.pdf")
    http.download = mock.AsyncMock(return_value=(b"shared bytes", "application/pdf"))
    value = incoming_tests.event(f"[shared]({temporary})", 501)
    value["message"]["timestamp"] = 1700000000
    incoming_tests.apply(incoming, value)
    file = api.get("/v1/messenger/files/").json()[0]
    assert file["provider_identity"] == original
    stored_identity = temporary if legacy_identity else original
    if legacy_identity:
        db.execute(
            "UPDATE m_workspace_files SET provider_identity=%s WHERE uuid=%s",
            (temporary, file["uuid"]),
        )
        db.execute(
            "UPDATE zulip_entity_links SET provider_identity=%s WHERE entity_type='file' AND entity_uuid=%s",
            (temporary, file["uuid"]),
        )
    linked, peer = incoming_tests.peer_account(incoming)
    try:
        incoming_tests.apply(
            peer,
            {
                "type": "subscription",
                "op": "add",
                "subscriptions": [{"stream_id": 42, "name": "Channel"}],
            },
        )
        incoming_tests.apply(peer, incoming_tests.event("Recipient joins", 502))
        assert not db.execute(
            "SELECT 1 FROM zulip_entity_links WHERE entity_type='file' AND entity_uuid=%s AND external_account_uuid=%s",
            (file["uuid"], linked.uuid),
        ).fetchall()
        assert (
            api.get(
                f"/v1/messenger/files/{file['uuid']}", user=linked.owner_user_uuid
            ).status_code
            == 404
        )
        incoming_tests.apply(peer, {**value, "_personal_only": True})
        incoming_tests.drain()
        response = api.get(
            f"/v1/messenger/files/{file['uuid']}/actions/download",
            user=linked.owner_user_uuid,
        )
        assert response.status_code == 200, response.text
        assert response.content == b"shared bytes"
        assert db.execute(
            "SELECT provider_identity FROM zulip_entity_links WHERE entity_type='file' AND entity_uuid=%s AND external_account_uuid=%s",
            (file["uuid"], linked.uuid),
        ).fetchall() == [(stored_identity,)]
        assert "file.created" in [
            item["kind"] for item in incoming_tests.client_events(peer)
        ]
        incoming_tests.apply(peer, {**value, "_personal_only": True})
        incoming_tests.drain()
        assert [item["kind"] for item in incoming_tests.client_events(peer)].count(
            "file.created"
        ) == 1
        http.download.assert_awaited_once_with(account, original)
    finally:
        linked.delete()
