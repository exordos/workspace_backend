# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import datetime
import hashlib
import re
from unittest import mock
import uuid

import pytest
import psycopg
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip import client
from workspace.bridges.zulip import content_conversion
from workspace.bridges.zulip import message_conversion
from workspace.bridges.zulip.dm import models
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_api.dm import v2_models
from workspace.services import history_import
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2
from workspace.tests.integration import test_zulip_messages
from workspace.tests.integration import test_zulip_outgoing


history_account = test_zulip_messages.history_account
outgoing = test_zulip_outgoing.outgoing


def _publish(account, db):
    states = models.ZulipStreamState.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    for state in states:
        with contexts.Context().session_manager():
            state.do()
        conftest.seed_user_stream_binding(
            db, account.project_id, state.stream_uuid, account.owner_user_uuid
        )
    with contexts.Context().session_manager():
        for state in models.ZulipTopicState.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        ):
            state.do()
        models.ZulipMessageState.reconcile_batch(100)
    test_messenger_v2._drain()


def test_imported_references_open_real_api_objects_and_convert_back(
    history_account, api, db, monkeypatch
):
    account = history_account
    zulip = mock.Mock()
    source = test_zulip_messages._message_data(content="Original **message**")
    linked = test_zulip_messages._message_data(
        43,
        content=(
            "@**Alias|7** #**Channel 42** #**Channel 42>General** "
            "[message](https://zulip.example.com/#narrow/near/42)\n\n"
            "@_**Alias|7** [said](https://zulip.example.com/#narrow/near/42):\n"
            "```quote\nOriginal **message**\n```\n:sunset:"
        ),
    )
    zulip.get_messages.return_value = [linked, source]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert test_zulip_messages._import_history(account).status == "done"
    _publish(account, db)
    api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
    imported = test_zulip_messages._messages(account)
    converted = imported[1].content.workspace_content
    stream_uuid = imported[0].channel.stream_uuid
    topic_uuid = imported[0].topic.uuid
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
    canonical = uuid.uuid5(account.project_id, str(uuid.uuid5(namespace, "42")))
    public_uuid = uuid.uuid5(canonical, "zulip-placement")
    assert f"urn:user:{account.owner_user_uuid}" in converted
    assert f"urn:stream:{stream_uuid}" in converted
    assert f"urn:topic:{topic_uuid}" in converted
    assert f"urn:message:{public_uuid}" in converted
    assert f"urn:quote:{public_uuid}" in converted
    assert "🌆" in converted
    for path in (
        f"/v1/users/{account.owner_user_uuid}",
        f"/v1/messenger/streams/{stream_uuid}",
        f"/v1/messenger/stream_topics/{topic_uuid}",
        f"/v1/messenger/messages/{public_uuid}",
    ):
        response = api.get(path)
        assert response.status_code == 200, (path, response.text)
    with contexts.Context().session_manager():
        text = content_conversion.ConversionContext(account).to_zulip(
            {"kind": "markdown", "content": converted}, stream_uuid, zulip
        )
    assert "|7**" in text
    assert "/#narrow/channel/42" in text
    assert "/topic/General" in text
    assert "/near/42" in text
    assert "```quote\nOriginal **message**\n```" in text
    with contexts.Context().session_manager():
        selected = content_conversion.ConversionContext(account).to_workspace(
            {
                **source,
                "id": 44,
                "content": "@_**Alias|7** [said](https://zulip.example.com/#narrow/near/42):\n```quote\n@**Alias|7**\n```",
            },
            stream_uuid,
            zulip,
        )
        assert "urn%3Auser%3A" in selected
        selected = content_conversion.ConversionContext(account).to_zulip(
            {"kind": "markdown", "content": selected}, stream_uuid, zulip
        )
        assert "|7**\n```" in selected and "urn:user:" not in selected


@pytest.mark.parametrize(
    "quote_context",
    [
        "",
        " to @_**Recipient**",
        " to @_**Recipient** and @_**Another|9**",
        " in [#**Channel 42>General**](https://zulip.example.com/#narrow/near/999)",
    ],
)
def test_nested_edited_quote_keeps_snapshot_structure_through_the_api(
    history_account, api, db, monkeypatch, quote_context
):
    account = history_account

    def quote(message_id, body, fence="```", url="near"):
        return (
            f"@_**Alias|7** [wrote](https://zulip.example.com/#narrow/{url}/{message_id}){quote_context}:\n"
            f"{fence}quote\n{body}\n{fence}"
        )

    inner = quote(42, "First leaf.") + "\n\nInner comment."
    group = (
        quote(42, "First leaf.")
        + "\n\nFirst comment.\n\n"
        + quote(43, "Second leaf.")
        + "\nImmediate second comment.\nAnother line.\n\n"
        + quote(44, inner, "````")
        + "\n\nThird comment."
    )
    sources = [
        test_zulip_messages._message_data(42, content="First leaf."),
        test_zulip_messages._message_data(43, content="Second leaf."),
        test_zulip_messages._message_data(44, content=inner),
        test_zulip_messages._message_data(
            45, content=group + "\n\nA later source edit."
        ),
        test_zulip_messages._message_data(
            46,
            content=quote(45, group, "`````", "dm/7,8-dm/near") + "\n\nOutside reply.",
        ),
    ]
    zulip = mock.Mock()
    zulip.get_messages.return_value = sources
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert test_zulip_messages._import_history(account, last=46).status == "done"
    _publish(account, db)
    api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
    imported = test_zulip_messages._messages(account)
    public = {
        source.message_id: str(
            uuid.uuid5(
                uuid.uuid5(account.project_id, str(source.message_uuid)),
                "zulip-placement",
            )
        )
        for source in imported
    }
    group_response = api.get(f"/v1/messenger/messages/{public[45]}")
    assert group_response.status_code == 200
    assert (
        f"[Alias](urn:quote:{public[43]})\n\nImmediate second comment."
        in group_response.json()["payload"]["content"]
    )
    response = api.get(f"/v1/messenger/messages/{public[46]}")
    assert response.status_code == 200
    payload = response.json()["payload"]
    kind, identifier, query = next(
        message_conversion.workspace_references(payload["content"])
    )
    assert (kind, identifier) == ("forward", public[45])
    snapshot = message_conversion.parse_snapshot(query)
    assert snapshot.count("urn:quote:") == 3
    assert f"urn:quote:{public[44]}" in snapshot
    assert "\n\nImmediate second comment.\nAnother line." in snapshot
    assert "Not supported" not in payload["content"]
    assert payload["content"].endswith("\n\nOutside reply.")
    stream_uuid = imported[0].channel.stream_uuid
    with contexts.Context().session_manager():
        text = content_conversion.ConversionContext(account).to_zulip(
            payload, stream_uuid, zulip
        )
        returned = content_conversion.ConversionContext(account).to_workspace(
            {**sources[-1], "content": text}, stream_uuid, zulip
        )
    assert "urn:" not in text
    assert "`````quote" in text
    _kind, returned_id, returned_query = next(
        message_conversion.workspace_references(returned)
    )
    assert returned_id == identifier
    returned_snapshot = message_conversion.parse_snapshot(returned_query)
    assert list(message_conversion.workspace_references(returned_snapshot)) == list(
        message_conversion.workspace_references(snapshot)
    )
    # Directory names can replace old aliases; quoted messages and comments must not change.
    assert [
        line for line in returned_snapshot.splitlines() if "urn:quote:" not in line
    ] == [line for line in snapshot.splitlines() if "urn:quote:" not in line]


