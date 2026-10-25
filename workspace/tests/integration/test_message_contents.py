# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import concurrent.futures
import json
import uuid

import psycopg
import pytest
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native


pytestmark = pytest.mark.isolated_schema


def test_content_hashes_restore_with_an_empty_search_path(db):
    # pg_restore clears search_path before recreating stored generated columns.
    with db.transaction():
        db.execute("SET LOCAL search_path = ''")
        db.execute(
            "CREATE TEMP TABLE restored_content_hashes ("
            "workspace_content jsonb,provider_content text,"
            "workspace_hash text GENERATED ALWAYS AS "
            "(public.messenger_workspace_content_hash(workspace_content)) STORED,"
            "provider_hash text GENERATED ALWAYS AS "
            "(public.messenger_provider_content_hash(provider_content)) STORED)"
        )
        assert db.execute(
            "INSERT INTO pg_temp.restored_content_hashes VALUES ('{}','hello') "
            "RETURNING workspace_hash,provider_hash"
        ).fetchone() == (
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )


def _stream(api):
    response = api.post(native.STREAMS, json={"name": "Immutable contents"})
    assert response.status_code == 201, response.text
    return response.json()


def _message(api, stream, text):
    response = api.post(
        native.MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": text},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _current(db, message):
    return db.execute(
        "SELECT root.uuid,root.content_uuid,root.content_revision "
        "FROM messenger_messages root JOIN messenger_message_placements placement "
        "ON placement.project_id=root.project_id AND placement.message_uuid=root.uuid "
        "WHERE placement.uuid=%s",
        (message["uuid"],),
    ).fetchone()


def _edit(api, message, text):
    response = api.put(
        native.MESSAGES + message["uuid"],
        json={"payload": {"kind": "markdown", "content": text}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["payload"] == {"kind": "markdown", "content": text}


def test_identical_messages_share_contents_without_changing_public_payload(api, db):
    stream = _stream(api)
    first = _message(api, stream, "Shared content")
    second = _message(api, stream, "Shared content")
    assert first["uuid"] != second["uuid"]
    assert first["payload"] == second["payload"]
    assert "content_uuid" not in first
    assert "content_revision" not in first
    assert _current(db, first)[1] == _current(db, second)[1]
    assert db.execute("SELECT count(*) FROM messenger_message_contents").fetchone() == (
        1,
    )
    assert (
        db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='messenger_messages' AND column_name IN ('payload','content_hash')"
        ).fetchall()
        == []
    )
    native._drain()
    response = api.get(native.MESSAGES)
    assert response.status_code == 200, response.text
    assert [message["payload"] for message in response.json()] == [first["payload"]] * 2


def test_edit_and_revert_reuse_pages_and_preserve_each_transition(api, db):
    stream = _stream(api)
    message = _message(api, stream, "A")
    sibling = _message(api, stream, "A")
    canonical, content_a, _revision = _current(db, message)
    _edit(api, message, "B")
    content_b = _current(db, message)[1]
    assert content_b != content_a
    assert _current(db, sibling)[1] == content_a
    _edit(api, message, "A")
    assert _current(db, message)[1:] == (content_a, 3)
    _edit(api, message, "A")
    assert _current(db, message)[1:] == (content_a, 3)
    assert db.execute(
        "SELECT revision,content_uuid,previous_content_uuid "
        "FROM messenger_message_content_versions WHERE message_uuid=%s ORDER BY revision",
        (canonical,),
    ).fetchall() == [
        (1, content_a, None),
        (2, content_b, content_a),
        (3, content_a, content_b),
    ]
    assert db.execute("SELECT count(*) FROM messenger_message_contents").fetchone() == (
        2,
    )


def test_converging_messages_keep_distinct_previous_contents(api, db):
    stream = _stream(api)
    first = _message(api, stream, "First predecessor")
    second = _message(api, stream, "Second predecessor")
    first_root, first_page, _revision = _current(db, first)
    second_root, second_page, _revision = _current(db, second)
    _edit(api, first, "Shared result")
    _edit(api, second, "Shared result")
    shared = _current(db, first)[1]
    assert _current(db, second)[1] == shared
    assert set(
        db.execute(
            "SELECT message_uuid,content_uuid,previous_content_uuid "
            "FROM messenger_message_content_versions WHERE revision=2"
        ).fetchall()
    ) == {
        (first_root, shared, first_page),
        (second_root, shared, second_page),
    }
    assert db.execute("SELECT count(*) FROM messenger_message_contents").fetchone() == (
        3,
    )


def test_concurrent_content_resolution_uses_one_row(_test_database_url, db):
    payload = json.dumps({"kind": "markdown", "content": "Concurrent shared content"})

    def resolve(_index):
        with psycopg.connect(_test_database_url) as connection:
            return connection.execute(
                "SELECT messenger_resolve_message_content(%s::jsonb,NULL)", (payload,)
            ).fetchone()[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        identifiers = list(pool.map(resolve, range(8)))
    assert len(set(identifiers)) == 1
    assert db.execute("SELECT count(*) FROM messenger_message_contents").fetchone() == (
        1,
    )


def test_content_hashes_cover_the_whole_payload_and_distinguish_null_provider(db):
    payload = {"kind": "markdown", "content": "Same bytes"}
    identifiers = []
    for workspace, provider in [
        (payload, None),
        (payload, ""),
        (payload, "Provider bytes"),
        ({**payload, "kind": "other"}, None),
    ]:
        identifiers.append(
            db.execute(
                "SELECT messenger_resolve_message_content(%s::jsonb,%s)",
                (json.dumps(workspace), provider),
            ).fetchone()[0]
        )
    assert len(set(identifiers)) == 4
    assert db.execute(
        "SELECT workspace_content,provider_content,workspace_content_hash,provider_content_hash "
        "FROM messenger_message_contents WHERE uuid=%s",
        (identifiers[0],),
    ).fetchone() == (
        payload,
        None,
        db.execute(
            "SELECT messenger_workspace_content_hash(%s::jsonb)", (json.dumps(payload),)
        ).fetchone()[0],
        None,
    )


def test_contents_are_immutable_and_unreferenced_pages_are_retained(api, db):
    message = _message(api, _stream(api), "Keep this page")
    canonical, content, _revision = _current(db, message)
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        with db.transaction():
            db.execute(
                "UPDATE messenger_message_contents SET provider_content='changed' WHERE uuid=%s",
                (content,),
            )
    db.execute("DELETE FROM messenger_messages WHERE uuid=%s", (canonical,))
    assert db.execute(
        "SELECT workspace_content FROM messenger_message_contents WHERE uuid=%s",
        (content,),
    ).fetchone() == ({"kind": "markdown", "content": "Keep this page"},)
    assert db.execute(
        "SELECT count(*) FROM messenger_message_content_versions"
    ).fetchone() == (0,)


def test_hash_collision_never_reuses_different_content(db):
    db.execute("""CREATE OR REPLACE FUNCTION messenger_workspace_content_hash(p_workspace jsonb)
        RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT repeat('a',64) $$""")
    first = json.dumps({"kind": "markdown", "content": "First"})
    second = json.dumps({"kind": "markdown", "content": "Different"})
    identifier = db.execute(
        "SELECT messenger_resolve_message_content(%s::jsonb,NULL)", (first,)
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation, match="collision"):
        with db.transaction():
            db.execute(
                "SELECT messenger_resolve_message_content(%s::jsonb,NULL)", (second,)
            )
    assert db.execute(
        "SELECT workspace_content FROM messenger_message_contents WHERE uuid=%s",
        (identifier,),
    ).fetchone() == (json.loads(first),)


def test_provider_representation_changes_preserve_prior_pages(api, db):
    message = _message(api, _stream(api), "Same Workspace payload")
    canonical, initial, _revision = _current(db, message)
    before = api.get(native.MESSAGES + message["uuid"]).json()
    pages = [initial]
    for provider in ("First provider spelling", "Second provider spelling"):
        page = db.execute(
            "SELECT messenger_resolve_message_content(%s::jsonb,%s)",
            (json.dumps(message["payload"]), provider),
        ).fetchone()[0]
        db.execute(
            "UPDATE messenger_messages SET content_uuid=%s WHERE uuid=%s",
            (page, canonical),
        )
        pages.append(page)
        assert api.get(native.MESSAGES + message["uuid"]).json() == before
    assert _current(db, message)[2] == 3
    assert db.execute(
        "SELECT revision,content_uuid,previous_content_uuid "
        "FROM messenger_message_content_versions WHERE message_uuid=%s ORDER BY revision",
        (canonical,),
    ).fetchall() == [
        (1, pages[0], None),
        (2, pages[1], pages[0]),
        (3, pages[2], pages[1]),
    ]


def test_populated_upgrade_preserves_messages_snapshots_and_prepared_request():
    predecessor = next(conftest.MIGRATIONS_DIR.glob("0272-*.py")).name
    engine = migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR))
    project, owner, provider, account, first, second, delivery, snapshot = (
        uuid.uuid4() for _ in range(8)
    )
    payload = {"kind": "markdown", "content": "Shared before upgrade"}
    provider_text = "Shared **before** upgrade"
    prepared = {
        "kind": "send",
        "method": "POST",
        "path": "/messages",
        "data": {
            "content": provider_text,
            "local_id": str(first),
            "queue_id": "old-queue",
        },
        "delivery_uuid": str(delivery),
        "last_event_id": 17,
    }
    with conftest.isolated_migration_database(predecessor) as database_url:
        with psycopg.connect(database_url, autocommit=True) as db:
            conftest.seed_workspace_user(db, owner, "contents-upgrade")
            stream = conftest.seed_user_stream(db, project, owner, "Upgrade")
            db.execute(
                "INSERT INTO messenger_providers(uuid,hostname) VALUES(%s,'contents-upgrade.test')",
                (provider,),
            )
            db.execute(
                "INSERT INTO m_external_accounts_v2(uuid,project_id,owner_user_uuid,provider,"
                "provider_uuid,external_account_id,settings,status) "
                "VALUES(%s,%s,%s,'zulip',%s,'7','{}','active')",
                (account, project, owner, provider),
            )
            for message in (first, second):
                db.execute(
                    "INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload,"
                    "source_name,provider_uuid,provider_identity,content_updated_at) "
                    "VALUES(%s,%s,%s,%s::jsonb,'zulip',%s,%s,'2026-10-01 01:02:03')",
                    (
                        message,
                        project,
                        owner,
                        json.dumps(payload),
                        provider,
                        str(message),
                    ),
                )
            db.execute(
                "INSERT INTO bridge_zulip_v5_deliveries(uuid,project_id,message_uuid,stream_uuid,"
                "external_account_uuid,hostname,external_account_id,recipient,sending) "
                "VALUES(%s,%s,%s,%s,%s,'contents-upgrade.test','7','{}',true)",
                (delivery, project, first, stream, account),
            )
            db.execute(
                "INSERT INTO bridge_zulip_v5_content_snapshots"
                "(uuid,delivery_uuid,workspace_content,zulip_content) VALUES(%s,%s,%s,%s)",
                (snapshot, delivery, payload["content"], provider_text),
            )
            db.execute(
                "INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,"
                "ordering_key,external_account_uuid,operation,data,status,request) "
                "VALUES(%s,'message',%s,%s,%s,'insert',%s::jsonb,'checking',%s::jsonb)",
                (
                    project,
                    first,
                    "message:" + str(first),
                    account,
                    json.dumps({"payload": payload}),
                    json.dumps(prepared),
                ),
            )
            before = db.execute(
                "SELECT uuid,payload,content_updated_at,created_at,updated_at,provider_identity "
                "FROM messenger_messages ORDER BY uuid"
            ).fetchall()
            engine.apply_migration(engine.get_latest_migration())
            assert (
                db.execute(
                    "SELECT uuid,payload,content_updated_at,created_at,updated_at,provider_identity "
                    "FROM messenger_messages_with_content_v1 ORDER BY uuid"
                ).fetchall()
                == before
            )
            assert db.execute(
                "SELECT contents.workspace_content,contents.provider_content "
                "FROM bridge_zulip_v5_content_snapshots snapshot "
                "JOIN messenger_message_contents contents ON contents.uuid=snapshot.content_uuid "
                "WHERE snapshot.uuid=%s",
                (snapshot,),
            ).fetchone() == (payload, provider_text)
            assert db.execute(
                "SELECT message.content_uuid=snapshot.content_uuid FROM messenger_messages message "
                "JOIN bridge_zulip_v5_content_snapshots snapshot ON snapshot.uuid=%s "
                "WHERE message.uuid=%s",
                (snapshot, first),
            ).fetchone() == (True,)
            assert db.execute(
                "SELECT request,data->>'content_revision' FROM zulip_outgoing_intents "
                "WHERE entity_uuid=%s",
                (first,),
            ).fetchone() == (prepared, "1")
            assert db.execute(
                "SELECT revision,previous_content_uuid FROM messenger_message_content_versions"
            ).fetchall() == [(1, None), (1, None)]
