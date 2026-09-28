# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Measure Workspace v3 message fan-out and projection throughput.

This benchmark accepts only a disposable PostgreSQL database whose name
contains ``test``. It prints current-run measurements and stores no result
artifacts in the repository.
"""

import argparse
import json
import os
import statistics
import time
import urllib.parse
import uuid as sys_uuid

import psycopg

from workspace.workspace_v3 import projections


DEFAULT_DATABASE_URL = os.environ.get(
    "WORKSPACE_TEST_DB_URL",
    "postgresql://workspace:pass@localhost:5432/workspace_test",
)
DEFAULT_SCENARIOS = "1:1000,10:1000,100:1000,275:200"


def _validate_database_url(parser, database_url):
    database_name = urllib.parse.urlsplit(database_url).path.lstrip("/")
    if "test" not in database_name.lower():
        parser.error("the database name must contain 'test'")


def _parse_scenarios(parser, value):
    scenarios = []
    try:
        for item in value.split(","):
            users, messages = (int(part) for part in item.split(":", 1))
            if users < 1 or messages < 1:
                raise ValueError
            scenarios.append((users, messages))
    except ValueError:
        parser.error("scenarios must use positive USERS:MESSAGES pairs")
    return scenarios


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


def _seed_topology(connection, user_count):
    project_id = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    provider_uuid = sys_uuid.uuid4()
    users = [sys_uuid.uuid4() for _index in range(user_count)]
    connection.execute(
        """
        INSERT INTO workspace_v3.users (
            uuid, created_at, updated_at, username, source, status, avatar
        )
        SELECT input.uuid, NOW(), NOW(), 'benchmark-' || input.uuid::text,
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
        ) VALUES (%s, %s, 'Benchmark', %s)
        """,
        (stream_uuid, project_id, users[0]),
    )
    connection.execute(
        """
        INSERT INTO workspace_v3.topics (
            uuid, project_id, stream_uuid, name
        ) VALUES (%s, %s, %s, 'General')
        """,
        (topic_uuid, project_id, stream_uuid),
    )
    connection.execute(
        """
        INSERT INTO workspace_v3.stream_bindings (
            uuid, project_id, stream_uuid, user_uuid, who_uuid,
            role, notification_mode
        )
        SELECT gen_random_uuid(), %s, %s, input.uuid, %s,
               CASE WHEN input.uuid = %s THEN 'owner' ELSE 'member' END,
               'all_messages'
        FROM unnest(%s::uuid[]) AS input(uuid)
        """,
        (project_id, stream_uuid, users[0], users[0], users),
    )
    connection.execute(
        """
        INSERT INTO workspace_v3.topic_bindings (
            uuid, project_id, stream_uuid, topic_uuid, user_uuid
        )
        SELECT gen_random_uuid(), %s, %s, %s, input.uuid
        FROM unnest(%s::uuid[]) AS input(uuid)
        """,
        (project_id, stream_uuid, topic_uuid, users),
    )
    connection.execute(
        """
        INSERT INTO workspace_v3.event_cursors (
            project_id, consumer_type, consumer_uuid
        ) VALUES (%s, 'provider', %s)
        """,
        (project_id, provider_uuid),
    )
    connection.execute("TRUNCATE workspace_v3.projection_tasks")
    return project_id, stream_uuid, topic_uuid, users


def _insert_messages(
    connection,
    project_id,
    stream_uuid,
    topic_uuid,
    users,
    message_count,
):
    seed = str(sys_uuid.uuid4())
    started_at = time.perf_counter()
    connection.execute(
        """
        INSERT INTO workspace_v3.messages (
            uuid, project_id, stream_uuid, topic_uuid,
            author_uuid, payload, created_at, updated_at
        )
        SELECT md5(%s || ':' || input.number::text)::uuid,
               %s, %s, %s,
               (%s::uuid[])[1 + ((input.number - 1) %% %s)],
               jsonb_build_object(
                   'kind', 'markdown',
                   'content', 'benchmark-' || input.number::text
               ),
               NOW() + input.number * INTERVAL '1 microsecond',
               NOW() + input.number * INTERVAL '1 microsecond'
        FROM generate_series(1, %s) AS input(number)
        """,
        (
            seed,
            project_id,
            stream_uuid,
            topic_uuid,
            users,
            len(users),
            message_count,
        ),
    )
    return time.perf_counter() - started_at


def _insert_flags(connection, project_id):
    started_at = time.perf_counter()
    connection.execute(
        """
        INSERT INTO workspace_v3.message_flags (
            uuid, project_id, stream_uuid, message_uuid, user_uuid,
            read, mentioned
        )
        SELECT gen_random_uuid(), message.project_id, message.stream_uuid,
               message.uuid,
               binding.user_uuid, false,
               message.author_uuid = binding.user_uuid
        FROM workspace_v3.messages AS message
        JOIN workspace_v3.stream_bindings AS binding
          ON binding.project_id = message.project_id
         AND binding.stream_uuid = message.stream_uuid
        WHERE message.project_id = %s
        """,
        (project_id,),
    )
    return time.perf_counter() - started_at


def _insert_reactions(connection, project_id, users):
    started_at = time.perf_counter()
    connection.execute(
        """
        INSERT INTO workspace_v3.message_reactions (
            uuid, project_id, message_uuid, user_uuid, emoji_name
        )
        SELECT gen_random_uuid(), message.project_id, message.uuid,
               (%s::uuid[])[
                   1 + ((row_number() OVER (ORDER BY message.uuid) - 1) %% %s)
               ],
               'eyes'
        FROM workspace_v3.messages AS message
        WHERE message.project_id = %s
        """,
        (users, len(users), project_id),
    )
    return time.perf_counter() - started_at


def _analyze(connection, *tables):
    started_at = time.perf_counter()
    for table in tables:
        connection.execute(f"ANALYZE workspace_v3.{table}")
    return time.perf_counter() - started_at


def _drain(connection, batch_size):
    totals = {
        "claimed": 0.0,
        "completed": 0.0,
        "failed": 0.0,
        "operations": 0.0,
        "projections": 0.0,
        "events": 0.0,
    }
    started_at = time.perf_counter()
    batch_count = 0
    while True:
        worker_id = f"benchmark:{sys_uuid.uuid4()}"
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
        batch_count += 1
        for name in totals:
            totals[name] += metrics[name]
    totals["elapsed_seconds"] = time.perf_counter() - started_at
    totals["batches"] = batch_count
    return totals


def _page_latency(connection, project_id, user_uuid):
    timings = []
    for _iteration in range(25):
        started_at = time.perf_counter()
        rows = connection.execute(
            """
            SELECT message.uuid, message.payload, message.reactions,
                   message.reaction_users, flag.read, flag.pinned,
                   flag.starred, flag.mentioned
            FROM workspace_v3.messages AS message
            JOIN workspace_v3.message_flags AS flag
              ON flag.project_id = message.project_id
             AND flag.message_uuid = message.uuid
             AND flag.user_uuid = %s
            WHERE message.project_id = %s
            ORDER BY message.created_at DESC, message.uuid DESC
            LIMIT 100
            """,
            (user_uuid, project_id),
        ).fetchall()
        if not rows:
            raise RuntimeError("benchmark page query returned no messages")
        timings.append((time.perf_counter() - started_at) * 1000)
    timings.sort()
    return {
        "median_ms": statistics.median(timings),
        "p95_ms": timings[int(len(timings) * 0.95) - 1],
    }


def _storage_bytes(connection):
    return int(
        connection.execute(
            """
        SELECT sum(pg_total_relation_size(format('%I.%I', schemaname, tablename)))
        FROM pg_tables
        WHERE schemaname = 'workspace_v3'
        """
        ).fetchone()[0]
    )


def _run_scenario(connection, user_count, message_count, batch_size):
    _clear_schema(connection)
    project_id, stream_uuid, topic_uuid, users = _seed_topology(
        connection,
        user_count,
    )
    message_seconds = _insert_messages(
        connection,
        project_id,
        stream_uuid,
        topic_uuid,
        users,
        message_count,
    )
    flag_seconds = _insert_flags(connection, project_id)
    counter_analyze_seconds = _analyze(
        connection,
        "messages",
        "message_flags",
        "stream_bindings",
        "topic_bindings",
    )
    flag_count = user_count * message_count
    task_count = connection.execute(
        """
        SELECT count(*) FROM workspace_v3.projection_tasks
        WHERE project_id = %s
        """,
        (project_id,),
    ).fetchone()[0]
    counter_drain = _drain(connection, batch_size)
    pending, failed = connection.execute(
        """
        SELECT
            count(*) FILTER (WHERE status IN ('pending', 'running')),
            count(*) FILTER (WHERE status IN ('failed', 'dead_letter'))
        FROM workspace_v3.projection_tasks
        WHERE project_id = %s
        """,
        (project_id,),
    ).fetchone()
    bad_counters = connection.execute(
        """
        SELECT count(*)
        FROM workspace_v3.stream_bindings
        WHERE project_id = %s
          AND (
                unread_count <> %s
                OR active_unread_count <> %s
                OR passive_unread_count <> 0
              )
        """,
        (project_id, message_count, message_count),
    ).fetchone()[0]
    if pending or failed or bad_counters:
        raise RuntimeError("projection benchmark correctness check failed")
    connection.execute(
        "DELETE FROM workspace_v3.projection_tasks WHERE project_id = %s",
        (project_id,),
    )
    reaction_write_seconds = _insert_reactions(connection, project_id, users)
    reaction_analyze_seconds = _analyze(connection, "message_reactions")
    reaction_task_count = connection.execute(
        """
        SELECT count(*) FROM workspace_v3.projection_tasks
        WHERE project_id = %s
        """,
        (project_id,),
    ).fetchone()[0]
    reaction_drain = _drain(connection, batch_size)
    projected_reactions = connection.execute(
        """
        SELECT count(*)
        FROM workspace_v3.messages
        WHERE project_id = %s AND reactions = '{"eyes": 1}'::jsonb
        """,
        (project_id,),
    ).fetchone()[0]
    if projected_reactions != message_count or reaction_drain["failed"]:
        raise RuntimeError("reaction benchmark correctness check failed")
    ingest_seconds = message_seconds + flag_seconds
    counter_end_to_end_seconds = ingest_seconds + counter_drain["elapsed_seconds"]
    counter_bulk_ready_seconds = counter_end_to_end_seconds + counter_analyze_seconds
    reaction_end_to_end_seconds = (
        reaction_write_seconds
        + reaction_analyze_seconds
        + reaction_drain["elapsed_seconds"]
    )
    return {
        "users": user_count,
        "messages": message_count,
        "message_flag_rows": flag_count,
        "projection_tasks": task_count,
        "message_insert_seconds": message_seconds,
        "message_insert_per_second": message_count / message_seconds,
        "flag_fanout_seconds": flag_seconds,
        "flag_rows_per_second": flag_count / flag_seconds,
        "ingest_messages_per_second": message_count / ingest_seconds,
        "counter_end_to_end_seconds": counter_end_to_end_seconds,
        "counter_end_to_end_messages_per_second": message_count
        / counter_end_to_end_seconds,
        "counter_end_to_end_user_message_states_per_second": flag_count
        / counter_end_to_end_seconds,
        "counter_analyze_seconds": counter_analyze_seconds,
        "counter_bulk_ready_seconds": counter_bulk_ready_seconds,
        "counter_bulk_ready_messages_per_second": message_count
        / counter_bulk_ready_seconds,
        "counter_projection_seconds": counter_drain["elapsed_seconds"],
        "counter_tasks_per_second": task_count / counter_drain["elapsed_seconds"],
        "counter_messages_per_second": message_count / counter_drain["elapsed_seconds"],
        "counter_user_message_states_per_second": flag_count
        / counter_drain["elapsed_seconds"],
        "counter_projection_batches": counter_drain["batches"],
        "counter_events": int(counter_drain["events"]),
        "counter_operations": int(counter_drain["operations"]),
        "reaction_write_seconds": reaction_write_seconds,
        "reaction_writes_per_second": message_count / reaction_write_seconds,
        "reaction_analyze_seconds": reaction_analyze_seconds,
        "reaction_end_to_end_seconds": reaction_end_to_end_seconds,
        "reaction_end_to_end_operations_per_second": message_count
        / reaction_end_to_end_seconds,
        "reaction_projection_seconds": reaction_drain["elapsed_seconds"],
        "reaction_tasks": reaction_task_count,
        "reaction_tasks_per_second": reaction_task_count
        / reaction_drain["elapsed_seconds"],
        "reaction_events_per_second": reaction_drain["events"]
        / reaction_drain["elapsed_seconds"],
        "reaction_projection_batches": reaction_drain["batches"],
        "reaction_events": int(reaction_drain["events"]),
        "reaction_operations": int(reaction_drain["operations"]),
        "page_100": _page_latency(connection, project_id, users[0]),
        "storage_bytes": _storage_bytes(connection),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    parser.add_argument("--batch-size", type=int, default=10000)
    args = parser.parse_args()
    _validate_database_url(parser, args.database_url)
    scenarios = _parse_scenarios(parser, args.scenarios)
    if args.batch_size < 1:
        parser.error("batch size must be positive")
    started_at = time.time()
    connection = psycopg.connect(args.database_url, autocommit=True)
    try:
        results = [
            _run_scenario(connection, users, messages, args.batch_size)
            for users, messages in scenarios
        ]
    finally:
        _clear_schema(connection)
        connection.close()
    print(
        json.dumps(
            {
                "started_at_unix": started_at,
                "database": urllib.parse.urlsplit(args.database_url).path.lstrip("/"),
                "batch_size": args.batch_size,
                "scenarios": results,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