def test_native_delivery_keeps_workspace_text_and_uses_public_message_ids(outgoing, db):
    api, account, channel, remote, zulip, _constructor = outgoing
    original = f"[Me](urn:user:{api.user_uuid}) [Chat](urn:stream:{channel.stream_uuid}) :sunset:"
    remote["content"] = original
    public = test_zulip_outgoing.create(outgoing)
    test_zulip_outgoing.dispatch()
    service = history_import.HistoryImportService()
    service._iteration()
    transmitted = zulip.send_message.call_args.args[1]
    assert "|7**" in transmitted and "/#narrow/channel/42" in transmitted
    assert "urn:" not in transmitted
    assert ":city_sunrise:" in transmitted
    remote["content"] = transmitted
    test_zulip_outgoing.ready(test_zulip_outgoing.tasks(account)[0])
    service._iteration()
    assert (
        api.get(f"/v1/messenger/messages/{public['uuid']}").json()["payload"]["content"]
        == original
    )
    with contexts.Context().session_manager():
        converter = content_conversion.ConversionContext(account)
        converted = converter.to_workspace(
            {
                **remote,
                "id": 12346,
                "content": f"[Sent](https://zulip.example.com/#narrow/near/{remote['id']})",
            },
            channel.stream_uuid,
            zulip,
        )
        assert converted == f"[Sent](urn:message:{public['uuid']})"
        text = content_conversion.ConversionContext(account).to_zulip(
            {"kind": "markdown", "content": converted}, channel.stream_uuid, zulip
        )
        assert text.endswith(f"/#narrow/near/{remote['id']})")
    (delivery,) = models.MessageDelivery.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    external_edit = {
        **remote,
        "content": "@**Me|7** :sunset:",
        "last_edit_timestamp": remote["timestamp"] + 1,
    }
    with contexts.Context().session_manager():
        delivery.observe(external_edit)
        delivery.observe(remote)
    content = api.get(f"/v1/messenger/messages/{public['uuid']}").json()["payload"][
        "content"
    ]
    assert f"urn:user:{api.user_uuid}" in content and "🌆" in content
    legacy_uuid = uuid.uuid4()
    db.execute(
        "UPDATE messenger_message_placements SET legacy_public_uuid=%s WHERE project_id=%s AND uuid=%s",
        (legacy_uuid, account.project_id, public["uuid"]),
    )
    with contexts.Context().session_manager():
        converter = content_conversion.ConversionContext(account)
        text = converter.to_zulip(
            {
                "kind": "markdown",
                "content": f"[Old link](urn:message:{legacy_uuid})\n[Quote](urn:quote:{public['uuid']})",
            },
            channel.stream_uuid,
            zulip,
        )
        assert f"/near/{remote['id']}" in text
        assert "```quote\n@**Me|7** :sunset:\n```" in text
        converted = content_conversion.ConversionContext(account).to_workspace(
            {
                **remote,
                "content": f"[Sent](https://zulip.example.com/#narrow/near/{remote['id']})",
            },
            channel.stream_uuid,
            zulip,
        )
        assert converted == f"[Sent](urn:message:{public['uuid']})"


@pytest.mark.parametrize("kind", ["user", "stream", "topic", "message"])
def test_missing_mapping_fails_before_the_send_attempt_is_marked(outgoing, kind):
    _api, account, _channel, remote, zulip, _constructor = outgoing
    remote["content"] = f"[Unknown](urn:{kind}:{uuid.uuid4()})"
    test_zulip_outgoing.create(outgoing)
    test_zulip_outgoing.dispatch()
    history_import.HistoryImportService()._iteration()
    (delivery,) = models.MessageDelivery.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert not delivery.sending
    assert test_zulip_outgoing.tasks(account)[0].status == "error"
    zulip.send_message.assert_not_called()


