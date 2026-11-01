# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util
import json
import uuid

import pytest
from restalchemy.storage.sql import sessions

from workspace.tests.integration import test_message_view_stale_statistics as history
from workspace.messenger_api.api import v2_store


def _plan(db, predicate, values, *, baseline=False):
    condition = "project_id=%s AND user_uuid=%s AND visible " + predicate
    if baseline:
        query = (
            "SELECT * FROM messenger_api_user_messages_v1 WHERE "
            + condition
            + " ORDER BY created_at DESC,uuid DESC LIMIT 101"
        )
    else:
        query = v2_store.SELECT_MESSAGE_PAGE_SQL.format(
            predicate=condition, direction="DESC"
        )
        values = (*values, 101, *values)
    return db.execute(
        "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + query, values
    ).fetchone()[0][0]


def _blocks(plan):
    return plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"]


def _history_work(plan):
    return sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
        * node["Actual Loops"]
        for node in history._nodes(plan["Plan"])
        if node.get("Relation Name")
        in {
            "messenger_user_message_bindings",
            "messenger_message_placements",
            "messenger_messages",
        }
    )


def _seed_pages(api, db, analyzed):
    for table in history.TABLES:
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    history._seed(db, uuid.uuid4(), uuid.uuid4(), 4096)
    for table in history.TABLES:
        db.execute(f"ANALYZE {table}")
    history._seed(db, api.project_id, api.user_uuid, 30000 if analyzed else 1001)
    if analyzed:
        for table in history.TABLES:
            db.execute(f"ANALYZE {table}")
    with db.transaction():
        db.execute("SET LOCAL statement_timeout='5s'")
        wanted = history._seed(db, api.project_id, api.user_uuid, 11)
    stream, topic = db.execute(
        "SELECT stream_uuid,topic_uuid FROM messenger_message_placements WHERE uuid=%s",
        (min(wanted),),
    ).fetchone()
    if analyzed:
        for table in history.TABLES:
            db.execute(f"ANALYZE {table}")
    return wanted, stream, topic


@pytest.mark.isolated_schema
@pytest.mark.parametrize("analyzed", [False, True])
def test_selective_pages_keep_indexed_history_work(api, db, analyzed):
    wanted, stream, topic = _seed_pages(api, db, analyzed)
    checks = (
        ("stream_uuid", stream),
        ("topic_uuid", topic),
        ("stream_uuid", uuid.uuid4()),
        ("topic_uuid", uuid.uuid4()),
    )
    for column, value in checks:
        with db.transaction():
            db.execute("SET LOCAL statement_timeout='5s'")
            plan = _plan(db, f"AND {column}=%s", (api.project_id, api.user_uuid, value))
        scans = [
            node
            for node in history._nodes(plan["Plan"])
            if node.get("Relation Name")
            in {
                "messenger_user_message_bindings",
                "messenger_message_placements",
                "messenger_messages",
            }
        ]
        assert scans
        assert all(
            (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
            * node["Actual Loops"]
            <= (4 * len(wanted) if analyzed else 1012)
            for node in scans
        ), json.dumps(plan)
        response = api.get(
            "/v1/messenger/messages/",
            params={
                column: str(value),
                "page_limit": 100,
                "sort_key": "created_at",
                "sort_dir": "desc",
            },
        )
        assert response.status_code == 200, response.text
        identifiers = [row["uuid"] for row in response.json()]
        assert identifiers == (
            sorted(wanted, reverse=True) if value in (stream, topic) else []
        )
        print(
            json.dumps(
                {
                    "analyzed": analyzed,
                    "filter": column,
                    "present": value in (stream, topic),
                    "sql_ms": plan["Execution Time"],
                    "blocks": _blocks(plan),
                }
            )
        )

    if analyzed:
        previous = next(history.conftest.MIGRATIONS_DIR.glob("0289-*.py"))
        spec = importlib.util.spec_from_file_location("page_baseline", previous)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        for predicate, params in (
            ("", {}),
            ("AND read", {"read": "true"}),
            ("AND NOT read", {"read": "false"}),
            ("AND mentioned", {"mentioned": "true"}),
            ("AND NOT mentioned", {"mentioned": "false"}),
            ("AND mentioned AND NOT read", {"mentioned": "true", "read": "false"}),
            ("AND pinned", {"pinned": "true"}),
            ("AND NOT pinned", {"pinned": "false"}),
            ("AND starred", {"starred": "true"}),
            ("AND NOT starred", {"starred": "false"}),
        ):
            values = (api.project_id, api.user_uuid)
            with db.transaction(force_rollback=True):
                db.execute("SET LOCAL statement_timeout='5s'")
                db.execute(baseline.MESSAGE_VIEW)
                before = _plan(db, predicate, values, baseline=True)
            with db.transaction():
                db.execute("SET LOCAL statement_timeout='5s'")
                after = _plan(db, predicate, values)
            # Bound one pass through history plus the final page expansion.
            # A hash scan and indexed probes have different row/buffer counts;
            # compare their latency separately rather than treating cache hits
            # as elapsed time.
            assert _history_work(after) <= 3 * (4096 + 30011) + 303, json.dumps(
                {"predicate": predicate, "before": before, "after": after}
            )
            response = api.get(
                "/v1/messenger/messages/",
                params={
                    "sort_key": "created_at",
                    "sort_dir": "desc",
                    "page_limit": 100,
                    **params,
                },
            )
            assert response.status_code == 200, response.text
            assert all(
                all(row[name] == (value == "true") for name, value in params.items())
                for row in response.json()
            )
            print(
                json.dumps(
                    {
                        "predicate": predicate,
                        "before_ms": before["Execution Time"],
                        "after_ms": after["Execution Time"],
                        "before_blocks": _blocks(before),
                        "after_blocks": _blocks(after),
                    }
                )
            )


@pytest.mark.isolated_schema
def test_http_topic_page_does_not_expand_unrelated_messages(api, db, monkeypatch):
    wanted, _stream, topic = _seed_pages(api, db, True)
    statements = []
    original = sessions.PgSQLSession.execute

    def capture(session, statement, values=None):
        if (
            "messenger_api_user_messages_v1" in statement
            and statement.lstrip().upper().startswith(("SELECT", "WITH"))
        ):
            statements.append((statement, values))
        return original(session, statement, values)

    monkeypatch.setattr(sessions.PgSQLSession, "execute", capture)
    response = api.get(
        "/v1/messenger/messages/",
        params={
            "topic_uuid": str(topic),
            "page_limit": 100,
            "sort_key": "created_at",
            "sort_dir": "desc",
        },
    )
    assert response.status_code == 200, response.text
    assert [row["uuid"] for row in response.json()] == sorted(wanted, reverse=True)
    assert statements
    for statement, values in statements:
        plan = db.execute(
            "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + statement, values
        ).fetchone()[0][0]
        scans = [
            node
            for node in history._nodes(plan["Plan"])
            if node.get("Relation Name")
            in {
                "messenger_user_message_bindings",
                "messenger_message_placements",
                "messenger_messages",
            }
        ]
        assert scans
        assert all(
            (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
            * node["Actual Loops"]
            <= 4 * len(wanted)
            for node in scans
        ), json.dumps(plan)
