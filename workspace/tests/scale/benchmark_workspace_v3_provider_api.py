# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Benchmark atomic Provider HTTP batches against a disposable v3 database."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import socketserver
import statistics
import threading
import time
import urllib.parse
import uuid as sys_uuid
import wsgiref.simple_server

import psycopg
from restalchemy.storage.sql import engines

from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.tests.integration import conftest
from workspace.tests.scale import benchmark_workspace_v3_api
from workspace.tests.scale import benchmark_workspace_v3_projection


DEFAULT_DATABASE_URL = os.environ.get(
    "WORKSPACE_TEST_DB_URL",
    "postgresql://workspace:pass@localhost:5432/workspace_test",
)
ROOT = "/v1/provider/entities"


class _ThreadingServer(socketserver.ThreadingMixIn, wsgiref.simple_server.WSGIServer):
    daemon_threads = True


def _hash(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _operation(resource, entity_uuid, data):
    return {
        "action": "upsert",
        "type": resource,
        "uuid": str(entity_uuid),
        "content_hash": _hash(data),
        "data": data,
    }


def _chunks(values, size):
    return [values[offset : offset + size] for offset in range(0, len(values), size)]


def _post_batch(api, operations):
    started_at = time.perf_counter()
    response = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={"operations": operations},
    )
    elapsed = time.perf_counter() - started_at
    response.raise_for_status()
    return elapsed, response.json()["results"]


def _seed(api, connection, user_count):
    provider_uuid = sys_uuid.uuid4()
    connection.execute(
        """
        INSERT INTO workspace_v3.provider_consumers (
            uuid, project_id, name, iam_user_uuid
        ) VALUES (%s, %s, 'zulip', %s)
        """,
        (provider_uuid, api.project_id, api.user_uuid),
    )
    created_at = "2026-09-19T08:00:00Z"
    users = [sys_uuid.uuid4() for _index in range(user_count)]
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    operations = []
    for user_uuid in users:
        operations.append(
            _operation(
                "users",
                user_uuid,
                {
                    "username": f"provider-benchmark-{user_uuid}",
                    "display_name": f"Provider benchmark {user_uuid}",
                    "status": "active",
                    "created_at": created_at,
                },
            )
        )
    operations.append(
        _operation(
            "streams",
            stream_uuid,
            {
                "name": "Provider benchmark",
                "owner_uuid": str(users[0]),
                "default_topic_uuid": str(topic_uuid),
                "created_at": created_at,
            },
        )
    )
    operations.append(
        _operation(
            "topics",
            topic_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "name": "General",
                "created_at": created_at,
            },
        )
    )
    for index, user_uuid in enumerate(users):
        operations.append(
            _operation(
                "stream_bindings",
                sys_uuid.uuid4(),
                {
                    "stream_uuid": str(stream_uuid),
                    "user_uuid": str(user_uuid),
                    "who_uuid": str(users[0]),
                    "role": "owner" if index == 0 else "member",
                    "created_at": created_at,
                },
            )
        )
    for user_uuid in users:
        operations.append(
            _operation(
                "topic_bindings",
                sys_uuid.uuid4(),
                {
                    "stream_uuid": str(stream_uuid),
                    "topic_uuid": str(topic_uuid),
                    "user_uuid": str(user_uuid),
                    "created_at": created_at,
                },
            )
        )
    for chunk in _chunks(operations, 500):
        _post_batch(api, chunk)
    connection.execute(
        """
        TRUNCATE workspace_v3.events,
                 workspace_v3.event_recipient_payloads,
                 workspace_v3.event_audience_members,
                 workspace_v3.event_audience_snapshots,
                 workspace_v3.event_cursors,
                 workspace_v3.projection_tasks
        RESTART IDENTITY CASCADE
        """
    )
    return provider_uuid, users, stream_uuid, topic_uuid


def _message_operations(message_count, users, stream_uuid, topic_uuid):
    created_at = "2026-09-19T08:00:01Z"
    return [
        _operation(
            "messages",
            sys_uuid.uuid4(),
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "author_uuid": str(users[number % len(users)]),
                "payload": {
                    "kind": "markdown",
                    "content": f"Provider benchmark message {number}",
                },
                "created_at": created_at,
            },
        )
        for number in range(message_count)
    ]


def _run_batches(api, operations, batch_size, writers):
    batches = _chunks(operations, batch_size)
    started_at = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=writers) as executor:
        responses = list(executor.map(lambda batch: _post_batch(api, batch), batches))
    elapsed = time.perf_counter() - started_at
    latencies = [item[0] for item in responses]
    statuses = [result["status"] for _latency, results in responses for result in results]
    return {
        "seconds": elapsed,
        "entities_per_second": len(operations) / elapsed,
        "request_count": len(batches),
        "request_p50_ms": statistics.median(latencies) * 1000,
        "request_p95_ms": benchmark_workspace_v3_api._percentile(latencies, 0.95)
        * 1000,
        "statuses": {status: statuses.count(status) for status in set(statuses)},
    }


def _list_all(api):
    count = 0
    parameters = {"limit": 500}
    started_at = time.perf_counter()
    requests = 0
    while True:
        response = api.get(f"{ROOT}/messages", params=parameters)
        response.raise_for_status()
        requests += 1
        body = response.json()
        count += len(body["items"])
        if body["next_cursor"] is None:
            break
        parameters = {"limit": 500, **body["next_cursor"]}
    return {
        "entities": count,
        "requests": requests,
        "seconds": time.perf_counter() - started_at,
    }


