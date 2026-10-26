# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import concurrent.futures
import importlib.util
import itertools
import json
import os
import threading
import time
import types
import uuid as sys_uuid

import psycopg
import pytest
from oslo_config import cfg
from restalchemy.common import contexts
from restalchemy.storage.sql import sessions

from workspace.common import messenger_reaction_opts
from workspace.messenger_api import publication
from workspace.messenger_api import events
from workspace.services.messenger_workers import agents
from workspace.tests.integration import legacy_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/messenger/streams/"
MESSAGES = f"{V1}/messenger/messages/"
MESSAGE_REACTIONS = f"{V1}/messenger/message_reactions/"


@pytest.fixture(autouse=True)
def _production_v2_store():
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(legacy_store.LegacyMessengerStoreFactory())


@pytest.fixture(autouse=True)
def _isolate_projection_queue(_database):
    def cleanup():
        with contexts.Context().session_manager() as session:
            session.execute("DELETE FROM messenger_projection_scope_leases", ())
            session.execute("DELETE FROM messenger_domain_outbox_events", ())

    cleanup()
    try:
        yield
    finally:
        cleanup()


def _drain() -> int:
    with contexts.Context().session_manager() as session:
        return v2_projection.drain_projection_queue(
            session,
            f"integration-performance:{sys_uuid.uuid4()}",
            limit=10000,
        )


def _create_message(api, name):
    stream = api.post(
        STREAMS,
        json={
            "name": name,
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": name},
        },
    ).json()
    _drain()
    return stream, message


