# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util

import psycopg
import pytest

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
        ), access AS (
            INSERT INTO messenger_user_message_bindings
                (uuid,project_id,placement_uuid,user_uuid,membership_generation,
                 relation_role,visibility,permissions)
            SELECT gen_random_uuid(),project_id,uuid,%s,1,'author','visible','{}'::jsonb
            FROM placements
            RETURNING uuid,project_id,placement_uuid,user_uuid,membership_generation
        )
        INSERT INTO messenger_user_message_states
            (uuid,project_id,placement_uuid,user_uuid,membership_generation,binding_uuid)
        SELECT gen_random_uuid(),project_id,placement_uuid,user_uuid,membership_generation,uuid
        FROM access
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
    for table in (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
        "messenger_user_message_states",
        "messenger_message_counter_changes",
    ):
        db.execute(f"ANALYZE {table}")
    return db, account, target


@pytest.mark.isolated_schema
def test_counter_reconciliation_reads_only_changed_placements(large_history):
    db, account, target = large_history
    parameters = (account["project_id"], account["owner_user_uuid"], target[0], 1000)
    # Exclude one-time PL/pgSQL catalog/planning reads from the per-batch budget.
    assert db.execute(
        "SELECT * FROM messenger_apply_message_counter_changes(%s,%s,%s,0)",
        parameters[:3],
    ).fetchone() == (0, True)
    plan = db.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
        "SELECT * FROM messenger_apply_message_counter_changes(%s,%s,%s,%s)",
        parameters,
    ).fetchone()[0][0]
    blocks = plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"]
    assert blocks < 1000, plan
    assert db.execute(
        "SELECT sum(unread_count),sum(mentioned_count) FROM messenger_message_counter_totals "
        "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        parameters[:3],
    ).fetchone() == (1, 0)
    assert db.execute(
        "SELECT * FROM messenger_apply_message_counter_changes(%s,%s,%s,%s)",
        parameters,
    ).fetchone() == (0, False)


def test_counter_reconciliation_preserves_visibility_and_exact_deltas(incoming):
    _, db, account, _, _ = incoming
    incoming_tests.apply(incoming, incoming_tests.event(identifier=101))
    stream, placement, message = db.execute(
        "SELECT placement.stream_uuid,placement.uuid,message.uuid "
        "FROM messenger_messages message JOIN messenger_message_placements placement "
        "ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid "
        "WHERE message.project_id=%s AND message.provider_identity='101'",
        (account["project_id"],),
    ).fetchone()
    parameters = (account["project_id"], account["owner_user_uuid"], stream, 1000)

    def reconcile(expected):
        assert (
            db.execute(
                "SELECT * FROM messenger_apply_message_counter_changes(%s,%s,%s,%s)",
                parameters,
            ).fetchone()[1]
            is False
        )
        assert (
            db.execute(
                "SELECT COALESCE(sum(unread_count),0),COALESCE(sum(mentioned_count),0) "
                "FROM messenger_message_counter_totals "
                "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
                parameters[:3],
            ).fetchone()
            == expected
        )
        assert db.execute(
            "SELECT * FROM messenger_apply_message_counter_changes(%s,%s,%s,%s)",
            parameters,
        ).fetchone() == (0, False)

    reconcile((1, 0))
    db.execute(
        "UPDATE messenger_user_message_states SET mentioned=true "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        (*parameters[:2], placement),
    )
    reconcile((1, 1))
    db.execute(
        "UPDATE messenger_user_message_states SET read_at=now() "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        (*parameters[:2], placement),
    )
    reconcile((0, 0))
    db.execute(
        "UPDATE messenger_user_message_states SET read_at=NULL "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        (*parameters[:2], placement),
    )
    reconcile((1, 1))
    for visibility, expected in (("hidden", (0, 0)), ("visible", (1, 1))):
        db.execute(
            "UPDATE messenger_user_message_bindings SET visibility=%s "
            "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
            (visibility, *parameters[:2], placement),
        )
        reconcile(expected)
    for deleted, expected in ((True, (0, 0)), (False, (1, 1))):
        db.execute(
            "UPDATE messenger_messages SET deleted_at=CASE WHEN %s THEN now() ELSE NULL END "
            "WHERE project_id=%s AND uuid=%s",
            (deleted, account["project_id"], message),
        )
        reconcile(expected)
    for active, expected in ((False, (0, 0)), (True, (1, 1))):
        db.execute(
            "UPDATE messenger_stream_bindings SET active=%s "
            "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
            (active, *parameters[:3]),
        )
        db.execute(
            "UPDATE messenger_user_message_states SET mentioned=%s "
            "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
            (active, *parameters[:2], placement),
        )
        reconcile(expected)
    db.execute(
        "UPDATE messenger_stream_bindings SET membership_generation=membership_generation+1 "
        "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        parameters[:3],
    )
    db.execute(
        "UPDATE messenger_user_message_states SET mentioned=false "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        (*parameters[:2], placement),
    )
    reconcile((0, 0))


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
