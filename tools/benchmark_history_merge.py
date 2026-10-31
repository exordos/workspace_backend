# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Compare real PostgreSQL history merges in a fresh, disposable database.

Set WORKSPACE_TEST_DB_URL to a maintenance database whose role can CREATE DATABASE,
then run ``python -m tools.benchmark_history_merge --rows 0 100000 1000000``.
The maintenance database is never migrated or cleared. The integration harness
and synthetic data live in a newly created database, which is dropped on exit.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import platform
import statistics
import time
import urllib.parse
import uuid
from unittest import mock

import psycopg
from psycopg import sql
import pytest

from workspace.bridges.zulip import history


class RollbackSample(Exception):
    pass


def migration():
    path = next(
        pathlib.Path(__file__)
        .resolve()
        .parents[1]
        .glob("migrations/0288-Avoid-redundant-history-flag-initialization-*.py")
    )
    spec = importlib.util.spec_from_file_location("history_flag_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def background(db, account, batch, first, last):
    """Grow the indexed stores outside the measured merge interval."""
    if first == last:
        return
    tables = (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
    )
    placement = batch["messenger_message_placements"][0]
    with db.transaction():
        # Only this disposable database is touched. Keep FK checks enabled;
        # synthetic background rows must not schedule live projection work.
        for table in tables:
            db.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        content = db.execute(
            "SELECT messenger_resolve_message_content("
            '\'{"kind":"markdown","content":"Synthetic background"}\',NULL)'
        ).fetchone()[0]
        db.execute(
            """INSERT INTO messenger_messages
               (uuid,project_id,author_uuid,content_uuid,created_at,content_updated_at,source_name,provider_uuid,provider_identity)
               SELECT md5('cassi-bench-message:'||n)::uuid,%s,%s,%s,'2000-01-01','2000-01-01','zulip',%s,'background:'||n
               FROM generate_series(%s,%s) n""",
            (
                account["project_id"],
                account["owner_user_uuid"],
                content,
                account["provider_uuid"],
                first + 1,
                last,
            ),
        )
        db.execute(
            """INSERT INTO messenger_message_placements
               (uuid,project_id,message_uuid,stream_uuid,topic_uuid,created_at)
               SELECT md5('cassi-bench-placement:'||n)::uuid,%s,md5('cassi-bench-message:'||n)::uuid,%s,%s,'2000-01-01'
               FROM generate_series(%s,%s) n""",
            (
                account["project_id"],
                placement["stream_uuid"],
                placement["topic_uuid"],
                first + 1,
                last,
            ),
        )
        db.execute(
            """INSERT INTO messenger_user_message_bindings(uuid,project_id,placement_uuid,user_uuid)
               SELECT md5('cassi-bench-binding:'||n)::uuid,%s,md5('cassi-bench-placement:'||n)::uuid,%s
               FROM generate_series(%s,%s) n""",
            (account["project_id"], account["owner_user_uuid"], first + 1, last),
        )
        for table in tables:
            db.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
            db.execute(f"ANALYZE {table}")


async def projection(connection, batch):
    result = {}
    for table, values in batch.items():
        if table == "_context" or not values:
            continue
        columns = list(values[0])
        keys = (
            ("project_id", "user_uuid", "placement_uuid")
            if table.startswith("messenger_user_message_")
            and table != "messenger_user_message_bindings"
            else ("uuid",)
        )
        if table == "messenger_message_contents":
            query = """SELECT dst.workspace_content,dst.provider_content
                FROM messenger_message_contents dst JOIN messenger_messages message ON message.content_uuid=dst.uuid
                JOIN history_messenger_messages src ON (src.project_id,src.uuid)=(message.project_id,message.uuid)
                ORDER BY message.uuid"""
        else:
            query = (
                f"SELECT {','.join('dst.' + c for c in columns)} FROM {table} dst JOIN history_{table} src ON "
                + " AND ".join(f"dst.{key}=src.{key}" for key in keys)
                + " ORDER BY "
                + ",".join("dst." + key for key in keys)
            )
        result[table] = await (await connection.execute(query)).fetchall()
    result["counters"] = await (
        await connection.execute(
            "SELECT user_uuid,stream_uuid,topic_uuid,publish_updates FROM messenger_counter_updates ORDER BY 1,2,3"
        )
    ).fetchall()
    return hashlib.sha256(
        json.dumps(result, sort_keys=True, default=str).encode()
    ).hexdigest()


async def compare(incoming, rows=(0,), messages=32, repeats=1):
    # Import the fixture harness after the CLI selects its disposable database.
    from workspace.tests.integration import test_zulip_history

    _api, db, account, *_ = incoming
    module = migration()
    candidate = db.execute(module.FUNCTION_SQL).fetchone()[0]
    assert module.HISTORY_GUARD in candidate
    baseline = candidate.replace(module.HISTORY_GUARD, "", 1)
    page = {
        n: test_zulip_history.message(
            n,
            f"Body {n} @**Viewer|7**" if n % 4 == 0 else f"Body {n}",
            flags=["read"] if n % 2 else ["starred"],
        )
        for n in range(10001, 10001 + messages)
    }
    test_zulip_history.request(db, account)
    output = {
        "python": platform.python_version(),
        "postgresql": db.execute("SHOW server_version").fetchone()[0],
        "messages": messages,
        "repeats": repeats,
        "results": [],
    }
    try:
        async with test_zulip_history.worker(incoming, page) as (service, _):
            selected = await test_zulip_history.job(service, account)
            batch = await service.operations.prepare_batch(
                selected, list(page.values())
            )
            size = 0
            for target in rows:
                background(db, account, batch, size, target)
                size = target
                samples = {name: [] for name in ("baseline", "candidate")}
                checksums = set()
                for repeat in range(repeats + 1):
                    # Alternate order; the first pair warms caches and plans.
                    order = (
                        ("baseline", "candidate")
                        if repeat % 2 == 0
                        else ("candidate", "baseline")
                    )
                    for name in order:
                        db.execute(baseline if name == "baseline" else candidate)
                        operations = {}

                        def logged(template, *args):
                            if template.startswith("History SQL operation="):
                                operations[args[0]] = {"ms": args[1], "rows": args[2]}

                        try:
                            async with service.db.transaction() as connection:
                                await connection.execute(
                                    "SET LOCAL statement_timeout='45s'"
                                )
                                with mock.patch.object(
                                    history.LOG, "info", side_effect=logged
                                ):
                                    start = time.perf_counter()
                                    await service.operations.merge_batch(
                                        connection, selected, batch
                                    )
                                    elapsed = time.perf_counter() - start
                                checksum = await projection(connection, batch)
                                queued = (
                                    await (
                                        await connection.execute(
                                            "SELECT count(*) AS n FROM bridge_zulip_v5_flag_changes WHERE project_id=%s",
                                            (account["project_id"],),
                                        )
                                    ).fetchone()
                                )["n"]
                                checksums.add(checksum)
                                if repeat:
                                    samples[name].append(
                                        {
                                            "merge_ms": elapsed * 1000,
                                            "operations": operations,
                                            "queued_repairs": queued,
                                        }
                                    )
                                raise RollbackSample
                        except RollbackSample:
                            pass
                assert len(checksums) == 1, "History projection changed"
                summary = {
                    name: {
                        "median_ms": statistics.median(s["merge_ms"] for s in values),
                        "samples": values,
                    }
                    for name, values in samples.items()
                }
                output["results"].append(
                    {
                        "background_rows_per_table": size,
                        "projection_sha256": checksums.pop(),
                        **summary,
                        "speedup": summary["baseline"]["median_ms"]
                        / summary["candidate"]["median_ms"],
                    }
                )
    finally:
        db.execute(candidate)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", nargs="+", type=int, default=[0, 100000, 1000000])
    parser.add_argument("--messages", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if (
        args.rows != sorted(set(args.rows))
        or min(args.rows) < 0
        or min(args.messages, args.repeats) < 1
    ):
        parser.error(
            "Use ascending distinct nonnegative row counts and positive messages/repeats"
        )
    url = os.environ["WORKSPACE_TEST_DB_URL"]
    name = "cassi_history_merge_bench_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
        try:
            os.environ["WORKSPACE_TEST_DB_URL"] = (
                urllib.parse.urlsplit(url)._replace(path="/" + name).geturl()
            )
            os.environ["WORKSPACE_REQUIRE_INTEGRATION_TESTS"] = "1"
            os.environ["WORKSPACE_HISTORY_MERGE_BENCHMARK"] = json.dumps(vars(args))
            root = pathlib.Path(__file__).resolve().parents[1]
            result = pytest.main(
                [
                    str(
                        root
                        / "workspace/tests/integration/test_history_flag_initialization.py"
                    ),
                    "-q",
                    "-k",
                    "matches_previous",
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