def _seed_partition_claim_tasks(api, specifications):
    event_uuids = [sys_uuid.uuid4() for _ in specifications]
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT input.uuid, %s, input.event_kind, input.scope_kind,
                   input.scope_key, input.payload::jsonb,
                   NOW() + input.position * INTERVAL '1 millisecond',
                   NOW() + input.position * INTERVAL '1 millisecond'
            FROM unnest(
                %s::uuid[], %s::text[], %s::text[], %s::text[],
                %s::text[], %s::integer[]
            ) AS input(
                uuid, event_kind, scope_kind, scope_key, payload, position
            )
            """,
            (
                api.project_id,
                event_uuids,
                [specification["task_kind"] for specification in specifications],
                [specification["scope_kind"] for specification in specifications],
                [specification["scope_key"] for specification in specifications],
                [
                    json.dumps(specification["payload"])
                    for specification in specifications
                ],
                list(range(len(specifications))),
            ),
        )
    return event_uuids


def _plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


@pytest.mark.parametrize(
    ("task_kind", "lane", "users"),
    [
        ("reaction_snapshot", "reaction_snapshot", {}),
        ("fanout", "fanout", {}),
        ("read_counters", "read_state", {"user_uuids": []}),
    ],
)
def test_claim_without_users_does_not_scan_unrelated_user_predecessors(
    api, db, monkeypatch, task_kind, lane, users
):
    db.execute(
        """
        INSERT INTO messenger_domain_outbox_events
            (uuid,project_id,event_kind,scope_kind,scope_key,payload,created_at)
        SELECT gen_random_uuid(),%s,'read_counters','user-topic',number::text,
               jsonb_build_object('source_kind','messages.read','user_uuid',%s::text),
               NOW()-interval '1 day'
        FROM generate_series(1,10000) AS input(number)
        """,
        (api.project_id, api.user_uuid),
    )
    target = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": task_kind,
                "scope_kind": "message",
                "scope_key": str(sys_uuid.uuid4()),
                "payload": {"source_kind": "zulip.incoming", **users},
            }
        ],
    )[0]
    db.execute("ANALYZE messenger_projection_tasks, messenger_domain_outbox_events")
    plans = []
    execute = sessions.PgSQLSession.execute

    def explain(current, statement, values=None):
        if "FROM messenger_projection_tasks AS partition_predecessor" in statement:
            plans.append(
                execute(
                    current,
                    "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + statement,
                    values,
                ).fetchone()["QUERY PLAN"][0]["Plan"]
            )
        return execute(current, statement, values)

    monkeypatch.setattr(sessions.PgSQLSession, "execute", explain)
    monkeypatch.setattr(v2_projection, "_FAIR_SCHEDULER_CYCLE", itertools.repeat(lane))
    with contexts.Context().session_manager() as session:
        claimed = v2_projection._claim_task(session, "integration:empty-users", 30)
        assert claimed["outbox_event_uuid"] == target
    assert len(plans) == 1
    scanned = [
        node
        for node in _plan_nodes(plans[0])
        if node.get("Alias") == "partition_predecessor"
    ]
    assert scanned
    assert all(node["Actual Loops"] == 0 for node in scanned), json.dumps(scanned)


@pytest.mark.parametrize("scope_kind", ["user-stream", "user-topic"])
def test_old_scope_preview_does_not_scan_unrelated_project_history(
    api, db, monkeypatch, scope_kind
):
    stream, message = _create_message(api, "old-preview-target")
    noise_stream, _noise_message = _create_message(api, "recent-preview-noise")
    db.execute(
        "UPDATE messenger_messages SET created_at='2020-01-01' "
        "WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s)",
        (api.project_id, api.project_id, message["uuid"]),
    )
    db.execute(
        "UPDATE messenger_message_placements SET created_at='2020-01-01' WHERE project_id=%s AND uuid=%s",
        (api.project_id, message["uuid"]),
    )
    noise_rows = int(os.environ.get("WORKSPACE_PREVIEW_NOISE_ROWS", "20000"))
    for offset in range(0, noise_rows, 5000):
        db.execute(
            """
            WITH content AS MATERIALIZED (
                SELECT messenger_resolve_message_content(
                    '{"kind":"markdown","content":"Recent unrelated message"}'::jsonb,NULL) AS uuid
            ), messages AS (
                INSERT INTO messenger_messages
                    (uuid,project_id,author_uuid,content_uuid,source_name,created_at)
                SELECT gen_random_uuid(),%s,%s,
                       content.uuid,
                       'native',
                       NOW()+number*interval '1 millisecond'
                FROM generate_series(1,%s) AS input(number) CROSS JOIN content
                RETURNING uuid,project_id,created_at
            ), placements AS (
                INSERT INTO messenger_message_placements
                    (uuid,project_id,message_uuid,stream_uuid,topic_uuid,created_at)
                SELECT gen_random_uuid(),project_id,uuid,%s,%s,created_at FROM messages
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
                (uuid,project_id,placement_uuid,user_uuid,membership_generation,binding_uuid,read_at)
            SELECT gen_random_uuid(),project_id,placement_uuid,user_uuid,membership_generation,uuid,NOW()
            FROM access
            """,
            (
                api.project_id,
                api.user_uuid,
                min(5000, noise_rows - offset),
                noise_stream["uuid"],
                noise_stream["default_topic_uuid"],
                api.user_uuid,
            ),
        )
    for table in (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_states",
        "messenger_user_message_bindings",
    ):
        db.execute(f"ANALYZE {table}")

    snapshots = []
    with contexts.Context().session_manager() as session:

        def execute(query, parameters=None):
            if query.lstrip().startswith("WITH unread AS"):
                snapshots.append((query, parameters))
            return session.execute(query, parameters)

        v2_projection._refresh_recipient_counters(
            types.SimpleNamespace(execute=execute),
            api.project_id,
            sys_uuid.UUID(stream["uuid"]),
            sys_uuid.UUID(stream["default_topic_uuid"]),
            [api.user_uuid],
            scope_kind,
        )
    table, column, scope_uuid = (
        ("messenger_stream_bindings", "stream_uuid", stream["uuid"])
        if scope_kind == "user-stream"
        else (
            "messenger_user_topic_bindings",
            "topic_uuid",
            stream["default_topic_uuid"],
        )
    )
    assert db.execute(
        f"SELECT last_message_uuid FROM {table} WHERE project_id=%s AND {column}=%s AND user_uuid=%s",
        (api.project_id, scope_uuid, api.user_uuid),
    ).fetchone() == (sys_uuid.UUID(message["uuid"]),)
    ((query, parameters),) = snapshots
    plan = db.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query, parameters
    ).fetchone()[0][0]
    examined_project_messages = sum(
        node["Actual Rows"] * node["Actual Loops"]
        for node in _plan_nodes(plan["Plan"])
        if node.get("Relation Name") == "messenger_messages"
    )
    examined_personal_states = sum(
        node["Actual Rows"] * node["Actual Loops"]
        for node in _plan_nodes(plan["Plan"])
        if node.get("Relation Name") == "messenger_user_message_states"
    )
    assert examined_personal_states <= 4, plan
    assert examined_project_messages <= 4


@pytest.mark.isolated_schema
def test_counter_preview_bounds_empty_and_sparse_recipient_history(
    api, db, monkeypatch
):
    stream, original = _create_message(api, "cassi-preview-large-topic")
    noise, _ = _create_message(api, "cassi-preview-other-topic")
    peer = sys_uuid.uuid4()
    for scope in (stream, noise):
        conftest.seed_user_stream_binding(db, api.project_id, scope["uuid"], peer)
    db.execute(
        "INSERT INTO messenger_user_topic_bindings (uuid,project_id,user_uuid,topic_uuid) "
        "VALUES (gen_random_uuid(),%s,%s,%s) ON CONFLICT DO NOTHING",
        (api.project_id, peer, stream["default_topic_uuid"]),
    )
    for scope, user, count in (
        (
            stream,
            api.user_uuid,
            int(os.environ.get("WORKSPACE_PREVIEW_SCOPE_ROWS", "30000")),
        ),
        (noise, peer, int(os.environ.get("WORKSPACE_PREVIEW_PEER_ROWS", "30000"))),
        (
            noise,
            api.user_uuid,
            int(os.environ.get("WORKSPACE_PREVIEW_OWNER_ROWS", "300")),
        ),
    ):
        for offset in range(0, count, 5000):
            db.execute(
                """
                WITH messages AS (
                    INSERT INTO messenger_messages
                        (uuid,project_id,author_uuid,content_uuid,source_name,created_at)
                    SELECT gen_random_uuid(),%s,%s,content_uuid,'native',
                           NOW()+number*interval '1 millisecond'
                    FROM generate_series(1,%s) AS input(number)
                    CROSS JOIN (SELECT content_uuid FROM messenger_messages
                                WHERE project_id=%s LIMIT 1) AS content
                    RETURNING uuid,project_id,created_at
                ), placements AS (
                    INSERT INTO messenger_message_placements
                        (uuid,project_id,message_uuid,stream_uuid,topic_uuid,created_at)
                    SELECT gen_random_uuid(),project_id,uuid,%s,%s,created_at FROM messages
                    RETURNING uuid,project_id
                ), access AS (
                    INSERT INTO messenger_user_message_bindings
                        (uuid,project_id,placement_uuid,user_uuid,membership_generation,
                         relation_role,visibility,permissions)
                    SELECT gen_random_uuid(),project_id,uuid,%s,1,'member','visible','{}'::jsonb
                    FROM placements
                    RETURNING uuid,project_id,placement_uuid,user_uuid,membership_generation
                )
                INSERT INTO messenger_user_message_states
                    (uuid,project_id,placement_uuid,user_uuid,membership_generation,binding_uuid,read_at)
                SELECT gen_random_uuid(),project_id,placement_uuid,user_uuid,membership_generation,uuid,NOW()
                FROM access
                """,
                (
                    api.project_id,
                    user,
                    min(5000, count - offset),
                    api.project_id,
                    scope["uuid"],
                    scope["default_topic_uuid"],
                    user,
                ),
            )
    db.execute(
        "UPDATE messenger_user_message_states SET read_at=NOW() WHERE project_id=%s AND read_at IS NULL",
        (api.project_id,),
    )
    for table in (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_states",
        "messenger_user_message_bindings",
    ):
        db.execute(f"ANALYZE {table}")
    full_preview = db.execute(
        "SELECT placement.uuid FROM messenger_message_placements placement "
        "JOIN messenger_messages message ON message.project_id=placement.project_id "
        "AND message.uuid=placement.message_uuid WHERE placement.project_id=%s "
        "AND placement.topic_uuid=%s ORDER BY message.created_at DESC,placement.uuid DESC LIMIT 1",
        (api.project_id, stream["default_topic_uuid"]),
    ).fetchone()[0]
    for sparse in (False, True):
        if sparse:
            db.execute(
                """WITH access AS (
                    INSERT INTO messenger_user_message_bindings
                        (uuid,project_id,placement_uuid,user_uuid,membership_generation,
                         relation_role,visibility,permissions)
                    VALUES (gen_random_uuid(),%s,%s,%s,1,'member','visible','{}'::jsonb)
                    RETURNING *
                ) INSERT INTO messenger_user_message_states
                    (uuid,project_id,placement_uuid,user_uuid,membership_generation,binding_uuid,read_at)
                SELECT gen_random_uuid(),project_id,placement_uuid,user_uuid,membership_generation,uuid,NOW()
                FROM access""",
                (api.project_id, original["uuid"], peer),
            )
        for scope_kind, table, column, scope_uuid in (
            (
                "user-topic",
                "messenger_user_topic_bindings",
                "topic_uuid",
                stream["default_topic_uuid"],
            ),
            ("user-stream", "messenger_stream_bindings", "stream_uuid", stream["uuid"]),
        ):
            # A recipient must receive a corrected empty snapshot, not be skipped.
            db.execute(
                f"UPDATE {table} SET unread_count=9,active_unread_count=9,last_message_uuid=%s "
                f"WHERE project_id=%s AND {column}=%s AND user_uuid=%s",
                (original["uuid"], api.project_id, scope_uuid, peer),
            )
            snapshots = []
            with contexts.Context().session_manager() as session:

                def execute(query, parameters=None):
                    if query.lstrip().startswith("WITH unread AS"):
                        snapshots.append((query, parameters))
                    return session.execute(query, parameters)

                v2_projection._refresh_recipient_counters(
                    types.SimpleNamespace(execute=execute),
                    api.project_id,
                    stream["uuid"],
                    stream["default_topic_uuid"],
                    [api.user_uuid, peer],
                    scope_kind,
                )
            for user, preview in (
                (api.user_uuid, full_preview),
                (peer, sys_uuid.UUID(original["uuid"]) if sparse else None),
            ):
                assert db.execute(
                    f"SELECT unread_count,active_unread_count,last_message_uuid FROM {table} "
                    f"WHERE project_id=%s AND {column}=%s AND user_uuid=%s",
                    (api.project_id, scope_uuid, user),
                ).fetchone() == (0, 0, preview)
            ((query, parameters),) = snapshots
            plan = db.execute(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query, parameters
            ).fetchone()[0][0]
            examined = sum(
                node["Actual Rows"] * node["Actual Loops"]
                for node in _plan_nodes(plan["Plan"])
                if node.get("Relation Name") == "messenger_message_placements"
            )
            assert examined < 1000, plan
            assert (
                plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"]
                < 25000
            ), plan


@pytest.mark.parametrize("scope_kind", ["user-stream", "user-topic"])
def test_old_scope_preview_preserves_visibility_dates_and_uuid_ties(
    api, db, monkeypatch, scope_kind
):
    stream, original = _create_message(api, "preview-visibility-target")
    messages = []
    for label in ("visible-a", "visible-b", "hidden", "old-generation", "deleted"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": stream["default_topic_uuid"],
                "payload": {"kind": "markdown", "content": label},
            },
        )
        assert response.status_code == 201, response.text
        messages.append(sys_uuid.UUID(response.json()["uuid"]))
        _drain()
    _create_message(api, "preview-visibility-noise")
    for position, placement_uuid in enumerate(messages):
        db.execute(
            "UPDATE messenger_messages SET created_at='2020-01-01'::timestamptz+%s*interval '1 day' "
            "WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s)",
            (max(1, position), api.project_id, api.project_id, placement_uuid),
        )
    db.execute(
        "UPDATE messenger_user_message_bindings SET visibility='hidden' "
        "WHERE project_id=%s AND placement_uuid=ANY(%s::uuid[])",
        (api.project_id, [sys_uuid.UUID(original["uuid"]), messages[2]]),
    )
    for table in ("messenger_user_message_states", "messenger_user_message_bindings"):
        db.execute(
            f"UPDATE {table} SET membership_generation=2 WHERE project_id=%s AND placement_uuid=%s",
            (api.project_id, messages[3]),
        )
    db.execute(
        "UPDATE messenger_messages SET deleted_at=NOW() "
        "WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s)",
        (api.project_id, api.project_id, messages[4]),
    )
    table, column, scope_uuid = (
        ("messenger_stream_bindings", "stream_uuid", stream["uuid"])
        if scope_kind == "user-stream"
        else (
            "messenger_user_topic_bindings",
            "topic_uuid",
            stream["default_topic_uuid"],
        )
    )
    for expected in (*sorted(messages[:2], reverse=True), None):
        with contexts.Context().session_manager() as session:
            v2_projection._refresh_recipient_counters(
                session,
                api.project_id,
                sys_uuid.UUID(stream["uuid"]),
                sys_uuid.UUID(stream["default_topic_uuid"]),
                [api.user_uuid],
                scope_kind,
            )
        assert db.execute(
            f"SELECT last_message_uuid FROM {table} WHERE project_id=%s AND {column}=%s AND user_uuid=%s",
            (api.project_id, scope_uuid, api.user_uuid),
        ).fetchone() == (expected,)
        if expected is not None:
            db.execute(
                "UPDATE messenger_user_message_bindings SET visibility='hidden' "
                "WHERE project_id=%s AND placement_uuid=%s",
                (api.project_id, expected),
            )


@pytest.mark.isolated_schema
@pytest.mark.parametrize("relocated_past_extent", [False, True])
def test_personal_preview_scope_backfills_existing_states(
    api, db, relocated_past_extent
):
    db.execute(
        "DROP TRIGGER messenger_set_personal_preview_scope ON messenger_user_message_states"
    )
    stream, message = _create_message(api, "preview-before-migration")
    placement_uuid = sys_uuid.UUID(message["uuid"])
    assert db.execute(
        "SELECT preview_stream_uuid,preview_topic_uuid,preview_created_at "
        "FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s",
        (api.project_id, placement_uuid),
    ).fetchone() == (None, None, None)
    db.execute(
        """WITH users AS (
            INSERT INTO m_workspace_users
                (uuid,username,source,status,avatar,created_at,updated_at)
            SELECT gen_random_uuid(),'preview-peer-'||number,'iam','active',
                   avatar,NOW(),NOW()
            FROM m_workspace_users CROSS JOIN generate_series(1,128) AS input(number)
            WHERE uuid=%s RETURNING uuid
        ), members AS (
            INSERT INTO messenger_project_users(project_id,user_uuid)
            SELECT %s,uuid FROM users RETURNING user_uuid
        ), access AS (
            INSERT INTO messenger_user_message_bindings
                (uuid,project_id,placement_uuid,user_uuid,membership_generation,
                 relation_role,visibility,permissions)
            SELECT gen_random_uuid(),%s,%s,user_uuid,1,
                   'member','visible','{}'::jsonb FROM members
            RETURNING uuid,project_id,placement_uuid,user_uuid,membership_generation
        ) INSERT INTO messenger_user_message_states
            (uuid,project_id,placement_uuid,user_uuid,membership_generation,binding_uuid,read_at)
          SELECT gen_random_uuid(),project_id,placement_uuid,user_uuid,
                 membership_generation,uuid,NOW() FROM access""",
        (api.user_uuid, api.project_id, api.project_id, placement_uuid),
    )
    before = db.execute(
        "SELECT read_at,mentioned,starred,pinned,created_at,updated_at "
        "FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s",
        (api.project_id, placement_uuid, api.user_uuid),
    ).fetchone()
    filename = next(conftest.MIGRATIONS_DIR.glob("0280-*.py"))
    spec = importlib.util.spec_from_file_location("personal_preview_scope", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BATCH_BLOCKS = 1
    with contexts.Context().session_manager() as session:
        captured_extent = False

        def execute(query, parameters=None):
            nonlocal captured_extent
            if (
                relocated_past_extent
                and not captured_extent
                and query.startswith("SELECT ceil")
            ):
                # Emulate pre-existing rows relocating beyond the extent that
                # was captured before a concurrent flag update.
                captured_extent = True
                return types.SimpleNamespace(fetchone=lambda: {"blocks": 0})
            return session.execute(query, parameters)

        module.migration_step.upgrade(
            types.SimpleNamespace(
                execute=execute, commit=session.commit, engine=session.engine
            )
        )
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_states "
        "WHERE project_id=%s AND preview_stream_uuid=%s AND preview_topic_uuid=%s "
        "AND preview_created_at IS NOT NULL",
        (api.project_id, stream["uuid"], stream["default_topic_uuid"]),
    ).fetchone() == (129,)
    assert db.execute(
        "SELECT personal.preview_stream_uuid=placement.stream_uuid,"
        "personal.preview_topic_uuid=placement.topic_uuid,"
        "personal.preview_created_at=message.created_at "
        "FROM messenger_user_message_states personal "
        "JOIN messenger_message_placements placement ON placement.project_id=personal.project_id "
        "AND placement.uuid=personal.placement_uuid "
        "JOIN messenger_messages message ON message.project_id=placement.project_id "
        "AND message.uuid=placement.message_uuid "
        "WHERE personal.project_id=%s AND personal.placement_uuid=%s",
        (api.project_id, placement_uuid),
    ).fetchone() == (True, True, True)
    assert (
        db.execute(
            "SELECT read_at,mentioned,starred,pinned,created_at,updated_at "
            "FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s",
            (api.project_id, placement_uuid, api.user_uuid),
        ).fetchone()
        == before
    )
    with contexts.Context().session_manager() as session:
        v2_projection._refresh_recipient_counters(
            session,
            api.project_id,
            stream["uuid"],
            stream["default_topic_uuid"],
            [api.user_uuid],
            "user-topic",
        )
    assert db.execute(
        "SELECT last_message_uuid FROM messenger_user_topic_bindings "
        "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (api.project_id, api.user_uuid, stream["default_topic_uuid"]),
    ).fetchone() == (placement_uuid,)


@pytest.mark.isolated_schema
def test_personal_preview_scope_resumes_while_history_is_being_read(
    api, db, _test_database_url
):
    _stream, message = _create_message(api, "preview-resume-with-readers")
    db.execute(
        "UPDATE messenger_user_message_states SET preview_stream_uuid=NULL "
        "WHERE project_id=%s AND placement_uuid=%s",
        (api.project_id, message["uuid"]),
    )
    filename = next(conftest.MIGRATIONS_DIR.glob("0280-*.py"))
    spec = importlib.util.spec_from_file_location("personal_preview_resume", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with psycopg.connect(_test_database_url) as reader:
        reader.execute("SELECT 1 FROM messenger_user_message_states LIMIT 1")
        with contexts.Context().session_manager() as session:
            module.migration_step.upgrade(session)
    assert db.execute(
        "SELECT preview_stream_uuid IS NOT NULL FROM messenger_user_message_states "
        "WHERE project_id=%s AND placement_uuid=%s",
        (api.project_id, message["uuid"]),
    ).fetchone() == (True,)


@pytest.mark.isolated_schema
def test_personal_preview_scope_tracks_moves_and_canonical_date_changes(api, db):
    original, first = _create_message(api, "preview-source")
    destination, second = _create_message(api, "preview-destination")
    first_uuid, second_uuid = (
        sys_uuid.UUID(first["uuid"]),
        sys_uuid.UUID(second["uuid"]),
    )
    db.execute(
        "UPDATE messenger_message_placements SET created_at='2030-01-01' "
        "WHERE project_id=%s AND uuid=%s",
        (api.project_id, first_uuid),
    )
    db.execute(
        "UPDATE messenger_messages SET created_at='2020-01-01' "
        "WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements "
        "WHERE project_id=%s AND uuid=%s)",
        (api.project_id, api.project_id, first_uuid),
    )
    db.execute(
        "UPDATE messenger_message_placements SET stream_uuid=%s,topic_uuid=%s "
        "WHERE project_id=%s AND uuid=%s",
        (
            destination["uuid"],
            destination["default_topic_uuid"],
            api.project_id,
            first_uuid,
        ),
    )
    assert db.execute(
        "SELECT preview_stream_uuid,preview_topic_uuid,preview_created_at::date::text "
        "FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s",
        (api.project_id, first_uuid),
    ).fetchone() == (
        sys_uuid.UUID(destination["uuid"]),
        sys_uuid.UUID(destination["default_topic_uuid"]),
        "2020-01-01",
    )
    for scope, expected in ((original, None), (destination, second_uuid)):
        with contexts.Context().session_manager() as session:
            v2_projection._refresh_recipient_counters(
                session,
                api.project_id,
                scope["uuid"],
                scope["default_topic_uuid"],
                [api.user_uuid],
                "user-stream",
            )
        assert db.execute(
            "SELECT last_message_uuid FROM messenger_stream_bindings "
            "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
            (api.project_id, api.user_uuid, scope["uuid"]),
        ).fetchone() == (expected,)
    db.execute(
        "UPDATE messenger_messages SET created_at='2040-01-01' "
        "WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements "
        "WHERE project_id=%s AND uuid=%s)",
        (api.project_id, api.project_id, first_uuid),
    )
    with contexts.Context().session_manager() as session:
        v2_projection._refresh_recipient_counters(
            session,
            api.project_id,
            destination["uuid"],
            destination["default_topic_uuid"],
            [api.user_uuid],
            "user-topic",
        )
    assert db.execute(
        "SELECT last_message_uuid FROM messenger_user_topic_bindings "
        "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (api.project_id, api.user_uuid, destination["default_topic_uuid"]),
    ).fetchone() == (first_uuid,)


@pytest.mark.isolated_schema
@pytest.mark.parametrize("date_first", [True, False])
def test_personal_preview_scope_preserves_concurrent_date_and_topic_changes(
    api, db, date_first, _test_database_url
):
    source, message = _create_message(api, "preview-concurrent-source")
    destination, _ = _create_message(api, "preview-concurrent-destination")
    placement_uuid = sys_uuid.UUID(message["uuid"])
    canonical_uuid = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
        (api.project_id, placement_uuid),
    ).fetchone()[0]
    operations = [
        (
            "UPDATE messenger_messages SET created_at='2021-01-01' WHERE project_id=%s AND uuid=%s",
            (api.project_id, canonical_uuid),
        ),
        (
            "UPDATE messenger_message_placements SET stream_uuid=%s,topic_uuid=%s WHERE project_id=%s AND uuid=%s",
            (
                destination["uuid"],
                destination["default_topic_uuid"],
                api.project_id,
                placement_uuid,
            ),
        ),
    ]
    if not date_first:
        operations.reverse()
    with (
        psycopg.connect(_test_database_url) as first,
        psycopg.connect(_test_database_url) as second,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool,
    ):
        first.execute(*operations[0])
        future = pool.submit(second.execute, *operations[1])
        deadline = time.monotonic() + 5
        blocked = False
        try:
            while time.monotonic() < deadline and not future.done():
                blocked = bool(
                    db.execute(
                        "SELECT %s=ANY(pg_blocking_pids(%s))",
                        (first.info.backend_pid, second.info.backend_pid),
                    ).fetchone()[0]
                )
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked
        finally:
            first.commit()
        future.result(timeout=5)
        second.commit()
    assert db.execute(
        "SELECT preview_stream_uuid,preview_topic_uuid,preview_created_at::date::text "
        "FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s",
        (api.project_id, placement_uuid),
    ).fetchone() == (
        sys_uuid.UUID(destination["uuid"]),
        sys_uuid.UUID(destination["default_topic_uuid"]),
        "2021-01-01",
    )


@pytest.mark.isolated_schema
def test_personal_preview_scope_handles_deferred_parent_inserts(
    api, db, _test_database_url
):
    stream, message = _create_message(api, "preview-deferred-parents")
    placement_uuid, canonical_uuid = sys_uuid.uuid4(), sys_uuid.uuid4()
    content_uuid = db.execute(
        "SELECT content_uuid FROM messenger_messages WHERE project_id=%s LIMIT 1",
        (api.project_id,),
    ).fetchone()[0]
    with psycopg.connect(_test_database_url) as connection:
        connection.execute("SET CONSTRAINTS ALL DEFERRED")
        connection.execute(
            "INSERT INTO messenger_user_message_states "
            "(uuid,project_id,placement_uuid,user_uuid,membership_generation) "
            "VALUES (gen_random_uuid(),%s,%s,%s,1)",
            (api.project_id, placement_uuid, api.user_uuid),
        )
        connection.execute(
            "INSERT INTO messenger_message_placements "
            "(uuid,project_id,message_uuid,stream_uuid,topic_uuid) VALUES (%s,%s,%s,%s,%s)",
            (
                placement_uuid,
                api.project_id,
                canonical_uuid,
                stream["uuid"],
                stream["default_topic_uuid"],
            ),
        )
        connection.execute(
            "INSERT INTO messenger_messages (uuid,project_id,author_uuid,content_uuid,created_at) "
            "VALUES (%s,%s,%s,%s,'2001-01-01')",
            (canonical_uuid, api.project_id, api.user_uuid, content_uuid),
        )
    assert db.execute(
        "SELECT preview_stream_uuid,preview_topic_uuid,preview_created_at::date::text "
        "FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s",
        (api.project_id, placement_uuid),
    ).fetchone() == (
        sys_uuid.UUID(stream["uuid"]),
        sys_uuid.UUID(stream["default_topic_uuid"]),
        "2001-01-01",
    )


@pytest.mark.parametrize("limit", [0, 2, 5])
def test_native_reaction_snapshot_switches_complete_lists_at_configured_limit(
    api, db, limit
):
    response = api.post(
        STREAMS,
        json={
            "name": "bounded-native-reactions",
            "description": "",
            "source_name": "native",
        },
    )
    assert response.status_code == 201, response.text
    stream = response.json()
    users = [str(api.user_uuid)] + [str(sys_uuid.uuid4()) for _index in range(5)]
    for user_uuid in users[1:]:
        conftest.seed_user_stream_binding(db, api.project_id, stream["uuid"], user_uuid)
    _drain()
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "bounded reactions"},
        },
    )
    assert response.status_code == 201, response.text
    message = response.json()
    _drain()
    cfg.CONF.set_override(
        "user_list_limit", limit, group=messenger_reaction_opts.DOMAIN
    )
    try:
        reactions = []
        response = api.post(
            MESSAGE_REACTIONS,
            json={"message_uuid": message["uuid"], "emoji_name": "eyes"},
        )
        assert response.status_code == 201, response.text
        _drain()

        def check_snapshot(count):
            expected_users = {"eyes": users[:1]} if limit > 0 else {}
            if 0 < count <= limit:
                expected_users["heart"] = users[:count]
            expected_counts = {"eyes": 1}
            if count:
                expected_counts["heart"] = count
            response = api.get(f"{MESSAGES}{message['uuid']}")
            assert response.status_code == 200, response.text
            assert response.json()["reactions"] == expected_counts
            assert response.json()["reaction_users"] == expected_users
            page = api.get(MESSAGES, params={"stream_uuid": stream["uuid"]})
            assert page.status_code == 200, page.text
            row = next(row for row in page.json() if row["uuid"] == message["uuid"])
            assert row["reaction_users"] == expected_users
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT message.reactions, message.reaction_users
                    FROM messenger_messages AS message
                    JOIN messenger_message_placements AS placement
                      ON placement.project_id = message.project_id
                     AND placement.message_uuid = message.uuid
                    WHERE placement.project_id = %s AND placement.uuid = %s
                    """,
                    (api.project_id, message["uuid"]),
                )
                assert cursor.fetchone() == (expected_counts, expected_users)
                cursor.execute(
                    """
                    SELECT payload
                    FROM m_workspace_visible_events
                    WHERE project_id = %s AND user_uuid = %s
                      AND object_type = 'message' AND action = 'updated'
                      AND payload->>'uuid' = %s
                    ORDER BY epoch_version DESC LIMIT 1
                    """,
                    (api.project_id, api.user_uuid, message["uuid"]),
                )
                payload = cursor.fetchone()[0]
                assert payload["reactions"] == expected_counts
                assert payload["reaction_users"] == expected_users

        for count, user_uuid in enumerate(users, 1):
            response = api.post(
                MESSAGE_REACTIONS,
                user=user_uuid,
                json={"message_uuid": message["uuid"], "emoji_name": "heart"},
            )
            assert response.status_code == 201, response.text
            reactions.append(response.json()["uuid"])
            _drain()
            check_snapshot(count)
        for index in range(len(users) - 1, -1, -1):
            response = api.delete(
                f"{MESSAGE_REACTIONS}{reactions[index]}", user=users[index]
            )
            assert response.status_code == 204, response.text
            _drain()
            check_snapshot(index)
    finally:
        cfg.CONF.clear_override("user_list_limit", group=messenger_reaction_opts.DOMAIN)


