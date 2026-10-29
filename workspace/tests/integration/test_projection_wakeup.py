# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import signal
import subprocess
import sys
import threading
import time
import types
import uuid as sys_uuid

import pytest

from workspace.common import database
from workspace.services.messenger_workers import agents
from workspace.services.messenger_workers import projection_wakeup
from workspace.tests.integration import conftest


@pytest.fixture
def listener(monkeypatch):
    name = f"cassi-wakeup-{sys_uuid.uuid4()}"
    url = database.connection_url_for_process(conftest.TEST_DB_URL, name)
    wakeup = projection_wakeup.ProjectionQueueWakeup(url, mutations=True)
    ready = threading.Event()
    connections = []
    connect = wakeup._connect

    def tracked_connect():
        connection = connect()
        connections.append(connection)
        ready.set()
        return connection

    monkeypatch.setattr(wakeup, "_connect", tracked_connect)
    try:
        yield types.SimpleNamespace(
            wakeup=wakeup, ready=ready, connections=connections, name=name
        )
    finally:
        wakeup.close()


def _start_wait(listener, timeout):
    listener.ready.clear()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(listener.wakeup.wait(timeout))
    )
    thread.start()
    assert listener.ready.wait(5), "LISTEN did not become ready"
    return thread, results


def _assert_no_listener(db, listener):
    assert all(connection.closed for connection in listener.connections)
    # pg_stat_activity can briefly retain an already closed backend.
    deadline = time.monotonic() + 2
    while db.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
        (listener.name,),
    ).fetchone()[0]:
        assert time.monotonic() < deadline, "LISTEN backend survived its wait"
        time.sleep(0.01)


@pytest.mark.parametrize("projection_only", [False, True])
def test_busy_worker_has_no_listener_and_vacuum_removes_versions(
    db, listener, monkeypatch, projection_only, record_property
):
    # Enter idle once, then process continuously without calling wait again.
    assert listener.wakeup.wait(0.02) is False
    worker = agents.MessengerWorkerAgent(
        v2_projection_enabled=True,
        projection_only=projection_only,
    )
    worker._v2_projection_wakeup = listener.wakeup
    worker._last_history_prune = worker._last_event_prune = time.monotonic()
    monkeypatch.setattr(worker, "_summarize_one_topic", lambda: False)
    monkeypatch.setattr(
        agents.messenger_dm_helpers,
        "mark_stale_workspace_users_offline",
        lambda **kwargs: None,
    )
    db.execute("CREATE TEMP TABLE wakeup_vacuum_probe (id int, payload text)")
    db.execute(
        "INSERT INTO wakeup_vacuum_probe SELECT id, repeat('x', 512) "
        "FROM generate_series(1, 7) AS id"
    )
    iterations = []

    def busy():
        index = len(iterations)
        db.execute("UPDATE wakeup_vacuum_probe SET payload = repeat('x', 512)")
        # Distinct payloads avoid PostgreSQL's within-transaction deduplication.
        db.execute(
            "SELECT pg_notify(channel, %s || id || repeat('x', 7800)) "
            "FROM unnest(ARRAY['workspace_projection_tasks', "
            "'workspace_mutation_commands']) AS channel "
            "CROSS JOIN generate_series(1, 50) AS id",
            (str(index),),
        )
        iterations.append(index)
        _assert_no_listener(db, listener)
        return True

    monkeypatch.setattr(worker, "_run_v2_projection_tasks", busy)
    try:
        for _ in range(40):
            worker._iteration()
        assert len(iterations) == 40
        db.execute("VACUUM wakeup_vacuum_probe")
        dead = db.execute(
            "SELECT n_dead_tup FROM pg_stat_all_tables "
            "WHERE relid = 'wakeup_vacuum_probe'::regclass"
        ).fetchone()[0]
        assert dead == 0
        plan = db.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) "
            "SELECT * FROM wakeup_vacuum_probe WHERE id = 999"
        ).fetchone()[0][0]
        record_property("empty_select_ms", plan["Execution Time"])
        record_property("notifications_while_busy", 4000)
        assert plan["Execution Time"] < 100
    finally:
        worker.stop()
        db.execute("DROP TABLE wakeup_vacuum_probe")
    _assert_no_listener(db, listener)


