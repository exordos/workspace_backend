# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import types

from workspace.services.messenger_workers import agents


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


def test_worker_commits_event_pruning_before_capability_refresh(monkeypatch):
    calls = []
    account_uuid = "00000000-0000-0000-0000-000000000001"
    sessions = iter(
        [
            types.SimpleNamespace(name="event-prune"),
            types.SimpleNamespace(name="presence"),
            types.SimpleNamespace(name="capability"),
            types.SimpleNamespace(name="capability-end"),
            types.SimpleNamespace(name="heartbeats"),
            types.SimpleNamespace(name="repair"),
        ]
    )
    monkeypatch.setattr(
        agents,
        "database_session_context",
        lambda: _SessionContext(next(sessions), calls),
    )
    monkeypatch.setattr(agents.time, "monotonic", lambda: 17.0)
    monkeypatch.setattr(
        agents.messenger_dm_helpers,
        "mark_stale_workspace_users_offline",
        lambda *, session: calls.append(("presence", session.name)),
    )
    monkeypatch.setattr(
        agents.sql_state,
        "degrade_stale_bridge_instances",
        lambda session, *, now: (
            calls.append(("degrade-bridges", session.name, now)) or 0
        ),
    )
    claim_values = iter((account_uuid, None))
    monkeypatch.setattr(
        agents.sql_state,
        "claim_capability_refresh_account",
        lambda session, *, after_uuid: (
            calls.append(("claim", session.name, after_uuid)) or next(claim_values)
        ),
    )
    monkeypatch.setattr(
        agents.sql_state,
        "refresh_effective_capabilities",
        lambda session, *, account_uuid, now: calls.append(
            ("capabilities", session.name, account_uuid, now)
        ),
    )
    monkeypatch.setattr(
        agents.sql_state,
        "prune_expired_heartbeats",
        lambda session, now, **kwargs: (
            calls.append(("heartbeats", session.name, now, kwargs)) or 0
        ),
    )
    worker = agents.MessengerWorkerAgent()
    monkeypatch.setattr(
        worker,
        "_prune_expired_events",
        lambda session, now: calls.append(("events", session.name, now)) or 3,
    )
    monkeypatch.setattr(
        worker,
        "_repair_external_projection_transitions",
        lambda session: calls.append(("repair", session.name)),
    )
    monkeypatch.setattr(
        worker,
        "_refresh_capability_projections",
        lambda: calls.append(("capability-projections",)),
    )

    worker._iteration()

    assert calls[0:3] == [
        ("enter", "event-prune"),
        ("events", "event-prune", calls[1][2]),
        ("exit", "event-prune", None),
    ]
    capability_calls = [call for call in calls if call[0] == "capabilities"]
    assert len(capability_calls) == 1
    assert capability_calls[0][1:3] == ("capability", account_uuid)
    assert calls[-2:] == [
        ("repair", "repair"),
        ("exit", "repair", None),
    ]
    assert worker._last_event_prune == 17.0


