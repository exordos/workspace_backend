# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Compare the 0.1.44 and split unread plans on a disposable database.

The fixture and view replacements run in one transaction that is always rolled
back. Output contains aggregate timings and a condition-free plan shape only.
"""

import argparse
import collections
import datetime
import importlib.util
import json
import os
import pathlib
import time
import urllib.parse
import uuid as sys_uuid

import psycopg

from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.tests.integration import conftest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    REPO_ROOT
    / "migrations"
    / "0149-split-messenger-unread-read-state-branches-c84ae9.py"
)
PARTIAL_UNREAD_INDEX = "m_workspace_unread_flags_user_message_idx"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "workspace_unread_branch_benchmark_migration",
        MIGRATION_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load unread migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _walk(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _walk(child)


def _safe_plan_shape(nodes):
    executed = [node for node in nodes if node.get("Actual Loops", 0) > 0]
    node_types = collections.Counter(node["Node Type"] for node in executed)
    relation_totals = collections.defaultdict(
        lambda: {"executions": 0, "visited_rows": 0}
    )
    for node in executed:
        if "Relation Name" not in node:
            continue
        key = (
            node["Relation Name"],
            node.get("Index Name"),
            node["Node Type"],
        )
        relation_totals[key]["executions"] += node["Actual Loops"]
        relation_totals[key]["visited_rows"] += (
            node.get("Actual Rows", 0) * node["Actual Loops"]
        )
    relations = [
        {
            "relation": relation,
            "index": index,
            "node_type": node_type,
            **totals,
        }
        for (relation, index, node_type), totals in sorted(
            relation_totals.items(),
            key=lambda item: tuple(value or "" for value in item[0]),
        )
    ]
    return {
        "node_type_counts": dict(sorted(node_types.items())),
        "executed_relations": relations,
    }


def _explain(cursor, name, sql, params, timeout_seconds):
    cursor.execute("SAVEPOINT unread_benchmark_query")
    cursor.execute(
        "SELECT set_config('statement_timeout', %s, TRUE)",
        (f"{timeout_seconds}s",),
    )
    started_at = time.monotonic()
    try:
        cursor.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
            params,
        )
        document = cursor.fetchone()[0][0]
    except psycopg.errors.QueryCanceled:
        elapsed = time.monotonic() - started_at
        cursor.execute("ROLLBACK TO SAVEPOINT unread_benchmark_query")
        cursor.execute("RELEASE SAVEPOINT unread_benchmark_query")
        return {
            "name": name,
            "timed_out": True,
            "elapsed_seconds": elapsed,
        }
    cursor.execute("RELEASE SAVEPOINT unread_benchmark_query")
    plan = document["Plan"]
    nodes = list(_walk(plan))
    return {
        "name": name,
        "timed_out": False,
        "planning_ms": document["Planning Time"],
        "execution_ms": document["Execution Time"],
        "returned_rows": plan["Actual Rows"],
        "partial_unread_index_used": any(
            node.get("Index Name") == PARTIAL_UNREAD_INDEX for node in nodes
        ),
        "message_rows_visited": sum(
            node.get("Actual Rows", 0) * node.get("Actual Loops", 0)
            for node in nodes
            if node.get("Relation Name") == "m_workspace_messages"
        ),
        "temp_read_blocks": max(
            (node.get("Temp Read Blocks", 0) for node in nodes),
            default=0,
        ),
        "temp_written_blocks": max(
            (node.get("Temp Written Blocks", 0) for node in nodes),
            default=0,
        ),
        "plan_shape": _safe_plan_shape(nodes),
    }


def _validate_database_url(parser, database_url):
    database_name = urllib.parse.urlsplit(database_url).path.lstrip("/")
    if "test" not in database_name.lower():
        parser.error("the database name must contain 'test'")


def _seed_fixture(db, message_count, unread_count):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_uuid,
        "Unread benchmark",
    )
    conftest.seed_user_stream_binding(db, project_uuid, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_uuid,
        owner_uuid,
        "general",
        is_default=True,
    )
    seed = str(sys_uuid.uuid4())
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, created_at, updated_at
            )
            SELECT md5(%s || ':' || series)::uuid, %s, %s, %s, %s,
                   '{"kind":"markdown","content":"benchmark"}'::jsonb,
                   NOW() + series * interval '1 microsecond',
                   NOW() + series * interval '1 microsecond'
            FROM generate_series(1, %s) AS series
            """,
            (
                seed,
                project_uuid,
                stream_uuid,
                topic_uuid,
                owner_uuid,
                message_count,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read
            )
            SELECT message.uuid, %s, message.project_id,
                   row_number() OVER (ORDER BY message.created_at, message.uuid)
                       <= %s
            FROM m_workspace_messages AS message
            WHERE message.project_id = %s AND message.stream_uuid = %s
            """,
            (
                reader_uuid,
                message_count - unread_count,
                project_uuid,
                stream_uuid,
            ),
        )
        cursor.execute("ANALYZE m_workspace_messages")
        cursor.execute("ANALYZE m_workspace_user_message_flags")
    return project_uuid, reader_uuid, stream_uuid


def main():
    started_at = datetime.datetime.now(datetime.timezone.utc)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        default=os.environ.get("WORKSPACE_TEST_DB_URL"),
    )
    parser.add_argument("--messages", type=int, default=250_000)
    parser.add_argument("--unread", type=int, default=100)
    parser.add_argument("--statement-timeout", type=int, default=120)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or WORKSPACE_TEST_DB_URL is required")
    if not 0 < args.unread <= args.messages:
        parser.error("--unread must be between one and --messages")
    _validate_database_url(parser, args.database_url)

    migration = _load_migration_module()
    exact_sql = """
        SELECT * FROM m_workspace_user_streams
        WHERE project_id = %s AND user_uuid = %s AND uuid = %s
    """
    collection_sql = """
        SELECT * FROM m_workspace_user_streams
        WHERE project_id = %s AND user_uuid = %s
    """
    report = {
        "schema_version": "workspace.messenger.unread-benchmark/v1",
        "started_at": started_at.isoformat(),
        "message_count": args.messages,
        "unread_count": args.unread,
    }

    with psycopg.connect(args.database_url) as db:
        try:
            project_uuid, reader_uuid, stream_uuid = _seed_fixture(
                db,
                args.messages,
                args.unread,
            )
            params = (project_uuid, reader_uuid, stream_uuid)
            with db.cursor() as cursor:
                cursor.execute(migration.PREVIOUS_UNREAD_MESSAGE_BASE_VIEW_SQL)
                cursor.execute(
                    migration._topic_unread_counts_view_sql(split_legacy_branch=False)
                )
                report["before"] = {
                    "exact": _explain(
                        cursor,
                        "exact_stream_0.1.44",
                        exact_sql,
                        params,
                        args.statement_timeout,
                    ),
                    "collection": _explain(
                        cursor,
                        "stream_collection_0.1.44",
                        collection_sql,
                        params[:2],
                        args.statement_timeout,
                    ),
                }

                cursor.execute(migration.UNREAD_MESSAGE_BASE_VIEW_SQL)
                cursor.execute(
                    migration._topic_unread_counts_view_sql(split_legacy_branch=True)
                )
                report["after"] = {
                    "exact": _explain(
                        cursor,
                        "exact_stream_split",
                        exact_sql,
                        params,
                        args.statement_timeout,
                    ),
                    "collection": _explain(
                        cursor,
                        "stream_collection_split",
                        collection_sql,
                        params[:2],
                        args.statement_timeout,
                    ),
                    "access": _explain(
                        cursor,
                        "exact_stream_access",
                        messenger_dm_helpers._WORKSPACE_USER_STREAM_ACCESS_SQL,
                        (project_uuid, stream_uuid, reader_uuid),
                        args.statement_timeout,
                    ),
                }
        finally:
            db.rollback()

    after_exact = report["after"]["exact"]
    after_collection = report["after"]["collection"]
    after_access = report["after"]["access"]
    if after_exact.get("timed_out") or after_collection.get("timed_out"):
        raise RuntimeError("split unread projection exceeded the statement timeout")
    if not after_exact["partial_unread_index_used"]:
        raise RuntimeError("exact stream plan did not use the partial unread index")
    if not after_collection["partial_unread_index_used"]:
        raise RuntimeError(
            "stream collection plan did not use the partial unread index"
        )
    if after_exact["temp_written_blocks"] or after_collection["temp_written_blocks"]:
        raise RuntimeError("split unread projection wrote temporary blocks")
    if after_access["message_rows_visited"]:
        raise RuntimeError("access validation visited message rows")
    report["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report["fixture_transaction_rolled_back"] = True
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
