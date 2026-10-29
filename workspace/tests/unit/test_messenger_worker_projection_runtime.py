# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import contextlib
import datetime
import logging
import threading
import types
import uuid as sys_uuid

import pytest

from workspace.cmd import messenger_worker
from workspace.common import messenger_worker_opts
from workspace.common import mutation_journal_opts
from workspace.services.messenger_workers import agents
from workspace.services.messenger_workers import projection_wakeup
from workspace.services.messenger_workers import v2_projection


@pytest.fixture(autouse=True)
def _no_pending_publications(monkeypatch):
    monkeypatch.setattr(agents.publication, "publish_pending", lambda _session: 0)


def test_stream_counter_snapshot_accepts_no_default_topic(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    calls = []
    monkeypatch.setattr(
        v2_projection,
        "_refresh_recipient_counters",
        lambda *args: calls.append(("refresh", args)),
    )
    monkeypatch.setattr(
        v2_projection,
        "_emit_unread_snapshots",
        lambda *args: calls.append(("emit", args)),
    )
    monkeypatch.setattr(
        v2_projection,
        "_enqueue_folder_outbox_events",
        lambda *args, **kwargs: calls.append(("folder", args, kwargs)),
    )
    monkeypatch.setattr(
        v2_projection,
        "_try_lock_project_event_tail",
        lambda *args: calls.append(("lock", args)),
    )
    cursor = types.SimpleNamespace(fetchall=lambda: [])
    session = types.SimpleNamespace(execute=lambda *_args, **_kwargs: cursor)

    v2_projection._process_read_counters(
        session,
        {
            "uuid": sys_uuid.uuid4(),
            "project_id": project_uuid,
            "scope_kind": "user-stream",
            "scope_key": f"{project_uuid}:{user_uuid}:{stream_uuid}",
            "outbox_event_uuid": sys_uuid.uuid4(),
            "payload": {
                "source_kind": "provider_history.finalized",
                "user_uuid": str(user_uuid),
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None,
            },
        },
    )

    assert calls[0][0] == "refresh"
    assert calls[0][1][3] is None
    assert calls[-1][0] == "emit"
    assert calls[-1][1][3] is None


def test_projection_worker_count_is_bounded_and_defaults_to_four():
    option = next(
        option
        for option in messenger_worker_opts.messenger_worker_opts
        if option.name == "v2-projection-workers"
    )

    assert option.default == 4
    assert option.type.min == 1
    assert option.type.max == 8

    idle_option = next(
        option
        for option in messenger_worker_opts.messenger_worker_opts
        if option.name == "v2-projection-idle-sleep-seconds"
    )
    assert idle_option.default == 0.5
    assert idle_option.type.min == 0.1
    assert idle_option.type.max == 3.0


def test_worker_entrypoint_builds_one_primary_and_projection_only_peers():
    messenger_worker.CONF.set_override(
        "v2_projection_enabled", True, group=messenger_worker.DOMAIN
    )
    messenger_worker.CONF.set_override(
        "v2_projection_workers", 4, group=messenger_worker.DOMAIN
    )
    try:
        services = messenger_worker.build_worker_services()
    finally:
        messenger_worker.CONF.clear_override(
            "v2_projection_enabled", group=messenger_worker.DOMAIN
        )
        messenger_worker.CONF.clear_override(
            "v2_projection_workers", group=messenger_worker.DOMAIN
        )

    assert len(services) == 5
    *services, publisher = services
    assert isinstance(publisher, agents.MessengerPublicationAgent)
    assert [service._projection_only for service in services] == [
        False,
        True,
        True,
        True,
    ]
    assert len({service._v2_worker_id for service in services}) == 4
    assert all(service._iter_min_period == 0 for service in services)
    assert all(service._iter_pause == 0 for service in services)
    assert all(service._v2_idle_sleep_seconds == 0.5 for service in services)


def test_projection_only_worker_sleeps_only_after_an_empty_cycle(monkeypatch):
    sleeps = []
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        projection_only=True,
        v2_idle_sleep_seconds=0.5,
    )
    monkeypatch.setattr(agents.time, "sleep", sleeps.append)
    monkeypatch.setattr(worker, "_run_v2_projection_tasks", lambda: True)

    worker._iteration()

    assert sleeps == []

    monkeypatch.setattr(worker, "_run_v2_projection_tasks", lambda: False)
    worker._iteration()

    assert sleeps == [0.5]


