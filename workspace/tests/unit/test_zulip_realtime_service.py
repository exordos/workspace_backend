# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import asyncio
import contextlib
import threading
import types
from unittest import mock

import pytest
from aiohttp import web

from workspace.services import zulip_realtime


def test_event_health_probe_uses_http_and_the_same_unacknowledged_cursor():
    async def scenario():
        release = asyncio.Event()
        queries = []
        event = {"id": 8, "type": "message", "message": {"id": 42}}

        async def events(request):
            queries.append(dict(request.query))
            if request.query["dont_block"] == "false":
                await release.wait()
            return web.json_response({"result": "success", "events": [event]})

        app = web.Application()
        app.router.add_get("/api/v1/events", events)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        account = mock.Mock(
            settings={
                "server_url": f"http://127.0.0.1:{port}",
                "email": "test@example.com",
                "api_key": "test-key",
            }
        )
        provider = zulip_realtime.client.AsyncZulipClient(account)
        subscription = types.SimpleNamespace(
            queue_id="queue", last_event_id=7, longpoll_timeout=90
        )
        try:
            result = await asyncio.wait_for(
                zulip_realtime.ZulipRealtimeService._receive_events(
                    provider, subscription
                ),
                1.5,
            )
            assert result == [event]
            assert [q["dont_block"] for q in queries] == ["false", "true"]
            assert all(
                q["queue_id"] == "queue" and q["last_event_id"] == "7" for q in queries
            )
        finally:
            release.set()
            await provider.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_expired_queue_from_health_probe_does_not_wait_for_a_stalled_poll():
    async def scenario():
        class Provider:
            async def get_events(self, *args, dont_block=False):
                if dont_block:
                    raise zulip_realtime.client.ZulipApiError(
                        "BAD_EVENT_QUEUE_ID", "expired"
                    )
                await asyncio.Event().wait()

        subscription = types.SimpleNamespace(
            queue_id="queue", last_event_id=7, longpoll_timeout=90
        )
        with pytest.raises(zulip_realtime.client.ZulipApiError, match="expired"):
            await asyncio.wait_for(
                zulip_realtime.ZulipRealtimeService._receive_events(
                    Provider(), subscription
                ),
                0.9,
            )

    asyncio.run(scenario())


def test_stalled_longpoll_does_not_hide_ready_provider_events(monkeypatch):
    async def scenario():
        service = zulip_realtime.ZulipRealtimeService("unused")
        committed = asyncio.Event()
        calls = []
        event = {"id": 1, "type": "message", "message": {"id": 42}}
        subscription = types.SimpleNamespace(
            queue_id="queue",
            last_event_id=0,
            longpoll_timeout=90,
            account_revision="revision",
            generation="generation",
        )
        account = mock.Mock()
        account.subscription_revision.return_value = "revision"

        class Provider:
            async def get_events(self, queue, cursor, timeout, *, dont_block=False):
                calls.append((queue, cursor, dont_block))
                if dont_block:
                    return [event]
                await asyncio.Event().wait()

            async def close(self):
                pass

        async def database(callback, *args):
            if callback == zulip_realtime.models.RealtimeSubscription.for_account:
                return subscription
            assert callback == zulip_realtime.models.RealtimeSubscription.append_events
            assert args[2] == [event]
            committed.set()
            return 1

        service._database = database
        monkeypatch.setattr(
            zulip_realtime.client, "AsyncZulipClient", lambda account: Provider()
        )
        poller = asyncio.create_task(service._poll_account(account))
        try:
            await asyncio.wait_for(committed.wait(), 0.9)
            assert ("queue", 0, True) in calls
        finally:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
            service._executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_blocked_inbox_batches_leave_database_capacity_for_new_sends():
    async def scenario():
        service = zulip_realtime.ZulipRealtimeService("unused")
        release = threading.Event()
        started = [threading.Event() for _ in range(4)]

        def blocked(index):
            started[index].set()
            release.wait(3)

        loop = asyncio.get_running_loop()
        jobs = [loop.run_in_executor(service._executor, blocked, i) for i in range(4)]
        try:
            while not all(event.is_set() for event in started):
                await asyncio.sleep(0.001)
            ready = loop.run_in_executor(service._executor, lambda: "send")
            assert await asyncio.wait_for(ready, 0.2) == "send"
        finally:
            release.set()
            await asyncio.gather(*jobs)
            service._executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_ready_inbox_work_does_not_wait_for_another_account():
    async def scenario():
        service = zulip_realtime.ZulipRealtimeService("unused")
        blocked = asyncio.Event()
        release = asyncio.Event()
        progressed = asyncio.Event()
        calls = 0

        async def database(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                blocked.set()
                await release.wait()
                return 1
            if calls > zulip_realtime.DATABASE_WORKERS:
                progressed.set()
                return 0
            return 1

        service._database = database
        consumer = asyncio.create_task(service._consume_events())
        try:
            await asyncio.wait_for(blocked.wait(), 1)
            await asyncio.wait_for(progressed.wait(), 0.2)
        finally:
            release.set()
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
            service._executor.shutdown(wait=True)

    asyncio.run(scenario())
