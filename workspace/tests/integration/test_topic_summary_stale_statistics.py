# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util
import json
import time
import uuid

import pytest
from restalchemy.storage.sql import sessions

from workspace.tests.integration import conftest
from workspace.tests.integration import test_message_view_stale_statistics as history


TOPICS = "/v1/messenger/stream_topics/"
MESSAGES = "/v1/messenger/messages/"


def _migration():
    path = next(conftest.MIGRATIONS_DIR.glob("0293-*.py"))
    spec = importlib.util.spec_from_file_location("summary_boundary_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_history(api, db, analyze_new):
    for table in history.TABLES:
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    history._seed(db, uuid.uuid4(), uuid.uuid4(), 4096)
    for table in history.TABLES:
        db.execute(f"ANALYZE {table}")
    history._seed(db, api.project_id, api.user_uuid, 30000)
    if analyze_new:
        for table in history.TABLES:
            db.execute(f"ANALYZE {table}")


def _refresh_plan(db, project, topic, user):
    # Explain the installed function's actual UPDATE, including the boundary
    # view and EXISTS subplan, rather than a simplified boundary-only query.
    query = db.execute(
        "SELECT prosrc FROM pg_proc "
        "WHERE oid='messenger_refresh_topic_summary_state(uuid,uuid,uuid[])'::regprocedure"
    ).fetchone()[0]
    query = (
        query.replace("target_project_id", "%(project)s::uuid")
        .replace("target_topic_uuid", "%(topic)s::uuid")
        .replace("target_users", "ARRAY[%(user)s::uuid]")
    )
    with db.transaction(force_rollback=True):
        db.execute("SET LOCAL statement_timeout='5s'")
        return db.execute(
            "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + query,
            {"project": project, "topic": topic, "user": user},
        ).fetchone()[0][0]


@pytest.mark.isolated_schema
@pytest.mark.parametrize("analyze_new", [False, True])
def test_empty_topic_creation_after_history_import(api, db, analyze_new):
    _seed_history(api, db, analyze_new)
    start = time.monotonic()
    with db.transaction():
        db.execute("SET LOCAL statement_timeout='5s'")
        wanted = history._seed(db, api.project_id, api.user_uuid, 1)
    elapsed = time.monotonic() - start
    topic = db.execute(
        "SELECT topic_uuid FROM messenger_message_placements WHERE uuid=%s",
        (min(wanted),),
    ).fetchone()[0]
    for stale_flag in (None, True, False):
        db.execute(
            "UPDATE messenger_user_topic_bindings SET summary_has_new_messages=%s "
            "WHERE project_id=%s AND topic_uuid=%s",
            (stale_flag, api.project_id, topic),
        )
        plan = _refresh_plan(db, api.project_id, topic, api.user_uuid)
        scans = [
            node
            for node in history._nodes(plan["Plan"])
            if node.get("Relation Name")
            in {"messenger_messages", "messenger_message_placements"}
        ]
        assert scans
        assert all(
            (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
            * node["Actual Loops"]
            == 0
            for node in scans
        ), json.dumps(plan)
        assert (
            plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"] < 100
        ), json.dumps(plan)
        print(
            {
                "analyzed": analyze_new,
                "stale_flag": stale_flag,
                "creation_seconds": elapsed,
                "refresh_ms": plan["Execution Time"],
                "refresh_blocks": plan["Plan"]["Shared Hit Blocks"],
                "history_rows_examined": 0,
            }
        )
    db.execute(
        "UPDATE messenger_topics SET summary='Saved summary',summary_last_message_uuid=%s "
        "WHERE project_id=%s AND uuid=%s",
        (min(wanted), api.project_id, topic),
    )
    plan = _refresh_plan(db, api.project_id, topic, api.user_uuid)
    # These are the aliases in messenger_topic_messages_v1. The personal
    # message view's subplan receives separate, suffixed aliases. Measure the
    # boundary's history work without requiring a particular join-node shape.
    scans = [
        node
        for node in history._nodes(plan["Plan"])
        if node.get("Relation Name")
        in {"messenger_messages", "messenger_message_placements"}
        and node["Alias"] in {"message", "placement"}
    ]
    assert len(scans) == 2, json.dumps(plan)
    assert all(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
        * node["Actual Loops"]
        <= 1
        for node in scans
    ), json.dumps(plan)
    blocks = sum(
        node["Shared Hit Blocks"] + node["Shared Read Blocks"] for node in scans
    )
    assert blocks < 100, json.dumps(plan)
    print(
        {
            "analyzed": analyze_new,
            "boundary_rows_examined": 2,
            "boundary_history_blocks": blocks,
            "summary_refresh_ms": plan["Execution Time"],
        }
    )


@pytest.mark.isolated_schema
def test_http_empty_topic_creation_after_history_import(api, db, monkeypatch):
    _seed_history(api, db, False)
    stream = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "cassi-topic-creation"
    )
    peer = uuid.uuid4()
    conftest.seed_workspace_user(db, peer, f"user-{peer}")
    conftest.seed_user_stream_binding(db, api.project_id, stream, peer)
    execute = sessions.PgSQLSession.execute

    def bounded(session, statement, values=None):
        execute(session, "SET LOCAL statement_timeout='5s'")
        return execute(session, statement, values)

    monkeypatch.setattr(sessions.PgSQLSession, "execute", bounded)
    start = time.monotonic()
    response = api.post(
        TOPICS,
        json={"name": "cassi-empty-topic", "stream_uuid": stream},
    )
    assert response.status_code == 201, response.text
    print({"http_creation_seconds": time.monotonic() - start})
    topic = response.json()["uuid"]
    for user in (api.user_uuid, peer):
        response = api.get(TOPICS + topic, user=user)
        assert response.status_code == 200, response.text
        assert response.json()["summary"] is None
        assert response.json()["summary_last_message_uuid"] is None
        assert response.json()["summary_has_new_messages"] is None
    for scope in ({"user": uuid.uuid4()}, {"project": uuid.uuid4()}):
        assert api.get(TOPICS + topic, **scope).status_code == 404
        denied = api.post(
            TOPICS, json={"name": "Denied", "stream_uuid": stream}, **scope
        )
        assert denied.status_code == 404, denied.text
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream,
            "topic_uuid": topic,
            "payload": {"kind": "markdown", "content": "After cold topic creation"},
        },
    )
    assert message.status_code == 201, message.text
    assert api.get(MESSAGES + message.json()["uuid"]).status_code == 200
    page = api.get(MESSAGES, params={"topic_uuid": topic, "page_limit": 10})
    assert page.status_code == 200, page.text
    assert [row["uuid"] for row in page.json()] == [message.json()["uuid"]]
    db.execute(
        "UPDATE messenger_stream_bindings SET active=false "
        "WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
        (api.project_id, stream, peer),
    )
    assert api.get(TOPICS + topic, user=peer).status_code == 404
    denied = api.post(
        TOPICS, user=peer, json={"name": "Inactive", "stream_uuid": stream}
    )
    assert denied.status_code == 404, denied.text


