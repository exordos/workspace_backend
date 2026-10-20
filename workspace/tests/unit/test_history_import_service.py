# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import contextlib
from unittest import mock
import uuid

import pytest

from workspace.services import history_import


def test_completion_keeps_immediate_pacing_when_iteration_clears_the_wakeup():
    service = history_import.HistoryImportService(queue="background", iter_min_period=3)
    future = concurrent.futures.Future()
    executor = mock.Mock()
    executor.submit.return_value = future
    service._executor = executor
    with (
        mock.patch.object(service, "_ensure_reconciliation_task"),
        mock.patch.object(history_import.contexts.Context, "session_manager"),
        mock.patch.object(
            service, "_claim_tasks", return_value=[(uuid.uuid4(), uuid.uuid4())]
        ),
    ):
        service._pool_iteration()
    service.reset_boost()
    future.set_result(None)
    # BasicService clears this event after an iteration. Completion must also
    # update pacing, so this clear cannot postpone work for three seconds.
    service._wake_event.clear()
    assert service._current_pacing().iter_min_period == 0
    assert service._current_pacing().iter_pause == 0
    service.stop()


def test_busy_loop_runs_without_waiting_and_restores_idle_pacing():
    service = history_import.HistoryImportService(iter_min_period=3)
    events = []
    first = mock.Mock(status="done")
    second = mock.Mock(status="retry")
    first.execute.side_effect = lambda: events.append("first")
    second.execute.side_effect = lambda: events.append("second")

    @contextlib.contextmanager
    def session_manager():
        yield
        events.append("commit")

    boost = service.boost

    def boost_after_commit(**kwargs):
        assert events[-1] == "commit"
        events.append("boost")
        return boost(**kwargs)

    def wait_for_idle(timeout):
        events.append(("wait", timeout))
        service.stop()

    with (
        mock.patch.object(service, "_ensure_reconciliation_task"),
        mock.patch.object(
            history_import.contexts.Context,
            "session_manager",
            side_effect=session_manager,
        ),
        mock.patch.object(
            service, "_get_tasks", side_effect=[[first], [second], []]
        ) as get_tasks,
        mock.patch.object(service, "boost", side_effect=boost_after_commit) as boosted,
        mock.patch.object(history_import.basic.time, "monotonic", return_value=0),
        mock.patch.object(service._wake_event, "wait", side_effect=wait_for_idle),
    ):
        service._loop()

    assert get_tasks.call_count == 3
    assert boosted.call_args_list == [mock.call(iterations=2), mock.call(iterations=2)]
    assert events == [
        "first",
        "commit",
        "boost",
        "second",
        "commit",
        "boost",
        "commit",
        ("wait", 3),
    ]
    assert not service.is_boosted


@pytest.mark.parametrize("phase", ["execute", "commit"])
def test_failed_iteration_does_not_enable_boost(phase):
    service = history_import.HistoryImportService()
    task = mock.Mock()
    error = RuntimeError(f"{phase} failed")
    if phase == "execute":
        task.execute.side_effect = error

    @contextlib.contextmanager
    def session_manager():
        yield
        if phase == "commit":
            raise error

    with (
        mock.patch.object(service, "_ensure_reconciliation_task"),
        mock.patch.object(
            history_import.contexts.Context,
            "session_manager",
            side_effect=session_manager,
        ),
        mock.patch.object(service, "_get_tasks", return_value=[task]),
        pytest.raises(RuntimeError, match=f"{phase} failed"),
    ):
        service._iteration()

    assert not service.is_boosted