def test_owned_dispatcher_cannot_run_slow_summary_or_presence_maintenance(monkeypatch):
    messenger_worker.CONF.set_override(
        "enabled", True, group=mutation_journal_opts.DOMAIN
    )
    try:
        services = messenger_worker.build_worker_services()
        dispatcher, maintenance = services
        assert dispatcher._v2_projection_enabled and dispatcher._projection_only
        assert (
            not maintenance._v2_projection_enabled and not maintenance._projection_only
        )
        monkeypatch.setattr(dispatcher, "_run_v2_projection_tasks", lambda: True)

        def forbidden():
            raise AssertionError("Coordinator must not run a blocking summary request")

        monkeypatch.setattr(dispatcher, "_summarize_one_topic", forbidden)
        dispatcher._iteration()
    finally:
        messenger_worker.CONF.clear_override(
            "enabled", group=mutation_journal_opts.DOMAIN
        )


def test_projection_worker_waits_for_database_notification(monkeypatch):
    waits = []
    wakeup = types.SimpleNamespace(
        wait=lambda timeout: waits.append(timeout),
        close=lambda: None,
    )
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        projection_only=True,
        v2_idle_sleep_seconds=0.5,
        v2_projection_db_url="postgresql://test",
    )
    worker._v2_projection_wakeup = wakeup
    monkeypatch.setattr(worker, "_run_v2_projection_tasks", lambda: False)

    worker._iteration()

    assert waits == [0.5]


@pytest.mark.parametrize("mutations", [False, True])
def test_projection_queue_wakeup_closes_listener_before_returning(
    monkeypatch, mutations
):
    executed = []
    closed = []
    delivered = []

    class Connection:
        def execute(self, query):
            executed.append(query)

        def notifies(self, *, timeout, stop_after):
            assert timeout == 0.5
            assert stop_after == 1
            for payload in ("1", "1", "4"):
                delivered.append(payload)
                yield types.SimpleNamespace(payload=payload)

        def close(self):
            closed.append(True)

    connection = Connection()
    connections = []

    def connect(db_url, *, autocommit, **kwargs):
        connections.append((db_url, autocommit))
        return connection

    monkeypatch.setattr(projection_wakeup.psycopg, "connect", connect)
    wakeup = projection_wakeup.ProjectionQueueWakeup(
        "postgresql://test", mutations=mutations
    )

    assert wakeup.wait(0.5) is True
    assert closed == [True]
    assert wakeup.wait(0.5) is True
    wakeup.close()

    assert connections == [("postgresql://test", True)] * 2
    assert (
        executed
        == (
            [f"LISTEN {projection_wakeup.CHANNEL}"]
            + (["LISTEN workspace_mutation_commands"] if mutations else [])
        )
        * 2
    )
    assert delivered == ["1", "1"]
    assert closed == [True, True]


@pytest.mark.parametrize("fail_at", ["projection", "mutation", "notifies"])
def test_projection_wakeup_closes_failed_connection_and_resubscribes(
    monkeypatch, fail_at
):
    connections = []
    subscriptions = []

    class Connection:
        closed = False

        def execute(self, query):
            subscriptions.append(query)
            if len(connections) == 1 and fail_at in query:
                raise RuntimeError("disconnected during LISTEN")

        def notifies(self, **kwargs):
            if len(connections) == 1 and fail_at == "notifies":
                raise RuntimeError("disconnected while waiting")
            yield object()

        def close(self):
            self.closed = True

    def connect(*args, **kwargs):
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(projection_wakeup.psycopg, "connect", connect)
    wakeup = projection_wakeup.ProjectionQueueWakeup(
        "postgresql://test", mutations=True
    )
    assert wakeup.wait(0.001) is False
    assert connections[0].closed
    assert wakeup.wait(0.001) is True
    assert all(connection.closed for connection in connections)
    assert subscriptions[-2:] == [
        "LISTEN workspace_projection_tasks",
        "LISTEN workspace_mutation_commands",
    ]


