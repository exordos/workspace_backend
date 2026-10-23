# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import asyncio
import contextlib
import threading

from workspace.services import zulip_realtime


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
