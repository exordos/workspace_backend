# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Opt-in lab measurements; invoke through tools.benchmark_unreads."""

import json
import os
import pathlib
import time
import uuid
from unittest import mock

import pytest
from oslo_config import cfg
from restalchemy.common import contexts

from tools import benchmark_unreads as benchmark
from workspace.common import mutation_journal_opts
from workspace.services.messenger_workers import counters
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal

conversation = journal.conversation
OPTIONS = json.loads(os.environ.get("WORKSPACE_UNREAD_BENCHMARK", "{}"))
pytestmark = pytest.mark.skipif(
    not OPTIONS, reason="Run tools.benchmark_unreads in a disposable database"
)


def record(kind, **values):
    row = {"kind": kind, **values}
    with pathlib.Path(OPTIONS["output"]).open("a") as target:
        target.write(json.dumps(row, default=str) + "\n")
    print(json.dumps(row, default=str), flush=True)


def count_series(db, values, rows, mode, stage):
    expected = (
        min(rows, 1001),
        min(rows, 1001) if mode in ("all", "mentions_all") else 0,
    )
    samples = {"baseline": [], "candidate": []}
    statements = {
        "baseline": benchmark.BASELINE_COUNT_TOPIC_SQL,
        "candidate": counters.COUNT_TOPIC_SQL,
    }
    for repeat in range(25):
        for name in (
            ("baseline", "candidate") if repeat % 2 else ("candidate", "baseline")
        ):
            started = time.perf_counter_ns()
            actual = db.execute(statements[name], values).fetchone()
            elapsed = (time.perf_counter_ns() - started) / 1e6
            assert actual == expected, (name, mode, actual, expected)
            if repeat >= 5:
                samples[name].append(elapsed)
    plans = {
        name: benchmark.plan_summary(
            db.execute(
                "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + statement, values
            ).fetchone()[0][0]
        )
        for name, statement in statements.items()
    }
    assert not {
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
    }.intersection(row["table"] for row in plans["candidate"]["relations"])
    record(
        "count_sql",
        rows=rows,
        mode=mode,
        stage=stage,
        samples={name: benchmark.stats(values) for name, values in samples.items()},
        plans=plans,
    )


def worker_series(api, db, values, stream, peer, mode):
    samples = {"baseline": [], "candidate": []}
    for repeat in range(8):
        for name in (
            ("baseline", "candidate") if repeat % 2 else ("candidate", "baseline")
        ):
            db.execute("DELETE FROM messenger_counter_updates")
            db.execute(
                "UPDATE messenger_user_topic_bindings SET unread_count=0,active_unread_count=0,passive_unread_count=0 WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
                values,
            )
            db.execute(
                "SELECT messenger_queue_counters(%s,%s,%s,%s)",
                (values[0], peer, stream["uuid"], values[2]),
            )
            before = db.execute(
                "SELECT coalesce(max(epoch_version),0) FROM m_workspace_visible_events WHERE project_id=%s AND user_uuid=%s",
                values[:2],
            ).fetchone()[0]
            statement = (
                benchmark.BASELINE_COUNT_TOPIC_SQL
                if name == "baseline"
                else counters.COUNT_TOPIC_SQL
            )
            started = time.perf_counter_ns()
            with mock.patch.object(counters, "COUNT_TOPIC_SQL", statement):
                with contexts.Context().session_manager() as session:
                    assert counters.process_pending(session) == 1
            benchmark.flush_publication()
            response = api.get(f"{native.STREAM_TOPICS}{values[2]}", user=peer)
            elapsed = (time.perf_counter_ns() - started) / 1e6
            assert response.status_code == 200, response.text
            assert response.json()["unread_count"] == 1001
            assert response.json()["active_unread_count"] == (
                1001 if mode == "all" else 0
            )
            delivered = db.execute(
                "SELECT epoch_version,payload FROM m_workspace_visible_events WHERE project_id=%s AND user_uuid=%s AND payload->>'uuid'=%s AND payload ? 'unread_count' ORDER BY epoch_version DESC LIMIT 1",
                values,
            ).fetchone()
            assert delivered[0] > before and delivered[1]["unread_count"] == 1001
            if repeat >= 3:
                samples[name].append(elapsed)
    record(
        "worker_publication_http",
        rows=435478,
        mode=mode,
        samples={name: benchmark.stats(values) for name, values in samples.items()},
    )


