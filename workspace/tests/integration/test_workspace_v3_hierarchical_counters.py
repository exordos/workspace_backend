# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""Synthetic PostgreSQL oracle, baseline and concurrency acceptance."""

import concurrent.futures
import json
import random
import threading
import time
import uuid as sys_uuid

import psycopg
from restalchemy.storage.sql import migrations as ra_migrations
import pytest
import webob

from workspace.messenger_api import provider_store
from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.api import middlewares as api_middlewares
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.tests.integration import conftest
from workspace.tests.integration import test_workspace_v3_projection as support
from workspace.tests.integration import (
    test_workspace_v3_provider_api as provider_support,
)


@pytest.fixture(autouse=True)
def _v3_store():
    api_store.configure_store_factory(store_factory.build_v3_store_factory())
    yield
    api_store.configure_store_factory(
        sql_canonical_store.SQLCanonicalMessengerStoreFactory()
    )


def _messages(db, project, stream, topic, user, count):
    return [
        row[0]
        for row in db.execute(
            """
        WITH messages AS (
            INSERT INTO workspace_v3.messages
                (project_id, uuid, stream_uuid, topic_uuid, author_uuid, payload)
            SELECT %s, gen_random_uuid(), %s, %s, %s,
                   '{"kind":"markdown","content":"synthetic"}'::jsonb
            FROM generate_series(1, %s) RETURNING uuid
        )
        INSERT INTO workspace_v3.message_flags
            (project_id, stream_uuid, message_uuid, user_uuid)
        SELECT %s, %s, uuid, %s FROM messages RETURNING message_uuid
        """,
            (project, stream, topic, user, count, project, stream, user),
        ).fetchall()
    ]


def _topic(db, project, stream, user, mode="default"):
    topic = sys_uuid.uuid4()
    db.execute(
        "INSERT INTO workspace_v3.topics(project_id,uuid,stream_uuid,name) VALUES (%s,%s,%s,'Synthetic')",
        (project, topic, stream),
    )
    db.execute(
        """INSERT INTO workspace_v3.topic_bindings
        (project_id,uuid,stream_uuid,topic_uuid,user_uuid,notification_mode)
        VALUES (%s,gen_random_uuid(),%s,%s,%s,%s)""",
        (project, stream, topic, user, mode),
    )
    return topic