def _run(database_url, connection, base_url, users, messages, batch_size, writers):
    benchmark_workspace_v3_projection._clear_schema(connection)
    api = conftest.ApiClient(base_url, sys_uuid.uuid4(), sys_uuid.uuid4())
    provider_uuid, user_uuids, stream_uuid, topic_uuid = _seed(
        api, connection, users
    )
    operations = _message_operations(
        messages, user_uuids, stream_uuid, topic_uuid
    )
    deadlocks_before = benchmark_workspace_v3_api._deadlocks(connection)
    lock_result = {"samples": 0, "wait_samples": 0, "max_waiters": 0}
    stop = threading.Event()
    sampler = threading.Thread(
        target=benchmark_workspace_v3_api._sample_locks,
        args=(database_url, stop, lock_result),
        daemon=True,
    )
    sampler.start()
    try:
        insert = _run_batches(api, operations, batch_size, writers)
        replay = _run_batches(api, operations, batch_size, writers)
    finally:
        stop.set()
        sampler.join(timeout=2)
    listing = _list_all(api)
    counts = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM workspace_v3.messages WHERE project_id = %s),
            (SELECT count(*) FROM workspace_v3.message_flags WHERE project_id = %s),
            (SELECT count(*) FROM workspace_v3.events WHERE project_id = %s),
            (SELECT count(*) FROM workspace_v3.event_recipient_payloads
             WHERE project_id = %s),
            (SELECT count(*) FROM workspace_v3.provider_entity_states
             WHERE project_id = %s AND provider_uuid = %s),
            (SELECT count(*) FROM workspace_v3.event_audience_members AS audience
             JOIN workspace_v3.events AS event
               ON event.project_id = audience.project_id
              AND event.audience_snapshot_uuid = audience.audience_snapshot_uuid
             WHERE event.project_id = %s
               AND audience.consumer_type = 'provider'
               AND audience.consumer_uuid = %s)
        """,
        (
            api.project_id,
            api.project_id,
            api.project_id,
            api.project_id,
            api.project_id,
            provider_uuid,
            api.project_id,
            provider_uuid,
        ),
    ).fetchone()
    expected_flags = users * messages
    if counts[:4] != (messages, expected_flags, messages, expected_flags):
        raise RuntimeError(f"Provider benchmark correctness failed: {counts}")
    if counts[5] != 0 or listing["entities"] != messages:
        raise RuntimeError("Provider echo suppression or listing check failed")
    relation_bytes = int(
        connection.execute(
            """
            SELECT sum(pg_total_relation_size(table_name::regclass))
            FROM unnest(%s::text[]) AS input(table_name)
            """,
            (
                [
                    "workspace_v3.messages",
                    "workspace_v3.message_flags",
                    "workspace_v3.provider_entity_states",
                    "workspace_v3.events",
                    "workspace_v3.event_recipient_payloads",
                ],
            ),
        ).fetchone()[0]
    )
    return {
        "users": users,
        "messages": messages,
        "batch_size": batch_size,
        "writers": writers,
        "insert": insert,
        "idempotent_replay": replay,
        "list": listing,
        "message_flags": counts[1],
        "events": counts[2],
        "recipient_payloads": counts[3],
        "provider_states": counts[4],
        "provider_echo_events": counts[5],
        "relation_bytes": relation_bytes,
        "deadlocks": benchmark_workspace_v3_api._deadlocks(connection)
        - deadlocks_before,
        "lock_samples": lock_result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--messages", type=int, default=2000)
    parser.add_argument("--batch-sizes", default="1,100,500")
    parser.add_argument("--writers", default="1,4")
    args = parser.parse_args()
    benchmark_workspace_v3_projection._validate_database_url(
        parser, args.database_url
    )
    batch_sizes = benchmark_workspace_v3_api._positive_list(
        parser, args.batch_sizes, "batch-sizes"
    )
    writers = benchmark_workspace_v3_api._positive_list(
        parser, args.writers, "writers"
    )
    if args.users < 1 or args.messages < 1:
        parser.error("users and messages must be positive")
    engines.engine_factory.configure_factory(db_url=args.database_url)
    api_store.configure_store_factory(store_factory.build_v3_store_factory())
    server = wsgiref.simple_server.make_server(
        "127.0.0.1",
        0,
        conftest.build_test_wsgi_application(),
        server_class=_ThreadingServer,
        handler_class=conftest._QuietHandler,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    connection = psycopg.connect(args.database_url, autocommit=True)
    try:
        scenarios = [
            _run(
                args.database_url,
                connection,
                base_url,
                args.users,
                args.messages,
                batch_size,
                writer_count,
            )
            for batch_size in batch_sizes
            for writer_count in writers
        ]
    finally:
        benchmark_workspace_v3_projection._clear_schema(connection)
        connection.close()
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()
        api_store.reset_store_factory()
        engines.engine_factory.get_engine()._pool.close()
        engines.engine_factory.destroy_all_engines()
    print(
        json.dumps(
            {
                "database": urllib.parse.urlsplit(args.database_url).path.lstrip("/"),
                "scenarios": scenarios,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
