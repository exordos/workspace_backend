# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Opt-in comparisons on disposable test data; never contact a real Zulip server."""

import ast
import concurrent.futures
import json
import os
import pathlib
import statistics
import subprocess
import threading
import time
from unittest import mock
import uuid

import pytest

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.services import history_import
from workspace.tests.integration import test_zulip_messages as message_tests
from workspace.tests.integration import test_zulip_task_workers as worker_tests


history_account = message_tests.history_account
REPORT = os.environ.get("WORKSPACE_ZULIP_BENCHMARK_REPORT")
pytestmark = pytest.mark.skipif(not REPORT, reason="Opt-in database/worker benchmark")


def _baseline_conversion():
    reference = os.environ.get("WORKSPACE_ZULIP_BENCHMARK_BASELINE", "e431b88")
    source = subprocess.run(
        ["git", "show", f"{reference}:workspace/bridges/zulip/dm/models.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    cls = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "ConvertMessageContent"
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute"
    )
    namespace = dict(vars(models))
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
            "<baseline-conversion>",
            "exec",
        ),
        namespace,
    )
    return reference, namespace["execute"]


def test_compare_conversion_and_worker_isolation(
    history_account, db, monkeypatch, scheduled_reconciliation
):
    account = history_account
    reference, baseline = _baseline_conversion()
    original = models.ConvertMessageContent.execute
    count = 1000
    remote = [
        message_tests._message_data(
            i,
            timestamp=1691057093 + i,
            content=f"@**Owner|7** [linked](https://zulip.example.com/#narrow/near/{max(1, i - 1)}) **message {i}** :sunset:",
        )
        for i in range(1, count + 1)
    ]
    zulip = mock.Mock()
    zulip.get_messages.side_effect = lambda first, last: [
        message for message in remote if first <= message["id"] <= last
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    for first in range(1, count + 1, 100):
        assert (
            message_tests._import_history(account, first, first + 99).status == "done"
        )
    content_ids = [
        row[0]
        for row in db.execute(
            "SELECT content FROM bridge_zulip_v5_messages WHERE project_id=%s",
            (account.project_id,),
        )
    ]
    conversion = {"baseline": [], "optimized": []}
    for name in ("baseline", "optimized", "optimized", "baseline"):
        db.execute(
            "UPDATE bridge_zulip_v5_message_contents SET conversion_version=0 WHERE uuid=ANY(%s)",
            (content_ids,),
        )
        task = models.Task(content=models.ConvertMessageContent())
        task.insert()
        service = history_import.HistoryImportService(
            queue="all" if name == "baseline" else "background",
            workers=1 if name == "baseline" else 4,
        )
        monkeypatch.setattr(
            models.ConvertMessageContent,
            "execute",
            baseline if name == "baseline" else original,
        )
        started = time.monotonic()
        try:
            worker_tests._until(
                service,
                lambda: (
                    db.execute(
                        "SELECT count(*) FROM bridge_zulip_v5_message_contents WHERE uuid=ANY(%s) AND conversion_version<1",
                        (content_ids,),
                    ).fetchone()[0]
                    == 0
                ),
                timeout=60,
            )
            conversion[name].append(time.monotonic() - started)
        finally:
            service.stop()
        db.execute(
            "DELETE FROM bridge_zulip_v5_tasks WHERE content->>'kind'='convert_message_content'"
        )
    monkeypatch.setattr(models.ConvertMessageContent, "execute", original)

    accounts = []
    io_results = {"baseline": [], "isolated": []}
    try:
        for i in range(4):
            value = external_models.ExternalAccount(
                project_id=uuid.uuid4(),
                owner_user_uuid=account.owner_user_uuid,
                provider="zulip",
                external_account_id=str(100 + i),
                settings=account.settings,
            )
            value.insert()
            accounts.append(value)
        for name in ("baseline", "isolated", "isolated", "baseline"):
            started_background = threading.Event()

            def constructor(current):
                connection = mock.Mock()

                def fetch(first, last):
                    if current.uuid != account.uuid:
                        started_background.set()
                        time.sleep(0.15)
                    return []

                connection.get_messages.side_effect = fetch
                return connection

            monkeypatch.setattr(client, "ZulipClient", constructor)
            tasks = []
            for value in accounts:
                for i in (43, 42):
                    task = models.Task(
                        project_id=value.project_id,
                        external_account_uuid=value.uuid,
                        content=models.ImportHistoryContent(
                            first_message_id=i, last_message_id=i
                        ),
                    )
                    task.insert()
                    tasks.append(task)
            bg = history_import.HistoryImportService(
                queue="all" if name == "baseline" else "background",
                workers=1 if name == "baseline" else 4,
            )
            rt = history_import.HistoryImportService(queue="realtime")
            started = time.monotonic()
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as driver:
                    first = driver.submit(bg._iteration)
                    assert started_background.wait(5)
                    urgent = models.Task(
                        project_id=account.project_id,
                        external_account_uuid=account.uuid,
                        queue="realtime",
                        priority=models.TASK_PRIORITY_REALTIME_MESSAGE,
                        content=models.ImportHistoryContent(
                            first_message_id=9001, last_message_id=9001
                        ),
                    )
                    urgent.insert()
                    arrived = time.monotonic()
                    urgent_done = None
                    if name == "isolated":
                        worker_tests._until(
                            rt, lambda: worker_tests._stored(urgent).status == "done"
                        )
                        urgent_done = time.monotonic()
                    first.result(timeout=5)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        bg._iteration()
                        if (
                            worker_tests._stored(urgent).status == "done"
                            and urgent_done is None
                        ):
                            urgent_done = time.monotonic()
                        if urgent_done is not None and all(
                            worker_tests._stored(task).status == "done"
                            for task in tasks
                        ):
                            break
                        time.sleep(0.002)
                    else:
                        pytest.fail("I/O comparison did not finish")
                io_results[name].append(
                    {
                        "seconds": time.monotonic() - started,
                        "realtime_task_ms": (urgent_done - arrived) * 1000,
                    }
                )
            finally:
                bg.stop()
                rt.stop()
                for task in tasks:
                    task.delete()
                urgent.delete()
    finally:
        for value in accounts:
            value.delete()
    report = {
        "baseline_commit": reference,
        "conversion": {
            "messages": count,
            "seconds": conversion,
            "speedup": statistics.median(conversion["baseline"])
            / statistics.median(conversion["optimized"]),
        },
        "io": {
            "description": "8 real database tasks across 4 accounts, emulated Zulip requests of 150ms; realtime arrives after background starts",
            "runs": io_results,
            "speedup": statistics.median(
                row["seconds"] for row in io_results["baseline"]
            )
            / statistics.median(row["seconds"] for row in io_results["isolated"]),
            "realtime_latency_ratio": statistics.median(
                row["realtime_task_ms"] for row in io_results["baseline"]
            )
            / statistics.median(
                row["realtime_task_ms"] for row in io_results["isolated"]
            ),
        },
    }
    pathlib.Path(REPORT).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
