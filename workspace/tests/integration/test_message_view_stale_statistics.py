# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import json
import uuid

import pytest

from workspace.tests.integration import conftest


TABLES = (
    "messenger_messages",
    "messenger_message_contents",
    "messenger_message_placements",
    "messenger_user_message_bindings",
    "messenger_user_message_unreads",
    "messenger_user_message_pins",
    "messenger_user_message_stars",
    "messenger_user_message_mentions",
    "messenger_stream_bindings",
    "messenger_streams",
    "messenger_topics",
)


def _seed(db, project, user, count):
    stream = conftest.seed_user_stream(db, project, user, "History")
    topic = conftest.seed_stream_topic(db, project, stream, user, "General")
    db.execute(
        "CREATE TEMP TABLE view_plan_ids AS "
        "SELECT i,gen_random_uuid() AS uuid FROM generate_series(1,%s) i",
        (count,),
    )
    db.execute(
        """WITH content AS MATERIALIZED (
               SELECT messenger_resolve_message_content(
                   '{"kind":"markdown","content":"History"}'::jsonb,NULL) AS uuid
           )
           INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid,created_at)
           SELECT ids.uuid,%s,%s,content.uuid,timestamp '2026-01-01'
           FROM view_plan_ids ids CROSS JOIN content""",
        (project, user),
    )
    db.execute(
        "INSERT INTO messenger_message_placements"
        "(uuid,project_id,message_uuid,stream_uuid,topic_uuid) "
        "SELECT uuid,%s,uuid,%s,%s FROM view_plan_ids",
        (project, stream, topic),
    )
    with db.transaction():
        db.execute("SET LOCAL workspace.zulip_history='on'")
        db.execute(
            "INSERT INTO messenger_user_message_bindings"
            "(uuid,project_id,placement_uuid,user_uuid) "
            "SELECT uuid,%s,uuid,%s FROM view_plan_ids",
            (project, user),
        )
    for table, divisor in (("unreads", 2), ("pins", 4), ("stars", 8), ("mentions", 16)):
        db.execute(
            f"INSERT INTO messenger_user_message_{table}"
            "(project_id,user_uuid,placement_uuid,stream_uuid,topic_uuid) "
            "SELECT %s,%s,uuid,%s,%s FROM view_plan_ids WHERE i %% %s=0",
            (project, user, stream, topic, divisor),
        )
    expected = {
        str(identifier): {
            "read": i % 2 != 0,
            "pinned": i % 4 == 0,
            "starred": i % 8 == 0,
            "mentioned": i % 16 == 0,
        }
        for i, identifier in db.execute("SELECT i,uuid FROM view_plan_ids")
    }
    db.execute("DROP TABLE view_plan_ids")
    return expected


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


@pytest.mark.isolated_schema
@pytest.mark.parametrize("lookup", ["project", "placement"])
def test_message_view_keeps_reads_keyed_with_stale_project_statistics(api, db, lookup):
    # A previous history batch dominates the statistics. The next project has
    # not been analyzed yet, as happens during the ordered bridge test run.
    for table in TABLES:
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    _seed(db, uuid.uuid4(), uuid.uuid4(), 4096)
    for table in TABLES:
        db.execute(f"ANALYZE {table}")
    expected = _seed(db, api.project_id, api.user_uuid, 1001)
    query = """SELECT message.uuid,state.read
               FROM messenger_api_user_messages_v1 state
               JOIN messenger_message_placements place ON place.uuid=state.uuid
               JOIN messenger_messages message ON message.uuid=place.message_uuid
               WHERE state.project_id=%s"""
    values = (api.project_id,)
    wanted = expected
    if lookup == "placement":
        identifier = min(expected)
        query += " AND state.uuid=%s AND state.user_uuid=%s"
        values += (identifier, api.user_uuid)
        wanted = {identifier: expected[identifier]}
    with db.transaction():
        # Bound a broken plan as well; the work assertion below also detects
        # regressions on machines fast enough to finish the bad nested loops.
        db.execute("SET LOCAL statement_timeout='5s'")
        plan = db.execute(
            "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + query,
            values,
        ).fetchone()[0][0]
        # Sparse fact joins retain planner freedom for selective flag pages.
        # Bound total work, but also check each history scan: an aggregate
        # budget alone can hide repeated history scans behind small fixtures.
        budget = 500000 if lookup == "project" else 1000
        assert (
            plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"]
            < budget
        ), json.dumps(plan)
        history_scans = [
            node
            for node in _nodes(plan["Plan"])
            if node.get("Relation Name")
            in {
                "messenger_user_message_bindings",
                "messenger_message_placements",
                "messenger_messages",
            }
        ]
        assert history_scans
        assert all(
            (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
            * node["Actual Loops"]
            <= len(wanted)
            for node in history_scans
        ), json.dumps(plan)
        states = db.execute(query, values).fetchall()
    assert len(states) == len(wanted)
    assert {str(identifier): read for identifier, read in states} == {
        identifier: flags["read"] for identifier, flags in wanted.items()
    }
    response = api.get(f"/v1/messenger/messages/{min(expected)}")
    assert response.status_code == 200, response.text
    assert response.json()["uuid"] == min(expected)
    seen = []
    params = {"sort_key": "created_at", "sort_dir": "asc", "page_limit": 100}
    for _ in range(12):
        response = api.get("/v1/messenger/messages/", params=params)
        assert response.status_code == 200, response.text
        rows = response.json()
        for row in rows:
            assert row["payload"] == {"kind": "markdown", "content": "History"}
            assert {name: row[name] for name in expected[row["uuid"]]} == expected[
                row["uuid"]
            ]
        seen.extend(row["uuid"] for row in rows)
        marker = response.headers.get("X-Pagination-Marker")
        if marker is None:
            break
        params["page_marker"] = marker
    assert marker is None
    assert seen == sorted(expected)
    for scope in ({"user": uuid.uuid4()}, {"project": uuid.uuid4()}):
        response = api.get("/v1/messenger/messages/", **scope)
        assert response.status_code == 200, response.text
        assert response.json() == []
    print(
        json.dumps(
            {
                "messages": len(expected),
                "lookup": lookup,
                "sql_ms": plan["Execution Time"],
                "shared_hits": plan["Plan"]["Shared Hit Blocks"],
            }
        )
    )
