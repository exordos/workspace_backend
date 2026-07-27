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
    sessions = iter(
        [
            types.SimpleNamespace(name="event-prune"),
            types.SimpleNamespace(name="maintenance"),
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
        "refresh_effective_capabilities",
        lambda session, *, now: calls.append(("capabilities", session.name, now)),
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

    worker._iteration()

    assert calls[0:3] == [
        ("enter", "event-prune"),
        ("events", "event-prune", calls[1][2]),
        ("exit", "event-prune", None),
    ]
    assert calls[3] == ("enter", "maintenance")
    assert calls[-2:] == [
        ("repair", "maintenance"),
        ("exit", "maintenance", None),
    ]
    assert worker._last_event_prune == 17.0


def test_worker_continues_projection_repair_after_event_prune_rollback(
    monkeypatch,
):
    calls = []
    sessions = iter(
        [
            types.SimpleNamespace(name="event-prune"),
            types.SimpleNamespace(name="maintenance"),
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
        "refresh_effective_capabilities",
        lambda session, *, now: calls.append(("capabilities", session.name, now)),
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

    worker._iteration()

    assert ("exit", "event-prune", "RuntimeError") in calls
    assert ("repair", "maintenance") in calls
    assert calls[-1] == ("exit", "maintenance", None)
    assert worker._last_event_prune is None
