# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
from unittest import mock

import pytest

from workspace.bridges.zulip import outgoing_operations


@pytest.mark.parametrize(
    "flags", [{"read": True}, {"starred": False}, {"read": False, "starred": True}]
)
def test_sparse_flag_intentions_do_not_require_a_state_row(flags):
    database = mock.Mock(sql=mock.AsyncMock())
    operations = outgoing_operations.Operations(database, mock.Mock())
    intent = {
        "entity_type": "message_state",
        "project_id": "project",
        "entity_uuid": "binding",
        "id": 9,
        "operation": "update",
        "data": {"remote_message_identity": "123", "flags": flags},
    }
    prepared = asyncio.run(operations.prepare(intent, {"external_account_id": "7"}))
    assert prepared["kind"] == "flags"
    assert prepared["message_id"] == 123
    assert prepared["flags"] == flags
    database.sql.assert_not_awaited()


@pytest.mark.parametrize(
    "before,read_at,starred,expected",
    [
        ({"read_at": None, "starred": True}, "2026-10-29", True, {"read": True}),
        (
            {"read_at": "2026-10-28", "starred": False},
            None,
            True,
            {"read": False, "starred": True},
        ),
    ],
)
def test_pre_migration_flag_intentions_keep_the_captured_transition(
    before, read_at, starred, expected
):
    database = mock.Mock(sql=mock.AsyncMock())
    operations = outgoing_operations.Operations(database, mock.Mock())
    intent = {
        "entity_type": "message_state",
        "project_id": "project",
        "entity_uuid": "binding",
        "id": 9,
        "operation": "update",
        "data": {
            "remote_message_identity": "123",
            "before": before,
            "read_at": read_at,
            "starred": starred,
        },
    }
    prepared = asyncio.run(operations.prepare(intent, {"external_account_id": "7"}))
    assert prepared["flags"] == expected


def test_finishing_flag_delivery_only_completes_the_durable_intention():
    database = mock.Mock(sql=mock.AsyncMock())
    operations = outgoing_operations.Operations(database, mock.Mock())
    asyncio.run(
        operations.finish(
            {
                "entity_type": "message_state",
                "id": 9,
                "project_id": "project",
                "entity_uuid": "binding",
            },
            {},
            {"kind": "flags"},
            {},
        )
    )
    database.sql.assert_awaited_once_with(outgoing_operations.COMPLETE_INTENT_SQL, (9,))