def test_migration_rebuilds_existing_reactions_without_a_new_reaction(api, db):
    _stream, message = _create_message(api, "existing-reaction-users")
    _stream, empty_message = _create_message(api, "existing-no-reactions")
    users = [str(api.user_uuid)] + [str(sys_uuid.uuid4()) for _index in range(5)]
    for user_uuid in users[1:]:
        conftest.seed_workspace_user(db, user_uuid, f"reactor-{user_uuid}")
        db.execute(
            "SELECT messenger_v2_register_project_user(%s, %s)",
            (api.project_id, user_uuid),
        )
    canonical_uuid = db.execute(
        "SELECT message_uuid FROM messenger_message_placements "
        "WHERE project_id=%s AND uuid=%s",
        (api.project_id, message["uuid"]),
    ).fetchone()[0]
    with db.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO messenger_message_reaction_facts
                (uuid, project_id, canonical_message_uuid, placement_uuid,
                 user_uuid, emoji_name)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    sys_uuid.uuid4(),
                    api.project_id,
                    canonical_uuid,
                    message["uuid"],
                    user_uuid,
                    emoji_name,
                )
                for emoji_name, reactors in (("heart", users), ("eyes", users[:2]))
                for user_uuid in reactors
            ],
        )
        cursor.execute(
            """
            UPDATE messenger_messages
            SET reactions = '{"heart":6,"eyes":2,"ghost":3}',
                reaction_users = %s::jsonb,
                created_at = NOW() - INTERVAL '30 days'
            WHERE project_id = %s AND uuid = %s
            """,
            (
                json.dumps({"heart": users, "ghost": users[:3]}),
                api.project_id,
                canonical_uuid,
            ),
        )
    path = next(
        conftest.MIGRATIONS_DIR.glob(
            "*-Rebuild-bounded-reaction-users-for-existing-messages-*.py"
        )
    )
    spec = importlib.util.spec_from_file_location("rebuild_reaction_users", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with contexts.Context().session_manager() as session:
        migration.migration_step.upgrade(session)
        migration.migration_step.upgrade(session)
    tasks = db.execute(
        """
        SELECT payload->>'placement_uuid'
        FROM messenger_domain_outbox_events
        WHERE project_id = %s AND payload->>'source_kind' = 'reaction_users.rebuild'
        """,
        (api.project_id,),
    ).fetchall()
    assert tasks == [(message["uuid"],)]
    _drain()
    response = api.get(f"{MESSAGES}{message['uuid']}")
    assert response.status_code == 200, response.text
    assert response.json()["reactions"] == {"heart": 6, "eyes": 2}
    assert sorted(response.json()["reaction_users"]["eyes"]) == sorted(users[:2])
    assert set(response.json()["reaction_users"]) == {"eyes"}
    page = api.get(MESSAGES, params={"stream_uuid": message["stream_uuid"]})
    assert page.status_code == 200, page.text
    assert (
        next(row for row in page.json() if row["uuid"] == message["uuid"])[
            "reaction_users"
        ]
        == response.json()["reaction_users"]
    )
    assert db.execute(
        "SELECT count(*) FROM messenger_message_reaction_facts "
        "WHERE project_id=%s AND canonical_message_uuid=%s",
        (api.project_id, canonical_uuid),
    ).fetchone() == (8,)
    assert api.get(f"{MESSAGES}{empty_message['uuid']}").json()["reaction_users"] == {}


def test_reaction_snapshot_uses_message_scoped_covering_index(api, db):
    _stream, target = _create_message(api, "reaction-index-target")
    _stream, distractor = _create_message(api, "reaction-index-distractor")
    target_reaction_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT placement.message_uuid, placement.uuid
            FROM messenger_message_placements AS placement
            WHERE placement.project_id = %s AND placement.uuid = ANY(%s::uuid[])
            ORDER BY placement.uuid
            """,
            (api.project_id, [target["uuid"], distractor["uuid"]]),
        )
        placements = {
            str(placement_uuid): canonical_uuid
            for canonical_uuid, placement_uuid in cursor.fetchall()
        }
        target_canonical = placements[target["uuid"]]
        distractor_canonical = placements[distractor["uuid"]]
        cursor.execute(
            """
            INSERT INTO messenger_message_reaction_facts (
                uuid, project_id, canonical_message_uuid, placement_uuid,
                user_uuid, emoji_name
            ) VALUES (%s, %s, %s, %s, %s, 'target')
            """,
            (
                target_reaction_uuid,
                api.project_id,
                target_canonical,
                target["uuid"],
                api.user_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO messenger_message_reaction_facts (
                uuid, project_id, canonical_message_uuid, placement_uuid,
                user_uuid, emoji_name
            )
            SELECT gen_random_uuid(), %s, %s, %s, %s,
                   'noise-' || input.number::text
            FROM generate_series(1, 20000) AS input(number)
            """,
            (
                api.project_id,
                distractor_canonical,
                distractor["uuid"],
                api.user_uuid,
            ),
        )
        cursor.execute("ANALYZE messenger_message_reaction_facts")
        cursor.execute(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT canonical_message_uuid, emoji_name,
                   count(*) AS reaction_count,
                   jsonb_agg(user_uuid::text ORDER BY created_at, uuid) AS users
            FROM messenger_message_reaction_facts
            WHERE project_id = %s AND canonical_message_uuid = %s
            GROUP BY canonical_message_uuid, emoji_name
            """,
            (api.project_id, target_canonical),
        )
        scoped_plan = cursor.fetchone()[0][0]
        cursor.execute(
            """
            EXPLAIN (FORMAT JSON)
            SELECT canonical_message_uuid, emoji_name,
                   count(*) AS reaction_count,
                   jsonb_agg(user_uuid::text ORDER BY created_at, uuid) AS users
            FROM messenger_message_reaction_facts
            WHERE project_id = %s
            GROUP BY canonical_message_uuid, emoji_name
            """,
            (api.project_id,),
        )
        project_plan = cursor.fetchone()[0][0]

    nodes = list(_plan_nodes(scoped_plan["Plan"]))
    assert any(
        node.get("Index Name") == "messenger_message_reaction_facts_snapshot_idx"
        for node in nodes
    ), json.dumps(scoped_plan)
    assert not any(node["Node Type"] == "Seq Scan" for node in nodes)
    assert scoped_plan["Plan"]["Total Cost"] * 20 < project_plan["Plan"]["Total Cost"]


@pytest.mark.parametrize("delete_reaction", [False, True])
def test_reaction_burst_coalesces_and_preserves_realtime_events(
    api, db, monkeypatch, delete_reaction
):
    _stream, message = _create_message(api, "reaction-coalescing")
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(max(epoch_version), 0) FROM m_workspace_visible_events"
        )
        baseline_epoch = cursor.fetchone()[0]

    created = api.post(
        MESSAGE_REACTIONS,
        json={"message_uuid": message["uuid"], "emoji_name": "eyes"},
    )
    assert created.status_code == 201, created.text
    reaction_uuid = created.json()["uuid"]
    updated = api.put(
        f"{MESSAGE_REACTIONS}{reaction_uuid}",
        json={"emoji_name": "heart"},
    )
    assert updated.status_code == 405, updated.text
    removed = api.delete(f"{MESSAGE_REACTIONS}{reaction_uuid}")
    assert removed.status_code == 204, removed.text
    added = api.post(
        MESSAGE_REACTIONS, json={"message_uuid": message["uuid"], "emoji_name": "heart"}
    )
    assert added.status_code == 201, added.text
    heart_reaction_uuid = added.json()["uuid"]
    if delete_reaction:
        deleted = api.delete(f"{MESSAGE_REACTIONS}{heart_reaction_uuid}")
        assert deleted.status_code in (200, 204), deleted.text

    with contexts.Context().session_manager() as session:
        processed = v2_projection.process_one_projection_task(
            session,
            "integration:reaction-coalescing",
        )
        assert processed is True

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE attempts > 0),
                   jsonb_agg(execution_stats ORDER BY created_at, uuid)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND payload->>'reaction_uuid' IN (%s,%s)
              AND status = 'completed'
            """,
            (api.project_id, reaction_uuid, heart_reaction_uuid),
        )
        completed, actually_claimed, execution_stats = cursor.fetchone()
        cursor.execute(
            """
            SELECT object_type, action, payload
            FROM m_workspace_visible_events
            WHERE project_id = %s AND epoch_version > %s
              AND object_type IN ('message', 'message_reaction')
            ORDER BY epoch_version
            """,
            (api.project_id, baseline_epoch),
        )
        events = cursor.fetchall()

    expected_events = 4 if delete_reaction else 3
    assert completed == expected_events
    assert actually_claimed == 1
    claimed_stats = [stats for stats in execution_stats if stats.get("claim_count")]
    coalesced_stats = [
        stats for stats in execution_stats if stats.get("last_outcome") == "coalesced"
    ]
    assert len(claimed_stats) == 1
    assert len(coalesced_stats) == expected_events - 1
    assert claimed_stats[0]["last_outcome"] == "completed"
    assert claimed_stats[0]["coalesced_task_count"] == expected_events - 1
    assert claimed_stats[0]["reaction_fact_count"] == int(not delete_reaction)
    assert claimed_stats[0]["reaction_event_count"] == expected_events
    assert claimed_stats[0]["queue_wait_ms"] >= 0
    assert claimed_stats[0]["outbox_wait_ms"] >= claimed_stats[0]["queue_wait_ms"]
    assert claimed_stats[0]["derivation_delay_ms"] >= 0
    assert claimed_stats[0]["claim_duration_ms"] >= 0
    assert claimed_stats[0]["processing_duration_ms"] >= 0
    assert claimed_stats[0]["outbox_to_finish_ms"] >= 0
    reaction_actions = [
        action for kind, action, _payload in events if kind == "message_reaction"
    ]
    assert reaction_actions == ["created", "deleted", "created"] + (
        ["deleted"] if delete_reaction else []
    )
    message_events = [payload for kind, _action, payload in events if kind == "message"]
    # A net-empty burst leaves the initial empty snapshot unchanged. All
    # individual reaction events still have to reach the realtime audience.
    assert len(message_events) == int(not delete_reaction)
    expected_reactions = {} if delete_reaction else {"heart": 1}
    if message_events:
        assert message_events[0]["reactions"] == expected_reactions
    assert (
        api.get(f"{MESSAGES}{message['uuid']}").json()["reactions"]
        == expected_reactions
    )


