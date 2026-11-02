# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Measure prepared unread counts and physical deletion in a disposable database.

WORKSPACE_TEST_DB_URL identifies a maintenance database with CREATE DATABASE
permission. Only a newly created benchmark database is migrated and cleared.
Run python -m tools.benchmark_unreads --output /tmp/unread-results.jsonl.
"""

import argparse
import json
import math
import os
import pathlib
import statistics
import urllib.parse
import uuid

import psycopg
from psycopg import sql
import pytest
from restalchemy.common import contexts

from workspace.messenger_api import publication


BASELINE_COUNT_TOPIC_SQL = """
    SELECT count(*) AS unread_count,
           count(*) FILTER (WHERE is_active) AS active_unread_count
    FROM (
        SELECT CASE
               WHEN personal.notification_mode='mute' THEN stream.source_name='zulip' AND mention.placement_uuid IS NOT NULL
               WHEN personal.notification_mode='follow' THEN true
               WHEN personal.notification_mode='unmute' THEN stream.source_name='zulip' OR mention.placement_uuid IS NOT NULL
               WHEN membership.notification_mode='all_messages' THEN true
               WHEN membership.notification_mode='mentions_only' THEN mention.placement_uuid IS NOT NULL
               ELSE false END AS is_active
        FROM messenger_user_message_unreads unread
        JOIN messenger_user_message_bindings access ON
            (access.project_id,access.user_uuid,access.placement_uuid)=(unread.project_id,unread.user_uuid,unread.placement_uuid)
        JOIN messenger_message_placements placement ON placement.project_id=unread.project_id AND placement.uuid=unread.placement_uuid
        JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid AND message.deleted_at IS NULL
        JOIN messenger_topics topic ON topic.project_id=unread.project_id AND topic.uuid=unread.topic_uuid AND topic.deleted_at IS NULL
        JOIN messenger_streams stream ON stream.project_id=unread.project_id AND stream.uuid=unread.stream_uuid AND stream.deleted_at IS NULL
        JOIN messenger_stream_bindings membership ON membership.project_id=unread.project_id AND membership.user_uuid=unread.user_uuid
            AND membership.stream_uuid=unread.stream_uuid AND membership.active
        LEFT JOIN messenger_user_topic_bindings personal ON personal.project_id=unread.project_id
            AND personal.user_uuid=unread.user_uuid AND personal.topic_uuid=unread.topic_uuid
        LEFT JOIN messenger_user_message_mentions mention ON
            (mention.project_id,mention.user_uuid,mention.placement_uuid)=(unread.project_id,unread.user_uuid,unread.placement_uuid)
        WHERE unread.project_id=%s AND unread.user_uuid=%s AND unread.topic_uuid=%s
        LIMIT 1001
    ) bounded
"""


def stats(values):
    ordered = sorted(values)
    return {
        "n": len(values),
        "p50_ms": statistics.median(values),
        "p95_ms": ordered[math.ceil(len(values) * 0.95) - 1],
        "min_ms": min(values),
        "max_ms": max(values),
        "raw_ms": values,
    }


def seed(db, api, stream, peer, count):
    # Only fixture construction bypasses user triggers. All constraints stay on;
    # preview keys and the shared pointer are populated explicitly.
    tables = (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
        "messenger_user_message_unreads",
    )
    for table in tables:
        db.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
    try:
        db.execute(
            """WITH template AS (
            SELECT content_uuid FROM messenger_messages WHERE project_id=%s LIMIT 1
        ), messages AS (
            INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid,content_updated_at)
            SELECT gen_random_uuid(),%s,%s,content_uuid,NOW() FROM generate_series(1,%s) CROSS JOIN template
            RETURNING uuid,project_id,created_at
        ), placements AS (
            INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid,created_at)
            SELECT gen_random_uuid(),project_id,uuid,%s,%s,created_at FROM messages RETURNING *
        ), bindings AS (
            INSERT INTO messenger_user_message_bindings(uuid,project_id,placement_uuid,user_uuid,
                preview_message_uuid,preview_stream_uuid,preview_topic_uuid,preview_created_at)
            SELECT gen_random_uuid(),project_id,uuid,%s,message_uuid,stream_uuid,topic_uuid,created_at
            FROM placements RETURNING project_id,user_uuid,placement_uuid
        ) INSERT INTO messenger_user_message_unreads(project_id,user_uuid,placement_uuid,stream_uuid,topic_uuid)
          SELECT project_id,user_uuid,placement_uuid,%s,%s FROM bindings""",
            (
                api.project_id,
                api.project_id,
                api.user_uuid,
                count,
                stream["uuid"],
                stream["default_topic_uuid"],
                peer,
                stream["uuid"],
                stream["default_topic_uuid"],
            ),
        )
    finally:
        for table in reversed(tables):
            db.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
    last = db.execute(
        "SELECT uuid,created_at FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s ORDER BY created_at DESC,uuid DESC LIMIT 1",
        (api.project_id, stream["default_topic_uuid"]),
    ).fetchone()
    db.execute(
        "SELECT messenger_set_last_message(%s,%s,%s,%s,%s)",
        (api.project_id, stream["uuid"], stream["default_topic_uuid"], *last),
    )


def plan_summary(plan):
    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    return {
        "execution_ms": plan["Execution Time"],
        "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks", 0),
        "shared_read_blocks": plan["Plan"].get("Shared Read Blocks", 0),
        "relations": [
            {
                "table": n["Relation Name"],
                "node": n["Node Type"],
                "rows": n["Actual Rows"],
                "loops": n["Actual Loops"],
                "heap_fetches": n.get("Heap Fetches"),
            }
            for n in nodes(plan["Plan"])
            if "Relation Name" in n
        ],
    }


def flush_publication():
    for _ in range(1000):
        with contexts.Context().session_manager() as session:
            pending = publication.publish_pending(session)
        if not pending:
            return
    raise AssertionError("Benchmark publication did not converge")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    url = os.environ["WORKSPACE_TEST_DB_URL"]
    parts = urllib.parse.urlsplit(url)
    # libpq query parameters override the URI path. Keep the generated database
    # authoritative even when the maintenance URL supplies ?dbname=....
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key != "dbname"
    ]
    name = "cassi_unread_bench_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
        try:
            os.environ["WORKSPACE_TEST_DB_URL"] = parts._replace(
                path="/" + name,
                query=urllib.parse.urlencode(query, quote_via=urllib.parse.quote),
            ).geturl()
            os.environ["WORKSPACE_REQUIRE_INTEGRATION_TESTS"] = "1"
            os.environ["WORKSPACE_UNREAD_BENCHMARK"] = json.dumps(vars(args))
            root = pathlib.Path(__file__).resolve().parents[1]
            result = pytest.main(
                [
                    str(root / "workspace/tests/integration/test_unread_benchmark.py"),
                    "-q",
                    "-s",
                    "--tb=short",
                ]
            )
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