@pytest.mark.parametrize(
    "status,notice",
    [(403, "access denied"), (404, "file deleted"), (410, "file deleted")],
)
def test_unavailable_attachments_do_not_block_import_or_stored_content_conversion(
    history_account, db, monkeypatch, tmp_path, status, notice
):
    account = history_account
    account.settings = {
        **account.settings,
        "email": "user@example.com",
        "api_key": "key",
    }
    account.status = "active"
    account.update()
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    test_zulip_messages._create_workspace_stream(account, db)
    missing = "/user_uploads/2"
    kept = "/user_uploads/2/kept/report.txt"
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError("File is unavailable", response=response)
    zulip = mock.Mock()

    def download(path):
        if path == missing:
            raise error
        assert path == kept
        return b"report", "text/plain"

    zulip.download_file.side_effect = download
    raw = (
        f"[report]({missing}) ![preview]({missing}) "
        f"https://zulip.example.com{missing}. {missing} [kept]({kept}) @**Alias|7**"
    )
    zulip.get_messages.return_value = [test_zulip_messages._message_data(content=raw)]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)

    assert test_zulip_messages._import_history(account).status == "done"
    (source,) = test_zulip_messages._messages(account)
    file_uuid = db.execute(
        "SELECT file_uuid FROM bridge_zulip_v5_file_links WHERE project_id=%s",
        (account.project_id,),
    ).fetchone()[0]
    expected = (
        f"report ({notice}) preview ({notice}) 2 ({notice}). 2 ({notice}) "
        f"[kept](urn:file:{file_uuid}) [Alias](urn:user:{account.owner_user_uuid})"
    )
    assert source.content.workspace_content == expected
    assert zulip.download_file.call_args_list == [mock.call(missing), mock.call(kept)]
    db.execute(
        "UPDATE bridge_zulip_v5_message_contents SET workspace_content=zulip_content,conversion_version=0 WHERE uuid=%s",
        (source.content.uuid,),
    )
    task = models.Task(
        content=models.ConvertMessageContent(content_uuid=source.content.uuid)
    )
    task.insert()
    try:
        with contexts.Context().session_manager():
            task.execute()
            assert task.status == "done"
        (source,) = test_zulip_messages._messages(account)
        assert source.content.workspace_content == expected
        assert source.content.conversion_version == content_conversion.VERSION
        assert zulip.download_file.call_args_list == [
            mock.call(missing),
            mock.call(kept),
            mock.call(missing),
        ]
    finally:
        task.delete()


def test_unavailable_attachment_states_are_distinct_and_retried_for_later_messages(
    history_account, db, monkeypatch, tmp_path
):
    account = history_account
    stream_uuid = test_zulip_messages._create_workspace_stream(account, db)
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    denied = "/user_uploads/2/denied/report.pdf"
    deleted = "/user_uploads/2/deleted/report.pdf"
    raw = f"[private]({denied}) [gone]({deleted}) https://zulip.example.com{denied}?download=1"
    zulip = mock.Mock()

    def download(path):
        response = requests.Response()
        response.status_code = 403 if path == denied else 404
        raise requests.HTTPError(response=response)

    zulip.download_file.side_effect = download
    with contexts.Context().session_manager():
        converter = content_conversion.ConversionContext(account)
        assert (
            converter.to_workspace(
                test_zulip_messages._message_data(content=raw), stream_uuid, zulip
            )
            == r"private (access denied) gone (file deleted) report\.pdf (access denied)"
        )
        assert zulip.download_file.call_args_list == [
            mock.call(denied),
            mock.call(deleted),
        ]

        zulip.download_file.side_effect = None
        zulip.download_file.return_value = b"report", "application/pdf"
        converted = converter.to_workspace(
            test_zulip_messages._message_data(id=102, content=raw), stream_uuid, zulip
        )
        assert "(access denied)" not in converted
        assert "(file deleted)" not in converted
        assert converted.count("urn:file:") == 3
        assert converter.context["deleted_files"] == set()
        assert converter.context["inaccessible_files"] == set()
        assert zulip.download_file.call_args_list == [
            mock.call(denied),
            mock.call(deleted),
            mock.call(denied),
            mock.call(deleted),
        ]


@pytest.mark.parametrize("status", [401, 429, 500, None, "timeout"])
def test_other_attachment_download_errors_are_not_reported_as_deletions(
    history_account, db, status
):
    account = history_account
    stream_uuid = test_zulip_messages._create_workspace_stream(account, db)
    if status == "timeout":
        error = requests.Timeout("Download timed out")
    else:
        response = requests.Response()
        response.status_code = status
        error = requests.HTTPError(response=response if status is not None else None)
    zulip = mock.Mock()
    zulip.download_file.side_effect = error
    with contexts.Context().session_manager():
        converter = content_conversion.ConversionContext(account)
        with pytest.raises(type(error)) as raised:
            converter.to_workspace(
                test_zulip_messages._message_data(
                    content="[report](/user_uploads/2/kept/report.pdf)"
                ),
                stream_uuid,
                zulip,
            )
        assert raised.value is error
        assert converter.context["deleted_files"] == set()
        assert converter.context["inaccessible_files"] == set()


