# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import contextlib
import datetime
import types
import uuid as sys_uuid

from workspace.cmd import messenger_worker
from workspace.common import messenger_worker_opts
from workspace.services.messenger_workers import agents
from workspace.services.messenger_workers import v2_projection


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
    session = types.SimpleNamespace(execute=lambda *_args, **_kwargs: None)

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

    assert len(services) == 4
    assert [service._projection_only for service in services] == [
        False,
        True,
        True,
        True,
    ]
    assert [service._projection_deriver for service in services] == [
        False,
        True,
        False,
        False,
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


def test_projection_pass_derives_and_checks_cleanup_once(monkeypatch):
    calls = []
    outcomes = iter((True, True, True, False))

    @contextlib.contextmanager
    def session_context():
        yield types.SimpleNamespace()

    monkeypatch.setattr(agents, "database_session_context", session_context)
    monkeypatch.setattr(
        agents.v2_projection,
        "process_one_provider_file_cleanup_task",
        lambda _session, _worker_id: calls.append("cleanup") or False,
    )
    monkeypatch.setattr(
        agents.v2_projection,
        "derive_projection_tasks",
        lambda _session: calls.append("derive") or 4,
    )

    def process(_session, _worker_id, **_kwargs):
        calls.append("process")
        return next(outcomes)

    monkeypatch.setattr(
        agents.v2_projection,
        "process_one_projection_task",
        process,
    )
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        v2_projection_max_tasks_per_iteration=10,
        v2_metrics_log_interval_seconds=300,
        projection_deriver=True,
    )

    assert worker._run_v2_projection_tasks() is True
    assert calls.count("cleanup") == 1
    assert calls.count("derive") == 1
    assert calls.count("process") == 4


def test_execution_stats_report_clock_skew_without_negative_latencies():
    now = datetime.datetime.now(datetime.timezone.utc)
    task = {
        "created_at": now - datetime.timedelta(milliseconds=100),
        "outbox_created_at": now + datetime.timedelta(milliseconds=200),
        "task_age_seconds": 0.1,
        "outbox_age_seconds": -0.2,
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
