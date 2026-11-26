# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import datetime
import hashlib
import uuid
from unittest import mock

import pytest

from workspace.bridges.zulip import content_cache
from workspace.bridges.zulip import outgoing_operations
from workspace.messenger_api import sticker_storage


@pytest.mark.parametrize("age", [3599, 3600, 3601])
@pytest.mark.parametrize("offset", [None, 0, 3])
def test_message_send_window_uses_message_creation_time(age, offset):
    now = datetime.datetime(2026, 11, 6, 12, tzinfo=datetime.timezone.utc)
    created = now - datetime.timedelta(seconds=age)
    created = (
        created.replace(tzinfo=None)
        if offset is None
        else created.astimezone(datetime.timezone(datetime.timedelta(hours=offset)))
    )

    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    http = mock.Mock(request=mock.AsyncMock(return_value={"id": 123}))
    operations = outgoing_operations.Operations(mock.Mock(), http)
    prepared = outgoing_operations.request(
        "send", "POST", "/messages", {"content": "marker"}
    )
    # Recreating the intention must not renew the message's send window.
    intent = {
        "created_at": now,
        "data": {"created_at": created.isoformat()},
    }
    with mock.patch.object(outgoing_operations.datetime, "datetime", Clock):
        if age > 3600:
            with pytest.raises(ValueError, match="one-hour.*expired"):
                asyncio.run(operations.execute(intent, {}, prepared, False))
            http.request.assert_not_called()
        else:
            assert asyncio.run(operations.execute(intent, {}, prepared, False)) == {
                "identity": "123"
            }
            http.request.assert_awaited_once()


@pytest.mark.parametrize("status,absent", [(404, True), (403, False), (500, False)])
def test_delete_only_accepts_absent_entities(status, absent):
    db = mock.Mock(sql=mock.AsyncMock())
    http = mock.Mock(
        request=mock.AsyncMock(side_effect=outgoing_operations.ApiError(status, {}))
    )
    operations = outgoing_operations.Operations(db, http)
    prepared = outgoing_operations.request("delete", "DELETE", "/messages/123")
    if absent:
        assert asyncio.run(operations.execute({}, {}, prepared, False)) == {}
    else:
        with pytest.raises(outgoing_operations.ApiError):
            asyncio.run(operations.execute({}, {}, prepared, False))


def test_ambiguous_send_without_a_receipt_requests_a_scheduled_resend():
    db = mock.Mock(sql=mock.AsyncMock(side_effect=[[{"zulip_message_id": None}], []]))
    http = mock.Mock(request=mock.AsyncMock(return_value={"events": []}))
    operations = outgoing_operations.Operations(db, http)
    prepared = outgoing_operations.request(
        "send",
        "POST",
        "/messages",
        {"queue_id": "queue", "local_id": "workspace-message"},
        delivery_uuid="receipt",
        last_event_id=0,
    )
    with pytest.raises(outgoing_operations.RetrySend):
        asyncio.run(
            operations.execute(
                {"entity_uuid": "workspace-message"},
                {"uuid": "account"},
                prepared,
                True,
            )
        )
    assert http.request.call_args.args[1:3] == ("GET", "/events")


@pytest.mark.parametrize(
    "status,data,expected",
    [
        (400, {"code": "BAD_EVENT_QUEUE_ID"}, True),
        (400, {"msg": "An event newer than 7 has already been pruned!"}, True),
        (400, {"msg": "Invalid message content"}, False),
        (403, {"msg": "Forbidden"}, False),
        (500, {}, False),
    ],
)
def test_receipt_queue_errors_are_distinct_from_permanent_send_errors(
    status, data, expected
):
    assert (
        outgoing_operations.lost_queue(outgoing_operations.ApiError(status, data))
        is expected
    )


def test_retry_verifies_edit_before_repeating_the_write():
    http = mock.Mock(
        request=mock.AsyncMock(return_value={"message": {"content": "already updated"}})
    )
    operations = outgoing_operations.Operations(
        mock.Mock(sql=mock.AsyncMock(return_value=[])), http
    )
    prepared = outgoing_operations.request(
        "edit", "PATCH", "/messages/123", {"content": "already updated"}
    )
    assert asyncio.run(
        operations.execute(
            {"project_id": "project", "entity_uuid": "message"},
            {"provider_uuid": "provider"},
            prepared,
            True,
        )
    ) == {"identity": "123"}
    http.request.assert_awaited_once_with(
        {"provider_uuid": "provider"}, "GET", "/messages/123", {"apply_markdown": False}
    )


def test_flags_are_sent_as_personal_idempotent_operations():
    http = mock.Mock(request=mock.AsyncMock(return_value={"result": "success"}))
    operations = outgoing_operations.Operations(mock.Mock(), http)
    prepared = outgoing_operations.request(
        "flags",
        "POST",
        "/messages/flags",
        message_id=123,
        flags={"read": True, "starred": False},
    )
    asyncio.run(operations.execute({}, {}, prepared, False))
    assert [call.args[3] for call in http.request.call_args_list] == [
        {"messages": [123], "flag": "read", "op": "add"},
        {"messages": [123], "flag": "starred", "op": "remove"},
    ]


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("remote_present", [False, True])
@pytest.mark.parametrize("reaction_type", ["unicode_emoji", "realm_emoji"])
def test_reaction_retry_verifies_the_prepared_reaction_type(
    present, remote_present, reaction_type
):
    http = mock.Mock(
        request=mock.AsyncMock(
            return_value={
                "message": {
                    "reactions": [
                        {
                            "user_id": 7,
                            "emoji_code": "42",
                            "reaction_type": reaction_type,
                        }
                    ]
                    if remote_present
                    else []
                }
            }
        )
    )
    operations = outgoing_operations.Operations(mock.Mock(), http)
    prepared = {
        "kind": "reaction",
        "path": "/messages/101/reactions",
        "user_id": 7,
        "present": present,
        "data": {"emoji_code": "42", "reaction_type": reaction_type},
    }
    done, _ = asyncio.run(operations.verify({}, {}, prepared))
    assert done is (present == remote_present)


