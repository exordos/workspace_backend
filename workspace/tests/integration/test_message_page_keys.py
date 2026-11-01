# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util
import json
import uuid

import pytest
from restalchemy.common import contexts

from workspace.tests.integration import conftest
from workspace.tests.integration import test_message_view_stale_statistics as history
from workspace.tests.integration import test_projection_performance as projection


def _migration():
    path = next(conftest.MIGRATIONS_DIR.glob("0291-Select-message-page-keys-*.py"))
    spec = importlib.util.spec_from_file_location("page_keys_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(db, view, project):
    return db.execute(
        f"SELECT * FROM {view} WHERE project_id=%s ORDER BY uuid", (project,)
    ).fetchall()


@pytest.mark.isolated_schema
def test_page_keys_resume_backfill_and_preserve_messages_and_flags(api, db):
    expected = history._seed(db, api.project_id, api.user_uuid, 1001)
    migration = _migration()
    original = _rows(db, "messenger_api_user_messages_v1", api.project_id)
    with contexts.Context().session_manager() as session:
        migration.migration_step.downgrade(session)
    assert _rows(db, "messenger_api_user_messages_v1", api.project_id) == original
    migration.BATCH_BLOCKS = 1

    class Interrupted(Exception):
        pass

    class InterruptAfterCommittedBatch:
        def __init__(self, session):
            self.session = session
            self.pending = False

        def __getattr__(self, name):
            return getattr(self.session, name)

        def execute(self, statement, values=None):
            if "source.ctid" in statement:
                self.pending = True
            return self.session.execute(statement, values)

        def commit(self):
            self.session.commit()
            if self.pending:
                raise Interrupted()

    with pytest.raises(Interrupted):
        with contexts.Context().session_manager() as session:
            migration.migration_step.upgrade(InterruptAfterCommittedBatch(session))
    filled, missing = db.execute(
        "SELECT count(*) FILTER (WHERE preview_message_uuid IS NOT NULL),"
        "count(*) FILTER (WHERE preview_message_uuid IS NULL) "
        "FROM messenger_user_message_bindings WHERE project_id=%s",
        (api.project_id,),
    ).fetchone()
    assert filled > 0 and missing > 0
    # Writers can keep changing source rows and adding bindings while the
    # committed backfill is interrupted. Synchronization is already installed.
    pending = db.execute(
        "SELECT placement_uuid FROM messenger_user_message_bindings "
        "WHERE project_id=%s AND preview_message_uuid IS NULL LIMIT 1",
        (api.project_id,),
    ).fetchone()[0]
    db.execute(
        "UPDATE messenger_messages SET created_at=timestamp '2026-05-01' "
        "WHERE uuid=(SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s)",
        (pending,),
    )
    expected.update(history._seed(db, api.project_id, api.user_uuid, 1))
    original = _rows(db, "messenger_api_user_messages_v1", api.project_id)
    for _ in range(2):
        with contexts.Context().session_manager() as session:
            migration.migration_step.upgrade(session)
        assert _rows(db, "messenger_api_user_messages_v1", api.project_id) == original
        assert (
            _rows(db, "messenger_api_message_page_candidates_v1", api.project_id)
            == original
        )
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_bindings WHERE project_id=%s "
        "AND preview_message_uuid IS NOT NULL AND preview_created_at IS NOT NULL "
        "AND preview_stream_uuid IS NOT NULL AND preview_topic_uuid IS NOT NULL",
        (api.project_id,),
    ).fetchone() == (len(expected),)
    for name, _statement in migration.INDEXES:
        assert db.execute(
            "SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)", (name,)
        ).fetchone() == (True,)
    response = api.get("/v1/messenger/messages/", params={"page_limit": 100})
    assert response.status_code == 200, response.text
    assert len(response.json()) == 100


