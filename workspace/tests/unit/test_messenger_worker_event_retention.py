# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import types

import pytest

from workspace.common import messenger_worker_opts
from workspace.messenger_api.dm import read_state
from workspace.services.messenger_workers import agents


@pytest.fixture(autouse=True)
def _disable_read_state_maintenance(monkeypatch):
    monkeypatch.setattr(
        agents.read_state,
        "maintain_next_project",
        lambda _session, **_kwargs: None,
    )


def test_read_state_failure_does_not_rollback_presence_or_bridge(monkeypatch):
    calls = []
    sessions = iter(
        [
            types.SimpleNamespace(name="presence"),
            types.SimpleNamespace(name="read-state"),
            types.SimpleNamespace(name="repair"),
        ]
    )
    monkeypatch.setattr(
        agents,
        "database_session_context",
        lambda: _SessionContext(next(sessions), calls),
    )
    monkeypatch.setattr(agents.time, "monotonic", lambda: 31.0)
    monkeypatch.setattr(
        agents.messenger_dm_helpers,
        "mark_stale_workspace_users_offline",
        lambda *, session: calls.append(("presence", session.name)),
    )

    def fail_maintenance(session, **kwargs):
        calls.append(("read-state", session.name, kwargs))
        raise RuntimeError("parity failed")

    monkeypatch.setattr(
        agents.read_state,
        "maintain_next_project",
        fail_maintenance,
    )
    worker = agents.MessengerWorkerAgent(
        read_state_compaction_enabled=True,
        read_state_cleanup_enabled=False,
        read_state_batch_size=50_000,
        read_state_max_batches_per_iteration=8,
    )
    worker._last_event_prune = 31.0
    monkeypatch.setattr(worker, "_summarize_one_topic", lambda: False)

    worker._iteration()

    assert ("exit", "presence", None) in calls
    assert ("exit", "read-state", "RuntimeError") in calls
    assert (
        "read-state",
        "read-state",
        {
            "batch_size": 50_000,
            "cleanup_enabled": False,
            "excluded_project_ids": set(),
        },
    ) in calls


def test_read_state_maintenance_is_disabled_until_rollout_is_current():
    worker = agents.MessengerWorkerAgent()
    compaction = next(
        option
        for option in messenger_worker_opts.messenger_worker_opts
        if option.name == "read-state-compaction-enabled"
    )
    cleanup = next(
        option
        for option in messenger_worker_opts.messenger_worker_opts
        if option.name == "read-state-cleanup-enabled"
    )

    assert worker._read_state_compaction_enabled is False
    assert worker._read_state_cleanup_enabled is False
    assert compaction.default is False
    assert cleanup.default is False
    assert "every Workspace API and worker process" in compaction.help
    assert "every Workspace API and worker process" in cleanup.help


def test_read_state_failure_rotates_to_another_project_with_backoff(monkeypatch):
    failed_project = "00000000-0000-0000-0000-000000000001"
    healthy_project = "00000000-0000-0000-0000-000000000002"
    maintenance_calls = []
    session_number = 0

    def session_context():
        nonlocal session_number
        session_number += 1
        return _SessionContext(
            types.SimpleNamespace(name=f"session-{session_number}"),
            [],
        )

    results = iter(
        (
            read_state.MaintenanceProjectError(failed_project),
            ("prepare", healthy_project, 17),
            None,
        )
    )

    def maintain(_session, **kwargs):
        maintenance_calls.append(kwargs)
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(agents, "database_session_context", session_context)
    monkeypatch.setattr(agents.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        agents.messenger_dm_helpers,
        "mark_stale_workspace_users_offline",
        lambda *, session: None,
    )
    monkeypatch.setattr(agents.read_state, "maintain_next_project", maintain)
    worker = agents.MessengerWorkerAgent(
        read_state_compaction_enabled=True,
        read_state_max_batches_per_iteration=3,
    )
    worker._last_event_prune = 100.0
    monkeypatch.setattr(worker, "_summarize_one_topic", lambda: False)

    worker._iteration()

    assert maintenance_calls[0]["excluded_project_ids"] == set()
    assert maintenance_calls[1]["excluded_project_ids"] == {failed_project}
    assert maintenance_calls[2]["excluded_project_ids"] == {failed_project}
    assert worker._read_state_failures == {failed_project: (1, 105.0)}


def test_read_state_failure_backoff_is_bounded():
    assert agents._read_state_failure_backoff_seconds(1) == 5.0
    assert agents._read_state_failure_backoff_seconds(2) == 10.0
    assert agents._read_state_failure_backoff_seconds(7) == 300.0
    assert agents._read_state_failure_backoff_seconds(1_000_000) == 300.0


class _SessionContext:
    def __init__(self, session, calls):
        self.session = session
        self.calls = calls

    def __enter__(self):
        self.calls.append(("enter", self.session.name))
        return self.session

    def __exit__(self, exc_type, exc_value, traceback):
        del exc_value, traceback
        self.calls.append(
            (
                "exit",
                self.session.name,
                exc_type.__name__ if exc_type is not None else None,
            )
        )


def test_worker_prunes_postgresql_events_in_owned_session(monkeypatch):
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    session = types.SimpleNamespace()
    calls = []
    monkeypatch.setattr(
        agents.sql_canonical_store,
        "prune_expired_events",
        lambda target_session, target_now, **kwargs: (
            calls.append((target_session, target_now, kwargs)) or 9
        ),
    )

    worker = agents.MessengerWorkerAgent()

    assert worker._prune_expired_events(session, now) == 9
    assert calls == [
        (
            session,
            now,
            {
                "retention": agents.sql_canonical_store.EVENT_RETENTION,
                "batch_size": agents.sql_canonical_store.EVENT_PRUNE_BATCH_SIZE,
            },
        )
    ]