@pytest.mark.parametrize("deleted", [False, True])
def test_first_direct_message_with_an_attachment_does_not_wait_for_its_own_chat(
    history_account, api, db, monkeypatch, tmp_path, deleted
):
    account = history_account
    owner = uuid.uuid4()
    conftest.seed_workspace_user(db, owner, f"attachment-peer-{owner}")
    peer = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=owner,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        settings=account.settings,
    )
    peer.insert()
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    path = "/user_uploads/2/new/photo.png"
    zulip = mock.Mock()
    zulip.download_file.return_value = b"photo", "image/png"
    if deleted:
        response = requests.Response()
        response.status_code = 404
        zulip.download_file.side_effect = requests.HTTPError(response=response)
    zulip.get_messages.return_value = [
        test_zulip_messages._message_data(
            type="private",
            subject="Zulip",
            content=f"![photo]({path})",
            display_recipient=[
                {"id": 7, "full_name": "Me"},
                {"id": 8, "full_name": "Peer"},
            ],
        )
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    try:
        assert test_zulip_messages._import_history(account).status == "done"
        (source,) = test_zulip_messages._messages(account)
        assert (
            db.execute(
                "SELECT uuid FROM messenger_streams WHERE project_id=%s AND uuid=%s",
                (account.project_id, source.channel.stream_uuid),
            ).fetchone()
            is not None
        )
        if deleted:
            assert source.content.workspace_content == "photo (file deleted)"
        else:
            assert source.content.workspace_content.startswith("![photo](urn:image:")
        with contexts.Context().session_manager():
            for state in models.ZulipStreamBindingState.objects.get_all(
                filters={"project_id": filters.EQ(account.project_id)}
            ):
                state.do()
        _publish(account, db)
        api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
        public = uuid.uuid5(
            uuid.uuid5(account.project_id, str(source.message_uuid)), "zulip-placement"
        )
        assert api.get(f"/v1/messenger/messages/{public}").status_code == 200
    finally:
        peer.delete()


def test_realtime_message_is_converted_with_attachments_before_workspace_creation(
    outgoing, db, monkeypatch, tmp_path
):
    api, account, _channel, remote, zulip, _constructor = outgoing
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    path = "/user_uploads/2/new/photo.png"
    remote.update(
        test_zulip_messages._message_data(
            remote["id"], content=f"@**Alias|7** ![photo]({path})"
        )
    )
    zulip.download_file.return_value = b"photo", "image/png"
    subscription = models.RealtimeSubscription.for_account(account)
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [{"id": 0, "type": "message", "message": dict(remote), "flags": []}],
        )
        assert models.RealtimeEvent.dispatch_one()
    created = []
    original = v2_models.WorkspaceMessage.insert

    def insert(message, *args, **kwargs):
        created.append(message.payload.content)
        return original(message, *args, **kwargs)

    monkeypatch.setattr(v2_models.WorkspaceMessage, "insert", insert)
    history_import.HistoryImportService()._iteration()
    _publish(account, db)
    (source,) = test_zulip_messages._messages(account)
    converted = source.content.workspace_content
    assert created == [converted]
    assert f"[Alias](urn:user:{account.owner_user_uuid})" in converted
    assert "![photo](urn:image:" in converted and "/user_uploads/" not in converted
    public = uuid.uuid5(
        uuid.uuid5(account.project_id, str(source.message_uuid)), "zulip-placement"
    )
    assert (
        api.get(f"/v1/messenger/messages/{public}").json()["payload"]["content"]
        == converted
    )
    file_uuid = re.search(r"urn:image:([\da-f-]{36})", converted)[1]
    assert (
        api.get(f"/v1/messenger/files/{file_uuid}/actions/download").content == b"photo"
    )
    zulip.download_file.assert_called_once_with(path)


