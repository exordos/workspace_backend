# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import uuid

import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import sessions

from workspace.messenger_api import topic_summarization
from workspace.messenger_api.api import v2_store


def _walk(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _walk(child)


@pytest.mark.parametrize("has_unread", [False, True])
def test_unread_existence_probe_does_not_order_candidates(db, monkeypatch, has_unread):
    store = v2_store.MessengerV2Store(uuid.uuid4(), uuid.uuid4())
    plans = []
    execute = sessions.PgSQLSession.execute

    with contexts.Context().session_manager() as session:
        session.execute("""
            CREATE TEMP TABLE cassi_read_probe ON COMMIT DROP AS
            SELECT value AS placement_uuid, value AS created_at
            FROM generate_series(1, 10000) AS value;
            ANALYZE cassi_read_probe;
        """)

        def explain(current, statement, values=None):
            if current is session and "cassi_read_probe" in statement:
                plan = execute(
                    current,
                    "EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF) " + statement,
                    values,
                ).fetchone()["QUERY PLAN"][0]["Plan"]
                plans.append(plan)
            return execute(current, statement, values)

        monkeypatch.setattr(sessions.PgSQLSession, "execute", explain)
        assert (
            store._has_unread_candidates(
                candidate_sql="""
                SELECT placement_uuid, created_at FROM cassi_read_probe
                WHERE %s ORDER BY created_at, placement_uuid
            """,
                candidate_values=(has_unread,),
            )
            is has_unread
        )

    assert len(plans) == 1
    assert not any(node["Node Type"] == "Sort" for node in _walk(plans[0]))
    if has_unread:
        scanned = [node for node in _walk(plans[0]) if "Relation Name" in node]
        assert sum(node["Actual Rows"] * node["Actual Loops"] for node in scanned) <= 1


@pytest.mark.isolated_schema
def test_summary_boundary_probes_do_not_scan_unrelated_topics(api, db):
    # The global summary claim must use this test's controlled population;
    # unrelated modules otherwise change the planner and measured row counts.
    response = api.post(
        "/v1/messenger/streams/",
        json={"name": "Summary query scale", "source_name": "native"},
    )
    assert response.status_code == 201, response.text
    stream = response.json()
    with db.transaction(force_rollback=True):
        topic_count = 60
        message_count = topic_count * 20
        db.execute("UPDATE m_workspace_topic_summary_global_settings SET enabled=TRUE")
        db.execute(
            "INSERT INTO m_workspace_topic_summary_project_settings(project_id,enabled) "
            "VALUES (%s,TRUE)",
            (api.project_id,),
        )
        db.execute(
            """
            INSERT INTO messenger_topics(uuid,project_id,stream_uuid,name)
            SELECT md5(%s || ':topic:' || value)::uuid,%s,%s,'Scale ' || value
            FROM generate_series(0,%s) AS value
            """,
            (api.project_id, api.project_id, stream["uuid"], topic_count - 1),
        )
        db.execute(
            """
            INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload,created_at)
            SELECT md5(%s || ':message:' || value)::uuid,%s,%s,
                   '{"kind":"markdown","content":"Scale fixture"}'::jsonb,
                   timestamp '2026-01-01' + value * interval '1 second'
            FROM generate_series(1,%s) AS value
            """,
            (api.project_id, api.project_id, api.user_uuid, message_count),
        )
        db.execute(
            """
            INSERT INTO messenger_message_placements
                (uuid,project_id,message_uuid,stream_uuid,topic_uuid)
            SELECT md5(%s || ':message:' || value)::uuid,%s,
                   md5(%s || ':message:' || value)::uuid,%s,
                   md5(%s || ':topic:' || ((value-1)/20))::uuid
            FROM generate_series(1,%s) AS value
            """,
            (
                api.project_id,
                api.project_id,
                api.project_id,
                stream["uuid"],
                api.project_id,
                message_count,
            ),
        )
        db.execute(
            """
            UPDATE messenger_topics AS topic
            SET summary_last_message_uuid=md5(%s || ':message:' || (value+1)*20)::uuid
            FROM generate_series(0,%s) AS value
            WHERE topic.project_id=%s
              AND topic.uuid=md5(%s || ':topic:' || value)::uuid
            """,
            (api.project_id, topic_count - 1, api.project_id, api.project_id),
        )
        for table in (
            "messenger_topics",
            "messenger_messages",
            "messenger_message_placements",
        ):
            db.execute(f"ANALYZE {table}")

        class Explained(Exception):
            pass

        class Probe:
            def execute(self, statement, values):
                self.plan = db.execute(
                    "EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF) " + statement,
                    values,
                ).fetchone()[0][0]["Plan"]
                raise Explained()

        probe = Probe()
        with pytest.raises(Explained):
            topic_summarization.claim_summary_work(
                probe,
                now=datetime.datetime.now(datetime.timezone.utc),
                key_material="unused",
                topic_claim_seconds=60,
                endpoint_claim_seconds=60,
            )
        scope_rows = sum(
            node["Actual Rows"] * node["Actual Loops"]
            for node in _walk(probe.plan)
            if node.get("Relation Name") in {"messenger_topics", "messenger_streams"}
        )
        assert scope_rows <= message_count * 8