def test_reaction_coalescing_locks_a_bounded_sibling_batch(api, db, monkeypatch):
    _stream, message = _create_message(api, "bounded-reaction-coalescing")
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    task_count = v2_projection.REACTION_COALESCE_LIMIT + 5
    with contexts.Context().session_manager() as session:
        canonical_message_uuid = session.execute(
            """
            SELECT message_uuid
            FROM messenger_message_placements
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message["uuid"]),
        ).fetchone()["message_uuid"]
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            )
            SELECT gen_random_uuid(), %s, 'reaction_snapshot', 'message', %s,
                   jsonb_build_object(
                       'source_kind', 'message_reaction.updated',
                       'placement_uuid', %s::text,
                       'emit_reaction_event', FALSE,
                       'emit_message_updated', FALSE
                   )
            FROM generate_series(1, %s)
            """,
            (
                api.project_id,
                f"{api.project_id}:{canonical_message_uuid}",
                message["uuid"],
                task_count,
            ),
        )
        assert v2_projection.process_one_projection_task(
            session,
            "integration:bounded-reaction:first",
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE status = 'pending'),
                   count(*) FILTER (WHERE attempts > 0),
                   max((execution_stats->>'coalesced_task_count')::integer)
                       FILTER (WHERE attempts > 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND scope_key = %s
            """,
            (
                api.project_id,
                f"{api.project_id}:{canonical_message_uuid}",
            ),
        )
        completed, pending, claimed, first_coalesced = cursor.fetchone()

    assert completed == v2_projection.REACTION_COALESCE_LIMIT
    assert pending == task_count - v2_projection.REACTION_COALESCE_LIMIT
    assert claimed == 1
    assert first_coalesced == v2_projection.REACTION_COALESCE_LIMIT - 1

    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:bounded-reaction:second",
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE status = 'pending'),
                   count(*) FILTER (WHERE attempts > 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND scope_key = %s
            """,
            (
                api.project_id,
                f"{api.project_id}:{canonical_message_uuid}",
            ),
        )
        completed, pending, claimed = cursor.fetchone()

    assert completed == task_count
    assert pending == 0
    assert claimed == 2


def test_reaction_coalescing_honors_sibling_retry_deadlines(api, db, monkeypatch):
    _stream, message = _create_message(api, "reaction-retry-deadline")
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    future_placement_uuid = sys_uuid.uuid4()
    scope_key = None
    with contexts.Context().session_manager() as session:
        canonical_message_uuid = session.execute(
            """
            SELECT message_uuid
            FROM messenger_message_placements
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message["uuid"]),
        ).fetchone()["message_uuid"]
        scope_key = f"{api.project_id}:{canonical_message_uuid}"
        for placement_uuid in (future_placement_uuid, message["uuid"]):
            session.execute(
                """
                INSERT INTO messenger_domain_outbox_events (
                    uuid, project_id, event_kind, scope_kind, scope_key, payload
                ) VALUES (
                    gen_random_uuid(), %s, 'reaction_snapshot', 'message', %s,
                    jsonb_build_object(
                        'source_kind', 'message_reaction.updated',
                        'placement_uuid', %s::text,
                        'emit_reaction_event', FALSE,
                        'emit_message_updated', FALSE
                    )
                )
                """,
                (api.project_id, scope_key, placement_uuid),
            )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'failed', next_retry_at = NOW() + INTERVAL '1 hour',
                last_error = 'transient provider failure'
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND payload->>'placement_uuid' = %s
            """,
            (api.project_id, str(future_placement_uuid)),
        )
        assert v2_projection.process_one_projection_task(
            session,
            "integration:reaction-retry-deadline",
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT payload->>'placement_uuid', status, attempts,
                   COALESCE(next_retry_at > NOW(), FALSE),
                   execution_stats->>'coalesced_task_count'
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND scope_key = %s
            ORDER BY payload->>'placement_uuid'
            """,
            (api.project_id, scope_key),
        )
        rows = {
            placement_uuid: (status, attempts, retry_in_future, coalesced_count)
            for (
                placement_uuid,
                status,
                attempts,
                retry_in_future,
                coalesced_count,
            ) in cursor.fetchall()
        }

    assert rows[str(future_placement_uuid)] == ("failed", 0, True, None)
    assert rows[message["uuid"]] == ("completed", 1, False, "0")


def test_concurrent_reaction_workers_keep_one_exact_snapshot(api, db, monkeypatch):
    _stream, message = _create_message(api, "reaction-concurrency")
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    reactions = []
    for emoji_name in ("eyes", "heart", "rocket", "check"):
        response = api.post(
            MESSAGE_REACTIONS,
            json={"message_uuid": message["uuid"], "emoji_name": emoji_name},
        )
        assert response.status_code == 201, response.text
        reactions.append(response.json())
    ready = threading.Barrier(2)

    def process(worker_id):
        with contexts.Context().session_manager() as session:
            ready.wait(timeout=5)
            return v2_projection.process_one_projection_task(session, worker_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(process, ("integration:reaction-a", "integration:reaction-b"))
        )

    assert any(results)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE attempts > 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND payload->>'placement_uuid' = %s
              AND status = 'completed'
            """,
            (api.project_id, message["uuid"]),
        )
        completed, actually_claimed = cursor.fetchone()
    assert completed == len(reactions)
    assert actually_claimed <= 2
    snapshot = api.get(f"{MESSAGES}{message['uuid']}").json()
    assert snapshot["reactions"] == {
        "check_mark_button": 1,
        "eyes": 1,
        "heart": 1,
        "rocket": 1,
    }


def test_coalesced_reaction_events_resolve_each_placement(api, db, monkeypatch):
    primary_stream, message = _create_message(api, "reaction-primary-placement")
    secondary_stream = api.post(
        STREAMS,
        json={
            "name": "reaction-secondary-placement",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    secondary_placement_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT message_uuid
            FROM messenger_message_placements
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message["uuid"]),
        )
        canonical_message_uuid = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO messenger_message_placements (
                uuid, project_id, message_uuid, stream_uuid, topic_uuid
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                secondary_placement_uuid,
                api.project_id,
                canonical_message_uuid,
                secondary_stream["uuid"],
                secondary_stream["default_topic_uuid"],
            ),
        )
    db.commit()

    emitted = []
    guarded_streams = []

    def capture_created(reaction, event_message, **_kwargs):
        emitted.append(
            (
                reaction.uuid,
                event_message.uuid,
                event_message.stream_uuid,
            )
        )
        return []

    monkeypatch.setattr(
        v2_projection.messenger_events,
        "create_message_reaction_created_event",
        capture_created,
    )
    monkeypatch.setattr(
        v2_projection,
        "_guard_emitted_events",
        lambda *_args, **kwargs: guarded_streams.append(kwargs["stream_uuid"]),
    )
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    placements = [
        (sys_uuid.UUID(message["uuid"]), sys_uuid.UUID(primary_stream["uuid"])),
        (secondary_placement_uuid, sys_uuid.UUID(secondary_stream["uuid"])),
    ]
    with contexts.Context().session_manager() as session:
        for index, (placement_uuid, _stream_uuid) in enumerate(placements):
            reaction_uuid = sys_uuid.uuid4()
            session.execute(
                """
                INSERT INTO messenger_domain_outbox_events (
                    uuid, project_id, event_kind, scope_kind, scope_key, payload
                ) VALUES (
                    gen_random_uuid(), %s, 'reaction_snapshot', 'message', %s,
                    %s::jsonb
                )
                """,
                (
                    api.project_id,
                    f"{api.project_id}:{canonical_message_uuid}",
                    json.dumps(
                        {
                            "source_kind": "message_reaction.created",
                            "reaction_uuid": str(reaction_uuid),
                            "placement_uuid": str(placement_uuid),
                            "reaction": {
                                "uuid": str(reaction_uuid),
                                "project_id": api.project_id,
                                "message_uuid": str(placement_uuid),
                                "user_uuid": api.user_uuid,
                                "emoji_name": f"probe-{index}",
                            },
                            "emit_reaction_event": True,
                            "emit_message_updated": False,
                        }
                    ),
                ),
            )
        assert v2_projection.process_one_projection_task(
            session,
            "integration:reaction-multi-placement",
        )

    assert {
        (placement_uuid, stream_uuid) for _, placement_uuid, stream_uuid in emitted
    } == {(placement_uuid, stream_uuid) for placement_uuid, stream_uuid in placements}
    assert set(guarded_streams) == {stream_uuid for _, stream_uuid in placements}


def test_replacing_a_reaction_on_another_message_rebuilds_both_snapshots(
    api, db, monkeypatch
):
    stream, source = _create_message(api, "reaction-move-source")
    target_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "reaction-move-target"},
        },
    )
    assert target_response.status_code == 201, target_response.text
    target = target_response.json()
    _drain()
    created = api.post(
        MESSAGE_REACTIONS,
        json={"message_uuid": source["uuid"], "emoji_name": "eyes"},
    )
    assert created.status_code == 201, created.text
    reaction_uuid = created.json()["uuid"]
    _drain()
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )

    moved = api.put(
        f"{MESSAGE_REACTIONS}{reaction_uuid}",
        json={"message_uuid": target["uuid"], "emoji_name": "heart"},
    )
    assert moved.status_code == 405, moved.text
    removed = api.delete(f"{MESSAGE_REACTIONS}{reaction_uuid}")
    assert removed.status_code == 204, removed.text
    added = api.post(
        MESSAGE_REACTIONS, json={"message_uuid": target["uuid"], "emoji_name": "heart"}
    )
    assert added.status_code == 201, added.text
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session, "integration:reaction-move:new"
        )
        assert v2_projection.process_one_projection_task(
            session, "integration:reaction-move:old"
        )

    assert api.get(f"{MESSAGES}{source['uuid']}").json()["reactions"] == {}
    assert api.get(f"{MESSAGES}{target['uuid']}").json()["reactions"] == {"heart": 1}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT payload->>'source_kind', status
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND payload->>'reaction_uuid' IN (%s,%s)
              AND payload->>'source_kind' IN ('message_reaction.created','message_reaction.deleted')
            ORDER BY created_at, uuid
            """,
            (api.project_id, reaction_uuid, added.json()["uuid"]),
        )
        assert set(cursor.fetchall()) == {
            ("message_reaction.created", "completed"),
            ("message_reaction.deleted", "completed"),
        }


def test_rolled_back_claim_is_immediately_reusable(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    event_uuid = sys_uuid.uuid4()
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                %s, %s, 'topic_state_projection', 'topic', %s,
                '{"source_kind":"topic.updated"}'::jsonb
            )
            """,
            (event_uuid, api.project_id, f"{api.project_id}:shutdown-reclaim"),
        )
    first_task_uuid = None
    try:
        with contexts.Context().session_manager() as session:
            claimed = v2_projection._claim_task(
                session, "integration:stopping-worker", 30
            )
            assert claimed is not None
            first_task_uuid = claimed["uuid"]
            raise RuntimeError("simulate graceful worker transaction rollback")
    except RuntimeError:
        pass

    with contexts.Context().session_manager() as session:
        reclaimed = v2_projection._claim_task(
            session, "integration:replacement-worker", 30
        )
        assert reclaimed is not None
        assert reclaimed["uuid"] == first_task_uuid
        assert reclaimed["attempts"] == 1


