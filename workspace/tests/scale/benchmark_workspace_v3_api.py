# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Measure full v3 message writes, fan-out, events, and projection catch-up."""

import argparse
import concurrent.futures
import json
import os
import statistics
import threading
import time
import urllib.parse

import psycopg
from restalchemy.common import contexts
from restalchemy.storage.sql import engines

from workspace.messenger_api.api import v3_store
from workspace.tests.scale import benchmark_workspace_v3_projection


DEFAULT_DATABASE_URL = os.environ.get(
    "WORKSPACE_TEST_DB_URL",
    "postgresql://workspace:pass@localhost:5432/workspace_test",
)


def _positive_list(parser, value, name):
    try:
        result = [int(item) for item in value.split(",")]
    except ValueError:
        parser.error(f"{name} must be comma-separated integers")
    if not result or any(item < 1 for item in result):
        parser.error(f"{name} must contain positive integers")
    return result


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _deadlocks(connection):
    return int(
        connection.execute(
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        ).fetchone()[0]
    )


def _sample_locks(database_url, stop, result):
    connection = psycopg.connect(database_url, autocommit=True)
    try:
        while not stop.wait(0.002):
            waiting = int(
                connection.execute(
                    """
                    SELECT count(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND wait_event_type = 'Lock'
                    """
                ).fetchone()[0]
            )
            result["samples"] += 1
            result["wait_samples"] += bool(waiting)
            result["max_waiters"] = max(result["max_waiters"], waiting)
    finally:
        connection.close()


def _write_message(project_id, stream_uuid, topic_uuid, author_uuid, number):
    started_at = time.perf_counter()
    with contexts.Context().session_manager():
        store = v3_store.MessengerV3Store(project_id, author_uuid)
        store.create_message(
            {
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": f"API benchmark {number}",
                },
            }
        )
    return time.perf_counter() - started_at


def _run(database_url, connection, user_count, message_count, writers, batch_size):
    benchmark_workspace_v3_projection._clear_schema(connection)
    project_id, stream_uuid, topic_uuid, users = (
        benchmark_workspace_v3_projection._seed_topology(connection, user_count)
    )
    connection.execute("TRUNCATE workspace_v3.projection_tasks")
    deadlocks_before = _deadlocks(connection)
    lock_result = {"samples": 0, "wait_samples": 0, "max_waiters": 0}
    stop = threading.Event()
    sampler = threading.Thread(
        target=_sample_locks,
        args=(database_url, stop, lock_result),
        daemon=True,
    )
    sampler.start()
    started_at = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=writers) as executor:
        latencies = list(
            executor.map(
                lambda number: _write_message(
                    project_id,
                    stream_uuid,
                    topic_uuid,
                    users[number % len(users)],
                    number,
                ),
                range(message_count),
            )
        )
    elapsed_seconds = time.perf_counter() - started_at
    stop.set()
    sampler.join(timeout=2)
    flag_rows, events, recipient_payloads, tasks = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM workspace_v3.message_flags
             WHERE project_id = %s),
            (SELECT count(*) FROM workspace_v3.events
             WHERE project_id = %s),
            (SELECT count(*) FROM workspace_v3.event_recipient_payloads
             WHERE project_id = %s),
            (SELECT count(*) FROM workspace_v3.projection_tasks
             WHERE project_id = %s AND status = 'pending')
        """,
        (project_id, project_id, project_id, project_id),
    ).fetchone()
    expected_fanout = user_count * message_count
    if (flag_rows, events, recipient_payloads) != (
        expected_fanout,
        message_count,
        expected_fanout,
    ):
        raise RuntimeError("v3 API benchmark correctness check failed")
    projection = benchmark_workspace_v3_projection._drain(connection, batch_size)
    event_bytes = int(
        connection.execute(
            """
            SELECT COALESCE(sum(pg_column_size(payload)), 0)
            FROM workspace_v3.event_recipient_payloads
            WHERE project_id = %s
            """,
            (project_id,),
        ).fetchone()[0]
    )
    page_latency = benchmark_workspace_v3_projection._page_latency(
        connection,
        project_id,
        users[0],
    )
    return {
        "users": user_count,
        "messages": message_count,
        "writers": writers,
        "write_seconds": elapsed_seconds,
        "messages_per_second": message_count / elapsed_seconds,
        "messages_per_minute": message_count * 60 / elapsed_seconds,
        "user_message_states_per_second": expected_fanout / elapsed_seconds,
        "user_message_states_per_minute": expected_fanout * 60 / elapsed_seconds,
        "request_p50_ms": statistics.median(latencies) * 1000,
        "request_p95_ms": _percentile(latencies, 0.95) * 1000,
        "flag_rows": flag_rows,
        "events": events,
        "recipient_payload_rows": recipient_payloads,
        "recipient_payload_bytes": event_bytes,
        "coalesced_projection_tasks": tasks,
        "projection": projection,
        "page_100": page_latency,
        "deadlocks": _deadlocks(connection) - deadlocks_before,
        "lock_samples": lock_result["samples"],
        "lock_wait_samples": lock_result["wait_samples"],
        "max_lock_waiters": lock_result["max_waiters"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--users", default="1,10,100,275")
    parser.add_argument("--writers", default="1,4")
    parser.add_argument("--messages", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=10000)
    args = parser.parse_args()
    benchmark_workspace_v3_projection._validate_database_url(
        parser,
        args.database_url,
    )
    users = _positive_list(parser, args.users, "users")
    writers = _positive_list(parser, args.writers, "writers")
    if args.messages < 1 or args.batch_size < 1:
        parser.error("messages and batch size must be positive")
    engines.engine_factory.configure_factory(db_url=args.database_url)
    connection = psycopg.connect(args.database_url, autocommit=True)
    try:
        results = [
            _run(
                args.database_url,
                connection,
                user_count,
                args.messages,
                writer_count,
                args.batch_size,
            )
            for user_count in users
            for writer_count in writers
        ]
    finally:
        benchmark_workspace_v3_projection._clear_schema(connection)
        connection.close()
        engines.engine_factory.get_engine()._pool.close()
        engines.engine_factory.destroy_all_engines()
    print(
        json.dumps(
            {
                "database": urllib.parse.urlsplit(args.database_url).path.lstrip("/"),
                "scenarios": results,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
