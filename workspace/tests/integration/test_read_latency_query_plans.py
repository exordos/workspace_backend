# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime

import pytest
from restalchemy.common import contexts

from workspace.messenger_api import message_flags, topic_summarization
from workspace.tests.integration import conftest


def _walk(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _walk(child)


@pytest.mark.parametrize("has_unread", [False, True])
def test_mark_read_deletes_only_the_requested_unread_scope(api, db, has_unread):
    stream = conftest.seed_user_stream(db, api.project_id, api.user_uuid, "Read target")
    topic = conftest.seed_stream_topic(
        db, api.project_id, stream, api.user_uuid, "Target", is_default=True
    )
    noise_stream = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Read noise"
    )
    noise_topic = conftest.seed_stream_topic(
        db, api.project_id, noise_stream, api.user_uuid, "Noise", is_default=True
    )
    for target_stream, target_topic, count in (
        (stream, topic, 10 if has_unread else 0),
        (noise_stream, noise_topic, 1500),
    ):
        db.execute(
            """WITH content AS MATERIALIZED (
                 SELECT messenger_resolve_message_content('{"kind":"markdown","content":"Read plan"}'::jsonb,NULL) AS uuid
               ), messages AS (
                 INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid)
                 SELECT gen_random_uuid(),%s,%s,uuid FROM generate_series(1,%s) CROSS JOIN content RETURNING *
               ), placements AS (
                 INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid,created_at)
                 SELECT gen_random_uuid(),project_id,uuid,%s,%s,created_at FROM messages RETURNING *
               ) INSERT INTO messenger_user_message_bindings(uuid,project_id,user_uuid,placement_uuid)
                 SELECT gen_random_uuid(),project_id,%s,uuid FROM placements""",
            (
                api.project_id,
                api.user_uuid,
                count,
                target_stream,
                target_topic,
                api.user_uuid,
            ),
        )
        db.execute(
            "INSERT INTO messenger_user_message_unreads SELECT project_id,%s,uuid,stream_uuid,topic_uuid "
            "FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s",
            (api.user_uuid, api.project_id, target_topic),
        )
    for table in (
        "messenger_user_message_unreads",
        "messenger_user_message_bindings",
        "messenger_messages",
        "messenger_message_placements",
    ):
        db.execute(f"ANALYZE {table}")
    plans = []

    class ExplainDelete:
        def __init__(self, session):
            self.session = session

        def execute(self, statement, values):
            with db.transaction(force_rollback=True):
                plans.append(
                    db.execute(
                        "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + statement,
                        values,
                    ).fetchone()[0][0]["Plan"]
                )
            return self.session.execute(statement, values)

    with contexts.Context().session_manager() as session:
        deleted = message_flags.mark_read(
            ExplainDelete(session), api.project_id, api.user_uuid, topic_uuid=topic
        )
    assert len(deleted) == (10 if has_unread else 0)
    assert len(plans) == 1
    unread_scans = [
        node
        for node in _walk(plans[0])
        if node.get("Relation Name") == "messenger_user_message_unreads"
        and node["Node Type"] != "ModifyTable"
    ]
    assert unread_scans, plans
    assert all(
        node["Node Type"] in {"Index Scan", "Index Only Scan", "Bitmap Heap Scan"}
        for node in unread_scans
    ), plans
    assert (
        sum(
            (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
            * node["Actual Loops"]
            for node in unread_scans
        )
        < 50
    ), plans
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_unreads WHERE project_id=%s AND topic_uuid=%s",
        (api.project_id, noise_topic),
    ).fetchone() == (1500,)


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
            WITH content AS MATERIALIZED (
                SELECT messenger_resolve_message_content(
                    '{"kind":"markdown","content":"Scale fixture"}'::jsonb,NULL) AS uuid
            )
            INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid,created_at)
            SELECT md5(%s || ':message:' || value)::uuid,%s,%s,
                   content.uuid,
                   timestamp '2026-01-01' + value * interval '1 second'
            FROM generate_series(1,%s) AS value CROSS JOIN content
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