def test_projection_claims_different_users_in_one_project_concurrently(
    api,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    user_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "topic.read",
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            }
            for user_uuid in user_uuids
        ],
    )
    claimed = threading.Barrier(3)
    release = threading.Event()

    def hold_claim(worker_id):
        with contexts.Context().session_manager() as session:
            task = v2_projection._claim_task(session, worker_id, 30)
            claimed.wait(timeout=5)
            assert release.wait(timeout=5)
            session.rollback()
            return task

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(hold_claim, f"integration:user-partition:{index}")
            for index in range(2)
        ]
        claimed.wait(timeout=5)
        release.set()
        tasks = [future.result(timeout=5) for future in futures]

    assert all(task is not None for task in tasks)
    assert {task["partition_kind"] for task in tasks} == {"user"}
    assert {task["partition_key"] for task in tasks} == {
        str(user_uuid) for user_uuid in user_uuids
    }


def test_projection_claim_serializes_different_scopes_for_one_user(
    api,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    user_uuid = sys_uuid.uuid4()
    _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": scope_kind,
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": source_kind,
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            }
            for scope_kind, source_kind in (
                ("user-stream", "stream.read"),
                ("user-topic", "topic.read"),
            )
        ],
    )
    ready = threading.Event()
    release = threading.Event()

    def hold_first_claim():
        with contexts.Context().session_manager() as session:
            task = v2_projection._claim_task(session, "integration:same-user:1", 30)
            ready.set()
            assert release.wait(timeout=5)
            session.rollback()
            return task

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold_first_claim)
        assert ready.wait(timeout=5)
        with contexts.Context().session_manager() as session:
            competing = v2_projection._claim_task(
                session,
                "integration:same-user:2",
                30,
            )
        release.set()
        first = future.result(timeout=5)

    assert first is not None
    assert first["partition_kind"] == "user"
    assert competing is None


def test_user_partition_and_scope_fifo_use_the_same_ordering_key(
    api,
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    user_uuid = sys_uuid.uuid4()
    scope_key = f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}"
    event_uuids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": scope_key,
                "payload": {
                    "source_kind": "topic.read",
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            }
            for _ in range(2)
        ],
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = CASE outbox_event_uuid
                    WHEN %s THEN NOW() + INTERVAL '2 seconds'
                    ELSE NOW() + INTERVAL '1 second'
                END,
                ordering_created_at = CASE outbox_event_uuid
                    WHEN %s THEN NOW() - INTERVAL '2 seconds'
                    ELSE NOW() - INTERVAL '1 second'
                END
            WHERE project_id = %s AND outbox_event_uuid IN (%s, %s)
            """,
            (
                event_uuids[0],
                event_uuids[0],
                api.project_id,
                event_uuids[0],
                event_uuids[1],
            ),
        )
    db.commit()

    with contexts.Context().session_manager() as session:
        task = v2_projection._claim_task(session, "integration:fifo-key", 30)
        assert task is not None
        assert task["outbox_event_uuid"] == event_uuids[1]
        session.rollback()


def test_user_projection_partition_blocks_project_global_task(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    user_uuid = sys_uuid.uuid4()
    event_uuids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "folder_projection",
                "scope_kind": "user-folder",
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "folder.updated",
                    "user_uuid": str(user_uuid),
                    "folder_uuid": str(sys_uuid.uuid4()),
                },
            },
            {
                "task_kind": "folder_projection",
                "scope_kind": "stream-folders",
                "scope_key": f"{api.project_id}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "stream.updated",
                    "stream_uuid": str(sys_uuid.uuid4()),
                },
            },
        ],
    )
    ready = threading.Event()
    release = threading.Event()
    global_started = threading.Event()

    def hold_user_claim():
        with contexts.Context().session_manager() as session:
            task = v2_projection._claim_task(session, "integration:user-gate", 30)
            ready.set()
            assert release.wait(timeout=5)
            session.execute(
                """
                UPDATE messenger_projection_tasks
                SET status = 'completed', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (task["project_id"], task["uuid"]),
            )
            session.execute(
                """
                UPDATE messenger_projection_scope_leases
                SET owner = NULL, lease_expires_at = NOW(), updated_at = NOW()
                WHERE project_id = %s AND scope_kind = %s AND scope_key = %s
                """,
                (task["project_id"], task["scope_kind"], task["scope_key"]),
            )
            return task

    def wait_for_global_claim():
        with contexts.Context().session_manager() as session:
            global_started.set()
            task = v2_projection._claim_task(
                session,
                "integration:global-gate",
                30,
            )
            session.rollback()
            return task

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        user_future = executor.submit(hold_user_claim)
        assert ready.wait(timeout=5)
        global_future = executor.submit(wait_for_global_claim)
        assert global_started.wait(timeout=5)
        assert not global_future.done()
        release.set()
        user_task = user_future.result(timeout=5)
        global_task = global_future.result(timeout=5)

    assert user_task is not None
    assert user_task["outbox_event_uuid"] == event_uuids[0]
    assert user_task["partition_kind"] == "user"
    assert global_task is not None
    assert global_task["outbox_event_uuid"] == event_uuids[1]
    assert global_task["partition_kind"] == "project"


def test_waiting_global_task_stops_younger_user_admission(api, db, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    user_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    event_uuids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "folder_projection",
                "scope_kind": "user-folder",
                "scope_key": f"{api.project_id}:{user_uuids[0]}:older-user",
                "payload": {
                    "source_kind": "folder.updated",
                    "user_uuid": str(user_uuids[0]),
                    "folder_uuid": str(sys_uuid.uuid4()),
                },
            },
            {
                "task_kind": "folder_projection",
                "scope_kind": "stream-folders",
                "scope_key": f"{api.project_id}:waiting-global",
                "payload": {
                    "source_kind": "stream.updated",
                    "stream_uuid": str(sys_uuid.uuid4()),
                },
            },
            {
                "task_kind": "folder_projection",
                "scope_kind": "user-folder",
                "scope_key": f"{api.project_id}:{user_uuids[1]}:younger-user",
                "payload": {
                    "source_kind": "folder.updated",
                    "user_uuid": str(user_uuids[1]),
                    "folder_uuid": str(sys_uuid.uuid4()),
                },
            },
        ],
    )
    other_project_id = sys_uuid.uuid4()
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), %s, 'folder_projection', 'stream-folders', %s,
                jsonb_build_object(
                    'source_kind', 'stream.updated',
                    'stream_uuid', gen_random_uuid()::text
                ),
                NOW() + interval '1 minute', NOW() + interval '1 minute'
            )
            """,
            (other_project_id, f"{other_project_id}:available-later"),
        )
    user_ready = threading.Event()
    release_user = threading.Event()
    global_started = threading.Event()
    global_pid = []

    def hold_older_user_claim():
        with contexts.Context().session_manager() as session:
            task = v2_projection._claim_task(
                session,
                "integration:global-admission:user",
                30,
            )
            assert task is not None
            user_ready.set()
            assert release_user.wait(timeout=10)
            session.execute(
                """
                UPDATE messenger_projection_tasks
                SET status = 'completed', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (task["project_id"], task["uuid"]),
            )
            session.execute(
                """
                UPDATE messenger_projection_scope_leases
                SET owner = NULL, lease_expires_at = NOW(), updated_at = NOW()
                WHERE project_id = %s AND scope_kind = %s AND scope_key = %s
                """,
                (task["project_id"], task["scope_kind"], task["scope_key"]),
            )
            return task

    def wait_for_global_claim():
        with contexts.Context().session_manager() as session:
            global_pid.append(
                session.execute("SELECT pg_backend_pid() AS pid", ()).fetchone()["pid"]
            )
            global_started.set()
            task = v2_projection._claim_task(
                session,
                "integration:global-admission:global",
                30,
            )
            return task

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        user_future = executor.submit(hold_older_user_claim)
        assert user_ready.wait(timeout=5)
        global_future = executor.submit(wait_for_global_claim)
        assert global_started.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_locks
                        WHERE pid = %s AND locktype = 'advisory'
                          AND NOT granted
                    )
                    """,
                    (global_pid[0],),
                )
                if cursor.fetchone()[0]:
                    break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        timed_out_global = global_future.result(timeout=3)
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT owner, lease_expires_at > NOW()
                FROM messenger_projection_scope_leases
                WHERE project_id = %s
                  AND scope_kind = %s
                  AND scope_key = %s
                """,
                (
                    api.project_id,
                    v2_projection.PROJECTION_PROJECT_ADMISSION_SCOPE_KIND,
                    str(api.project_id),
                ),
            )
            admission_marker = cursor.fetchone()
        with contexts.Context().session_manager() as session:
            started_at = time.monotonic()
            competing = v2_projection._claim_task(
                session,
                "integration:global-admission:competing",
                30,
            )
            competing_elapsed = time.monotonic() - started_at
        release_user.set()
        user_task = user_future.result(timeout=5)

    assert user_task["outbox_event_uuid"] == event_uuids[0]
    assert timed_out_global is None
    assert admission_marker is not None
    assert admission_marker[1] is True
    assert competing is None
    assert competing_elapsed < 2
    with contexts.Context().session_manager() as session:
        global_task = v2_projection._claim_task(
            session,
            "integration:global-admission:after-drain",
            30,
        )
        assert global_task is not None
        assert global_task["outbox_event_uuid"] == event_uuids[1]
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'completed', lease_owner = NULL,
                lease_expires_at = NULL, updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (global_task["project_id"], global_task["uuid"]),
        )
        session.execute(
            """
            UPDATE messenger_projection_scope_leases
            SET owner = NULL, lease_expires_at = NOW(), updated_at = NOW()
            WHERE project_id = %s AND scope_kind = %s AND scope_key = %s
            """,
            (
                global_task["project_id"],
                global_task["scope_kind"],
                global_task["scope_key"],
            ),
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM messenger_projection_scope_leases
            WHERE project_id = %s
              AND scope_kind = %s
              AND scope_key = %s
            """,
            (
                api.project_id,
                v2_projection.PROJECTION_PROJECT_ADMISSION_SCOPE_KIND,
                str(api.project_id),
            ),
        )
        assert cursor.fetchone()[0] == 0
    with contexts.Context().session_manager() as session:
        younger_user = v2_projection._claim_task(
            session,
            "integration:global-admission:younger-user",
            30,
        )
        assert younger_user is not None
        assert younger_user["outbox_event_uuid"] == event_uuids[2]
        session.rollback()


def test_project_admission_marker_preserves_oldest_global_task(api):
    _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "fanout",
                "scope_kind": "message",
                "scope_key": f"{api.project_id}:older-global",
                "payload": {"placement_uuid": str(sys_uuid.uuid4())},
            },
            {
                "task_kind": "folder_projection",
                "scope_kind": "stream-folders",
                "scope_key": f"{api.project_id}:newer-global",
                "payload": {
                    "source_kind": "stream.updated",
                    "stream_uuid": str(sys_uuid.uuid4()),
                },
            },
        ],
    )
    with contexts.Context().session_manager() as session:
        tasks = session.execute(
            """
            SELECT *
            FROM messenger_projection_tasks
            WHERE project_id = %s
            ORDER BY created_at, ordering_created_at, outbox_event_uuid
            """,
            (api.project_id,),
        ).fetchall()
        v2_projection._mark_projection_project_admission(session, tasks[0], 30)
        v2_projection._mark_projection_project_admission(session, tasks[1], 30)
        marker = session.execute(
            """
            SELECT owner
            FROM messenger_projection_scope_leases
            WHERE project_id = %s AND scope_kind = %s
            """,
            (
                api.project_id,
                v2_projection.PROJECTION_PROJECT_ADMISSION_SCOPE_KIND,
            ),
        ).fetchone()
        assert marker["owner"] == str(tasks[0]["uuid"])
        v2_projection._clear_projection_project_admission(session, tasks[1])
        marker = session.execute(
            """
            SELECT owner
            FROM messenger_projection_scope_leases
            WHERE project_id = %s AND scope_kind = %s
            """,
            (
                api.project_id,
                v2_projection.PROJECTION_PROJECT_ADMISSION_SCOPE_KIND,
            ),
        ).fetchone()
        assert marker["owner"] == str(tasks[0]["uuid"])

        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'completed'
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, tasks[0]["uuid"]),
        )
        v2_projection._mark_projection_project_admission(session, tasks[1], 30)
        marker = session.execute(
            """
            SELECT owner
            FROM messenger_projection_scope_leases
            WHERE project_id = %s AND scope_kind = %s
            """,
            (
                api.project_id,
                v2_projection.PROJECTION_PROJECT_ADMISSION_SCOPE_KIND,
            ),
        ).fetchone()
        assert marker["owner"] == str(tasks[1]["uuid"])
        v2_projection._clear_projection_project_admission(session, tasks[0])
        v2_projection._clear_projection_project_admission(session, tasks[1])
        marker = session.execute(
            """
            SELECT owner
            FROM messenger_projection_scope_leases
            WHERE project_id = %s AND scope_kind = %s
            """,
            (
                api.project_id,
                v2_projection.PROJECTION_PROJECT_ADMISSION_SCOPE_KIND,
            ),
        ).fetchone()
        assert marker is None


def test_global_projection_claim_keeps_project_event_ordering_lock(
    api,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "folder_projection",
                "scope_kind": "stream-folders",
                "scope_key": f"{api.project_id}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "stream.updated",
                    "stream_uuid": str(sys_uuid.uuid4()),
                },
            }
        ],
    )

    with contexts.Context().session_manager() as session:
        task = v2_projection._claim_task(session, "integration:global-event", 30)
        assert task is not None
        assert task["partition_kind"] == "project"
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as observer:
            with observer.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s::text, 0))",
                    (api.project_id,),
                )
                assert cursor.fetchone() == (False,)
        session.rollback()


def test_user_projection_requeues_without_attempt_on_event_lock_contention(
    api,
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    user_uuid = sys_uuid.uuid4()
    event_uuids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "topic.read",
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            },
            {
                "task_kind": "read_counters",
                "scope_kind": "user-stream",
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "stream.read",
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            },
        ],
    )
    event_uuid = event_uuids[0]
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'failed', attempts = 1, next_retry_at = NOW(),
                last_error = 'transient projection failure'
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
    db.commit()

    def publish_at_tail(session, task, _batch_size):
        session.execute("SELECT pg_sleep(0.2)", ())
        v2_projection._try_lock_project_event_tail(session, task["project_id"])
        return True

    monkeypatch.setattr(v2_projection, "_process_task", publish_at_tail)
    metrics = {}
    blocker = psycopg.connect(conftest.TEST_DB_URL)
    with blocker.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s::text, 0))",
            (api.project_id,),
        )
    try:
        with contexts.Context().session_manager() as session:
            assert not v2_projection.process_one_projection_task(
                session,
                "integration:event-contention",
                metrics=metrics,
            )
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXTRACT(EPOCH FROM (next_retry_at - NOW()))
                FROM messenger_projection_tasks
                WHERE project_id = %s AND outbox_event_uuid = %s
                """,
                (api.project_id, event_uuid),
            )
            first_retry_delay = float(cursor.fetchone()[0])
            assert first_retry_delay > 0.05
            cursor.execute(
                """
                UPDATE messenger_projection_tasks
                SET next_retry_at = NOW()
                WHERE project_id = %s AND outbox_event_uuid = %s
                """,
                (api.project_id, event_uuid),
            )
        db.commit()
        with contexts.Context().session_manager() as session:
            assert not v2_projection.process_one_projection_task(
                session,
                "integration:event-contention-repeat",
                metrics=metrics,
            )
    finally:
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s::text, 0))",
                (api.project_id,),
            )
        blocker.close()

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET next_retry_at = NOW() + INTERVAL '1 hour'
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
    db.commit()
    with contexts.Context().session_manager() as session:
        assert (
            v2_projection._claim_task(
                session,
                "integration:ordered-event-contention",
                30,
            )
            is None
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, last_error,
                   execution_stats->>'last_outcome',
                   execution_stats->>'partition_kind',
                   (execution_stats->>'event_lock_contention_streak')::integer,
                   EXTRACT(EPOCH FROM (next_retry_at - NOW()))
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
        row = cursor.fetchone()
        assert row[:6] == (
            "pending",
            1,
            "transient projection failure",
            "event_lock_contention",
            "user",
            2,
        )
        assert float(row[6]) > first_retry_delay + 0.05
    assert metrics["event_lock_contention"] == 2
    assert metrics["claimed_partition_user"] == 2
    assert metrics["claim_seconds_read_counters"] >= 0
    assert metrics["processing_seconds_read_counters"] >= 0
    assert "outbox_to_finish_seconds_read_counters" not in metrics

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET next_retry_at = NOW()
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
    db.commit()
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:event-contention-retry",
            metrics=metrics,
        )
    assert metrics["claimed_partition_user"] == 3
    assert metrics["outbox_to_finish_seconds_read_counters"] >= 0


