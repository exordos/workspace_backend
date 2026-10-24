# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import datetime
from unittest import mock

import pytest

from workspace.bridges.zulip import outgoing_operations


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


def test_ambiguous_send_without_a_receipt_is_never_reposted():
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
    with pytest.raises(outgoing_operations.Pending):
        asyncio.run(operations.execute({}, {"uuid": "account"}, prepared, True))
    assert http.request.call_args.args[1:3] == ("GET", "/events")


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