@pytest.mark.parametrize("checking", [False, True])
def test_saved_edit_never_overwrites_a_newer_provider_change(checking):
    database = mock.Mock(
        sql=mock.AsyncMock(
            return_value=[
                {
                    "content_updated_at": datetime.datetime(
                        2026, 10, 24, 12, 0, 0, 900000
                    )
                }
            ]
        )
    )
    http = mock.Mock(
        request=mock.AsyncMock(return_value={"message": {"content": "newer"}})
    )
    operations = outgoing_operations.Operations(database, http)
    prepared = outgoing_operations.request(
        "edit", "PATCH", "/messages/123", {"content": "stale"}, identity="123"
    )
    intent = {
        "project_id": "project",
        "entity_uuid": "message",
        "data": {"content_updated_at": "2026-10-24T12:00:00.800000"},
    }
    assert asyncio.run(
        operations.execute(intent, {"provider_uuid": "provider"}, prepared, checking)
    ) == {"identity": "123"}
    http.request.assert_not_called()


def _sticker_content(content: bytes) -> dict:
    return {
        "uuid": uuid.uuid4(),
        "format": "png",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_object_id": "sticker/media.png",
    }


def test_upload_retry_does_not_reuse_an_attachment_with_different_bytes(monkeypatch):
    content = b"sticker"
    sticker = _sticker_content(content)
    storage = mock.Mock(read=mock.Mock(return_value=content))
    monkeypatch.setattr(sticker_storage, "get_sticker_storage", lambda: storage)
    account = {"settings": {"server_url": "https://zulip.example.com"}}
    path = "/user_uploads/1/data/correct.png"
    http = mock.Mock(
        request=mock.AsyncMock(
            return_value={"uri": "https://zulip.example.com" + path}
        ),
        download=mock.AsyncMock(return_value=(b"not-img", "image/png")),
    )
    operations = outgoing_operations.Operations(mock.Mock(), http)

    assert (
        asyncio.run(
            operations._sticker_upload(
                account,
                sticker,
                "sticker.png",
                [{"name": "sticker.png", "size": 7, "path_id": "1/data/forged.png"}],
            )
        )
        == path
    )
    http.download.assert_awaited_once_with(account, "/user_uploads/1/data/forged.png")
    http.request.assert_awaited_once_with(
        account, "POST", "/user_uploads", file=("sticker.png", content, "image/png")
    )


@pytest.mark.parametrize(
    "error, retryable",
    [
        (sticker_storage.StickerStorageNotFoundError, False),
        (sticker_storage.StickerStorageBackendError, True),
    ],
)
def test_sticker_storage_missing_is_not_a_transient_failure(
    monkeypatch, error, retryable
):
    store = mock.Mock(read=mock.Mock(side_effect=error("unavailable")))
    monkeypatch.setattr(sticker_storage, "get_sticker_storage", lambda: store)
    http = mock.Mock(request=mock.AsyncMock())
    operations = outgoing_operations.Operations(mock.Mock(), http)

    convert = operations._sticker_upload(
        {}, _sticker_content(b"sticker"), "test.png", []
    )
    if retryable:
        with pytest.raises(
            outgoing_operations.Pending, match="temporarily unavailable"
        ):
            asyncio.run(convert)
    else:
        assert asyncio.run(convert) is None
    http.request.assert_not_called()


@pytest.mark.parametrize(
    ("example", "uploads"),
    [
        ("Example: `{link}`", 0),
        ("***\n    {link}", 0),
        ("`\n***\n{link}\n`", 1),
    ],
)
def test_only_rendered_stickers_upload_catalog_media(monkeypatch, example, uploads):
    media = b"sticker"
    sticker = _sticker_content(media)
    reference = f"![sticker](urn:sticker:{sticker['uuid']})"
    source = example.format(link=reference)
    marker = f"![workspace-sticker:v1:{sticker['uuid']}](/user_uploads/1/a/sticker.png)"
    store = mock.Mock(read=mock.Mock(return_value=media))
    monkeypatch.setattr(sticker_storage, "get_sticker_storage", lambda: store)
    http = mock.Mock(
        request=mock.AsyncMock(return_value={"uri": "/user_uploads/1/a/sticker.png"})
    )
    content_uuid = uuid.uuid4()

    async def sql(statement, params):
        if statement == outgoing_operations.SELECT_STICKERS_SQL:
            return [sticker]
        if statement == content_cache.RESOLVE_SQL:
            return [{"uuid": content_uuid}]
        return []

    operations = outgoing_operations.Operations(mock.Mock(sql=sql), http)
    result = asyncio.run(
        operations.convert(
            {
                "uuid": uuid.uuid4(),
                "attempts": 0,
                "data": {"payload": {"kind": "markdown", "content": source}},
            },
            {
                "provider_uuid": uuid.uuid4(),
                "settings": {"server_url": "https://zulip.example.test"},
            },
        )
    )

    assert http.request.await_count == store.read.call_count == uploads
    assert result == {
        "uuid": content_uuid,
        "provider_content": source.replace(reference, marker) if uploads else source,
    }