def test_worker_continues_projection_repair_after_event_prune_rollback(
    monkeypatch,
):
    calls = []
    account_uuid = "00000000-0000-0000-0000-000000000001"
    sessions = iter(
        [
            types.SimpleNamespace(name="event-prune"),
            types.SimpleNamespace(name="presence"),
            types.SimpleNamespace(name="capability"),
            types.SimpleNamespace(name="capability-end"),
            types.SimpleNamespace(name="heartbeats"),
            types.SimpleNamespace(name="repair"),
        ]
    )
    monkeypatch.setattr(
        agents,
        "database_session_context",
        lambda: _SessionContext(next(sessions), calls),
    )
    monkeypatch.setattr(agents.time, "monotonic", lambda: 23.0)
    monkeypatch.setattr(
        agents.messenger_dm_helpers,
        "mark_stale_workspace_users_offline",
        lambda *, session: calls.append(("presence", session.name)),
    )
    monkeypatch.setattr(
        agents.sql_state,
        "degrade_stale_bridge_instances",
        lambda session, *, now: (
            calls.append(("degrade-bridges", session.name, now)) or 0
        ),
    )
    claim_values = iter((account_uuid, None))
    monkeypatch.setattr(
        agents.sql_state,
        "claim_capability_refresh_account",
        lambda session, *, after_uuid: (
            calls.append(("claim", session.name, after_uuid)) or next(claim_values)
        ),
    )
    monkeypatch.setattr(
        agents.sql_state,
        "refresh_effective_capabilities",
        lambda session, *, account_uuid, now: calls.append(
            ("capabilities", session.name, account_uuid, now)
        ),
    )
    monkeypatch.setattr(
        agents.sql_state,
        "prune_expired_heartbeats",
        lambda session, now, **kwargs: (
            calls.append(("heartbeats", session.name, now, kwargs)) or 0
        ),
    )
    worker = agents.MessengerWorkerAgent()

    def fail_prune(session, now):
        calls.append(("events", session.name, now))
        raise RuntimeError("prune failed")

    monkeypatch.setattr(worker, "_prune_expired_events", fail_prune)
    monkeypatch.setattr(
        worker,
        "_repair_external_projection_transitions",
        lambda session: calls.append(("repair", session.name)),
    )
    monkeypatch.setattr(
        worker,
        "_refresh_capability_projections",
        lambda: calls.append(("capability-projections",)),
    )

    worker._iteration()

    assert ("exit", "event-prune", "RuntimeError") in calls
    assert ("repair", "repair") in calls
    assert calls[-1] == ("exit", "repair", None)
    assert worker._last_event_prune is None


def test_capability_refresh_retries_deadlock_without_duplicate_commit(monkeypatch):
    class DeadlockDetected(Exception):
        sqlstate = "40P01"

    calls = []
    account_uuid = "00000000-0000-0000-0000-000000000001"
    sessions = iter(
        [
            types.SimpleNamespace(name="attempt-1"),
            types.SimpleNamespace(name="attempt-2"),
            types.SimpleNamespace(name="attempt-3"),
            types.SimpleNamespace(name="end"),
        ]
    )
    monkeypatch.setattr(
        agents,
        "database_session_context",
        lambda: _SessionContext(next(sessions), calls),
    )
    monkeypatch.setattr(
        agents.sql_state,
        "claim_capability_refresh_account",
        lambda session, *, after_uuid: (
            calls.append(("claim", session.name, after_uuid))
            or (account_uuid if after_uuid is None else None)
        ),
    )
    attempts = []

    def refresh(session, *, account_uuid, now):
        attempts.append((session.name, account_uuid, now))
        if len(attempts) < 3:
            raise DeadlockDetected("retry")
        calls.append(("committed", account_uuid))

    monkeypatch.setattr(
        agents.sql_state,
        "refresh_effective_capabilities",
        refresh,
    )
    monkeypatch.setattr(
        agents.time,
        "sleep",
        lambda delay: calls.append(("sleep", delay)),
    )
    monkeypatch.setattr(agents.random, "uniform", lambda _start, _end: 1.0)

    worker = agents.MessengerWorkerAgent()
    now = datetime.datetime(2026, 7, 29, tzinfo=datetime.timezone.utc)
    worker._refresh_capabilities(now)

    assert len(attempts) == 3
    assert calls.count(("committed", account_uuid)) == 1
    assert ("exit", "attempt-1", "DeadlockDetected") in calls
    assert ("exit", "attempt-2", "DeadlockDetected") in calls
    assert ("exit", "attempt-3", None) in calls