@pytest.mark.isolated_schema
def test_page_keys_follow_placement_moves_canonical_changes_and_dates(api, db):
    messages = history._seed(db, api.project_id, api.user_uuid, 2)
    placement = min(messages)
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (placement,),
    ).fetchone()[0]
    db.execute(
        "UPDATE messenger_messages SET created_at=timestamp '2026-02-01' WHERE uuid=%s",
        (canonical,),
    )
    stream = conftest.seed_user_stream(db, api.project_id, api.user_uuid, "Destination")
    topic = conftest.seed_stream_topic(
        db, api.project_id, stream, api.user_uuid, "Destination"
    )
    db.execute(
        "UPDATE messenger_message_placements SET stream_uuid=%s,topic_uuid=%s WHERE uuid=%s",
        (stream, topic, placement),
    )
    replacement = uuid.uuid4()
    db.execute(
        "INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid,created_at) "
        "VALUES(%s,%s,%s,messenger_resolve_message_content("
        '\'{"kind":"markdown","content":"Replacement"}\'::jsonb,NULL),timestamp \'2026-03-01\')',
        (replacement, api.project_id, api.user_uuid),
    )
    db.execute(
        "UPDATE messenger_message_placements SET message_uuid=%s WHERE uuid=%s",
        (replacement, placement),
    )
    db.execute(
        "UPDATE messenger_messages SET created_at=timestamp '2026-04-01' WHERE uuid=%s",
        (replacement,),
    )
    assert db.execute(
        "SELECT preview_message_uuid=%s,preview_stream_uuid=%s,preview_topic_uuid=%s,"
        "preview_created_at=timestamp '2026-04-01' "
        "FROM messenger_user_message_bindings WHERE project_id=%s AND placement_uuid=%s",
        (replacement, stream, topic, api.project_id, placement),
    ).fetchone() == (True, True, True, True)
    response = api.get(
        "/v1/messenger/messages/",
        params={"stream_uuid": stream, "topic_uuid": topic, "page_limit": 100},
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["uuid"] for row in rows] == [placement]
    assert rows[0]["payload"] == {"kind": "markdown", "content": "Replacement"}
    assert rows[0]["created_at"].startswith("2026-04-01")
    assert _rows(
        db, "messenger_api_message_page_candidates_v1", api.project_id
    ) == _rows(db, "messenger_api_user_messages_v1", api.project_id)


@pytest.mark.isolated_schema
def test_page_keys_downgrade_resumes_after_view_drop(api, db, monkeypatch):
    history._seed(db, api.project_id, api.user_uuid, 3)
    original = _rows(db, "messenger_api_user_messages_v1", api.project_id)
    migration = _migration()
    indexes = migration._indexes

    class Interrupted(Exception):
        pass

    def interrupt_before_indexes(session, *, create):
        assert not create
        # Concurrent index operations commit the preceding view drop.
        session.commit()
        raise Interrupted()

    monkeypatch.setattr(migration, "_indexes", interrupt_before_indexes)
    with pytest.raises(Interrupted):
        with contexts.Context().session_manager() as session:
            migration.migration_step.downgrade(session)
    assert db.execute(
        "SELECT to_regclass('messenger_api_message_page_candidates_v1')"
    ).fetchone() == (None,)
    monkeypatch.setattr(migration, "_indexes", indexes)
    with contexts.Context().session_manager() as session:
        migration.migration_step.downgrade(session)
    assert _rows(db, "messenger_api_user_messages_v1", api.project_id) == original
    with contexts.Context().session_manager() as session:
        migration.migration_step.upgrade(session)
    assert (
        _rows(db, "messenger_api_message_page_candidates_v1", api.project_id)
        == original
    )


