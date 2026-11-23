# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import uuid
from unittest import mock

import pytest

from workspace.bridges.zulip import incoming_operations


@pytest.mark.parametrize(
    ("source", "identity", "revoked"),
    [("zulip", "42:7", True), ("zulip", "42:8", False), ("native", None, False)],
)
def test_stream_delete_event_revokes_only_receiving_accounts_binding(
    source, identity, revoked
):
    account = {
        "project_id": uuid.uuid4(),
        "provider_uuid": uuid.uuid4(),
        "owner_user_uuid": uuid.uuid4(),
        "external_account_id": "7",
    }
    event = {
        "type": "stream",
        "op": "delete",
        "streams": [{"stream_id": 42}],
        "stream_ids": [42],
    }
    stream = {"uuid": uuid.uuid4(), "provider_identity": "42"}
    binding = {
        "active": True,
        "source_name": source,
        "provider_uuid": account["provider_uuid"],
        "provider_identity": identity,
    }
    connection = mock.Mock()
    http = mock.Mock(request=mock.AsyncMock(return_value={"streams": []}))
    operations = incoming_operations.Operations(
        mock.Mock(sql=mock.AsyncMock(return_value=[])), http
    )
    operations.identity = mock.AsyncMock(return_value=stream["uuid"])
    operations.bind = mock.AsyncMock()

    async def run():
        with mock.patch.object(
            incoming_operations, "rows", side_effect=[[stream], [binding]]
        ):
            prepared = await operations.prepare(account, event)
            await operations.apply(connection, account, prepared)

    connection.execute = mock.AsyncMock()
    asyncio.run(run())

    http.request.assert_awaited_once_with(
        account,
        "GET",
        "/streams",
        {"include_all": True, "exclude_archived": False},
    )
    if revoked:
        operations.bind.assert_awaited_once_with(
            connection,
            account,
            stream,
            account["owner_user_uuid"],
            account["external_account_id"],
            active=False,
        )
    else:
        operations.bind.assert_not_awaited()
    connection.execute.assert_not_awaited()