def _oracle(db, project):
    # Recompute only in the test oracle, independently from contribution state.
    rows = db.execute(
        """
        SELECT binding.topic_uuid, binding.user_uuid,
               binding.exact_unread_count, binding.exact_mentioned_unread_count,
               binding.exact_active_unread_count, binding.notification_mode,
               stream.notification_mode,
               count(flag.uuid) FILTER (WHERE NOT flag.read),
               count(flag.uuid) FILTER (WHERE NOT flag.read AND flag.mentioned)
        FROM workspace_v3.topic_bindings AS binding
        JOIN workspace_v3.stream_bindings AS stream USING (project_id,stream_uuid,user_uuid)
        LEFT JOIN workspace_v3.messages AS message
          ON message.project_id=binding.project_id AND message.topic_uuid=binding.topic_uuid
        LEFT JOIN workspace_v3.message_flags AS flag
          ON flag.project_id=message.project_id AND flag.message_uuid=message.uuid
         AND flag.user_uuid=binding.user_uuid
        WHERE binding.project_id=%s
        GROUP BY binding.project_id,binding.uuid,stream.notification_mode
        """,
        (project,),
    ).fetchall()
    for (
        topic,
        user,
        unread,
        mentioned,
        active,
        topic_mode,
        stream_mode,
        raw,
        raw_mentions,
    ) in rows:
        assert (unread, mentioned) == (raw, raw_mentions)
        expected = (
            0
            if topic_mode == "mute"
            else raw
            if topic_mode == "follow"
            else raw_mentions
            if topic_mode == "unmute"
            else raw
            if stream_mode == "all_messages"
            else raw_mentions
            if stream_mode == "mentions_only"
            else 0
        )
        assert active == expected
    assert (
        db.execute(
            """
        SELECT count(*) FROM workspace_v3.stream_bindings AS stream
        WHERE project_id=%s AND (unread_topic_count,active_unread_topic_count) IS DISTINCT FROM (
            SELECT ROW(count(*) FILTER (WHERE exact_unread_count>0),
                   count(*) FILTER (WHERE exact_active_unread_count>0))
            FROM workspace_v3.topic_bindings AS topic
            WHERE topic.project_id=stream.project_id AND topic.stream_uuid=stream.stream_uuid
              AND topic.user_uuid=stream.user_uuid)
        """,
            (project,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            """
        SELECT count(*) FROM workspace_v3.folders AS folder
        WHERE project_id=%s AND (unread_stream_count,active_unread_stream_count) IS DISTINCT FROM (
            SELECT ROW(count(DISTINCT stream.stream_uuid) FILTER (WHERE unread_topic_count>0),
                   count(DISTINCT stream.stream_uuid) FILTER (WHERE active_unread_topic_count>0))
            FROM workspace_v3.folder_items AS item
            JOIN workspace_v3.stream_bindings AS stream USING (project_id,stream_uuid,user_uuid)
            WHERE item.project_id=folder.project_id AND item.user_uuid=folder.user_uuid
              AND item.folder_uuid=folder.uuid)
        """,
            (project,),
        ).fetchone()[0]
        == 0
    )


def test_counter_01_02_saturation_and_mixed_child_units(_database, db, api):
    project, stream, topic, users = support._seed_conversation(
        db, user_count=1, clear_tasks=False
    )
    user = users[0]
    second = _topic(db, project, stream, user, "mute")
    _topic(db, project, stream, user)
    messages = _messages(db, project, stream, topic, user, 2500)
    _messages(db, project, stream, second, user, 3)
    support._drain(db)
    counts = db.execute(
        """SELECT unread_count,unread_topic_count,active_unread_topic_count,
        passive_unread_topic_count FROM workspace_v3.stream_bindings WHERE project_id=%s""",
        (project,),
    ).fetchone()
    assert counts == (2, 2, 1, 1)
    version = db.execute(
        "SELECT counter_version FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
        (topic,),
    ).fetchone()[0]
    db.execute(
        "UPDATE workspace_v3.message_flags SET read=true WHERE message_uuid=%s",
        (messages[0],),
    )
    support._drain(db)
    row = db.execute(
        "SELECT exact_unread_count,unread_count,counter_version FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
        (topic,),
    ).fetchone()
    assert row[:2] == (2499, 1001)
    assert row[2] > version
    db.execute(
        "UPDATE workspace_v3.message_flags SET read=true WHERE message_uuid=%s",
        (messages[0],),
    )
    assert (
        db.execute(
            "SELECT counter_version FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
            (topic,),
        ).fetchone()[0]
        == row[2]
    )
    _oracle(db, project)
    snapshot = api.get(f"/v1/streams/{stream}", user=user, project=project).json()
    assert "counter_schema_version" not in snapshot
    assert snapshot["unread_count"] == 2
    folders = api.get("/v1/folders/", user=user, project=project).json()
    assert sorted(folder["unread_count"] for folder in folders) == [0, 1, 1]
    for remaining in (1002, 1001, 1000, 1, 0):
        db.execute(
            """UPDATE workspace_v3.message_flags SET read=true WHERE uuid IN (
            SELECT flag.uuid FROM workspace_v3.message_flags AS flag
            JOIN workspace_v3.messages AS message ON message.uuid=flag.message_uuid
            WHERE message.topic_uuid=%s AND NOT flag.read ORDER BY flag.uuid
            OFFSET %s)""",
            (topic, remaining),
        )
        support._drain(db)
        assert db.execute(
            "SELECT exact_unread_count,unread_count FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
            (topic,),
        ).fetchone() == (remaining, min(remaining, 1001))
        _oracle(db, project)


def test_counter_06_random_flags_mentions_modes_moves_and_delete(_database, db):
    project, stream, topic, users = support._seed_conversation(
        db, user_count=1, clear_tasks=False
    )
    user = users[0]
    second = _topic(db, project, stream, user)
    ids = _messages(db, project, stream, topic, user, 35)
    rng = random.Random(1701)
    for _ in range(60):
        message = rng.choice(ids)
        operation = rng.randrange(4)
        if operation == 0:
            db.execute(
                "UPDATE workspace_v3.message_flags SET read=%s,mentioned=%s WHERE message_uuid=%s",
                (rng.choice([True, False]), rng.choice([True, False]), message),
            )
        elif operation == 1:
            db.execute(
                "UPDATE workspace_v3.messages SET topic_uuid=%s WHERE uuid=%s",
                (rng.choice([topic, second]), message),
            )
        elif operation == 2:
            db.execute(
                "UPDATE workspace_v3.topic_bindings SET notification_mode=%s WHERE topic_uuid=%s",
                (
                    rng.choice(["mute", "follow", "default"]),
                    rng.choice([topic, second]),
                ),
            )
        else:
            db.execute(
                "UPDATE workspace_v3.stream_bindings SET notification_mode=%s WHERE project_id=%s",
                (rng.choice(["muted", "mentions_only", "all_messages"]), project),
            )
        support._drain(db)
        _oracle(db, project)
    db.execute("DELETE FROM workspace_v3.messages WHERE uuid=ANY(%s)", (ids[:20],))
    support._drain(db)
    _oracle(db, project)
    db.execute(
        "DELETE FROM workspace_v3.stream_bindings WHERE project_id=%s", (project,)
    )
    support._drain(db)
    _oracle(db, project)
    assert (
        db.execute(
            "SELECT count(*) FROM workspace_v3.unread_contributions WHERE project_id=%s",
            (project,),
        ).fetchone()[0]
        == 0
    )


def test_counter_05_concurrent_duplicate_read_new_message_and_rollback(_database, db):
    project, stream, topic, users = support._seed_conversation(db, user_count=1)
    user = users[0]
    ids = _messages(db, project, stream, topic, user, 2)
    barrier = threading.Barrier(3)

    def change(kind):
        with psycopg.connect(conftest.TEST_DB_URL) as conn:
            barrier.wait(timeout=5)
            if kind == "new":
                _messages(conn, project, stream, topic, user, 1)
            else:
                conn.execute(
                    "UPDATE workspace_v3.message_flags SET read=true WHERE message_uuid=%s AND NOT read",
                    (ids[0],),
                )

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = [pool.submit(change, kind) for kind in ["read", "read", "new"]]
        for result in results:
            result.result(timeout=15)
    support._drain(db)
    assert (
        db.execute(
            "SELECT exact_unread_count FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
            (topic,),
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(RuntimeError), db.transaction():
        db.execute(
            "UPDATE workspace_v3.message_flags SET read=true WHERE project_id=%s",
            (project,),
        )
        raise RuntimeError("injected rollback")
    _oracle(db, project)


@pytest.mark.parametrize("batch_size", [7, 1000])
def test_migration_01_restartable_baseline_with_concurrent_flag_write(
    _database, db, api, batch_size, record_property
):
    project, stream, topic, users = support._seed_conversation(db, user_count=1)
    user = users[0]
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = engine._load_migrations()[
        "0207-Maintain-exact-hierarchical-unread-counters-77e2f5.py"
    ]
    baseline_queue_migration = engine._load_migrations()[
        "0208-Keep-unread-baseline-snapshots-out-of-the-legacy-projection-queue-ec0e37.py"
    ]
    with db.transaction():
        baseline_queue_migration.downgrade(db)
        migration.downgrade(db)
    message_count = 10033 if batch_size == 1000 else 33
    ids = _messages(db, project, stream, topic, user, message_count)
    with db.transaction():
        migration.upgrade(db)
        baseline_queue_migration.upgrade(db)
    before = api.get(f"/v1/streams/{stream}", user=user, project=project).json()
    assert "unread_topic_count" not in before and "counter_schema_version" not in before
    started = threading.Event()

    def writer():
        with psycopg.connect(conftest.TEST_DB_URL) as conn:
            started.wait(timeout=5)
            conn.execute(
                "UPDATE workspace_v3.message_flags SET read=true WHERE message_uuid=ANY(%s)",
                (ids[:10],),
            )
            _messages(conn, project, stream, topic, user, 7)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        started.set()
        baseline_started = time.monotonic()
        advanced_rows = 0
        for _ in range(10000):
            count = db.execute(
                "SELECT workspace_v3.advance_unread_baseline(%s)", (batch_size,)
            ).fetchone()[0]
            assert 0 <= count <= batch_size
            advanced_rows += count
            if count == 0:
                break
        else:
            raise AssertionError("Baseline did not finish")
        future.result(timeout=15)
        record_property("baseline_batch_size", batch_size)
        record_property("baseline_rows", advanced_rows)
        record_property("baseline_seconds", time.monotonic() - baseline_started)
    support._drain(db)
    _oracle(db, project)
    assert (
        db.execute(
            "SELECT exact_unread_count FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
            (topic,),
        ).fetchone()[0]
        == message_count - 3
    )
    assert (
        db.execute("SELECT workspace_v3.advance_unread_baseline(7)").fetchone()[0] == 0
    )
    assert (
        api.get(f"/v1/streams/{stream}", user=user, project=project).json()[
            "unread_count"
        ]
        == 1
    )


def test_counter_01_parents_above_cap_and_overlapping_folders(_database, db):
    project, stream, topic, users = support._seed_conversation(db, user_count=1)
    user = users[0]
    topics = [sys_uuid.uuid4() for _ in range(1002)]
    db.execute(
        """INSERT INTO workspace_v3.topics(project_id,uuid,stream_uuid,name)
        SELECT %s,uuid,%s,'Synthetic' FROM unnest(%s::uuid[]) AS input(uuid)""",
        (project, stream, topics),
    )
    db.execute(
        """INSERT INTO workspace_v3.topic_bindings(project_id,uuid,stream_uuid,topic_uuid,user_uuid)
        SELECT %s,gen_random_uuid(),%s,uuid,%s FROM unnest(%s::uuid[]) AS input(uuid)""",
        (project, stream, user, topics),
    )
    db.execute(
        """WITH messages AS (
        INSERT INTO workspace_v3.messages(project_id,uuid,stream_uuid,topic_uuid,author_uuid,payload)
        SELECT %s,gen_random_uuid(),%s,uuid,%s,'{"kind":"markdown","content":"synthetic"}'::jsonb
        FROM unnest(%s::uuid[]) AS input(uuid) RETURNING uuid)
        INSERT INTO workspace_v3.message_flags(project_id,stream_uuid,message_uuid,user_uuid)
        SELECT %s,%s,uuid,%s FROM messages""",
        (project, stream, user, topics, project, stream, user),
    )
    assert (
        db.execute(
            "SELECT unread_topic_count FROM workspace_v3.stream_bindings WHERE project_id=%s",
            (project,),
        ).fetchone()[0]
        == 1002
    )
    streams = [sys_uuid.uuid4() for _ in range(1001)]
    db.execute(
        """INSERT INTO workspace_v3.streams(project_id,uuid,name,owner_uuid)
        SELECT %s,uuid,'Synthetic',%s FROM unnest(%s::uuid[]) AS input(uuid)""",
        (project, user, streams),
    )
    db.execute(
        """INSERT INTO workspace_v3.stream_bindings(project_id,uuid,stream_uuid,user_uuid,who_uuid)
        SELECT %s,gen_random_uuid(),uuid,%s,%s FROM unnest(%s::uuid[]) AS input(uuid)""",
        (project, user, user, streams),
    )
    db.execute(
        """INSERT INTO workspace_v3.topics(project_id,uuid,stream_uuid,name)
        SELECT %s,uuid,uuid,'Synthetic' FROM unnest(%s::uuid[]) AS input(uuid)""",
        (project, streams),
    )
    db.execute(
        """INSERT INTO workspace_v3.topic_bindings(project_id,uuid,stream_uuid,topic_uuid,user_uuid)
        SELECT %s,gen_random_uuid(),uuid,uuid,%s FROM unnest(%s::uuid[]) AS input(uuid)""",
        (project, user, streams),
    )
    db.execute(
        """WITH messages AS (
        INSERT INTO workspace_v3.messages(project_id,uuid,stream_uuid,topic_uuid,author_uuid,payload)
        SELECT %s,gen_random_uuid(),uuid,uuid,%s,'{"kind":"markdown","content":"synthetic"}'::jsonb
        FROM unnest(%s::uuid[]) AS input(uuid) RETURNING uuid,stream_uuid)
        INSERT INTO workspace_v3.message_flags(project_id,stream_uuid,message_uuid,user_uuid)
        SELECT %s,stream_uuid,uuid,%s FROM messages""",
        (project, user, streams, project, user),
    )
    folders = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    for folder in folders:
        db.execute(
            "INSERT INTO workspace_v3.folders(project_id,user_uuid,uuid,kind,title) VALUES (%s,%s,%s,'custom','Synthetic')",
            (project, user, folder),
        )
        db.execute(
            """INSERT INTO workspace_v3.folder_items(project_id,user_uuid,uuid,folder_uuid,stream_uuid,chat_type)
            SELECT %s,%s,gen_random_uuid(),%s,uuid,'stream' FROM unnest(%s::uuid[]) AS input(uuid)
            ON CONFLICT (project_id,user_uuid,folder_uuid,stream_uuid) DO NOTHING""",
            (project, user, folder, [stream, *streams]),
        )
    assert db.execute(
        "SELECT unread_stream_count,active_unread_stream_count FROM workspace_v3.folders WHERE uuid=ANY(%s) ORDER BY uuid",
        (folders,),
    ).fetchall() == [(1002, 1002), (1002, 1002)]
    db.execute(
        "DELETE FROM workspace_v3.folder_items WHERE folder_uuid=%s AND stream_uuid=%s",
        (folders[0], streams[0]),
    )
    assert (
        db.execute(
            "SELECT unread_stream_count FROM workspace_v3.folders WHERE uuid=%s",
            (folders[0],),
        ).fetchone()[0]
        == 1001
    )
    assert (
        db.execute(
            "SELECT unread_stream_count FROM workspace_v3.folders WHERE uuid=%s",
            (folders[1],),
        ).fetchone()[0]
        == 1002
    )
    _oracle(db, project)


def test_counter_03_hundred_thousand_flag_batch_and_incremental_parent(_database, db):
    project, stream, topic, users = support._seed_conversation(db, user_count=1)
    user = users[0]
    with db.transaction():
        db.execute(
            "SELECT set_config('workspace_v3.provider_backfill_flags','on',true)"
        )
        _messages(db, project, stream, topic, user, 100000)
    before = db.execute(
        "SELECT counter_version,unread_topic_count FROM workspace_v3.stream_bindings WHERE project_id=%s",
        (project,),
    ).fetchone()
    with db.transaction():
        db.execute(
            "SELECT set_config('workspace_v3.provider_backfill_flags','on',true)"
        )
        db.execute(
            "UPDATE workspace_v3.message_flags SET read=true WHERE project_id=%s",
            (project,),
        )
    after = db.execute(
        "SELECT counter_version,unread_topic_count FROM workspace_v3.stream_bindings WHERE project_id=%s",
        (project,),
    ).fetchone()
    assert before[1] == 1 and after[1] == 0 and after[0] > before[0]
    assert (
        db.execute(
            "SELECT exact_unread_count FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
            (topic,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT count(*) FROM workspace_v3.unread_contributions WHERE project_id=%s",
            (project,),
        ).fetchone()[0]
        == 0
    )
    _oracle(db, project)


@pytest.mark.parametrize("grant_first", [False, True])
def test_acl_grant_and_flag_write_serialize_missing_binding(_database, db, grant_first):
    project, stream, topic, users = support._seed_conversation(db, user_count=1)
    user = users[0]
    _messages(db, project, stream, topic, user, 3)
    db.execute("DELETE FROM workspace_v3.topic_bindings WHERE topic_uuid=%s", (topic,))
    first_applied = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def change(grant, first):
        with psycopg.connect(conftest.TEST_DB_URL) as conn:
            if not first:
                first_applied.wait(timeout=5)
                second_started.set()
            if grant:
                conn.execute(
                    """INSERT INTO workspace_v3.topic_bindings(project_id,uuid,stream_uuid,topic_uuid,user_uuid)
                    VALUES (%s,gen_random_uuid(),%s,%s,%s)""",
                    (project, stream, topic, user),
                )
            else:
                _messages(conn, project, stream, topic, user, 2)
            if first:
                first_applied.set()
                release.wait(timeout=5)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(change, grant_first, True)
        second = pool.submit(change, not grant_first, False)
        assert second_started.wait(timeout=5)
        release.set()
        first.result(timeout=10)
        second.result(timeout=10)
    support._drain(db)
    _oracle(db, project)
    assert (
        db.execute(
            "SELECT exact_unread_count FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
            (topic,),
        ).fetchone()[0]
        == 5
    )


def test_reversed_overlapping_counter_batches_and_mode_read(_database, db):
    project, stream, topic, users = support._seed_conversation(db, user_count=2)
    topics = [topic, _topic(db, project, stream, users[0])]
    second = topics[1]
    db.execute(
        """INSERT INTO workspace_v3.topic_bindings(project_id,uuid,stream_uuid,topic_uuid,user_uuid)
        VALUES (%s,gen_random_uuid(),%s,%s,%s)""",
        (project, stream, second, users[1]),
    )
    ids = []
    for current_topic in topics:
        for user in users:
            ids += _messages(db, project, stream, current_topic, user, 2)
    scopes = [
        (project, current_topic, user) for current_topic in topics for user in users
    ]
    barrier = threading.Barrier(2)

    def project_batch(reverse):
        with psycopg.connect(conftest.TEST_DB_URL) as conn:
            barrier.wait(timeout=5)
            support.projections._update_topic_counters(
                conn, list(reversed(scopes)) if reverse else scopes
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(project_batch, value) for value in [False, True]]
        for result in results:
            result.result(timeout=10)
    for _ in range(4):
        barrier = threading.Barrier(2)

        def read_or_mode(read):
            with psycopg.connect(conftest.TEST_DB_URL) as conn:
                barrier.wait(timeout=5)
                if read:
                    conn.execute(
                        "UPDATE workspace_v3.message_flags SET read=NOT read WHERE message_uuid=ANY(%s)",
                        (ids,),
                    )
                else:
                    conn.execute(
                        "UPDATE workspace_v3.stream_bindings SET notification_mode=CASE WHEN notification_mode='all_messages' THEN 'muted' ELSE 'all_messages' END WHERE project_id=%s",
                        (project,),
                    )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(read_or_mode, value) for value in [False, True]]
            for result in results:
                result.result(timeout=10)
        support._drain(db)
        _oracle(db, project)


@pytest.mark.parametrize("make_unread", [False, True])
@pytest.mark.parametrize("repeat", range(2))
def test_concurrent_topic_move_read_mention_edit_and_delete(
    _database, db, make_unread, repeat, record_property
):
    project, stream, topic, users = support._seed_conversation(db, user_count=1)
    user = users[0]
    destination = _topic(db, project, stream, user)
    ids = _messages(db, project, stream, topic, user, 24)
    if make_unread:
        db.execute(
            "UPDATE workspace_v3.message_flags SET read=true WHERE message_uuid=ANY(%s)",
            (ids[:6],),
        )
    barrier = threading.Barrier(4)

    def change(operation):
        barrier.wait(timeout=5)
        for attempt in range(api_middlewares.DATABASE_DEADLOCK_MAX_ATTEMPTS):
            try:
                with psycopg.connect(conftest.TEST_DB_URL) as conn:
                    if operation == "move":
                        conn.execute(
                            "UPDATE workspace_v3.messages SET topic_uuid=%s WHERE uuid=ANY(%s)",
                            (destination, ids[:18]),
                        )
                    elif operation == "read":
                        conn.execute(
                            "UPDATE workspace_v3.message_flags SET read=%s WHERE message_uuid=ANY(%s)",
                            (not make_unread, ids[:6]),
                        )
                    elif operation == "mention":
                        conn.execute(
                            "UPDATE workspace_v3.message_flags SET mentioned=true WHERE message_uuid=ANY(%s)",
                            (ids[6:12],),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM workspace_v3.messages WHERE uuid=ANY(%s)",
                            (ids[18:],),
                        )
                return attempt
            except psycopg.errors.DeadlockDetected:
                if attempt + 1 == api_middlewares.DATABASE_DEADLOCK_MAX_ATTEMPTS:
                    raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = [
            pool.submit(change, op) for op in ("move", "read", "mention", "delete")
        ]
        retries = sum(result.result(timeout=10) for result in results)
    record_property("source_dml_deadlocks_retried", retries)
    support._drain(db)
    _oracle(db, project)
    assert db.execute(
        "SELECT exact_unread_count,exact_mentioned_unread_count FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
        (destination,),
    ).fetchone() == (18 if make_unread else 12, 6)


@pytest.mark.parametrize("first_topic_size", [2, 2500])
def test_existing_client_receives_stream_then_absolute_folder_correction(
    _database, db, api, first_topic_size
):
    project, stream, topic, users = support._seed_conversation(
        db, user_count=1, clear_tasks=False
    )
    user = users[0]
    second = _topic(db, project, stream, user)
    ids = _messages(db, project, stream, topic, user, first_topic_size)
    _messages(db, project, stream, second, user, 1)
    support._drain(db)
    folder = db.execute(
        "SELECT uuid FROM workspace_v3.folders WHERE project_id=%s AND user_uuid=%s AND kind='all_chats'",
        (project, user),
    ).fetchone()[0]
    watermark = db.execute(
        "SELECT COALESCE(max(epoch_version),0) FROM workspace_v3.events WHERE project_id=%s",
        (project,),
    ).fetchone()[0]
    read_ids = ids if first_topic_size == 2 else ids[:1]
    db.execute(
        "UPDATE workspace_v3.message_flags SET read=true WHERE message_uuid=ANY(%s)",
        (read_ids,),
    )
    support._drain(db)
    events = db.execute(
        """SELECT event.epoch_version,event.object_type,
            event.payload || COALESCE(recipient.payload,'{}'::jsonb)
        FROM workspace_v3.events AS event
        LEFT JOIN workspace_v3.event_recipient_payloads AS recipient
          ON recipient.event_uuid=event.uuid AND recipient.consumer_uuid=%s
        WHERE event.project_id=%s AND event.epoch_version>%s
          AND event.entity_uuid=ANY(%s) AND event.action='updated'
        ORDER BY event.epoch_version""",
        (user, project, watermark, [stream, folder]),
    ).fetchall()
    stream_events = [row for row in events if row[1] == "stream"]
    folder_events = [row for row in events if row[1] == "folder"]
    assert stream_events and folder_events
    assert stream_events[-1][2]["unread_count"] == (1 if first_topic_size == 2 else 2)
    assert folder_events[-1][2]["unread_count"] == 1
    assert folder_events[-1][0] > stream_events[-1][0]
    assert not any(
        "counter_version" in row[2] or "unread_topic_count" in row[2] for row in events
    )
    response = api.get(f"/v1/folders/{folder}", user=user, project=project)
    assert response.status_code == 200 and response.json()["unread_count"] == 1
    assert support._process(db)["claimed"] == 0


@pytest.mark.parametrize("insert_first", [False, True])
def test_new_flag_and_topic_move_serialize_missing_contribution(
    _database, db, insert_first
):
    project, stream, topic, users = support._seed_conversation(db, user_count=1)
    user = users[0]
    destination = _topic(db, project, stream, user)
    message = support._insert_message(db, project, stream, topic, user)
    first_applied = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def change(insert, first):
        with psycopg.connect(conftest.TEST_DB_URL) as conn:
            if not first:
                assert first_applied.wait(timeout=5)
                second_started.set()
            if insert:
                conn.execute(
                    "INSERT INTO workspace_v3.message_flags(project_id,stream_uuid,message_uuid,user_uuid) VALUES (%s,%s,%s,%s)",
                    (project, stream, message, user),
                )
            else:
                conn.execute(
                    "UPDATE workspace_v3.messages SET topic_uuid=%s WHERE uuid=%s",
                    (destination, message),
                )
            if first:
                first_applied.set()
                assert release.wait(timeout=5)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(change, insert_first, True)
        second = pool.submit(change, not insert_first, False)
        assert second_started.wait(timeout=5)
        release.set()
        first.result(timeout=10)
        second.result(timeout=10)
    support._drain(db)
    _oracle(db, project)
    assert (
        db.execute(
            "SELECT exact_unread_count FROM workspace_v3.topic_bindings WHERE topic_uuid=%s",
            (destination,),
        ).fetchone()[0]
        == 1
    )


def _plan_rows(plan, relation):
    count = 0
    if plan.get("Relation Name") == relation:
        count += plan["Actual Rows"] * plan["Actual Loops"]
    return count + sum(_plan_rows(child, relation) for child in plan.get("Plans", []))


def test_real_provider_retry_uses_fresh_transaction_during_native_read(
    _database, db, api, monkeypatch, record_property
):
    class ProviderClient:
        project_id = api.project_id
        user_uuid = api.user_uuid

        def post(self, path, **kwargs):
            return api.post(path, permissions=provider_support.PROVIDER_SYNC, **kwargs)

    fixture = provider_support._import_provider_conversation(ProviderClient(), db)
    destination = sys_uuid.uuid4()
    topic_data = {"stream_uuid": str(fixture["stream"]), "name": "Destination"}
    created = api.put(
        f"{provider_support.ROOT}/topics/{destination}",
        json={"content_hash": provider_support._hash(topic_data), "data": topic_data},
        permissions=provider_support.PROVIDER_SYNC,
    )
    assert created.status_code == 200
    support._drain(db)
    db.execute(
        "CREATE TABLE public.cassi_counter_retry_probe (label integer, txid bigint)"
    )
    app = conftest.build_test_wsgi_application()
    forced_collision = threading.Barrier(2)
    start = threading.Barrier(3)
    local = threading.local()
    attempts = {0: [], 1: []}
    original = provider_store.ProviderEntityStore.upsert

    def collide(store, *args, **kwargs):
        label = local.label
        txid = store.session.execute("SELECT txid_current() AS txid").fetchone()["txid"]
        attempts[label].append(txid)
        store.session.execute(
            "INSERT INTO public.cassi_counter_retry_probe VALUES (%s,%s)", (label, txid)
        )
        if len(attempts[label]) == 1:
            # Two actual PostgreSQL transactions own opposite advisory locks.
            # The probe write must roll back and the existing request must run
            # again with a fresh transaction and the original request body.
            store.session.execute("SELECT pg_advisory_xact_lock(91827,%s)", (label,))
            forced_collision.wait(timeout=5)
            store.session.execute(
                "SELECT pg_advisory_xact_lock(91827,%s)", (1 - label,)
            )
        return original(store, *args, **kwargs)

    monkeypatch.setattr(provider_store.ProviderEntityStore, "upsert", collide)
    move = {
        "stream_uuid": str(fixture["stream"]),
        "topic_uuid": str(destination),
        "author_uuid": str(fixture["owner"]),
        "payload": {"kind": "markdown", "content": "synthetic"},
    }
    edit = {
        "stream_uuid": str(fixture["stream"]),
        "message_uuid": str(fixture["message"]),
        "user_uuid": str(fixture["owner"]),
        "read": False,
        "mentioned": True,
    }

    def request(label):
        local.label = label
        if label == 2:
            path = f"/v1/stream_topics/{fixture['topic']}/actions/read/invoke"
            body = {}
        else:
            resource, entity, data = (
                ("messages", fixture["message"], move)
                if label == 0
                else ("message_flags", fixture["flag"], edit)
            )
            path = f"{provider_support.ROOT}/actions/apply/invoke"
            body = {
                "operations": [
                    {
                        "action": "upsert",
                        "type": resource,
                        "uuid": str(entity),
                        "content_hash": provider_support._hash(data),
                        "data": data,
                    }
                ]
            }
        req = webob.Request.blank(
            path,
            method="POST",
            body=json.dumps(body).encode(),
            content_type="application/json",
            headers=api._headers(permissions=provider_support.PROVIDER_SYNC),
        )
        start.wait(timeout=5)
        response = req.get_response(app)
        assert response.status_code == 200, response.status_code

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = [pool.submit(request, label) for label in range(3)]
            for result in results:
                result.result(timeout=15)
        assert sorted(map(len, attempts.values())) == [1, 2]
        assert len({txid for values in attempts.values() for txid in values}) == 3
        assert db.execute(
            "SELECT count(*),count(DISTINCT label) FROM public.cassi_counter_retry_probe"
        ).fetchone() == (2, 2)
        support._drain(db)
        _oracle(db, sys_uuid.UUID(api.project_id))
        record_property("provider_real_deadlock_retries", 1)
        record_property("provider_committed_transactions", 2)
    finally:
        db.execute("DROP TABLE public.cassi_counter_retry_probe")


def test_counter_read_projection_does_not_scan_unread_history(
    _database, db, record_property
):
    project, stream, topic, users = support._seed_conversation(
        db, user_count=1, clear_tasks=False
    )
    user = users[0]
    _messages(db, project, stream, topic, user, 10000)
    support._drain(db)
    for table in ("unread_contributions", "topic_unread_state"):
        sizes = db.execute(
            f"SELECT count(*),avg(pg_column_size(row)),pg_total_relation_size('workspace_v3.{table}') FROM workspace_v3.{table} AS row"
        ).fetchone()
        record_property(table + "_live_rows", sizes[0])
        record_property(table + "_average_row_bytes", float(sizes[1]))
        record_property(table + "_allocated_table_and_index_bytes", sizes[2])
        # A fresh synthetic copy measures current heap and indexes without
        # the dead pages left by earlier bulk-read tests in this test database.
        with db.transaction():
            db.execute(
                f"CREATE TEMP TABLE sample_{table} (LIKE workspace_v3.{table} INCLUDING ALL) ON COMMIT DROP"
            )
            db.execute(f"INSERT INTO sample_{table} SELECT * FROM workspace_v3.{table}")
            compact_bytes = db.execute(
                f"SELECT pg_total_relation_size('sample_{table}')"
            ).fetchone()[0]
            record_property(table + "_fresh_bytes_per_row", compact_bytes / sizes[0])
    plans = []

    class Explain:
        def execute(self, query, params=()):
            if (
                "raw_counter_snapshots AS MATERIALIZED" in query
                or "counter_snapshots AS MATERIALIZED" in query
                or "UPDATE workspace_v3.folders AS folder" in query
            ):
                plan = db.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params
                ).fetchone()[0][0]
                plans.append(plan)
            return db.execute(query, params)

    with db.transaction():
        support.projections._update_topic_counters(Explain(), [(project, topic, user)])
        support.projections._update_stream_counters(
            Explain(), [(project, stream, user)]
        )
        folder = db.execute(
            "SELECT uuid FROM workspace_v3.folders WHERE project_id=%s AND user_uuid=%s AND kind='all_chats'",
            (project, user),
        ).fetchone()[0]
        support.projections._update_folder_counters(
            Explain(), {(project, user, folder)}
        )
    assert len(plans) == 3
    assert _plan_rows(plans[0]["Plan"], "message_flags") == 0
    assert _plan_rows(plans[0]["Plan"], "messages") <= 2
    assert _plan_rows(plans[1]["Plan"], "topic_bindings") == 0
    assert _plan_rows(plans[2]["Plan"], "folder_items") == 0
    assert _plan_rows(plans[2]["Plan"], "stream_bindings") == 0
    record_property("topic_projection_execution_ms", plans[0]["Execution Time"])
    record_property("stream_projection_execution_ms", plans[1]["Execution Time"])
    record_property("folder_projection_execution_ms", plans[2]["Execution Time"])
    settings = db.execute(
        "SELECT proconfig FROM pg_proc WHERE oid='workspace_v3.advance_unread_baseline_page(integer)'::regprocedure"
    ).fetchone()[0]
    assert settings == ["enable_sort=off", "jit=off"]
    for table in ("message_flags", "topic_bindings"):
        db.execute(f"ANALYZE workspace_v3.{table}")
        for cursor in (
            sys_uuid.UUID(int=0),
            sys_uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
        ):
            with db.transaction():
                # Match the function-local planner settings, restored on return.
                db.execute("SET LOCAL enable_sort=off")
                db.execute("SET LOCAL jit=off")
                lock = " FOR UPDATE" if table == "message_flags" else ""
                page = db.execute(
                    f"EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT uuid FROM workspace_v3.{table} WHERE uuid>%s ORDER BY uuid LIMIT 1000{lock}",
                    (cursor,),
                ).fetchone()[0][0]
            assert _plan_rows(page["Plan"], table) <= 1000
            assert "Sort" not in str(page["Plan"])
            assert "Seq Scan" not in str(page["Plan"])