def test_outgoing_messages_and_forwards_upload_before_sending_and_reuse_files(
    outgoing, monkeypatch, tmp_path
):
    api, account, channel, remote, zulip, _constructor = outgoing
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    uploaded = api.post(
        "/v1/messenger/files/",
        data={"stream_uuid": str(channel.stream_uuid)},
        files={"file": ("report.txt", b"report", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = f"[Me](urn:user:{account.owner_user_uuid}) [report](urn:file:{uploaded.json()['uuid']})"
    order, sent = [], []
    path = "/user_uploads/2/new/report.txt"

    def upload(*args):
        order.append("upload")
        return path

    def send(recipient, content, topic, queue_id, local_id):
        order.append("send")
        sent.append(content)
        remote["content"] = content
        return remote["id"]

    zulip.upload_file.side_effect = upload
    zulip.send_message.side_effect = send
    remote["content"] = body
    first = test_zulip_outgoing.create(outgoing)
    test_zulip_outgoing.dispatch()
    history_import.HistoryImportService()._iteration()
    test_zulip_outgoing.ready(test_zulip_outgoing.tasks(account)[0])
    history_import.HistoryImportService()._iteration()
    assert order == ["upload", "send"]
    assert (
        api.get(f"/v1/messenger/messages/{first['uuid']}").json()["payload"]["content"]
        == body
    )
    snapshot = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    forward = f"[Forward](urn:forward:{first['uuid']}?snapshot={snapshot})"
    remote["id"] += 1
    remote["content"] = forward
    second = test_zulip_outgoing.create(outgoing)
    test_zulip_outgoing.dispatch()
    history_import.HistoryImportService()._iteration()
    test_zulip_outgoing.ready(test_zulip_outgoing.tasks(account)[-1])
    history_import.HistoryImportService()._iteration()
    assert order == ["upload", "send", "send"]
    assert all(
        "urn:" not in content and "|7**" in content and f"[report]({path})" in content
        for content in sent
    )
    assert sent[1] == f"```quote\n{sent[0]}\n```"
    assert (
        api.get(f"/v1/messenger/messages/{second['uuid']}").json()["payload"]["content"]
        == forward
    )
    zulip.upload_file.assert_called_once_with("report.txt", b"report", "text/plain")
    assert all(task.status == "done" for task in test_zulip_outgoing.tasks(account)), [
        (task.content.kind, task.status) for task in test_zulip_outgoing.tasks(account)
    ]


def test_attachments_are_downloadable_and_uploaded_once(
    history_account, api, db, monkeypatch, tmp_path
):
    account = history_account
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    stream_uuid = test_zulip_messages._create_workspace_stream(account, db)
    zulip = mock.Mock()
    data = b"\x89PNG\r\n\x1a\nimage bytes"
    path = "/user_uploads/1/hash/photo.png"
    zulip.download_file.return_value = data, "image/png"
    zulip.get_messages.return_value = [
        test_zulip_messages._message_data(content=f"![photo]({path})")
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    for _ in range(2):
        assert test_zulip_messages._import_history(account).status == "done"
    zulip.download_file.assert_called_once_with(path)
    _publish(account, db)
    (source,) = test_zulip_messages._messages(account)
    (file_uuid,) = re.findall(
        r"urn:image:([\da-f-]{36})", source.content.workspace_content
    )
    api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
    response = api.get(f"/v1/messenger/files/{file_uuid}/actions/download")
    assert response.status_code == 200, response.text
    assert response.content == data
    assert (
        api.get(
            f"/v1/messenger/files/{file_uuid}/actions/download", user=uuid.uuid4()
        ).status_code
        == 404
    )
    with contexts.Context().session_manager():
        assert (
            content_conversion.ConversionContext(account).to_zulip(
                {"kind": "markdown", "content": source.content.workspace_content},
                stream_uuid,
                zulip,
            )
            == f"![photo]({path})"
        )
    zulip.upload_file.assert_not_called()
    encoded = (
        base64.urlsafe_b64encode(source.content.workspace_content.encode())
        .decode()
        .rstrip("=")
    )
    with contexts.Context().session_manager():
        text = content_conversion.ConversionContext(account).to_zulip(
            {
                "kind": "markdown",
                "content": f"[Quote](urn:forward:{uuid.uuid4()}?snapshot={encoded})",
            },
            stream_uuid,
            zulip,
        )
        assert text == f"```quote\n![photo]({path})\n```"
    upload = api.post(
        "/v1/messenger/files/",
        data={"stream_uuid": str(stream_uuid)},
        files={"file": ("report.txt", b"report", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    file_uuid = upload.json()["uuid"]
    zulip.upload_file.return_value = "/user_uploads/1/new/report.txt"
    for _ in range(2):
        with contexts.Context().session_manager():
            text = content_conversion.ConversionContext(account).to_zulip(
                {"kind": "markdown", "content": f"[report](urn:file:{file_uuid})"},
                stream_uuid,
                zulip,
            )
        assert text == "[report](/user_uploads/1/new/report.txt)"
    zulip.upload_file.assert_called_once_with("report.txt", b"report", "text/plain")


@pytest.mark.parametrize("same_server", [True, False])
def test_context_scopes_user_ids_to_project_and_server(
    history_account, db, same_server
):
    account = history_account
    foreign_owner = uuid.uuid4()
    conftest.seed_workspace_user(db, foreign_owner, f"foreign-owner-{foreign_owner}")
    other = external_models.ExternalAccount(
        project_id=uuid.uuid4() if same_server else account.project_id,
        owner_user_uuid=foreign_owner,
        provider="zulip",
        external_account_id=f"{'zulip' if same_server else 'other'}.example.com:7",
        settings={
            "server_url": f"https://{'zulip' if same_server else 'other'}.example.com"
        },
    )
    other.insert()
    try:
        with contexts.Context().session_manager():
            converter = content_conversion.ConversionContext(account)
            assert converter.context["mention_uuids"]["id:7"] == str(
                account.owner_user_uuid
            )
            with pytest.raises(ValueError, match="mapping"):
                converter.to_zulip(
                    {
                        "kind": "markdown",
                        "content": f"[Foreign](urn:user:{foreign_owner})",
                    },
                    uuid.uuid4(),
                    mock.Mock(),
                )
    finally:
        other.delete()


def test_reconversion_updates_old_content_without_faking_an_edit(
    history_account, api, db, monkeypatch
):
    account = history_account
    zulip = mock.Mock()
    raw = "Hello @**Alias|7**"
    zulip.get_messages.return_value = [test_zulip_messages._message_data(content=raw)]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert test_zulip_messages._import_history(account).status == "done"
    _publish(account, db)
    (source,) = test_zulip_messages._messages(account)
    (canonical,) = v2_models.WorkspaceMessage.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    created_at, edited_at = canonical.created_at, canonical.content_updated_at
    db.execute(
        "UPDATE bridge_zulip_v5_message_contents SET workspace_content=%s,conversion_version=0 WHERE uuid=%s",
        (raw, source.content.uuid),
    )
    db.execute(
        "UPDATE messenger_messages SET content_updated_at=content_updated_at-interval '1 microsecond' WHERE project_id=%s AND uuid=%s",
        (account.project_id, canonical.uuid),
    )
    db.execute(
        "UPDATE messenger_messages SET payload=jsonb_build_object('kind','markdown','content',%s::text),content_updated_at=%s WHERE project_id=%s AND uuid=%s",
        (raw, edited_at.replace(tzinfo=None), account.project_id, canonical.uuid),
    )
    task = models.Task(content=models.ConvertMessageContent())
    task.insert()
    try:
        with contexts.Context().session_manager():
            task.execute()
            assert task.status == "done"
            models.ZulipMessageState.reconcile_batch(100)
        (message,) = v2_models.WorkspaceMessage.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        assert (
            message.payload.content
            == f"Hello [Alias](urn:user:{account.owner_user_uuid})"
        )
        assert (message.created_at, message.content_updated_at) == (
            created_at,
            edited_at,
        )
    finally:
        task.delete()


def test_reference_to_pending_native_message_waits_for_its_remote_id(outgoing):
    _api, account, _channel, remote, zulip, _constructor = outgoing
    first = test_zulip_outgoing.create(outgoing)
    remote["content"] = f"[Earlier](urn:message:{first['uuid']})"
    test_zulip_outgoing.create(outgoing)
    test_zulip_outgoing.dispatch()
    _first_task, second_task = test_zulip_outgoing.tasks(account)
    with contexts.Context().session_manager():
        with pytest.raises(content_conversion.ConversionPendingError):
            second_task.content.prepare(second_task)
    zulip.send_message.assert_not_called()
    history_import.HistoryImportService()._iteration()
    with contexts.Context().session_manager() as session:
        assert second_task.content.prepare(second_task)
        text = session.execute(
            "SELECT zulip_content FROM bridge_zulip_v5_content_snapshots WHERE uuid=%s",
            (second_task.uuid,),
        ).fetchone()["zulip_content"]
    assert text == f"[Earlier](https://zulip.example.com/#narrow/near/{remote['id']})"


@pytest.mark.parametrize("user_ids", [(7, 8), (7, 8, 9)])
def test_direct_and_group_chat_links_use_real_conversations(
    history_account, db, monkeypatch, user_ids
):
    account = history_account
    peers = []
    for user_id in user_ids[1:]:
        owner = uuid.uuid4()
        conftest.seed_workspace_user(db, owner, f"peer-{user_id}-{owner}")
        peer = external_models.ExternalAccount(
            project_id=account.project_id,
            owner_user_uuid=owner,
            provider="zulip",
            external_account_id=f"zulip.example.com:{user_id}",
            settings=account.settings,
        )
        peer.insert()
        peers.append(peer)
    try:
        users = ",".join(map(str, user_ids[1:]))
        url = f"https://zulip.example.com/#narrow/dm/{users}"
        zulip = mock.Mock()
        zulip.get_messages.return_value = [
            test_zulip_messages._direct_message_data(
                user_ids=user_ids, content=f"[Chat]({url})"
            )
        ]
        monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
        assert test_zulip_messages._import_history(account).status == "done"
        _publish(account, db)
        (source,) = test_zulip_messages._messages(account)
        stream_uuid = source.channel.stream_uuid
        for peer in peers:
            conftest.seed_user_stream_binding(
                db, account.project_id, stream_uuid, peer.owner_user_uuid
            )
        assert source.content.workspace_content == f"[Chat](urn:stream:{stream_uuid})"
        with contexts.Context().session_manager():
            text = content_conversion.ConversionContext(account).to_zulip(
                {"kind": "markdown", "content": source.content.workspace_content},
                stream_uuid,
                zulip,
            )
        assert text == f"[Chat]({url}{'-group' if len(user_ids) > 2 else ''})"
    finally:
        for peer in peers:
            peer.delete()


def test_quote_cannot_read_another_users_private_history(
    history_account, db, monkeypatch
):
    account = history_account
    owner = uuid.uuid4()
    conftest.seed_workspace_user(db, owner, f"private-owner-{owner}")
    other = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=owner,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        settings=account.settings,
    )
    other.insert()
    try:
        test_zulip_messages._channel(other, 43)
        zulip = mock.Mock()
        zulip.get_messages.return_value = [
            test_zulip_messages._message_data(
                44, stream_id=43, sender_id=8, content="Private source text"
            )
        ]
        monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
        assert test_zulip_messages._import_history(other).status == "done"
        _publish(other, db)
        source = test_zulip_messages._messages(other)[0]
        canonical = uuid.uuid5(other.project_id, str(source.message_uuid))
        public_uuid = uuid.uuid5(canonical, "zulip-placement")
        with contexts.Context().session_manager():
            converter = content_conversion.ConversionContext(account)
            with pytest.raises(ValueError, match="mapping"):
                converter.to_zulip(
                    {
                        "kind": "markdown",
                        "content": f"[Secret](urn:quote:{public_uuid})",
                    },
                    source.channel.stream_uuid,
                    zulip,
                )
            assert converter.context["messages"] == {}
    finally:
        other.delete()


def test_content_conversion_migration_round_trips(_database):
    target = "0241-Convert-Zulip-message-content-using-persisted-identities-and-files-c8ac7d.py"
    with conftest.isolated_migration_database(target) as url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        with psycopg.connect(url, autocommit=True) as connection:
            assert connection.execute(
                "SELECT to_regclass('bridge_zulip_v5_file_links') IS NOT NULL"
            ).fetchone() == (True,)
            assert connection.execute(
                "SELECT count(*) FROM bridge_zulip_v5_tasks WHERE content->>'kind'='convert_message_content'"
            ).fetchone() == (0,)
            engine.rollback_migration(target)
            assert connection.execute(
                "SELECT to_regclass('bridge_zulip_v5_file_links')"
            ).fetchone() == (None,)
            engine.apply_migration(target)
            assert connection.execute(
                "SELECT to_regclass('bridge_zulip_v5_content_snapshots') IS NOT NULL"
            ).fetchone() == (True,)


@pytest.mark.parametrize("newer_workspace_edit", [False, True])
def test_contextual_quote_repair_restores_saved_content_without_an_edit(
    history_account, api, db, monkeypatch, newer_workspace_edit
):
    account = history_account
    target = "0261-Reconvert-Zulip-quotes-with-recipient-and-channel-context-1adfa4.py"
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(target)
    task = None
    try:
        zulip = mock.Mock()
        zulip.get_messages.return_value = [
            test_zulip_messages._message_data(42, content="Original leaf."),
            test_zulip_messages._message_data(
                43,
                content=(
                    "@_**Alias|7** [said](https://zulip.example.com/#narrow/near/42)"
                    " to @_**Recipient**:\n```quote\nOriginal leaf.\n```"
                ),
            ),
        ]
        monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
        assert test_zulip_messages._import_history(account, last=43).status == "done"
        _publish(account, db)
        leaf, source = test_zulip_messages._messages(account)
        canonical = uuid.uuid5(account.project_id, str(source.message_uuid))
        public = uuid.uuid5(canonical, "zulip-placement")
        expected = source.content.workspace_content
        before = db.execute(
            "SELECT content_updated_at FROM messenger_messages WHERE uuid=%s",
            (canonical,),
        ).fetchone()[0]
        broken = "Legacy anonymous quote"
        with db.transaction():
            db.execute(
                "SELECT set_config('workspace.projected_message_content', %s, true)",
                (f"{account.project_id}:{canonical}",),
            )
            db.execute(
                "UPDATE messenger_messages SET payload=jsonb_build_object('kind','markdown','content',%s::text) WHERE uuid=%s",
                (broken, canonical),
            )
            db.execute(
                "UPDATE bridge_zulip_v5_message_contents SET workspace_content=%s,workspace_content_hash=%s,conversion_version=1 WHERE uuid=%s",
                (
                    broken,
                    hashlib.sha256(broken.encode()).hexdigest(),
                    source.content.uuid,
                ),
            )
        api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
        assert (
            api.get(f"/v1/messenger/messages/{public}").json()["payload"]["content"]
            == broken
        )
        if newer_workspace_edit:
            expected = "A newer Workspace edit."
            response = api.put(
                f"/v1/messenger/messages/{public}",
                json={"payload": {"kind": "markdown", "content": expected}},
            )
            assert response.status_code == 200, response.text
            _publish(account, db)
            before = db.execute(
                "SELECT content_updated_at FROM messenger_messages WHERE uuid=%s",
                (canonical,),
            ).fetchone()[0]
        existing_tasks = set(
            row[0] for row in db.execute("SELECT uuid FROM bridge_zulip_v5_tasks")
        )
        engine.apply_migration(target)
        assert db.execute(
            "SELECT conversion_version FROM bridge_zulip_v5_message_contents WHERE uuid=%s",
            (leaf.content.uuid,),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT conversion_version FROM bridge_zulip_v5_message_contents WHERE uuid=%s",
            (source.content.uuid,),
        ).fetchone() == (0,)
        added = [
            row[0]
            for row in db.execute("SELECT uuid FROM bridge_zulip_v5_tasks")
            if row[0] not in existing_tasks
        ]
        assert len(added) == 1
        task = models.Task.objects.get_one(filters={"uuid": filters.EQ(added[0])})
        assert task.queue == "background" and task.content.batch_size == 100
        with contexts.Context().session_manager():
            task.execute()
            assert task.status == "done"
        _publish(account, db)
        assert (
            api.get(f"/v1/messenger/messages/{public}").json()["payload"]["content"]
            == expected
        )
        assert db.execute(
            "SELECT content_updated_at FROM messenger_messages WHERE uuid=%s",
            (canonical,),
        ).fetchone() == (before,)
    finally:
        engine.apply_migration(target)
        if task is not None:
            task.delete()


@pytest.mark.parametrize(
    "malformed",
    [
        "https://[broken/user_uploads/file.pdf",
        "<https://broken]/user_uploads/file.pdf>",
        "[file](https://[not-an-ip]/user_uploads/file.pdf)",
        '![image](<https://[broken/user_uploads/file.png> "Image")',
    ],
)
def test_malformed_links_import_and_reconvert_without_retry(
    history_account, db, monkeypatch, malformed
):
    account = history_account
    raw = f"{malformed}\n@**Alias|7** :sunset:"
    expected = f"{malformed}\n[Alias](urn:user:{account.owner_user_uuid}) 🌆"
    zulip = mock.Mock()
    zulip.get_messages.return_value = [test_zulip_messages._message_data(content=raw)]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)

    assert test_zulip_messages._import_history(account).status == "done"
    (source,) = test_zulip_messages._messages(account)
    assert source.content.workspace_content == expected
    db.execute(
        "UPDATE bridge_zulip_v5_message_contents SET workspace_content=zulip_content,conversion_version=0 WHERE uuid=%s",
        (source.content.uuid,),
    )
    task = models.Task(
        content=models.ConvertMessageContent(content_uuid=source.content.uuid)
    )
    task.insert()
    try:
        with contexts.Context().session_manager():
            task.execute()
            assert task.status == "done"
        (source,) = test_zulip_messages._messages(account)
        assert source.content.workspace_content == expected
        assert source.content.conversion_version == content_conversion.VERSION
        zulip.download_file.assert_not_called()
    finally:
        task.delete()


@pytest.mark.parametrize(
    "target",
    [
        "urn:topic:" + "a" * 36,
        "urn:message:" + "-" * 36,
        "urn:quote:" + "a" * 36,
        "urn:message:11111111-1111-4111-8111-111111111111-extra",
        "urn:forward:11111111-1111-4111-8111-111111111111",
        "urn:forward:11111111-1111-4111-8111-111111111111?snapshot=invalid!",
        "urn:forward:11111111-1111-4111-8111-111111111111?snapshot=_w",
    ],
)
def test_malformed_workspace_references_export_as_text(
    history_account, monkeypatch, target
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = [test_zulip_messages._message_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert test_zulip_messages._import_history(account).status == "done"
    (source,) = test_zulip_messages._messages(account)
    original = f'[broken](<{target}> "Keep this")'
    payload = {
        "kind": "markdown",
        "content": f"{original}\n[Alias](urn:user:{account.owner_user_uuid})",
    }

    with contexts.Context().session_manager():
        text = content_conversion.ConversionContext(account).to_zulip(
            payload, source.channel.stream_uuid, zulip
        )
    prefix, mention = text.split("\n")
    assert prefix == original
    assert mention.startswith("@**") and mention.endswith("|7**")
    zulip.upload_file.assert_not_called()


def test_failed_reconversion_does_not_block_other_messages(
    history_account, db, monkeypatch
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = [
        test_zulip_messages._message_data(value, content="@**Alias|7**")
        for value in (42, 43)
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert test_zulip_messages._import_history(account).status == "done"
    sources = test_zulip_messages._messages(account)
    db.execute(
        "UPDATE bridge_zulip_v5_message_contents SET conversion_version=0 WHERE uuid=ANY(%s)",
        ([source.content.uuid for source in sources],),
    )
    convert = content_conversion.ConversionContext.to_workspace

    def fail_one(converter, data, stream_uuid, zulip):
        if data["id"] == 42:
            raise ValueError("Temporary conversion failure")
        return convert(converter, data, stream_uuid, zulip)

    previous = {
        task.uuid
        for task in models.Task.objects.get_all(
            filters={"project_id": filters.Is(None)}
        )
    }
    task = models.Task(content=models.ConvertMessageContent())
    task.insert()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                content_conversion.ConversionContext, "to_workspace", fail_one
            )
            with contexts.Context().session_manager():
                task.execute()
                assert task.status == "done"
        versions = db.execute(
            "SELECT conversion_version FROM bridge_zulip_v5_message_contents WHERE uuid=ANY(%s) ORDER BY uuid",
            ([source.content.uuid for source in sources],),
        ).fetchall()
        assert sorted(value[0] for value in versions) == [0, 1]
        retry = [
            row
            for row in models.Task.objects.get_all(
                filters={"project_id": filters.Is(None)}
            )
            if row.content.kind == "convert_message_content"
            and row.content.content_uuid == sources[0].content.uuid
        ][0]
        with contexts.Context().session_manager():
            retry.execute()
            assert retry.status == "done"
    finally:
        for pending in models.Task.objects.get_all(
            filters={"project_id": filters.Is(None)}
        ):
            if pending.uuid not in previous:
                pending.delete()


def test_link_resolution_does_not_walk_the_linked_conversation(
    history_account, monkeypatch
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = [
        test_zulip_messages._message_data(
            value,
            content=f"[Previous](https://zulip.example.com/#narrow/near/{value - 1})",
        )
        for value in range(40, 47)
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert (
        test_zulip_messages._import_history(account, first=40, last=46).status == "done"
    )
    with contexts.Context().session_manager() as session:
        with mock.patch.object(session, "execute", wraps=session.execute) as execute:
            converted = content_conversion.ConversionContext(
                account, session
            ).to_workspace(
                {
                    **test_zulip_messages._message_data(100),
                    "content": "[Latest](https://zulip.example.com/#narrow/near/46)",
                },
                test_zulip_messages._messages(account)[0].channel.stream_uuid,
                zulip,
            )
            assert converted.startswith("[Latest](urn:message:")
            assert execute.call_count <= 6


def test_attachment_export_uses_live_membership_and_allows_public_files(
    outgoing, db, monkeypatch, tmp_path
):
    api, account, channel, _remote, zulip, _constructor = outgoing
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    files = []
    try:
        source_stream = conftest.seed_user_stream(
            db, account.project_id, account.owner_user_uuid, "source-files"
        )
        private = api.post(
            "/v1/messenger/files/",
            data={"stream_uuid": source_stream},
            files={"file": ("private.txt", b"private", "text/plain")},
        )
        assert private.status_code == 201, private.text
        files.append(private.json()["uuid"])
        db.execute(
            "UPDATE messenger_stream_bindings SET active=false WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
            (account.project_id, source_stream, account.owner_user_uuid),
        )
        with contexts.Context().session_manager():
            with pytest.raises(ValueError, match="not accessible"):
                content_conversion.ConversionContext(account).to_zulip(
                    {"kind": "markdown", "content": f"[Private](urn:file:{files[0]})"},
                    channel.stream_uuid,
                    zulip,
                )
        zulip.upload_file.assert_not_called()
        api.project_id = uuid.uuid4()
        public = api.post(
            "/v1/messenger/files/",
            data={"acl": '{"mode":"public"}'},
            files={"file": ("public.txt", b"public", "text/plain")},
        )
        assert public.status_code == 201, public.text
        files.append(public.json()["uuid"])
        api.project_id = account.project_id
        zulip.upload_file.return_value = "/user_uploads/1/hash/public.txt"
        with contexts.Context().session_manager():
            text = content_conversion.ConversionContext(account).to_zulip(
                {"kind": "markdown", "content": f"[Public](urn:file:{files[1]})"},
                channel.stream_uuid,
                zulip,
            )
        assert text == "[Public](/user_uploads/1/hash/public.txt)"
        zulip.upload_file.assert_called_once_with("public.txt", b"public", "text/plain")
    finally:
        api.project_id = account.project_id
        for file_uuid in files:
            file = messenger_models.WorkspaceFile.objects.get_one(
                filters={"uuid": filters.EQ(file_uuid)}
            )
            file.delete()
        assert db.execute(
            "SELECT count(*) FROM bridge_zulip_v5_file_links WHERE project_id=%s",
            (account.project_id,),
        ).fetchone() == (0,)


def test_unverified_import_waits_instead_of_guessing_user_identity(
    history_account, monkeypatch
):
    account = history_account
    account.properties["external_account_id"].set_value_force(None)
    account.update()
    constructor = mock.Mock()
    monkeypatch.setattr(client, "ZulipClient", constructor)
    task = test_zulip_messages._import_history(account)
    assert task.status == "retry"
    constructor.assert_not_called()
    assert test_zulip_messages._messages(account) == []


def test_editing_imported_content_preserves_its_workspace_representation(
    history_account, api, db, monkeypatch, scheduled_reconciliation
):
    account = history_account
    account.status = "active"
    account.settings = {
        **account.settings,
        "email": "sender@example.com",
        "api_key": "sender-key",
    }
    account.update()
    remote = test_zulip_messages._message_data(content="Original")
    zulip = mock.Mock()
    zulip.get_messages.side_effect = lambda first, last: [dict(remote)]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert test_zulip_messages._import_history(account).status == "done"
    _publish(account, db)
    api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
    public = api.get("/v1/messenger/messages/").json()[0]
    original = f"Edited [My label](urn:user:{account.owner_user_uuid}) :sunset:"
    response = api.put(
        f"/v1/messenger/messages/{public['uuid']}",
        json={"payload": {"kind": "markdown", "content": original}},
    )
    assert response.status_code == 200, response.text
    remote["last_edit_timestamp"] = (
        int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 10
    )
    zulip.edit_message.side_effect = lambda message_id, text: remote.update(
        content=text
    )
    test_zulip_outgoing.dispatch()
    history_import.HistoryImportService()._iteration()
    transmitted = zulip.edit_message.call_args.args[1]
    assert "urn:" not in transmitted and ":city_sunrise:" in transmitted
    for _ in range(2):
        assert test_zulip_messages._import_history(account).status == "done"
        _publish(account, db)
        assert (
            api.get(f"/v1/messenger/messages/{public['uuid']}").json()["payload"][
                "content"
            ]
            == original
        )
        assert (
            test_zulip_messages._messages(account)[0].content.workspace_content
            == original
        )
    remote["content"] = "External :sunset:"
    remote["last_edit_timestamp"] += 1
    assert test_zulip_messages._import_history(account).status == "done"
    assert (
        api.get(f"/v1/messenger/messages/{public['uuid']}").json()["payload"]["content"]
        == "External 🌆"
    )
    remote["content"] = transmitted
    remote["last_edit_timestamp"] += 1
    assert test_zulip_messages._import_history(account).status == "done"
    assert (
        api.get(f"/v1/messenger/messages/{public['uuid']}").json()["payload"]["content"]
        == original
    )
    owner = uuid.uuid4()
    conftest.seed_workspace_user(db, owner, f"observer-{owner}")
    observer = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=owner,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        status="active",
        settings=account.settings,
    )
    observer.insert()
    try:
        test_zulip_messages._channel(observer)
        account.settings = {
            key: value for key, value in account.settings.items() if key != "api_key"
        }
        account.disabled = True
        account.update()
        remote["content"] = "Observed external edit :sunset:"
        remote["last_edit_timestamp"] += 1
        assert test_zulip_messages._import_history(observer).status == "done"
        assert (
            api.get(f"/v1/messenger/messages/{public['uuid']}").json()["payload"][
                "content"
            ]
            == "Observed external edit 🌆"
        )
    finally:
        observer.delete()