def test_projection_wakeup_close_waits_for_owner_and_prevents_new_connections(
    monkeypatch,
):
    waiting = threading.Event()
    release = threading.Event()
    closed_by = []

    class Connection:
        def execute(self, query):
            pass

        def notifies(self, **kwargs):
            waiting.set()
            assert release.wait(2)
            yield from ()

        def close(self):
            closed_by.append(threading.get_ident())

    monkeypatch.setattr(
        projection_wakeup.psycopg, "connect", lambda *a, **k: Connection()
    )
    wakeup = projection_wakeup.ProjectionQueueWakeup("postgresql://test")
    owner = threading.Thread(target=wakeup.wait, args=(0.5,))
    closer = threading.Thread(target=wakeup.close)
    owner.start()
    try:
        assert waiting.wait(2)
        closer.start()
        closer.join(0.02)
        assert closer.is_alive()
    finally:
        release.set()
        owner.join(2)
        if closer.ident is not None:
            closer.join(2)
    assert not owner.is_alive() and not closer.is_alive()
    assert closed_by == [owner.ident]
    assert wakeup.wait(0.5) is False
    assert closed_by == [owner.ident]


def test_mutation_runtime_initialization_does_not_open_listener(monkeypatch):
    from workspace.services.messenger_workers import mutation_runtime

    connects = []
    monkeypatch.setattr(agents.mutation_journal_opts, "enabled", lambda: True)
    runtime = types.SimpleNamespace(tick=lambda: True, close=lambda: None)
    monkeypatch.setattr(mutation_runtime, "MutationRuntime", lambda **kwargs: runtime)
    monkeypatch.setattr(
        projection_wakeup.psycopg, "connect", lambda *a, **k: connects.append(True)
    )
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        projection_only=True,
        v2_projection_db_url="postgresql://test",
    )
    assert worker._run_v2_projection_tasks() is True
    assert connects == []
    worker.stop()


def test_projection_pass_keeps_earlier_progress_after_event_lock_contention(
    monkeypatch,
):
    calls = []

    @contextlib.contextmanager
    def session_context():
        yield types.SimpleNamespace()

    monkeypatch.setattr(agents, "database_session_context", session_context)
    monkeypatch.setattr(
        agents.v2_projection,
        "derive_projection_tasks",
        lambda _session: 0,
    )

    def process(_session, _worker_id, *, metrics, **_kwargs):
        calls.append("process")
        if len(calls) == 1:
            return True
        metrics["event_lock_contention"] = metrics.get("event_lock_contention", 0.0) + 1
        return False

    monkeypatch.setattr(
        agents.v2_projection,
        "process_one_projection_task",
        process,
    )
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        v2_projection_max_tasks_per_iteration=10,
        v2_metrics_log_interval_seconds=300,
    )

    assert worker._run_v2_projection_tasks() is True
    assert calls == ["process", "process"]


def test_projection_pass_reports_no_progress_for_only_event_lock_contention(
    monkeypatch,
):
    calls = []

    @contextlib.contextmanager
    def session_context():
        yield types.SimpleNamespace()

    monkeypatch.setattr(agents, "database_session_context", session_context)
    monkeypatch.setattr(
        agents.v2_projection,
        "derive_projection_tasks",
        lambda _session: 0,
    )

    def process(_session, _worker_id, *, metrics, **_kwargs):
        calls.append("process")
        metrics["event_lock_contention"] = metrics.get("event_lock_contention", 0.0) + 1
        return False

    monkeypatch.setattr(
        agents.v2_projection,
        "process_one_projection_task",
        process,
    )
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        v2_projection_max_tasks_per_iteration=10,
        v2_metrics_log_interval_seconds=300,
    )

    assert worker._run_v2_projection_tasks() is False
    assert calls == ["process"]


