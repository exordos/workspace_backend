# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util

import psycopg
import pytest

from workspace.services.messenger_workers import counters
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming


@pytest.fixture
def large_history(incoming):
    _, db, account, http, _ = incoming
    incoming_tests.apply(incoming, incoming_tests.event(identifier=101))
    target = db.execute(
        "SELECT placement.stream_uuid,placement.topic_uuid,message.content_uuid,placement.uuid "
        "FROM messenger_messages message JOIN messenger_message_placements placement "
        "ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid "
        "WHERE message.project_id=%s AND message.provider_identity='101'",
        (account["project_id"],),
    ).fetchone()
    http.request.return_value = {
        "stream": {"stream_id": 43, "name": "Unrelated history", "description": ""}
    }
    event = incoming_tests.event(identifier=102)
    event["message"]["stream_id"] = 43
    incoming_tests.apply(incoming, event)
    stream, topic = db.execute(
        "SELECT placement.stream_uuid,placement.topic_uuid "
        "FROM messenger_messages message JOIN messenger_message_placements placement "
        "ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid "
        "WHERE message.project_id=%s AND message.provider_identity='102'",
        (account["project_id"],),
    ).fetchone()
    db.execute(
        """
        WITH messages AS (
            INSERT INTO messenger_messages
                (uuid,project_id,author_uuid,content_uuid,source_name,provider_uuid,provider_identity)
            SELECT gen_random_uuid(),%s,%s,%s,'zulip',%s,(1000000+number)::text
            FROM generate_series(1,10000) AS input(number)
            RETURNING uuid,project_id
        ), placements AS (
            INSERT INTO messenger_message_placements
                (uuid,project_id,message_uuid,stream_uuid,topic_uuid)
            SELECT gen_random_uuid(),project_id,uuid,%s,%s FROM messages
            RETURNING uuid,project_id
        )
        INSERT INTO messenger_user_message_bindings
            (uuid,project_id,placement_uuid,user_uuid)
        SELECT gen_random_uuid(),project_id,uuid,%s FROM placements
        """,
        (
            account["project_id"],
            account["owner_user_uuid"],
            target[2],
            account["provider_uuid"],
            stream,
            topic,
            account["owner_user_uuid"],
        ),
    )
    db.execute(
        """INSERT INTO messenger_user_message_unreads
               (project_id,user_uuid,placement_uuid,stream_uuid,topic_uuid)
           SELECT binding.project_id,binding.user_uuid,binding.placement_uuid,%s,%s
           FROM messenger_user_message_bindings binding
           JOIN messenger_message_placements placement
             ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid
           WHERE binding.project_id=%s AND binding.user_uuid=%s AND placement.topic_uuid=%s
           ON CONFLICT DO NOTHING""",
        (stream, topic, account["project_id"], account["owner_user_uuid"], topic),
    )
    for table in (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
        "messenger_user_message_unreads",
        "messenger_user_message_mentions",
        "messenger_stream_bindings",
        "messenger_streams",
        "messenger_topics",
        "messenger_user_topic_bindings",
    ):
        db.execute(f"ANALYZE {table}")
    return db, account, target


@pytest.mark.isolated_schema
def test_unread_count_does_not_scan_unrelated_topic_history(large_history):
    db, account, target = large_history
    parameters = (account["project_id"], account["owner_user_uuid"], target[1])
    assert db.execute(counters.COUNT_TOPIC_SQL, parameters).fetchone()[0] == 1
    plan = db.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + counters.COUNT_TOPIC_SQL,
        parameters,
    ).fetchone()[0][0]
    blocks = plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"]
    assert blocks < 1000, plan


def test_unread_count_preserves_visibility_mentions_and_membership(incoming):
    _, db, account, _, _ = incoming
    incoming_tests.apply(incoming, incoming_tests.event(identifier=101))
    stream, topic, placement, message = db.execute(
        "SELECT placement.stream_uuid,placement.topic_uuid,placement.uuid,message.uuid "
        "FROM messenger_messages message JOIN messenger_message_placements placement "
        "ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid "
        "WHERE message.project_id=%s AND message.provider_identity='101'",
        (account["project_id"],),
    ).fetchone()
    parameters = (account["project_id"], account["owner_user_uuid"], topic)
    fact = (account["project_id"], account["owner_user_uuid"], placement, stream, topic)
    db.execute(
        "UPDATE messenger_user_topic_bindings SET notification_mode='mute' "
        "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        parameters,
    )

    def count(expected):
        assert db.execute(counters.COUNT_TOPIC_SQL, parameters).fetchone() == expected

    count((1, 0))
    db.execute(
        "INSERT INTO messenger_user_message_mentions VALUES(%s,%s,%s,%s,%s)", fact
    )
    count((1, 1))
    db.execute(
        "DELETE FROM messenger_user_message_unreads WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        fact[:3],
    )
    count((0, 0))
    db.execute(
        "INSERT INTO messenger_user_message_unreads VALUES(%s,%s,%s,%s,%s)", fact
    )
    count((1, 1))
    db.execute(
        "DELETE FROM messenger_user_message_bindings WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        fact[:3],
    )
    count((0, 0))
    db.execute(
        "INSERT INTO messenger_user_message_bindings(uuid,project_id,user_uuid,placement_uuid) VALUES(gen_random_uuid(),%s,%s,%s)",
        fact[:3],
    )
    count((1, 1))
    for deleted, expected in ((True, (0, 0)), (False, (1, 1))):
        db.execute(
            "UPDATE messenger_messages SET deleted_at=CASE WHEN %s THEN now() ELSE NULL END "
            "WHERE project_id=%s AND uuid=%s",
            (deleted, account["project_id"], message),
        )
        count(expected)
    for active, expected in ((False, (0, 0)), (True, (1, 1))):
        db.execute(
            "UPDATE messenger_stream_bindings SET active=%s "
            "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
            (active, account["project_id"], account["owner_user_uuid"], stream),
        )
        count(expected)
    db.execute(
        "UPDATE messenger_stream_bindings SET membership_generation=membership_generation+1 "
        "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        (account["project_id"], account["owner_user_uuid"], stream),
    )
    count((0, 0))


def test_bounded_counter_migration_restores_the_previous_function(_database):
    with conftest.isolated_migration_database(
        "0275-Qualify-immutable-content-digest-functions-for-backup-restore-67692f.py"
    ) as url:
        path = next(conftest.MIGRATIONS_DIR.glob("0276-Bound-message-counter-*.py"))
        spec = importlib.util.spec_from_file_location("bounded_counter_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        query = "SELECT pg_get_functiondef('messenger_apply_message_counter_changes(uuid,uuid,uuid,integer)'::regprocedure)"
        with psycopg.connect(url, autocommit=True) as db:
            before = db.execute(query).fetchone()[0]
            migration.migration_step.upgrade(db)
            assert "CROSS JOIN LATERAL" in db.execute(query).fetchone()[0]
            migration.migration_step.downgrade(db)
            assert db.execute(query).fetchone()[0] == before
            migration.migration_step.upgrade(db)
