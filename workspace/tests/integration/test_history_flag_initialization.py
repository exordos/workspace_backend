# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import urllib.parse
import uuid

import psycopg.rows
import psycopg.sql
import pytest

from tools import benchmark_history_merge
from workspace.tests.integration import conftest, test_zulip_history

incoming = test_zulip_history.incoming


def test_history_imports_final_flags_without_enqueuing_provider_repairs(incoming):
    api, db, account, *_ = incoming
    messages = {
        101: test_zulip_history.message(101, flags=["read"]),
        102: test_zulip_history.message(102, flags=["starred"]),
        103: test_zulip_history.message(103, "@**Viewer|7**", flags=["read"]),
    }
    test_zulip_history.request(db, account)

    async def run():
        async with test_zulip_history.worker(incoming, messages) as (service, _):
            await test_zulip_history.drain(service, account)

    asyncio.run(run())
    rows = api.get("/v1/messenger/messages/").json()
    actual = {
        row["provider_identity"]: (row["read"], row["starred"], row["mentioned"])
        for row in rows
    }
    assert actual == {
        "101": (True, False, False),
        "102": (False, True, False),
        "103": (True, False, True),
    }
    assert db.execute(
        "SELECT count(*) FROM bridge_zulip_v5_flag_changes WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (0,)
    assert db.execute(
        "SELECT count(*),bool_or(publish_updates) FROM messenger_counter_updates "
        "WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (1, False)

    # The transaction-local history path must not suppress realtime defaults.
    test_zulip_history.incoming_tests.apply(
        incoming, test_zulip_history.incoming_tests.event(identifier=104)
    )
    assert db.execute(
        "SELECT count(*) FROM bridge_zulip_v5_flag_changes WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (1,)


def test_history_keeps_existing_provider_repairs_and_personal_pins(incoming):
    api, db, account, *_ = incoming
    test_zulip_history.incoming_tests.apply(
        incoming, test_zulip_history.incoming_tests.event(identifier=101)
    )
    repair = db.execute(
        "SELECT state_uuid,revision FROM bridge_zulip_v5_flag_changes WHERE project_id=%s",
        (account["project_id"],),
    ).fetchall()
    assert len(repair) == 1
    db.execute(
        "INSERT INTO messenger_user_message_pins(project_id,user_uuid,placement_uuid,stream_uuid,topic_uuid) "
        "SELECT binding.project_id,binding.user_uuid,placement.uuid,placement.stream_uuid,placement.topic_uuid "
        "FROM messenger_user_message_bindings binding JOIN messenger_message_placements placement "
        "ON (placement.project_id,placement.uuid)=(binding.project_id,binding.placement_uuid) "
        "WHERE binding.project_id=%s",
        (account["project_id"],),
    )
    messages = {
        101: test_zulip_history.message(101, flags=["read", "starred"]),
        102: test_zulip_history.message(102, flags=[]),
    }
    test_zulip_history.request(db, account)

    async def run():
        async with test_zulip_history.worker(incoming, messages) as (service, _):
            await test_zulip_history.drain(service, account)

    asyncio.run(run())
    assert (
        db.execute(
            "SELECT state_uuid,revision FROM bridge_zulip_v5_flag_changes WHERE project_id=%s",
            (account["project_id"],),
        ).fetchall()
        == repair
    )
    rows = {
        row["provider_identity"]: row
        for row in api.get("/v1/messenger/messages/").json()
    }
    assert rows["101"]["read"] and rows["101"]["starred"] and rows["101"]["pinned"]
    assert not rows["102"]["read"] and not rows["102"]["pinned"]


def test_history_merge_matches_previous_initializer(incoming):
    options = json.loads(os.environ.get("WORKSPACE_HISTORY_MERGE_BENCHMARK", "{}"))
    result = asyncio.run(
        benchmark_history_merge.compare(
            incoming,
            rows=options.get("rows", [0]),
            messages=options.get("messages", 32),
            repeats=options.get("repeats", 1),
        )
    )
    for row in result["results"]:
        assert all(
            sample["queued_repairs"] == result["messages"]
            for sample in row["baseline"]["samples"]
        )
        assert all(
            sample["queued_repairs"] == 0 for sample in row["candidate"]["samples"]
        )
        assert all(
            sample["operations"]["delete.messenger_user_message_unreads"]["rows"] == 0
            for sample in row["candidate"]["samples"]
        )
    if "output" in options:
        pathlib.Path(options["output"]).write_text(json.dumps(result, indent=2) + "\n")


def test_history_initializer_migration_round_trip(db):
    module = benchmark_history_merge.migration()
    with db.cursor(row_factory=psycopg.rows.dict_row) as connection:
        candidate = connection.execute(module.FUNCTION_SQL).fetchone()["definition"]
        try:
            module.migration_step.downgrade(connection)
            previous = connection.execute(module.FUNCTION_SQL).fetchone()["definition"]
            assert module.HISTORY_GUARD not in previous
            module.migration_step.upgrade(connection)
            assert (
                connection.execute(module.FUNCTION_SQL).fetchone()["definition"]
                == candidate
            )
        finally:
            connection.execute(candidate)


@pytest.mark.parametrize("query_override", [False, True])
def test_benchmark_preserves_the_maintenance_database(
    _database, tmp_path, query_override
):
    name = "cassi_benchmark_maintenance_" + uuid.uuid4().hex
    with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as admin:
        admin.execute(
            psycopg.sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                psycopg.sql.Identifier(name)
            )
        )
        try:
            parts = urllib.parse.urlsplit(conftest.TEST_DB_URL)._replace(
                path="/" + name
            )
            if query_override:
                query = dict(urllib.parse.parse_qsl(parts.query))
                query["dbname"] = name
                parts = parts._replace(
                    query=urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
                )
            url = parts.geturl()
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute("CREATE TABLE benchmark_sentinel(value int)")
                connection.execute("INSERT INTO benchmark_sentinel VALUES(42)")
                output = tmp_path / "benchmark.json"
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tools.benchmark_history_merge",
                        "--rows",
                        "0",
                        "--messages",
                        "8",
                        "--repeats",
                        "1",
                        "--output",
                        str(output),
                    ],
                    cwd=conftest.REPO_ROOT,
                    env={**os.environ, "WORKSPACE_TEST_DB_URL": url},
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                assert connection.execute(
                    "SELECT value FROM benchmark_sentinel"
                ).fetchone() == (42,)
                assert json.loads(output.read_text())["messages"] == 8
        finally:
            admin.execute(
                psycopg.sql.SQL("DROP DATABASE {}").format(psycopg.sql.Identifier(name))
            )
