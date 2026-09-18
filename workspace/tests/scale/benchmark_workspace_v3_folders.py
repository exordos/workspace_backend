# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Measure lock-light Workspace v3 folder projections on PostgreSQL.

The benchmark accepts only a disposable database whose name contains ``test``.
It compares user fan-out, 1/2/4/8 projection workers, and a one-stream
incremental change against the row count of a full folder rebuild.
"""

import argparse
import concurrent.futures
import json
import math
import os
import statistics
import threading
import time
import urllib.parse
import uuid as sys_uuid

import psycopg

from workspace.workspace_v3 import projections


DEFAULT_DATABASE_URL = os.environ.get(
    "WORKSPACE_TEST_DB_URL",
    "postgresql://workspace:pass@localhost:5432/workspace_test",
)
DEFAULT_USERS = "1,10,100,275"
DEFAULT_WORKERS = "1,2,4,8"


def _parse_positive_list(parser, value, name):
    try:
        values = [int(item) for item in value.split(",")]
    except ValueError:
        parser.error(f"{name} must be comma-separated integers")
    if not values or any(item < 1 for item in values):
        parser.error(f"{name} must contain positive integers")
    return values


def _validate_database_url(parser, database_url):
    database_name = urllib.parse.urlsplit(database_url).path.lstrip("/")
    if "test" not in database_name.lower():
        parser.error("the database name must contain 'test'")


def _clear_schema(connection):
    connection.execute(
        """
        TRUNCATE TABLE
            workspace_v3.event_recipient_payloads,
            workspace_v3.events,
            workspace_v3.event_audience_members,
            workspace_v3.event_audience_snapshots,
            workspace_v3.event_cursors,
            workspace_v3.projection_tasks,
            workspace_v3.folder_items,
            workspace_v3.folders,
            workspace_v3.message_reactions,
            workspace_v3.message_flags,
            workspace_v3.messages,
            workspace_v3.topic_bindings,
            workspace_v3.stream_bindings,
            workspace_v3.topics,
            workspace_v3.streams,
            workspace_v3.users
        RESTART IDENTITY CASCADE
        """
    )


def _seed(connection, user_count, stream_count):
    project_id = sys_uuid.uuid4()
    users = [sys_uuid.uuid4() for _index in range(user_count)]
    streams = [sys_uuid.uuid4() for _index in range(stream_count)]
    connection.execute(
        """
        INSERT INTO workspace_v3.users (
            uuid, created_at, updated_at, username, source, status, avatar
        )
        SELECT input.uuid, NOW(), NOW(), 'folder-benchmark-' || input.uuid,
               'iam', 'active',
               'urn:gravatar:00000000000000000000000000000000'
        FROM unnest(%s::uuid[]) AS input(uuid)
        """,
        (users,),
    )
    connection.execute(
        """
        INSERT INTO workspace_v3.streams (
            uuid, project_id, name, owner_uuid
        )
        SELECT input.uuid, %s, 'Folder benchmark ' || input.ordinality, %s
        FROM unnest(%s::uuid[]) WITH ORDINALITY AS input(uuid, ordinality)
        """,
        (project_id, users[0], streams),
    )
    started_at = time.perf_counter()
    connection.execute(
        """
        INSERT INTO workspace_v3.stream_bindings (
            uuid, project_id, stream_uuid, user_uuid, who_uuid, role
        )
        SELECT gen_random_uuid(), %s, stream.uuid, workspace_user.uuid, %s,
               CASE WHEN workspace_user.uuid = %s
                    THEN 'owner' ELSE 'member' END
        FROM unnest(%s::uuid[]) AS stream(uuid)
        CROSS JOIN unnest(%s::uuid[]) AS workspace_user(uuid)
        """,
        (project_id, users[0], users[0], streams, users),
    )
    enqueue_seconds = time.perf_counter() - started_at
    connection.execute(
        """
        DELETE FROM workspace_v3.projection_tasks
        WHERE project_id = %s AND task_type = 'read_counters'
        """,
        (project_id,),
    )
    connection.execute("ANALYZE workspace_v3.stream_bindings")
    connection.execute("ANALYZE workspace_v3.projection_tasks")
    return project_id, users, streams, enqueue_seconds


def _database_deadlocks(connection):
    return int(
        connection.execute(
            """
            SELECT deadlocks
            FROM pg_stat_database
            WHERE datname = current_database()
            """
        ).fetchone()[0]
    )


def _lock_sampler(database_url, stop, result):
    connection = psycopg.connect(database_url, autocommit=True)
    try:
        while not stop.wait(0.002):
            waiting = int(
                connection.execute(
                    """
                    SELECT count(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND application_name LIKE 'workspace-v3-folder-benchmark:%'
                      AND wait_event_type = 'Lock'
                    """
                ).fetchone()[0]
            )
            result["samples"] += 1
            result["wait_samples"] += bool(waiting)
            result["max_waiters"] = max(result["max_waiters"], waiting)
    finally:
        connection.close()


def _worker(database_url, worker_number, batch_size):
    connection = psycopg.connect(database_url, autocommit=True)
    worker_id = f"folder-benchmark:{worker_number}:{sys_uuid.uuid4()}"
    connection.execute(
        "SELECT set_config('application_name', %s, false)",
        (f"workspace-v3-folder-benchmark:{worker_number}",),
    )
    batches = []
    totals = {"claimed": 0.0, "completed": 0.0, "failed": 0.0, "events": 0.0}
    try:
        while True:
            started_at = time.perf_counter()
            with connection.transaction():
                tasks = projections.claim_projection_tasks(
                    connection,
                    worker_id,
                    batch_size=batch_size,
                )
            if not tasks:
                break
            with connection.transaction():
                metrics = projections.process_claimed_projection_tasks(
                    connection,
                    worker_id,
                    tasks,
                )
            batches.append((time.perf_counter() - started_at) * 1000)
            for name in totals:
                totals[name] += metrics[name]
    finally:
        connection.close()
    return totals, batches


def _drain(database_url, connection, worker_count, batch_size):
    deadlocks_before = _database_deadlocks(connection)
    stop = threading.Event()
    lock_result = {"samples": 0, "wait_samples": 0, "max_waiters": 0}
    sampler = threading.Thread(
        target=_lock_sampler,
        args=(database_url, stop, lock_result),
        daemon=True,
    )
    sampler.start()
    started_at = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        worker_results = list(
            executor.map(
                lambda worker_number: _worker(
                    database_url,
                    worker_number,
                    batch_size,
                ),
                range(worker_count),
            )
        )
    elapsed_seconds = time.perf_counter() - started_at
    stop.set()
    sampler.join(timeout=2)
    totals = {
        name: sum(result[0][name] for result in worker_results)
        for name in ("claimed", "completed", "failed", "events")
    }
    batches = sorted(
        timing for _totals, timings in worker_results for timing in timings
    )
    return {
        **totals,
        "elapsed_seconds": elapsed_seconds,
        "tasks_per_second": totals["completed"] / elapsed_seconds,
        "batch_p50_ms": statistics.median(batches) if batches else 0.0,
        "batch_p95_ms": (
            batches[math.ceil(len(batches) * 0.95) - 1] if batches else 0.0
        ),
        "deadlocks": _database_deadlocks(connection) - deadlocks_before,
        "lock_samples": lock_result["samples"],
        "lock_wait_samples": lock_result["wait_samples"],
        "max_lock_waiters": lock_result["max_waiters"],
    }


def _assert_initial_state(connection, project_id, user_count, stream_count):
    folders, items, pending, failed = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM workspace_v3.folders WHERE project_id = %s),
            (SELECT count(*) FROM workspace_v3.folder_items WHERE project_id = %s),
            (SELECT count(*) FROM workspace_v3.projection_tasks
             WHERE project_id = %s AND status IN ('pending', 'running')),
            (SELECT count(*) FROM workspace_v3.projection_tasks
             WHERE project_id = %s AND status IN ('failed', 'dead_letter'))
        """,
        (project_id, project_id, project_id, project_id),
    ).fetchone()
    if (folders, items, pending, failed) != (
        user_count * 3,
        user_count * stream_count * 2,
        0,
        0,
    ):
        raise RuntimeError("folder benchmark correctness check failed")
    return folders, items