def test_history_import_snapshot_requeues_on_event_lock_contention(
    api,
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("read_state"),
    )
    user_uuid = sys_uuid.uuid4()
    event_uuid = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "history.imported",
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            }
        ],
    )[0]
    blocker = psycopg.connect(conftest.TEST_DB_URL)
    with blocker.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s::text, 0))",
            (api.project_id,),
        )
    started_at = time.monotonic()
    try:
        with contexts.Context().session_manager() as session:
            assert not v2_projection.process_one_projection_task(
                session,
                "integration:history-event-contention",
            )
    finally:
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s::text, 0))",
                (api.project_id,),
            )
        blocker.close()
    elapsed = time.monotonic() - started_at

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, execution_stats->>'last_outcome'
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
        assert cursor.fetchone() == ("pending", 0, "event_lock_contention")
    assert elapsed < 1


def test_broadcast_guard_does_not_deadlock_with_existing_project_user_update(api):
    stream, message = _create_message(api, "broadcast-project-user-lock-order")
    audience_uuid = sys_uuid.uuid4()
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO m_workspace_event_audience_snapshots_v1 (
                uuid, project_id, membership_digest
            ) VALUES (%s, %s, %s)
            """,
            (audience_uuid, api.project_id, f"lock-order:{audience_uuid}"),
        )
        session.execute(
            """
            INSERT INTO m_workspace_event_audience_members_v1 (
                audience_snapshot_uuid, user_uuid
            ) VALUES (%s, %s)
            """,
            (audience_uuid, api.user_uuid),
        )

    ready = threading.Barrier(2)

    def emit_while_holding_project_lock():
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL lock_timeout = '5s'", ())
            session.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (api.project_id,),
            )
            ready.wait(timeout=5)
            session.execute(
                """
                INSERT INTO m_workspace_broadcast_message_events_v1 (
                    uuid, project_id, entity_uuid, audience_snapshot_uuid,
                    object_type, action, payload
                ) VALUES (
                    gen_random_uuid(), %s, %s, %s, 'message', 'created',
                    jsonb_build_object(
                        'uuid', %s::text,
                        'stream_uuid', %s::text,
                        'topic_uuid', %s::text,
                        'source_name', 'native'
                    )
                )
                """,
                (
                    api.project_id,
                    message["uuid"],
                    audience_uuid,
                    message["uuid"],
                    stream["uuid"],
                    stream["default_topic_uuid"],
                ),
            )
        return "event"

    def update_user_then_take_project_lock():
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL lock_timeout = '5s'", ())
            session.execute(
                """
                UPDATE messenger_project_users
                SET updated_at = NOW()
                WHERE project_id = %s AND user_uuid = %s
                """,
                (api.project_id, api.user_uuid),
            )
            ready.wait(timeout=5)
            session.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (api.project_id,),
            )
        return "user"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(emit_while_holding_project_lock),
            executor.submit(update_user_then_take_project_lock),
        )
        assert {future.result(timeout=10) for future in futures} == {
            "event",
            "user",
        }


def test_bulk_outbox_insert_enqueues_projection_tasks_inline(api):
    fanout_uuid = sys_uuid.uuid4()
    started_at = time.monotonic()
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT gen_random_uuid(), %s, 'read_counters', 'user-topic',
                   %s || ':derive-read:' || input.number::text,
                   '{"source_kind":"legacy_message_state.updated"}'::jsonb,
                   NOW() - interval '1 day', NOW() - interval '1 day'
            FROM generate_series(1, 10000) AS input(number)
            """,
            (api.project_id, str(api.project_id)),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                %s, %s, 'fanout', 'topic', %s,
                jsonb_build_object('placement_uuid', gen_random_uuid())
            )
            """,
            (fanout_uuid, api.project_id, f"{api.project_id}:derive-fanout"),
        )
        task_counts = session.execute(
            """
            SELECT task_kind, count(*) AS task_count
            FROM messenger_projection_tasks
            WHERE project_id = %s
            GROUP BY task_kind
            ORDER BY task_kind
            """,
            (api.project_id,),
        ).fetchall()
    elapsed = time.monotonic() - started_at

    assert [(row["task_kind"], row["task_count"]) for row in task_counts] == [
        ("fanout", 1),
        ("read_counters", 10000),
    ]
    assert elapsed < 10


def test_fair_scheduler_bounds_fanout_under_large_read_backlog(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.cycle(v2_projection.FAIR_SCHEDULER_LANES),
    )
    project_id = api.project_id
    special_events = (
        ("fanout", "topic", "fanout", {"placement_uuid": str(sys_uuid.uuid4())}),
        (
            "read_counters",
            "user-topic",
            "interactive",
            {"source_kind": "topic.read"},
        ),
        (
            "reaction_snapshot",
            "message",
            "reaction",
            {"placement_uuid": str(sys_uuid.uuid4())},
        ),
        (
            "topic_state_projection",
            "topic",
            "background",
            {"source_kind": "topic.updated"},
        ),
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            "DELETE FROM messenger_domain_outbox_events WHERE project_id = %s",
            (project_id,),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT gen_random_uuid(), %s, 'read_counters', 'user-topic',
                   %s || ':read:' || input.number::text,
                   jsonb_build_object(
                       'source_kind', 'legacy_message_state.updated',
                       'user_uuid', md5(
                           %s || ':user:' || input.number::text
                       )::uuid::text
                   ),
                   NOW() - interval '1 day', NOW() - interval '1 day'
            FROM generate_series(1, 30000) AS input(number)
            """,
            (project_id, str(project_id), str(project_id)),
        )
        for event_kind, scope_kind, suffix, payload in special_events:
            session.execute(
                """
                INSERT INTO messenger_domain_outbox_events (
                    uuid, project_id, event_kind, scope_kind, scope_key, payload
                ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    project_id,
                    event_kind,
                    scope_kind,
                    f"{project_id}:{suffix}",
                    json.dumps(payload),
                ),
            )
        # Plan bulk fixture alignment against its current size, not earlier tests.
        session.execute(
            "ANALYZE messenger_projection_tasks, messenger_domain_outbox_events", ()
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = event.updated_at
            FROM messenger_domain_outbox_events AS event
            WHERE task.project_id = %s
              AND event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
            """,
            (project_id,),
        )
        session.execute("ANALYZE messenger_projection_tasks", ())
        plan = session.execute(
            """
            EXPLAIN (FORMAT JSON)
            SELECT project_id, uuid
            FROM messenger_projection_tasks
            WHERE status NOT IN ('completed', 'dead_letter')
              AND task_kind = 'fanout'
            ORDER BY created_at, ordering_created_at, outbox_event_uuid
            LIMIT 128
            """,
            (),
        ).fetchone()["QUERY PLAN"][0]
        user_partition_plan = session.execute(
            """
            EXPLAIN (FORMAT JSON)
            SELECT project_id, uuid
            FROM messenger_projection_tasks
            WHERE project_id = %s
              AND payload->>'user_uuid' IS NOT NULL
              AND payload->>'user_uuid' = %s
              AND task_kind IN ('read_counters', 'folder_projection')
              AND status NOT IN ('completed', 'dead_letter')
            ORDER BY created_at, ordering_created_at, outbox_event_uuid
            LIMIT 1
            """,
            (project_id, api.user_uuid),
        ).fetchone()["QUERY PLAN"][0]
        started_at = time.monotonic()
        claimed = []
        for index in range(len(v2_projection.FAIR_SCHEDULER_LANES)):
            task = v2_projection._claim_task(
                session,
                f"integration:fair:{index}",
                30,
            )
            assert task is not None
            claimed.append((task["task_kind"], task["payload"].get("source_kind")))
            session.execute(
                """
                UPDATE messenger_projection_tasks
                SET status = 'completed', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (project_id, task["uuid"]),
            )
            session.execute(
                """
                UPDATE messenger_projection_scope_leases
                SET owner = NULL, lease_expires_at = NOW(), updated_at = NOW()
                WHERE project_id = %s AND scope_kind = %s AND scope_key = %s
                """,
                (project_id, task["scope_kind"], task["scope_key"]),
            )
        elapsed = time.monotonic() - started_at
        metrics = v2_projection.projection_queue_metrics(session)
        session.execute(
            "DELETE FROM messenger_domain_outbox_events WHERE project_id = %s",
            (project_id,),
        )

    assert claimed[0][0] == "fanout"
    assert ("read_counters", "topic.read") in claimed
    assert any(task_kind == "reaction_snapshot" for task_kind, _source in claimed)
    assert any(task_kind == "topic_state_projection" for task_kind, _source in claimed)
    assert any(
        node.get("Index Name") == "messenger_projection_tasks_fair_claim_idx"
        for node in _plan_nodes(plan["Plan"])
    ), json.dumps(plan)
    assert any(
        node.get("Index Name") == "messenger_projection_tasks_user_partition_idx"
        for node in _plan_nodes(user_partition_plan["Plan"])
    ), json.dumps(user_partition_plan)
    # The indexed, bounded plan is the stable regression check. Keep only a
    # coarse end-to-end guard here so shared-runner I/O and autovacuum timing
    # cannot turn an otherwise bounded query into a flaky test.
    assert v2_projection.CLAIM_CANDIDATE_LIMIT < 30000
    assert elapsed < 10
    assert metrics["unfinished"] >= 29994
    assert metrics["oldest_pending_task_seconds"] >= 23 * 60 * 60


def test_fair_scheduler_scans_past_dense_locked_project(api, monkeypatch):
    locked_project_id = sys_uuid.UUID(api.project_id)
    available_project_id = sys_uuid.UUID(int=locked_project_id.int + 1)
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT gen_random_uuid(), %s, 'fanout', 'message',
                   %s || ':locked:' || input.number::text,
                   jsonb_build_object('placement_uuid', gen_random_uuid()),
                   NOW() - interval '2 days' +
                       input.number * interval '1 microsecond',
                   NOW() - interval '2 days'
            FROM generate_series(1, %s) AS input(number)
            """,
            (
                locked_project_id,
                str(locked_project_id),
                2000,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), %s, 'fanout', 'message', %s,
                jsonb_build_object('placement_uuid', gen_random_uuid()),
                NOW() - interval '1 day', NOW() - interval '1 day'
            )
            """,
            (available_project_id, f"{available_project_id}:available"),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = event.updated_at
            FROM messenger_domain_outbox_events AS event
            WHERE event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
            """,
            (),
        )

    locked = threading.Event()
    release = threading.Event()

    def hold_project_lock():
        with contexts.Context().session_manager() as session:
            session.execute(
                """
                SELECT pg_advisory_xact_lock_shared(
                    hashtextextended(%s, 0)
                )
                """,
                (
                    f"{v2_projection.PROJECTION_PROJECT_ADMISSION_PREFIX}:"
                    f"{locked_project_id}",
                ),
            )
            locked.set()
            assert release.wait(timeout=10)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_project_lock)
        assert locked.wait(timeout=5)
        try:
            with contexts.Context().session_manager() as session:
                started_at = time.monotonic()
                deferred = v2_projection._claim_task(
                    session,
                    "integration:dense-project-fairness",
                    30,
                )
                assert deferred is None
            with contexts.Context().session_manager() as session:
                claimed = v2_projection._claim_task(
                    session,
                    "integration:dense-project-fairness",
                    30,
                )
                elapsed = time.monotonic() - started_at
                assert claimed is not None
                assert claimed["project_id"] == available_project_id
                assert elapsed < 2
        finally:
            release.set()
        holder.result(timeout=10)


