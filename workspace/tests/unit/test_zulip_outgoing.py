# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
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
        {"queue_id": "queue"},
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
    operations = outgoing_operations.Operations(mock.Mock(), http)
    prepared = outgoing_operations.request(
        "edit", "PATCH", "/messages/123", {"content": "already updated"}
    )
    assert asyncio.run(operations.execute({}, {}, prepared, True)) == {
        "identity": "123"
    }
    http.request.assert_awaited_once_with(
        {}, "GET", "/messages/123", {"apply_markdown": False}
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
