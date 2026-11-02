# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Small counter scopes must not scan another project's prepared history."""

import json
import uuid

import pytest
from psycopg import sql

from tools import benchmark_unreads
from workspace.services.messenger_workers import counters
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal

conversation = journal.conversation


@pytest.fixture
def sparse_scope(api, db, conversation):
    stream, _, peer = conversation
    for table in (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
        "messenger_user_message_unreads",
    ):
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    benchmark_unreads.seed(db, api, stream, peer, 99998)
    db.execute(
        "ANALYZE messenger_messages,messenger_message_placements,messenger_user_message_bindings,messenger_user_message_unreads,messenger_user_topic_bindings,messenger_stream_bindings,messenger_topics,messenger_streams"
    )
    other = conftest.ApiClient(api.base_url, api.user_uuid, uuid.uuid4())
    response = other.post(native.STREAMS, json={"name": "Small counter scope"})
    assert response.status_code == 201, response.text
    small = response.json()
    response = other.post(
        native.MESSAGES,
        json={
            "stream_uuid": small["uuid"],
            "payload": {"kind": "markdown", "content": "Small scope"},
        },
    )
    assert response.status_code == 201, response.text
    native._drain()
    return other.project_id, api.user_uuid, small["default_topic_uuid"]


def nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from nodes(child)


@pytest.mark.isolated_schema
def test_generic_empty_scope_count_never_scans_unrelated_unreads(db, sparse_scope):
    db.execute("SET plan_cache_mode='force_generic_plan'")
    statement = counters.COUNT_TOPIC_SQL
    for number in range(1, 4):
        statement = statement.replace("%s", f"${number}", 1)
    db.execute("PREPARE cassi_empty_count(uuid,uuid,uuid) AS " + statement)
    statement = sql.SQL("EXECUTE cassi_empty_count({})").format(
        sql.SQL(",").join(map(sql.Literal, sparse_scope))
    )
    assert db.execute(statement).fetchone() == (0, 0)
    plan = db.execute(
        sql.SQL("EXPLAIN(ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) ") + statement
    ).fetchone()[0][0]
    work = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
        * node["Actual Loops"]
        for node in nodes(plan["Plan"])
        if node.get("Relation Name") == "messenger_user_message_unreads"
    )
    assert work <= 1001, plan


@pytest.mark.isolated_schema
def test_empty_summary_refresh_has_no_history_buffer_work(db, sparse_scope):
    project, user, topic = sparse_scope
    plan = db.execute(
        "EXPLAIN(ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) SELECT messenger_refresh_topic_summary_state(%s,%s,%s::uuid[])",
        (project, topic, [user]),
    ).fetchone()[0][0]
    blocks = plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"]
    print(
        json.dumps(
            {"summary_scope_buffers": blocks, "execution_ms": plan["Execution Time"]}
        )
    )
    assert blocks < 1000, plan


def test_removed_summary_clears_only_requested_users(api, db, conversation):
    stream, _, peer = conversation
    topic = stream["default_topic_uuid"]
    db.execute(
        "UPDATE messenger_user_topic_bindings SET summary_has_new_messages=true WHERE project_id=%s AND topic_uuid=%s",
        (api.project_id, topic),
    )
    for project, users in ((uuid.uuid4(), [peer]), (api.project_id, [])):
        db.execute(
            "SELECT messenger_refresh_topic_summary_state(%s,%s,%s::uuid[])",
            (project, topic, users),
        )
    assert db.execute(
        "SELECT bool_and(summary_has_new_messages) FROM messenger_user_topic_bindings WHERE project_id=%s AND topic_uuid=%s",
        (api.project_id, topic),
    ).fetchone() == (True,)
    db.execute(
        "SELECT messenger_refresh_topic_summary_state(%s,%s,%s::uuid[])",
        (api.project_id, topic, [peer]),
    )
    rows = dict(
        db.execute(
            "SELECT user_uuid,summary_has_new_messages FROM messenger_user_topic_bindings WHERE project_id=%s AND topic_uuid=%s",
            (api.project_id, topic),
        ).fetchall()
    )
    assert rows[peer] is None and rows[uuid.UUID(api.user_uuid)] is True
    db.execute(
        "SELECT messenger_refresh_topic_summary_state(%s,%s)", (api.project_id, topic)
    )
    assert db.execute(
        "SELECT count(*) FROM messenger_user_topic_bindings WHERE project_id=%s AND topic_uuid=%s AND summary_has_new_messages IS NOT NULL",
        (api.project_id, topic),
    ).fetchone() == (0,)