def test_capability_refresh_cursor_continues_after_bounded_batch(monkeypatch):
    calls = []
    first_uuid = "00000000-0000-0000-0000-000000000001"
    second_uuid = "00000000-0000-0000-0000-000000000002"
    sessions = iter(
        [
            types.SimpleNamespace(name="first"),
            types.SimpleNamespace(name="second"),
        ]
    )
    monkeypatch.setattr(agents, "CAPABILITY_REFRESH_LIMIT", 1)
    monkeypatch.setattr(
        agents,
        "database_session_context",
        lambda: _SessionContext(next(sessions), calls),
    )

    def claim(session, *, after_uuid):
        calls.append(("claim", session.name, after_uuid))
        return first_uuid if after_uuid is None else second_uuid

    monkeypatch.setattr(
        agents.sql_state,
        "claim_capability_refresh_account",
        claim,
    )
    monkeypatch.setattr(
        agents.sql_state,
        "refresh_effective_capabilities",
        lambda session, *, account_uuid, now: calls.append(
            ("refresh", session.name, account_uuid, now)
        ),
    )

    worker = agents.MessengerWorkerAgent()
    now = datetime.datetime(2026, 7, 29, tzinfo=datetime.timezone.utc)
    worker._refresh_capabilities(now)
    worker._refresh_capabilities(now)

    assert ("claim", "first", None) in calls
    assert ("claim", "second", first_uuid) in calls
    assert worker._capability_refresh_cursor == second_uuid


def test_capability_projection_refresh_commits_each_bounded_batch(monkeypatch):
    calls = []
    account_uuid = "00000000-0000-0000-0000-000000000001"
    sessions = iter(
        [
            types.SimpleNamespace(name="first-batch"),
            types.SimpleNamespace(name="wrap"),
            types.SimpleNamespace(name="second-batch"),
        ]
    )
    monkeypatch.setattr(agents, "CAPABILITY_PROJECTION_REFRESH_LIMIT", 3)
    monkeypatch.setattr(
        agents,
        "database_session_context",
        lambda: _SessionContext(next(sessions), calls),
    )
    claim_values = iter((account_uuid, None, account_uuid))
    monkeypatch.setattr(
        agents.sql_state,
        "claim_capability_projection_refresh_account",
        lambda session, *, after_uuid: (
            calls.append(("claim-projection", session.name, after_uuid))
            or next(claim_values)
        ),
    )
    monkeypatch.setattr(
        agents.sql_state,
        "refresh_projected_capabilities_batch",
        lambda session, *, account_uuid, batch_size: (
            calls.append(("refresh-projection", session.name, account_uuid, batch_size))
            or (batch_size, 80, batch_size)
        ),
    )

    worker = agents.MessengerWorkerAgent()
    worker._refresh_capability_projections()

    assert ("exit", "first-batch", None) in calls
    assert ("enter", "second-batch") in calls
    assert calls.index(("exit", "first-batch", None)) < calls.index(
        ("enter", "second-batch")
    )
    refreshes = [call for call in calls if call[0] == "refresh-projection"]
    assert len(refreshes) == 2
    assert all(call[3] == agents.CAPABILITY_PROJECTION_BATCH_SIZE for call in refreshes)


def test_capability_refresh_failure_is_not_counted_as_success(monkeypatch):
    calls = []
    account_uuid = "00000000-0000-0000-0000-000000000001"
    sessions = iter(
        [
            types.SimpleNamespace(name="failed"),
            types.SimpleNamespace(name="end"),
        ]
    )
    monkeypatch.setattr(
        agents,
        "database_session_context",
        lambda: _SessionContext(next(sessions), calls),
    )
    claims = []

    def claim(_session, *, after_uuid):
        claims.append(after_uuid)
        return account_uuid if after_uuid is None else None

    monkeypatch.setattr(
        agents.sql_state,
        "claim_capability_refresh_account",
        claim,
    )
    monkeypatch.setattr(
        agents.sql_state,
        "refresh_effective_capabilities",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )
    metrics = []
    monkeypatch.setattr(
        agents.LOG,
        "info",
        lambda message, *args, **kwargs: metrics.append((message, kwargs.get("extra"))),
    )

    worker = agents.MessengerWorkerAgent()
    worker._refresh_capabilities(
        datetime.datetime(2026, 7, 29, tzinfo=datetime.timezone.utc)
    )

    assert claims == [None, account_uuid]
    assert metrics[-1][1]["capability_refresh_batch_size"] == 0
    assert metrics[-1][1]["capability_refresh_failure_count"] == 1