@pytest.mark.parametrize(
    "channel",
    [
        "workspace_projection_tasks",
        "workspace_mutation_commands",
    ],
)
def test_idle_wakes_for_either_subscription(db, listener, channel):
    thread, results = _start_wait(listener, 2)
    try:
        row = db.execute(
            "SELECT backend_xmin, state FROM pg_stat_activity WHERE pid = %s",
            (listener.connections[-1].info.backend_pid,),
        ).fetchone()
        assert row == (None, "idle")
        started = time.monotonic()
        db.execute("SELECT pg_notify(%s, 'work')", (channel,))
        thread.join(1)
        assert not thread.is_alive()
        assert results == [True]
        assert time.monotonic() - started < 1
    finally:
        listener.wakeup.close()
        thread.join(5)
    _assert_no_listener(db, listener)


def test_notification_burst_is_coalesced_and_cannot_accumulate(db, listener):
    thread, results = _start_wait(listener, 2)
    try:
        db.execute(
            "SELECT pg_notify('workspace_projection_tasks', "
            "id || repeat('x', 7000)) FROM generate_series(1, 2000) AS id"
        )
        thread.join(2)
        assert not thread.is_alive()
        assert results == [True]
        _assert_no_listener(db, listener)
        # A fresh idle wait has no backlog of the previous burst to spin over.
        started = time.monotonic()
        assert listener.wakeup.wait(0.1) is False
        assert time.monotonic() - started >= 0.09
    finally:
        listener.wakeup.close()
        thread.join(5)
    _assert_no_listener(db, listener)


@pytest.mark.parametrize(
    "channel",
    [
        "workspace_projection_tasks",
        "workspace_mutation_commands",
    ],
)
def test_disconnect_reconnects_and_restores_both_subscriptions(db, listener, channel):
    thread, results = _start_wait(listener, 0.1)
    try:
        pid = listener.connections[-1].info.backend_pid
        db.execute("SELECT pg_terminate_backend(%s)", (pid,))
        thread.join(2)
        assert not thread.is_alive()
        assert results == [False]
        _assert_no_listener(db, listener)
        thread, results = _start_wait(listener, 2)
        assert listener.connections[-1].info.backend_pid != pid
        db.execute("SELECT pg_notify(%s, 'retry')", (channel,))
        thread.join(1)
        assert not thread.is_alive()
        assert results == [True]
    finally:
        listener.wakeup.close()
        thread.join(5)
    _assert_no_listener(db, listener)


def test_idle_timeout_polls_and_shutdown_leaves_no_connection_or_thread(db, listener):
    started = time.monotonic()
    assert listener.wakeup.wait(0.1) is False
    assert time.monotonic() - started >= 0.09
    _assert_no_listener(db, listener)
    thread, results = _start_wait(listener, 0.1)
    listener.wakeup.close()
    thread.join(1)
    assert not thread.is_alive()
    assert results == [False]
    _assert_no_listener(db, listener)
    before = len(listener.connections)
    assert listener.wakeup.wait(0.1) is False
    assert len(listener.connections) == before


def test_sigterm_during_idle_wait_exits_and_closes_listener(db):
    name = f"cassi-wakeup-sigterm-{sys_uuid.uuid4()}"
    url = database.connection_url_for_process(conftest.TEST_DB_URL, name)
    program = """
import signal
import sys
from workspace.services.messenger_workers import agents

worker = agents.MessengerWorkerAgent(
    v2_projection_enabled=True, v2_projection_db_url=sys.argv[1]
)
signal.signal(signal.SIGTERM, lambda *_: worker.stop())
worker._v2_projection_wakeup.wait(0.5)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, url],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 5
        while not db.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
            "WHERE application_name=%s AND state='idle' "
            "AND query='LISTEN workspace_projection_tasks')",
            (name,),
        ).fetchone()[0]:
            assert process.poll() is None, "Worker exited before LISTEN"
            assert time.monotonic() < deadline, "LISTEN did not become ready"
            time.sleep(0.01)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=2)
        assert process.returncode == 0, output.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    deadline = time.monotonic() + 2
    while db.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name=%s",
        (name,),
    ).fetchone()[0]:
        assert time.monotonic() < deadline, "LISTEN backend survived shutdown"
        time.sleep(0.01)
