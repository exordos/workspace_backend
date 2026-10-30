# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import uuid
from unittest import mock

from workspace.services import zulip_history


def test_same_provider_in_different_projects_keeps_both_running_tasks(monkeypatch):
    provider = uuid.uuid4()
    jobs = [
        {"uuid": uuid.uuid4(), "project_id": uuid.uuid4(), "provider_uuid": provider}
        for _ in range(2)
    ]

    async def run():
        service = zulip_history.ZulipHistoryService("postgresql:///unused")
        service.db = mock.Mock(
            open=mock.AsyncMock(),
            close=mock.AsyncMock(),
            sql=mock.AsyncMock(return_value=jobs),
        )
        observed = []
        callback_errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: callback_errors.append(context)
        )

        async def step(account):
            observed.append((account["project_id"], len(service.tasks)))
            if len(observed) == 2:
                service.stopping.set()
                service.ready.set()
            await asyncio.sleep(0)

        monkeypatch.setattr(service, "step", step)
        monkeypatch.setattr(
            zulip_history.zulip_outgoing,
            "Http",
            mock.Mock(return_value=mock.Mock(close=mock.AsyncMock())),
        )
        await asyncio.wait_for(service.run(), 2)
        assert {project for project, _count in observed} == {
            job["project_id"] for job in jobs
        }
        assert {count for _project, count in observed} == {2}
        assert not callback_errors
        assert not service.tasks

    asyncio.run(run())