def test_fair_scheduler_scans_past_dense_busy_user_partition(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    busy_user_uuid = sys_uuid.uuid4()
    available_user_uuid = sys_uuid.uuid4()
    specifications = [
        {
            "task_kind": "folder_projection",
            "scope_kind": "user-folder",
            "scope_key": f"{api.project_id}:{busy_user_uuid}:{index}",
            "payload": {
                "source_kind": "folder.updated",
                "user_uuid": str(busy_user_uuid),
                "folder_uuid": str(sys_uuid.uuid4()),
            },
        }
        for index in range(2000)
    ]
    specifications.append(
        {
            "task_kind": "folder_projection",
            "scope_kind": "user-folder",
            "scope_key": f"{api.project_id}:{available_user_uuid}:available",
            "payload": {
                "source_kind": "folder.updated",
                "user_uuid": str(available_user_uuid),
                "folder_uuid": str(sys_uuid.uuid4()),
            },
        }
    )
    event_uuids = _seed_partition_claim_tasks(api, specifications)
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'running', lease_owner = 'busy-user-worker',
                lease_expires_at = NOW() + INTERVAL '1 hour'
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuids[0]),
        )

    with contexts.Context().session_manager() as session:
        claimed = v2_projection._claim_task(
            session,
            "integration:dense-user-fairness",
            30,
        )
        assert claimed is not None
        assert claimed["outbox_event_uuid"] == event_uuids[-1]
        assert claimed["partition_key"] == str(available_user_uuid)
        session.rollback()


def test_fair_scheduler_rotates_bounded_user_partition_scan(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    user_uuids = sorted(
        (
            sys_uuid.uuid4()
            for _index in range(v2_projection.CLAIM_PARTITION_SCAN_LIMIT + 1)
        ),
        key=str,
    )
    specifications = [
        {
            "task_kind": "folder_projection",
            "scope_kind": "user-folder",
            "scope_key": f"{api.project_id}:{user_uuid}",
            "payload": {
                "source_kind": "folder.updated",
                "user_uuid": str(user_uuid),
                "folder_uuid": str(sys_uuid.uuid4()),
            },
        }
        for user_uuid in user_uuids
    ]
    event_uuids = _seed_partition_claim_tasks(api, specifications)
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'running', lease_owner = 'busy-window-worker',
                lease_expires_at = NOW() + INTERVAL '1 hour'
            WHERE project_id = %s
              AND payload->>'user_uuid' = ANY(%s::text[])
            """,
            (api.project_id, [str(value) for value in user_uuids[:-1]]),
        )

    with contexts.Context().session_manager() as session:
        metrics = {}
        claimed = v2_projection._claim_task(
            session,
            "integration:bounded-user-rotation",
            30,
            metrics,
        )
        assert claimed is None
        assert metrics["partition_scan_continuation"] == 1
        claimed = v2_projection._claim_task(
            session,
            "integration:bounded-user-rotation",
            30,
            metrics,
        )
        assert claimed is not None
        assert claimed["outbox_event_uuid"] == event_uuids[-1]
        assert claimed["partition_key"] == str(user_uuids[-1])
        session.rollback()


def test_deferred_global_claim_keeps_truncated_window_cursor(monkeypatch):
    worker_id = "integration:deferred-truncated-cursor"
    deferred_project_id = sys_uuid.uuid4()
    window_end_project_id = sys_uuid.uuid4()
    scan_state = {
        "user_partition_scan_count": 0,
        "user_cursor_project_id": None,
        "user_cursor_partition_key": None,
        "global_project_scan_count": v2_projection.CLAIM_PARTITION_SCAN_LIMIT,
        "global_cursor_project_id": window_end_project_id,
    }
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    monkeypatch.setattr(
        v2_projection,
        "_claim_task_for_lane",
        lambda *_args, **_kwargs: (None, scan_state, deferred_project_id),
    )
    metrics = {}
    try:
        assert v2_projection._claim_task(None, worker_id, 30, metrics) is None
        assert v2_projection._claim_partition_cursors(worker_id)[2] == (
            window_end_project_id
        )
        assert metrics["admission_commit_required"] == 1
        assert "partition_scan_continuation" not in metrics
    finally:
        with v2_projection._CLAIM_PARTITION_CURSOR_LOCK:
            v2_projection._CLAIM_PARTITION_CURSORS.pop(worker_id, None)


def test_fallback_lane_reuses_preferred_partition_window(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    user_uuids = sorted(
        (
            sys_uuid.uuid4()
            for _index in range(v2_projection.CLAIM_PARTITION_SCAN_LIMIT + 1)
        ),
        key=str,
    )
    event_uuids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "folder_projection",
                "scope_kind": "user-folder",
                "scope_key": f"{api.project_id}:{user_uuid}",
                "payload": {
                    "source_kind": "folder.updated",
                    "user_uuid": str(user_uuid),
                    "folder_uuid": str(sys_uuid.uuid4()),
                },
            }
            for user_uuid in user_uuids
        ],
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'running', lease_owner = 'busy-fallback-worker',
                lease_expires_at = NOW() + INTERVAL '1 hour'
            WHERE project_id = %s
              AND payload->>'user_uuid' <> %s
            """,
            (api.project_id, str(user_uuids[0])),
        )

    with contexts.Context().session_manager() as session:
        claimed = v2_projection._claim_task(
            session,
            "integration:fallback-window",
            30,
        )
        assert claimed is not None
        assert claimed["outbox_event_uuid"] == event_uuids[0]
        assert claimed["partition_key"] == str(user_uuids[0])
        session.rollback()


def test_null_user_partition_payload_reaches_failure_path(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    event_uuid = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "folder_projection",
                "scope_kind": "user-folder",
                "scope_key": f"{api.project_id}:null-user",
                "payload": {
                    "source_kind": "folder.updated",
                    "user_uuid": None,
                    "folder_uuid": str(sys_uuid.uuid4()),
                },
            }
        ],
    )[0]

    with contexts.Context().session_manager() as session:
        claimed = v2_projection._claim_task(
            session,
            "integration:null-user-payload",
            30,
        )
        assert claimed is not None
        assert claimed["outbox_event_uuid"] == event_uuid
        assert claimed["partition_kind"] == "project"
        session.rollback()


def test_fair_scheduler_skips_project_with_retry_blocked_predecessor(
    api,
    monkeypatch,
):
    blocked_project_id = sys_uuid.UUID(api.project_id)
    available_project_id = sys_uuid.uuid4()
    blocked_placement_uuid = sys_uuid.uuid4()
    blocked_scope_key = f"{blocked_project_id}:retry-blocked"
    available_scope_key = f"{available_project_id}:available"
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES
                (
                    gen_random_uuid(), %s, 'fanout', 'message', %s,
                    jsonb_build_object('placement_uuid', %s::text),
                    NOW() - interval '2 days', NOW() - interval '2 days'
                ),
                (
                    gen_random_uuid(), %s, 'fanout', 'message', %s,
                    jsonb_build_object('placement_uuid', %s::text),
                    NOW() - interval '1 day', NOW() - interval '1 day'
                ),
                (
                    gen_random_uuid(), %s, 'fanout', 'message', %s,
                    jsonb_build_object('placement_uuid', gen_random_uuid()),
                    NOW() - interval '1 hour', NOW() - interval '1 hour'
                )
            """,
            (
                blocked_project_id,
                blocked_scope_key,
                blocked_placement_uuid,
                blocked_project_id,
                blocked_scope_key,
                blocked_placement_uuid,
                available_project_id,
                available_scope_key,
            ),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = event.updated_at
            FROM messenger_domain_outbox_events AS event
            WHERE event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
            """,
            (),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'failed', next_retry_at = NOW() + interval '5 minutes'
            WHERE project_id = %s AND uuid = (
                SELECT uuid
                FROM messenger_projection_tasks
                WHERE project_id = %s AND scope_key = %s
                ORDER BY created_at, ordering_created_at, outbox_event_uuid
                LIMIT 1
            )
            """,
            (blocked_project_id, blocked_project_id, blocked_scope_key),
        )

        claimed = v2_projection._claim_task(
            session,
            "integration:retry-blocked-project",
            30,
        )

        assert claimed is not None
        assert claimed["project_id"] == available_project_id


def test_fair_scheduler_filters_blocked_rows_before_candidate_limit(
    api,
    monkeypatch,
):
    project_id = sys_uuid.UUID(api.project_id)
    blocked_prefix = f"{project_id}:leased:"
    available_scope_key = f"{project_id}:available-after-leases"
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT gen_random_uuid(), %s, 'fanout', 'message',
                   %s || input.number::text,
                   jsonb_build_object('placement_uuid', gen_random_uuid()),
                   NOW() - interval '2 days' +
                       input.number * interval '1 microsecond',
                   NOW() - interval '2 days'
            FROM generate_series(1, %s) AS input(number)
            """,
            (
                project_id,
                blocked_prefix,
                v2_projection.CLAIM_CANDIDATE_LIMIT,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), %s, 'fanout', 'message', %s,
                jsonb_build_object('placement_uuid', gen_random_uuid()),
                NOW() - interval '1 day', NOW() - interval '1 day'
            )
            """,
            (project_id, available_scope_key),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = event.updated_at
            FROM messenger_domain_outbox_events AS event
            WHERE event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
            """,
            (),
        )
        session.execute(
            """
            INSERT INTO messenger_projection_scope_leases (
                uuid, project_id, scope_kind, scope_key, owner,
                fencing_token, lease_expires_at
            )
            SELECT gen_random_uuid(), task.project_id, task.scope_kind,
                   task.scope_key, 'integration:blocker', 1,
                   NOW() + interval '5 minutes'
            FROM messenger_projection_tasks AS task
            WHERE task.project_id = %s AND task.scope_key LIKE %s
            """,
            (project_id, f"{blocked_prefix}%"),
        )

        claimed = v2_projection._claim_task(
            session,
            "integration:scan-after-candidate-window",
            30,
        )

        assert claimed is not None
        assert claimed["scope_key"] == available_scope_key