def _run_scenario(database_url, connection, user_count, stream_count, workers, batch):
    _clear_schema(connection)
    project_id, _users, streams, enqueue_seconds = _seed(
        connection,
        user_count,
        stream_count,
    )
    initial = _drain(database_url, connection, workers, batch)
    folders, items = _assert_initial_state(
        connection,
        project_id,
        user_count,
        stream_count,
    )
    folder_storage_bytes = int(
        connection.execute(
            """
            SELECT sum(pg_total_relation_size(input.name::regclass))
            FROM unnest(%s::text[]) AS input(name)
            """,
            (
                [
                    "workspace_v3.folders",
                    "workspace_v3.folder_items",
                ],
            ),
        ).fetchone()[0]
    )
    connection.execute(
        "DELETE FROM workspace_v3.projection_tasks WHERE project_id = %s",
        (project_id,),
    )
    started_at = time.perf_counter()
    connection.execute(
        """
        UPDATE workspace_v3.streams
        SET is_archived = true, updated_at = clock_timestamp()
        WHERE project_id = %s AND uuid = %s
        """,
        (project_id, streams[0]),
    )
    archive_enqueue_seconds = time.perf_counter() - started_at
    incremental = _drain(database_url, connection, workers, batch)
    expected_items = user_count * (stream_count - 1) * 2
    actual_items = int(
        connection.execute(
            "SELECT count(*) FROM workspace_v3.folder_items WHERE project_id = %s",
            (project_id,),
        ).fetchone()[0]
    )
    if actual_items != expected_items or incremental["failed"]:
        raise RuntimeError("incremental folder benchmark correctness check failed")
    return {
        "users": user_count,
        "streams": stream_count,
        "workers": workers,
        "bindings": user_count * stream_count,
        "folders": folders,
        "folder_items": items,
        "folder_storage_bytes": folder_storage_bytes,
        "enqueue_seconds": enqueue_seconds,
        "initial_projection": initial,
        "archive_enqueue_seconds": archive_enqueue_seconds,
        "incremental_projection": incremental,
        "incremental_rows_removed": user_count * 2,
        "full_rebuild_rows_rewritten_reference": user_count * stream_count * 2,
        "row_touch_reduction_factor": float(stream_count),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--users", default=DEFAULT_USERS)
    parser.add_argument("--workers", default=DEFAULT_WORKERS)
    parser.add_argument("--streams", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    _validate_database_url(parser, args.database_url)
    user_counts = _parse_positive_list(parser, args.users, "users")
    worker_counts = _parse_positive_list(parser, args.workers, "workers")
    if args.streams < 1 or args.batch_size < 1:
        parser.error("streams and batch size must be positive")
    connection = psycopg.connect(args.database_url, autocommit=True)
    try:
        user_scale = [
            _run_scenario(
                args.database_url,
                connection,
                users,
                args.streams,
                1,
                args.batch_size,
            )
            for users in user_counts
        ]
        concurrency = [
            _run_scenario(
                args.database_url,
                connection,
                max(user_counts),
                args.streams,
                workers,
                args.batch_size,
            )
            for workers in worker_counts
        ]
    finally:
        _clear_schema(connection)
        connection.close()
    print(
        json.dumps(
            {
                "database": urllib.parse.urlsplit(args.database_url).path.lstrip("/"),
                "user_scale": user_scale,
                "concurrency": concurrency,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