def test_large_count_matrix(api, db, conversation):
    stream, _, peer = conversation
    values = (api.project_id, peer, stream["default_topic_uuid"])
    record(
        "environment",
        postgres=db.execute("SHOW server_version").fetchone()[0],
        workload="Synthetic dense topic; warmed plans and buffers; no UI or scheduler queue wait",
    )
    for table in (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
        "messenger_user_message_unreads",
    ):
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    previous = 2
    for rows in (2, 1000, 30000, 435478):
        if rows > previous:
            benchmark.seed(db, api, stream, peer, rows - previous)
        if rows != 435478:
            db.execute(
                "ANALYZE messenger_messages,messenger_message_placements,messenger_user_message_bindings,messenger_user_message_unreads,messenger_user_message_mentions,messenger_user_topic_bindings,messenger_stream_bindings,messenger_topics,messenger_streams"
            )
        for mode, policy in (
            ("all", "all_messages"),
            ("none", "muted"),
            ("mentions_empty", "mentions_only"),
        ):
            db.execute(
                "UPDATE messenger_stream_bindings SET notification_mode=%s WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
                (policy, api.project_id, peer, stream["uuid"]),
            )
            count_series(
                db,
                values,
                rows,
                mode,
                "stale_30k_statistics" if rows == 435478 else "analyzed",
            )
        previous = rows
    db.execute("VACUUM (ANALYZE) messenger_user_message_unreads")
    db.execute(
        "ANALYZE messenger_messages,messenger_message_placements,messenger_user_message_bindings"
    )
    for mode, policy in (("all", "all_messages"), ("mentions_empty", "mentions_only")):
        db.execute(
            "UPDATE messenger_stream_bindings SET notification_mode=%s WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
            (policy, api.project_id, peer, stream["uuid"]),
        )
        count_series(db, values, 435478, mode, "vacuumed_analyzed")
        worker_series(api, db, values, stream, peer, mode)
    # Dense positive mentions exercise hits as well as missing sparse keys.
    db.execute("ALTER TABLE messenger_user_message_mentions DISABLE TRIGGER USER")
    try:
        db.execute(
            "INSERT INTO messenger_user_message_mentions SELECT * FROM messenger_user_message_unreads WHERE project_id=%s AND user_uuid=%s ON CONFLICT DO NOTHING",
            values[:2],
        )
    finally:
        db.execute("ALTER TABLE messenger_user_message_mentions ENABLE TRIGGER USER")
    db.execute("ANALYZE messenger_user_message_mentions")
    count_series(db, values, 435478, "mentions_all", "vacuumed_analyzed")


@pytest.mark.parametrize("recipients", [2, 100])
def test_physical_delete_pipeline(api, db, conversation, recipients):
    stream, _, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    if recipients > 2:
        users = [uuid.uuid4() for _ in range(recipients - 2)]
        for user in users:
            conftest.seed_workspace_user(db, user, "cassi-benchmark-" + str(user))
            native._register_project_user(db, api.project_id, user)
        response = api.post(
            f"{native.STREAMS}{stream['uuid']}/actions/add_users/invoke",
            json={"member": [str(user) for user in users]},
        )
        assert response.status_code == 200, response.text
        native._drain()
        benchmark.flush_publication()
    samples = {"http": [], "http_worker_publication": []}
    for repeat in range(6):
        response = api.post(
            native.MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "payload": {
                    "kind": "markdown",
                    "content": "Physical deletion benchmark",
                },
            },
        )
        assert response.status_code == 201, response.text
        message = response.json()
        native._drain()
        benchmark.flush_publication()
        canonical = db.execute(
            "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
            (message["uuid"],),
        ).fetchone()[0]
        started = time.perf_counter_ns()
        response = api.delete(f"{native.MESSAGES}{message['uuid']}")
        http_ms = (time.perf_counter_ns() - started) / 1e6
        assert response.status_code == 204, response.text
        assert (
            db.execute(
                "SELECT 1 FROM messenger_messages WHERE uuid=%s", (canonical,)
            ).fetchone()
            is None
        )
        native._drain()
        benchmark.flush_publication()
        total_ms = (time.perf_counter_ns() - started) / 1e6
        assert (
            api.get(f"{native.MESSAGES}{message['uuid']}", user=peer).status_code == 404
        )
        assert db.execute(
            "SELECT count(DISTINCT user_uuid) FROM m_workspace_visible_events WHERE project_id=%s AND action='deleted' AND payload->>'uuid'=%s",
            (api.project_id, message["uuid"]),
        ).fetchone() == (recipients,)
        if repeat:
            samples["http"].append(http_ms)
            samples["http_worker_publication"].append(total_ms)
    record(
        "physical_delete",
        recipients=recipients,
        samples={name: benchmark.stats(values) for name, values in samples.items()},
    )