def test_projection_pass_continues_a_truncated_partition_scan(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def session_context():
        yield types.SimpleNamespace()

    monkeypatch.setattr(agents, "database_session_context", session_context)
    monkeypatch.setattr(
        agents.v2_projection,
        "derive_projection_tasks",
        lambda _session: 0,
    )

    def process(_session, _worker_id, *, metrics, **_kwargs):
        calls.append("process")
        if len(calls) <= 2:
            metrics["partition_scan_continuation"] = (
                metrics.get("partition_scan_continuation", 0.0) + 1
            )
            return False
        return len(calls) == 3

    monkeypatch.setattr(
        agents.v2_projection,
        "process_one_projection_task",
        process,
    )
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        v2_projection_max_tasks_per_iteration=10,
        v2_metrics_log_interval_seconds=300,
    )

    assert worker._run_v2_projection_tasks() is True
    assert calls == ["process", "process", "process", "process"]


def test_projection_pass_paces_after_maximum_truncated_scans(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def session_context():
        yield types.SimpleNamespace()

    monkeypatch.setattr(agents, "database_session_context", session_context)
    monkeypatch.setattr(
        agents.v2_projection,
        "derive_projection_tasks",
        lambda _session: 0,
    )

    def process(_session, _worker_id, *, metrics, **_kwargs):
        calls.append("process")
        metrics["partition_scan_continuation"] = (
            metrics.get("partition_scan_continuation", 0.0) + 1
        )
        return False

    monkeypatch.setattr(
        agents.v2_projection,
        "process_one_projection_task",
        process,
    )
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        v2_projection_max_tasks_per_iteration=3,
        v2_metrics_log_interval_seconds=300,
    )

    assert worker._run_v2_projection_tasks() is False
    assert calls == ["process", "process", "process"]


def test_operator_drain_continues_a_truncated_partition_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(v2_projection, "derive_projection_tasks", lambda *_args: 0)

    def process(*_args, metrics, **_kwargs):
        calls.append("process")
        if len(calls) == 1:
            metrics["partition_scan_continuation"] = 1
            return False
        return True

    monkeypatch.setattr(v2_projection, "process_one_projection_task", process)

    assert v2_projection.drain_projection_queue(None, "operator:drain", limit=1) == 1
    assert calls == ["process", "process"]


def test_operator_drain_stops_to_commit_an_admission_marker(monkeypatch):
    calls = []
    monkeypatch.setattr(v2_projection, "derive_projection_tasks", lambda *_args: 0)

    def process(*_args, metrics, **_kwargs):
        calls.append("process")
        metrics["admission_commit_required"] = 1
        return False

    monkeypatch.setattr(v2_projection, "process_one_projection_task", process)

    assert v2_projection.drain_projection_queue(None, "operator:drain", limit=1) == 0
    assert calls == ["process"]


def test_projection_pass_sleeps_after_a_complete_empty_partition_scan(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def session_context():
        yield types.SimpleNamespace()

    monkeypatch.setattr(agents, "database_session_context", session_context)
    monkeypatch.setattr(
        agents.v2_projection,
        "derive_projection_tasks",
        lambda _session: 0,
    )

    def process(_session, _worker_id, *, metrics, **_kwargs):
        calls.append("process")
        if len(calls) == 1:
            metrics["partition_scan_continuation"] = 1
        return False

    monkeypatch.setattr(
        agents.v2_projection,
        "process_one_projection_task",
        process,
    )
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        v2_projection_max_tasks_per_iteration=10,
        v2_metrics_log_interval_seconds=300,
    )

    assert worker._run_v2_projection_tasks() is False
    assert calls == ["process", "process"]