def test_zulip_delivery_cutover_normalizes_pending_entity_order_only(api, db):
    entity = sys_uuid.uuid4()
    old_keys = [str(sys_uuid.uuid4()) for _ in range(3)]
    ids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "delivery_snapshot_event",
                "scope_kind": "resource",
                "scope_key": key,
                "payload": {
                    "source_kind": "zulip.incoming",
                    "kind": "message.read",
                    "entity_uuid": str(entity),
                },
            }
            for key in old_keys
        ],
    )
    db.execute(
        "UPDATE messenger_projection_tasks SET status='completed' "
        "WHERE outbox_event_uuid=%s",
        (ids[-1],),
    )
    path = next(conftest.MIGRATIONS_DIR.glob("0268-*.py"))
    spec = importlib.util.spec_from_file_location("zulip_delivery_order", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with contexts.Context().session_manager() as session:
        migration.migration_step.upgrade(session)
        migration.migration_step.upgrade(session)
    rows = db.execute(
        "SELECT task.scope_key,task.ordering_key,task.payload->>'resource_uuid',"
        "event.scope_key,event.payload->>'resource_uuid' "
        "FROM messenger_projection_tasks task JOIN messenger_domain_outbox_events event "
        "ON event.project_id=task.project_id AND event.uuid=task.outbox_event_uuid "
        "WHERE task.outbox_event_uuid=ANY(%s) ORDER BY event.created_at",
        (ids,),
    ).fetchall()
    key = f"{api.project_id}:incoming:{entity}"
    assert rows[:2] == [(key, str(entity), str(entity), key, str(entity))] * 2
    assert rows[-1][0] == rows[-1][3] == old_keys[-1]
    assert rows[-1][2] is rows[-1][4] is None


@pytest.mark.parametrize("batched", [False, True])
def test_personal_counters_leave_journal_unlocked_and_fence_every_recipient(
    api, batched
):
    users = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    payload = (
        {"user_uuids": [str(users[1]), str(users[0]), str(users[1])]}
        if batched
        else {"user_uuid": str(users[0])}
    )
    task = {
        "uuid": sys_uuid.uuid4(),
        "project_id": api.project_id,
        "task_kind": "read_counters",
        "payload": payload,
    }
    with contexts.Context().session_manager() as session:
        assert v2_projection._try_lock_projection_partition(session, task) == (
            True,
            False,
        )
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as observer:
            assert observer.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (api.project_id,),
            ).fetchone() == (True,)
            for user in users if batched else users[:1]:
                key = f"{v2_projection.PROJECTION_USER_PARTITION_PREFIX}:{api.project_id}:{user}"
                assert observer.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (key,)
                ).fetchone() == (False,)
            key = f"{v2_projection.PROJECTION_USER_PARTITION_PREFIX}:{api.project_id}:{sys_uuid.uuid4()}"
            assert observer.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (key,)
            ).fetchone() == (True,)


@pytest.mark.parametrize("older_batched", [False, True])
@pytest.mark.parametrize("overlap", [False, True])
def test_batch_and_single_user_tasks_preserve_overlapping_fifo(
    api, db, older_batched, overlap
):
    users = [str(sys_uuid.uuid4()) for _ in range(3)]
    specs = []
    for index, batched in enumerate((older_batched, not older_batched)):
        user = users[0] if index == 0 or overlap else users[1]
        specs.append(
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": f"{api.project_id}:fifo:{index}",
                "payload": {
                    "source_kind": "message.created",
                    **(
                        {"user_uuids": [user, users[2]]}
                        if batched
                        else {"user_uuid": user}
                    ),
                },
            }
        )
    ids = _seed_partition_claim_tasks(api, specs)
    db.execute(
        "UPDATE messenger_projection_tasks SET next_retry_at = NOW() + interval '1 hour' "
        "WHERE outbox_event_uuid = %s",
        (ids[0],),
    )
    db.commit()
    with contexts.Context().session_manager() as session:
        task = v2_projection._claim_task(
            session, f"integration:batch-fifo:{sys_uuid.uuid4()}", 30
        )
        if overlap:
            assert task is None
        else:
            assert task is not None and task["outbox_event_uuid"] == ids[1]
        session.rollback()


def test_counter_coalescing_does_not_expand_beyond_fenced_users(api, db, monkeypatch):
    users = [str(sys_uuid.uuid4()) for _ in range(2)]
    stream, topic = str(sys_uuid.uuid4()), str(sys_uuid.uuid4())
    specs = [
        {
            "task_kind": "read_counters",
            "scope_kind": "user-topic",
            "scope_key": f"{api.project_id}:same-topic",
            "payload": {
                "source_kind": "message.created",
                "stream_uuid": stream,
                "topic_uuid": topic,
                "user_uuids": [user],
                "emit_public_event": False,
            },
        }
        for user in users
    ]
    ids = _seed_partition_claim_tasks(api, specs)
    refreshed = []
    monkeypatch.setattr(
        v2_projection,
        "_refresh_recipient_counters",
        lambda *args: refreshed.extend(args[4]),
    )
    monkeypatch.setattr(
        v2_projection, "_enqueue_counter_outbox_events", lambda *args, **kwargs: None
    )
    with contexts.Context().session_manager() as session:
        task = v2_projection._claim_task(session, "integration:batch-coalesce", 30)
        assert task is not None
        v2_projection._process_read_counters(session, task)
    assert refreshed == [sys_uuid.UUID(users[0])]
    assert db.execute(
        "SELECT status FROM messenger_projection_tasks WHERE outbox_event_uuid=%s",
        (ids[1],),
    ).fetchone() == ("pending",)


@pytest.mark.parametrize("hold_global", [False, True])
def test_batched_user_work_and_global_work_still_fence_each_other(api, hold_global):
    personal = {
        "uuid": sys_uuid.uuid4(),
        "project_id": api.project_id,
        "task_kind": "read_counters",
        "payload": {"user_uuids": [api.user_uuid]},
    }
    global_task = {
        "uuid": sys_uuid.uuid4(),
        "project_id": api.project_id,
        "task_kind": "fanout",
        "payload": {},
    }
    held, attempted = (
        (global_task, personal) if hold_global else (personal, global_task)
    )
    with contexts.Context().session_manager() as session:
        assert v2_projection._try_lock_projection_partition(session, held) == (
            True,
            False,
        )
        with psycopg.connect(
            conftest.TEST_DB_URL, row_factory=psycopg.rows.dict_row
        ) as other:
            assert v2_projection._try_lock_projection_partition(other, attempted) == (
                False,
                not hold_global,
            )
            other.rollback()


def test_waiting_global_admission_also_blocks_younger_batch(api, db):
    ids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "fanout",
                "scope_kind": "stream",
                "scope_key": "global-before-batch",
                "payload": {},
            },
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": "batch-after-global",
                "payload": {
                    "user_uuids": [api.user_uuid],
                    "source_kind": "message.created",
                },
            },
        ],
    )
    with contexts.Context().session_manager() as session:
        task = session.execute(
            "SELECT * FROM messenger_projection_tasks WHERE outbox_event_uuid=%s",
            (ids[0],),
        ).fetchone()
        v2_projection._mark_projection_project_admission(session, task, 30)
        session.execute(
            "UPDATE messenger_projection_tasks SET next_retry_at=NOW()+interval '1 hour' WHERE uuid=%s",
            (task["uuid"],),
        )
    with contexts.Context().session_manager() as session:
        assert (
            v2_projection._claim_task(session, "integration:batch-admission", 30)
            is None
        )
        session.rollback()
    db.execute(
        "UPDATE messenger_projection_tasks SET status='completed' WHERE outbox_event_uuid=%s",
        (ids[0],),
    )
    db.commit()
    with contexts.Context().session_manager() as session:
        assert (
            v2_projection._claim_task(session, "integration:batch-admission", 30)[
                "outbox_event_uuid"
            ]
            == ids[1]
        )
        session.rollback()


def test_independent_publisher_commits_while_batched_counter_is_blocked(
    api, db, monkeypatch
):
    stream, message = _create_message(api, "publication-during-counter")
    _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": "slow-counter",
                "payload": {
                    "source_kind": "message.created",
                    "user_uuids": [api.user_uuid],
                    "stream_uuid": stream["uuid"],
                    "topic_uuid": stream["default_topic_uuid"],
                    "emit_public_event": False,
                },
            }
        ],
    )
    ready, release = threading.Event(), threading.Event()
    original = v2_projection._refresh_recipient_counters

    def blocked_refresh(*args):
        ready.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(v2_projection, "_refresh_recipient_counters", blocked_refresh)
    projection = agents.MessengerWorkerAgent(
        v2_projection_enabled=True, projection_only=True
    )
    publisher = agents.MessengerPublicationAgent(iter_min_period=0, iter_pause=0)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        counter = pool.submit(projection._run_v2_projection_tasks)
        try:
            assert ready.wait(5)
            with contexts.Context().session_manager() as session:
                queued = publication.enqueue(
                    session,
                    api.project_id,
                    message["uuid"],
                    [api.user_uuid],
                    "message.created",
                    {"marker": "during-counter"},
                )
            publishing = pool.submit(publisher._iteration)
            publishing.result(2)
            assert not counter.done()
            row = db.execute(
                "SELECT epoch_version FROM m_workspace_visible_events WHERE uuid=%s",
                (queued[0].uuid,),
            ).fetchone()
            assert row is not None
            assert db.execute(
                "SELECT count(*) FROM messenger_publication_outbox WHERE uuid=%s",
                (queued[0].uuid,),
            ).fetchone() == (0,)
            publisher._iteration()
            assert db.execute(
                "SELECT count(*) FROM m_workspace_visible_events WHERE uuid=%s",
                (queued[0].uuid,),
            ).fetchone() == (1,)
        finally:
            release.set()
            counter.result(10)
            projection.stop()
            publisher.stop()


def test_failed_batch_claim_releases_all_partially_acquired_user_locks(api):
    users = sorted([str(sys_uuid.uuid4()), str(sys_uuid.uuid4())])
    _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": "partly-busy-batch",
                "payload": {"source_kind": "message.created", "user_uuids": users},
            }
        ],
    )
    with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as blocker:
        blocked_key = f"{v2_projection.PROJECTION_USER_PARTITION_PREFIX}:{api.project_id}:{users[1]}"
        blocker.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (blocked_key,)
        )
        with contexts.Context().session_manager() as session:
            assert (
                v2_projection._claim_task(session, "integration:partial-batch", 30)
                is None
            )
            first_key = f"{v2_projection.PROJECTION_USER_PARTITION_PREFIX}:{api.project_id}:{users[0]}"
            assert blocker.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                (first_key,),
            ).fetchone() == (True,)


def test_independent_publisher_preserves_committed_epoch_order(api, db):
    publisher = agents.MessengerPublicationAgent(iter_min_period=0.1, iter_pause=0)
    with contexts.Context().session_manager() as writer:
        before = publication.enqueue(
            writer,
            api.project_id,
            sys_uuid.uuid4(),
            [api.user_uuid],
            "message.created",
            {"marker": "before"},
        )
        assert publication.publish(writer, api.project_id) == 1
        # A later committed snapshot cannot advance the cursor past an earlier
        # event whose epoch has been allocated in an uncommitted transaction.
        with psycopg.connect(
            conftest.TEST_DB_URL, row_factory=psycopg.rows.dict_row
        ) as producer:
            after = publication.enqueue(
                producer,
                api.project_id,
                sys_uuid.uuid4(),
                [api.user_uuid],
                "message.created",
                {"marker": "after"},
            )
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pool.submit(publisher._iteration).result(5)
        assert db.execute(
            "SELECT count(*) FROM m_workspace_visible_events WHERE uuid=ANY(%s::uuid[])",
            ([before[0].uuid, after[0].uuid],),
        ).fetchone() == (0,)
    publisher._iteration()
    rows = db.execute(
        "SELECT uuid, epoch_version FROM m_workspace_visible_events WHERE uuid=ANY(%s::uuid[]) ORDER BY epoch_version",
        ([before[0].uuid, after[0].uuid],),
    ).fetchall()
    assert [row[0] for row in rows] == [before[0].uuid, after[0].uuid]
    assert rows[0][1] < rows[1][1]


def test_existing_empty_reaction_users_do_not_break_message_pages_or_replay(api, db):
    stream, message = _create_message(api, "existing-count-only-reaction-users")
    for index in range(49):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": stream["default_topic_uuid"],
                "payload": {"kind": "markdown", "content": f"Page row {index}"},
            },
        )
        assert response.status_code == 201, response.text
    _drain()
    counts = {"grin": 12, "eyes": 1}
    old_users = {"grin": [], "eyes": [str(api.user_uuid)]}
    expected_users = {"eyes": [str(api.user_uuid)]}
    canonical_uuid = db.execute(
        "SELECT message_uuid FROM messenger_message_placements "
        "WHERE project_id=%s AND uuid=%s",
        (api.project_id, message["uuid"]),
    ).fetchone()[0]
    db.execute(
        "UPDATE messenger_messages SET reactions=%s::jsonb,reaction_users=%s::jsonb "
        "WHERE project_id=%s AND uuid=%s",
        (json.dumps(counts), json.dumps(old_users), api.project_id, canonical_uuid),
    )
    for path, params in (
        (MESSAGES, {"stream_uuid": stream["uuid"], "page_limit": 50}),
        (f"{MESSAGES}{message['uuid']}", {}),
    ):
        response = api.get(path, params=params)
        assert response.status_code == 200, response.text
        payload = response.json()
        if isinstance(payload, list):
            assert len(payload) == 50
            assert all(
                users for row in payload for users in row["reaction_users"].values()
            )
            payload = next(row for row in payload if row["uuid"] == message["uuid"])
        assert payload["reactions"] == counts
        assert payload["reaction_users"] == expected_users
    replay = events.event_row_to_messenger_event(
        {
            "uuid": sys_uuid.uuid4(),
            "project_id": api.project_id,
            "user_uuid": api.user_uuid,
            "epoch_version": 1,
            "payload": {
                **message,
                "kind": "message.updated",
                "reactions": counts,
                "reaction_users": old_users,
            },
        }
    )
    assert replay["payload"]["reactions"] == counts
    assert replay["payload"]["reaction_users"] == expected_users
    assert (
        db.execute(
            "SELECT reaction_users FROM messenger_messages WHERE uuid=%s",
            (canonical_uuid,),
        ).fetchone()[0]
        == old_users
    )
