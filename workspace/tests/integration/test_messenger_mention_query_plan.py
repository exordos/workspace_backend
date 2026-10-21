# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
import uuid

from workspace.tests.integration import conftest


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def test_unread_mentions_avoid_scanning_the_users_entire_history(api, db):
    stream = conftest.seed_user_stream(db, api.project_id, api.user_uuid, "Mentions")
    topic = conftest.seed_stream_topic(
        db, api.project_id, stream, api.user_uuid, "General", is_default=True
    )
    count = 30000
    prefix = str(uuid.uuid4())
    try:
        db.execute(
            """CREATE TEMP TABLE mention_plan_ids AS
               SELECT md5(%s || i::text)::uuid AS message,
                      md5(%s || 'placement:' || i::text)::uuid AS placement,
                      i FROM generate_series(1,%s) AS i""",
            (prefix, prefix, count),
        )
        db.execute(
            """INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload)
               SELECT message,%s,%s,'{"kind":"markdown","content":"history"}'::jsonb
               FROM mention_plan_ids""",
            (api.project_id, api.user_uuid),
        )
        db.execute(
            """INSERT INTO messenger_message_placements
                   (uuid,project_id,message_uuid,stream_uuid,topic_uuid)
               SELECT placement,%s,message,%s,%s FROM mention_plan_ids""",
            (api.project_id, stream, topic),
        )
        db.execute(
            """INSERT INTO messenger_user_message_bindings
                   (uuid,project_id,placement_uuid,user_uuid,membership_generation,
                    relation_role,visibility,permissions)
               SELECT ids.placement,%s,ids.placement,%s,member.membership_generation,
                      'member','visible','{}'::jsonb
               FROM mention_plan_ids ids CROSS JOIN messenger_stream_bindings member
               WHERE member.project_id=%s AND member.stream_uuid=%s AND member.user_uuid=%s""",
            (api.project_id, api.user_uuid, api.project_id, stream, api.user_uuid),
        )
        db.execute(
            """INSERT INTO messenger_user_message_states
                   (uuid,project_id,placement_uuid,user_uuid,membership_generation,mentioned)
               SELECT ids.placement,%s,ids.placement,%s,member.membership_generation,ids.i=%s
               FROM mention_plan_ids ids CROSS JOIN messenger_stream_bindings member
               WHERE member.project_id=%s AND member.stream_uuid=%s AND member.user_uuid=%s""",
            (
                api.project_id,
                api.user_uuid,
                count,
                api.project_id,
                stream,
                api.user_uuid,
            ),
        )
        for table in (
            "messenger_messages",
            "messenger_message_placements",
            "messenger_user_message_bindings",
            "messenger_user_message_states",
            "messenger_stream_bindings",
        ):
            db.execute(f"ANALYZE {table}")
        query = """SELECT * FROM messenger_api_user_messages_v1
                   WHERE project_id=%s AND user_uuid=%s AND visible
                     AND mentioned AND NOT read
                   ORDER BY created_at DESC,uuid DESC LIMIT 101"""
        values = (api.project_id, api.user_uuid)
        explain = "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) "
        plan = db.execute(explain + query, values).fetchone()[0][0]
        assert "messenger_user_message_states_unread_mentions_idx" in {
            node.get("Index Name") for node in _nodes(plan["Plan"])
        }, json.dumps(plan)
        scans = [
            node
            for node in _nodes(plan["Plan"])
            if node.get("Relation Name") == "messenger_user_message_states"
        ]
        assert (
            sum(
                (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
                * node["Actual Loops"]
                for node in scans
            )
            < 50
        ), json.dumps(plan)
        expected = db.execute(
            "SELECT placement FROM mention_plan_ids WHERE i=%s", (count,)
        ).fetchone()[0]
        response = api.get(
            "/v1/messenger/messages/",
            params={
                "project_id": api.project_id,
                "mentioned": "true",
                "read": "false",
                "sort_key": "created_at",
                "sort_dir": "desc",
                "page_limit": 100,
            },
        )
        assert response.status_code == 200, response.text
        assert [row["uuid"] for row in response.json()] == [str(expected)]
        print(
            json.dumps(
                {"history_states": count, "mention_query_ms": plan["Execution Time"]}
            )
        )
    finally:
        db.execute(
            "DELETE FROM messenger_messages WHERE project_id=%s", (api.project_id,)
        )
        db.execute(
            "DELETE FROM messenger_streams WHERE project_id=%s", (api.project_id,)
        )
        db.execute("DROP TABLE IF EXISTS mention_plan_ids")