@pytest.mark.isolated_schema
def test_summary_refresh_preserves_flags_boundaries_and_scope(api, db):
    messages = sorted(history._seed(db, api.project_id, api.user_uuid, 3))
    stream, topic = db.execute(
        "SELECT stream_uuid,topic_uuid FROM messenger_message_placements WHERE uuid=%s",
        (messages[0],),
    ).fetchone()
    peer = uuid.uuid4()
    conftest.seed_workspace_user(db, peer, f"user-{peer}")
    conftest.seed_user_stream_binding(db, api.project_id, stream, peer)
    # The peer sees only a covered message; canonical boundary visibility is
    # independent of that user's personal message bindings.
    db.execute(
        "INSERT INTO messenger_user_message_bindings"
        "(uuid,project_id,placement_uuid,user_uuid) VALUES (%s,%s,%s,%s)",
        (uuid.uuid4(), api.project_id, messages[0], peer),
    )
    db.execute(
        "INSERT INTO messenger_user_topic_bindings(uuid,project_id,user_uuid,topic_uuid) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (uuid.uuid4(), api.project_id, peer, topic),
    )
    migration = _migration()

    def flags():
        return {
            str(user): flag
            for user, flag in db.execute(
                "SELECT user_uuid,summary_has_new_messages "
                "FROM messenger_user_topic_bindings WHERE project_id=%s AND topic_uuid=%s",
                (api.project_id, topic),
            )
        }

    for scenario, expected in (
        ("middle", (True, False)),
        ("last", (False, False)),
        ("no_boundary", (True, True)),
        ("deleted_boundary", (True, True)),
        ("deleted_topic", (False, False)),
        ("deleted_stream", (False, False)),
        ("inactive_peer", (True, False)),
        ("disabled_summary", (True, False)),
        ("no_summary", (None, None)),
    ):
        for definition in (
            migration.PREVIOUS_SUMMARY_REFRESH_SQL,
            migration.SUMMARY_REFRESH_SQL,
        ):
            with db.transaction(force_rollback=True):
                db.execute(definition)
                boundary = messages[-1] if scenario == "last" else messages[1]
                if scenario == "no_boundary":
                    boundary = None
                db.execute(
                    "UPDATE messenger_topics SET summary=%s,summary_last_message_uuid=%s "
                    "WHERE project_id=%s AND uuid=%s",
                    (
                        None if scenario == "no_summary" else "Saved summary",
                        boundary,
                        api.project_id,
                        topic,
                    ),
                )
                if scenario == "deleted_boundary":
                    db.execute(
                        "UPDATE messenger_messages SET deleted_at=NOW() WHERE uuid=%s",
                        (boundary,),
                    )
                if scenario in {"deleted_topic", "deleted_stream"}:
                    table, key = (
                        ("messenger_topics", topic)
                        if scenario == "deleted_topic"
                        else ("messenger_streams", stream)
                    )
                    db.execute(
                        f"UPDATE {table} SET deleted_at=NOW() WHERE uuid=%s", (key,)
                    )
                if scenario == "inactive_peer":
                    db.execute(
                        "UPDATE messenger_stream_bindings SET active=false "
                        "WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
                        (api.project_id, stream, peer),
                    )
                if scenario == "disabled_summary":
                    db.execute(
                        "UPDATE messenger_topics SET summary_enabled=false WHERE uuid=%s",
                        (topic,),
                    )
                db.execute(
                    "UPDATE messenger_user_topic_bindings SET summary_has_new_messages=true "
                    "WHERE project_id=%s AND topic_uuid=%s",
                    (api.project_id, topic),
                )
                for project, users in ((uuid.uuid4(), None), (api.project_id, [])):
                    db.execute(
                        "SELECT messenger_refresh_topic_summary_state(%s,%s,%s::uuid[])",
                        (project, topic, users),
                    )
                    assert flags() == {api.user_uuid: True, str(peer): True}
                db.execute(
                    "SELECT messenger_refresh_topic_summary_state(%s,%s,%s::uuid[])",
                    (api.project_id, topic, [api.user_uuid]),
                )
                assert flags() == {api.user_uuid: expected[0], str(peer): True}, (
                    scenario
                )
                db.execute(
                    "SELECT messenger_refresh_topic_summary_state(%s,%s)",
                    (api.project_id, topic),
                )
                assert flags() == {
                    api.user_uuid: expected[0],
                    str(peer): expected[1],
                }, scenario


@pytest.mark.isolated_schema
def test_summary_boundary_migration_round_trip_preserves_topic_state(api, db):
    messages = sorted(history._seed(db, api.project_id, api.user_uuid, 3))
    db.execute(
        "UPDATE messenger_topics SET summary='Saved summary',summary_last_message_uuid=%s "
        "WHERE project_id=%s",
        (messages[1], api.project_id),
    )
    migration = _migration()

    def rows():
        return db.execute(
            "SELECT * FROM messenger_api_user_topics_v1 WHERE project_id=%s ORDER BY uuid",
            (api.project_id,),
        ).fetchall()

    original = rows()
    for operation in ("downgrade", "upgrade", "upgrade"):
        getattr(migration.migration_step, operation)(db)
        assert rows() == original