def test_execution_stats_report_clock_skew_without_negative_latencies():
    now = datetime.datetime.now(datetime.timezone.utc)
    task = {
        "created_at": now - datetime.timedelta(milliseconds=100),
        "outbox_created_at": now + datetime.timedelta(milliseconds=200),
        "task_age_seconds": 0.1,
        "outbox_age_seconds": -0.2,
        "partition_kind": "user",
        "payload": {},
    }

    stats = v2_projection._finish_execution_stats(
        task,
        worker_id="unit:clock-skew",
        claimed_at=now,
        claim_seconds=0.002,
        processing_seconds=0.003,
        outcome="completed",
    )

    assert stats["queue_wait_ms"] == 100
    assert stats["outbox_wait_ms"] == 0
    assert stats["derivation_delay_ms"] == 0
    assert stats["outbox_to_finish_ms"] == 0
    assert stats["observed_clock_skew_ms"] >= 299
    assert stats["partition_kind"] == "user"


def test_projection_metrics_log_surfaces_partition_activity(
    caplog,
    monkeypatch,
):
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        v2_metrics_log_interval_seconds=1,
        projection_only=True,
    )
    worker._v2_metrics = {
        "claimed": 4,
        "completed": 3,
        "claimed_partition_user": 3,
        "claimed_partition_project": 1,
        "partition_contention": 5,
        "event_lock_contention": 2,
        "claim_seconds": 0.01,
        "claim_seconds_max": 0.004,
        "processing_seconds": 0.02,
        "task_age_seconds_max": 3,
    }
    worker._v2_metrics_started_at = 0
    monkeypatch.setattr(agents.time, "monotonic", lambda: 2)

    with caplog.at_level(logging.INFO):
        worker._record_v2_projection_metrics({})

    assert "claimed=4 completed=3" in caplog.text
    assert "user_partitions=3 project_partitions=1" in caplog.text
    assert "partition_contention=5" in caplog.text
    assert "event_lock_contention=2" in caplog.text
    assert "claim_ms_avg=2.500 claim_ms_max=4.000" in caplog.text
    assert "processing_ms_avg=5.000 task_age_max_s=3.000" in caplog.text


def test_publication_has_an_independent_service_even_with_one_projection_worker():
    messenger_worker.CONF.set_override(
        "v2_projection_workers", 1, group=messenger_worker.DOMAIN
    )
    messenger_worker.CONF.set_override(
        "enabled", False, group=mutation_journal_opts.DOMAIN
    )
    try:
        services = messenger_worker.build_worker_services()
        publishers = [
            service
            for service in services
            if isinstance(service, agents.MessengerPublicationAgent)
        ]
        assert len(publishers) == 1
        assert publishers[0]._iter_min_period == 0.1
        assert len(services) == 2
    finally:
        messenger_worker.CONF.clear_override(
            "v2_projection_workers", group=messenger_worker.DOMAIN
        )
        messenger_worker.CONF.clear_override(
            "enabled", group=mutation_journal_opts.DOMAIN
        )


def test_publication_service_commits_each_pass_and_retries(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def session_context():
        calls.append("begin")
        try:
            yield object()
        except Exception:
            calls.append("rollback")
            raise
        else:
            calls.append("commit")

    monkeypatch.setattr(agents, "database_session_context", session_context)
    worker = agents.MessengerPublicationAgent(iter_min_period=0, iter_pause=0)
    monkeypatch.setattr(agents.publication, "publish_pending", lambda session: 0)
    worker._iteration()
    assert calls == ["begin", "commit"]
    calls.clear()

    def failed(session):
        raise RuntimeError("transient publication failure")

    monkeypatch.setattr(agents.publication, "publish_pending", failed)
    worker._loop_iteration()
    assert calls == ["begin", "rollback"]
    calls.clear()
    monkeypatch.setattr(agents.publication, "publish_pending", lambda session: 1)
    worker._iteration()
    assert calls == ["begin", "commit"]
