# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
from unittest import mock

import psycopg
import pytest

from workspace.services import zulip_incoming
from workspace.services import zulip_outgoing


@pytest.mark.parametrize("direction", ["incoming", "outgoing"])
def test_dispatch_does_not_reclaim_work_immediately_when_retry_cannot_be_persisted(
    direction,
):
    async def run():
        service = (
            zulip_incoming.ZulipIncomingService
            if direction == "incoming"
            else zulip_outgoing.ZulipOutgoingService
        )("postgresql://unused", poll_interval=0.01)
        item = {
            "id": 1,
            "external_account_uuid": "account",
            "project_id": "project",
            "request": None,
            "payload": {},
            "attempts": 0,
        }
        selected = (
            zulip_incoming.SELECT_EVENT_SQL
            if direction == "incoming"
            else zulip_outgoing.SELECT_READY_INTENTS_SQL
        )
        writes = []

        async def sql(query, parameters=()):
            if query == selected:
                return [item]
            writes.append(query)
            raise psycopg.errors.ReadOnlySqlTransaction("database is recovering")

        service.db.sql = sql
        try:
            await service.dispatch()
            task = service.tasks[1]
            await asyncio.sleep(0.03)
            # The failed retry must pause inside the coroutine, retaining its
            # dispatcher slot rather than immediately selecting the same row.
            assert not task.done()
            assert 1 in service.tasks
            assert len(writes) == 2  # account lookup and failed retry write
        finally:
            tasks = list(service.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_outgoing_read_only_database_is_transient_and_does_not_fail_the_intention():
    async def run():
        service = zulip_outgoing.ZulipOutgoingService("postgresql://unused")
        account = {"external_account_id": "7", "provider_uuid": "provider"}
        intent = {
            "id": 1,
            "request": None,
            "external_account_uuid": "account",
            "project_id": "project",
            "data": {},
        }
        writes = []

        async def sql(query, parameters=()):
            if query == zulip_outgoing.SELECT_ACCOUNT_SQL:
                return [account]
            writes.append((query, parameters))
            return []

        service.db.sql = sql
        service.operations = mock.Mock(
            prepare=mock.AsyncMock(
                side_effect=psycopg.errors.ReadOnlySqlTransaction(
                    "database is recovering"
                )
            ),
            status=mock.AsyncMock(),
        )
        await service.process(intent)
        assert any(
            query == zulip_outgoing.RETRY_INTENT_SQL and parameters[-2] == 1
            for query, parameters in writes
        )
        assert not any(query == zulip_outgoing.FAIL_INTENT_SQL for query, _ in writes)
        service.operations.status.assert_not_awaited()

    asyncio.run(run())