@pytest.mark.isolated_schema
def test_bulk_page_keys_remain_keyed_with_stale_project_statistics(api, db):
    history._seed(db, uuid.uuid4(), uuid.uuid4(), 4096)
    for table in history.TABLES:
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
        db.execute(f"ANALYZE {table}")
    stream, _original = projection._create_message(api, "bulk-page-keys")
    db.execute("ANALYZE messenger_messages,messenger_message_placements")
    with db.transaction():
        db.execute("SET LOCAL statement_timeout='30s'")
        plan = db.execute(
            """EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON)
            WITH content AS MATERIALIZED (
                SELECT content_uuid FROM messenger_messages WHERE project_id=%s LIMIT 1
            ), messages AS (
                INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid)
                SELECT gen_random_uuid(),%s,%s,content_uuid
                FROM generate_series(1,30000) CROSS JOIN content
                RETURNING uuid,project_id,created_at
            ), placements AS (
                INSERT INTO messenger_message_placements
                    (uuid,project_id,message_uuid,stream_uuid,topic_uuid,created_at)
                SELECT gen_random_uuid(),project_id,uuid,%s,%s,created_at
                FROM messages RETURNING *
            ) INSERT INTO messenger_user_message_bindings
                    (uuid,project_id,placement_uuid,user_uuid)
              SELECT gen_random_uuid(),project_id,uuid,%s FROM placements""",
            (
                api.project_id,
                api.project_id,
                api.user_uuid,
                stream["uuid"],
                stream["default_topic_uuid"],
                api.user_uuid,
            ),
        ).fetchone()[0][0]
    # Source INSERT synchronization is bounded per statement. Per-row repair
    # multiplies lookups across the newly inserted history and caused a timeout.
    names = {
        "messenger_00_move_personal_preview_scope",
        "messenger_00_date_personal_preview_scope",
        "messenger_insert_personal_preview_scope",
        "messenger_insert_personal_preview_dates",
    }
    calls = sum(t["Calls"] for t in plan["Triggers"] if t["Trigger Name"] in names)
    assert calls <= 2, plan
    print(
        json.dumps(
            {
                "bulk_rows": 30000,
                "source_synchronization_calls": calls,
                "sql_ms": plan["Execution Time"],
            }
        )
    )
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_bindings personal "
        "JOIN messenger_message_placements placement ON placement.uuid=personal.placement_uuid "
        "JOIN messenger_messages message ON message.uuid=placement.message_uuid "
        "WHERE personal.project_id=%s AND "
        "(personal.preview_message_uuid,personal.preview_stream_uuid,personal.preview_topic_uuid,personal.preview_created_at) "
        "IS DISTINCT FROM (message.uuid,placement.stream_uuid,placement.topic_uuid,message.created_at)",
        (api.project_id,),
    ).fetchone() == (0,)
    response = api.get(
        "/v1/messenger/messages/",
        params={"topic_uuid": stream["default_topic_uuid"], "page_limit": 100},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 100


@pytest.mark.isolated_schema
@pytest.mark.parametrize("canonical_first", [False, True])
def test_page_keys_follow_deferred_source_inserts(api, db, canonical_first):
    existing = history._seed(db, api.project_id, api.user_uuid, 1)
    stream, topic, content = db.execute(
        "SELECT placement.stream_uuid,placement.topic_uuid,message.content_uuid "
        "FROM messenger_message_placements placement "
        "JOIN messenger_messages message ON message.uuid=placement.message_uuid "
        "WHERE placement.uuid=%s",
        (min(existing),),
    ).fetchone()
    placement, canonical = uuid.uuid4(), uuid.uuid4()
    message_insert = (
        "INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid,created_at) "
        "VALUES(%s,%s,%s,%s,timestamp '2026-04-01')",
        (canonical, api.project_id, api.user_uuid, content),
    )
    placement_insert = (
        "INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid) "
        "VALUES(%s,%s,%s,%s,%s)",
        (placement, api.project_id, canonical, stream, topic),
    )
    with db.transaction():
        db.execute("SET CONSTRAINTS ALL DEFERRED")
        db.execute(
            "INSERT INTO messenger_user_message_bindings(uuid,project_id,placement_uuid,user_uuid) VALUES(%s,%s,%s,%s)",
            (uuid.uuid4(), api.project_id, placement, api.user_uuid),
        )
        for statement, values in (
            (message_insert, placement_insert)
            if canonical_first
            else (placement_insert, message_insert)
        ):
            db.execute(statement, values)
    assert _rows(
        db, "messenger_api_message_page_candidates_v1", api.project_id
    ) == _rows(db, "messenger_api_user_messages_v1", api.project_id)
    response = api.get(
        "/v1/messenger/messages/", params={"topic_uuid": str(topic), "page_limit": 100}
    )
    assert response.status_code == 200, response.text
    assert placement in {uuid.UUID(row["uuid"]) for row in response.json()}
