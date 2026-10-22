# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Reverse entity lookup must follow canonical writes without projection jobs."""

import base64
import urllib.parse
import uuid

import psycopg.types.json
import pytest
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest


@pytest.mark.parametrize(
    "kind",
    [
        "user",
        "stream",
        "topic",
        "message",
        "quote",
        "file",
        "image",
        "video",
        "forward",
        "custom_entity",
    ],
)
def test_reference_index_extracts_entity_urns_without_duplicates(db, kind):
    target = f"urn:{kind}:{uuid.uuid4()}"
    payload = {
        "kind": "markdown",
        "content": f"[first]({target.upper()}) [second]({target}?name=x)",
    }
    row = db.execute(
        "SELECT messenger_message_references(%s)",
        (psycopg.types.json.Jsonb(payload),),
    ).fetchone()
    assert row[0] == [target]


def test_reference_index_includes_nested_forward_and_selected_quote_text(db):
    target = f"urn:topic:{uuid.uuid4()}"
    selected = urllib.parse.urlencode({"text": f"[topic]({target})"})
    quoted = f"[selection](urn:quote:{uuid.uuid4()}?{selected})"
    snapshot = base64.urlsafe_b64encode(quoted.encode()).decode().rstrip("=")
    payload = {
        "kind": "markdown",
        "content": f"[forward](urn:forward:{uuid.uuid4()}?snapshot={snapshot})",
    }
    row = db.execute(
        "SELECT messenger_message_references(%s)",
        (psycopg.types.json.Jsonb(payload),),
    ).fetchone()
    assert target in row[0]


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "e2ee", "content": "urn:topic:00000000-0000-0000-0000-000000000000"},
        {
            "kind": "markdown",
            "content": "[bad](urn:forward:00000000-0000-0000-0000-000000000000?snapshot=!)",
        },
        {
            "kind": "markdown",
            "content": "[bad](urn:quote:00000000-0000-0000-0000-000000000000?text=%FF%XX)",
        },
    ],
)
def test_reference_index_tolerates_opaque_or_malformed_embedded_content(db, payload):
    row = db.execute(
        "SELECT messenger_message_references(%s)",
        (psycopg.types.json.Jsonb(payload),),
    ).fetchone()
    if payload["kind"] == "e2ee":
        assert row[0] == []


def test_reference_index_tracks_http_create_edit_and_delete(api, db):
    stream = api.post(
        "/v1/messenger/streams/",
        json={
            "name": "References",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream.status_code == 201, stream.text
    topic = api.post(
        "/v1/messenger/stream_topics/",
        json={
            "stream_uuid": stream.json()["uuid"],
            "name": "References",
            "source": {"kind": "native"},
        },
    )
    assert topic.status_code == 201, topic.text
    target = f"urn:topic:{topic.json()['uuid']}"
    created = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream.json()["uuid"],
            "topic_uuid": topic.json()["uuid"],
            "payload": {"kind": "markdown", "content": f"[topic]({target})"},
        },
    )
    assert created.status_code == 201, created.text
    path = f"/v1/messenger/messages/{created.json()['uuid']}"

    def matches(project):
        return db.execute(
            "SELECT uuid FROM messenger_messages WHERE project_id=%s AND deleted_at IS NULL "
            "AND messenger_message_references(payload) @> %s::text[]",
            (project, [target]),
        ).fetchall()

    assert len(matches(api.project_id)) == 1
    assert matches(uuid.uuid4()) == []
    # With a one-message project, its PK is naturally cheaper than the GIN.
    # Exercise selective lookup against unrelated history in the same project.
    unrelated = db.execute(
        "INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload) "
        'SELECT gen_random_uuid(),%s,%s,\'{"kind":"markdown","content":"Unrelated history"}\'::jsonb '
        "FROM generate_series(1,2000) RETURNING uuid",
        (api.project_id, api.user_uuid),
    ).fetchall()
    try:
        db.execute("ANALYZE messenger_messages")
        plan = db.execute(
            "EXPLAIN (COSTS OFF) SELECT uuid FROM messenger_messages "
            "WHERE project_id=%s AND deleted_at IS NULL "
            "AND messenger_message_references(payload) @> %s::text[]",
            (api.project_id, [target]),
        ).fetchall()
        assert "messenger_messages_entity_references_idx" in "\n".join(
            row[0] for row in plan
        )
    finally:
        db.execute(
            "DELETE FROM messenger_messages WHERE project_id=%s AND uuid=ANY(%s)",
            (api.project_id, [row[0] for row in unrelated]),
        )
    edited = api.put(
        path, json={"payload": {"kind": "markdown", "content": "Link removed"}}
    )
    assert edited.status_code == 200, edited.text
    assert matches(api.project_id) == []
    edited = api.put(
        path, json={"payload": {"kind": "markdown", "content": f"[restored]({target})"}}
    )
    assert edited.status_code == 200, edited.text
    assert len(matches(api.project_id)) == 1
    assert api.delete(path).status_code == 204
    assert matches(api.project_id) == []


@pytest.mark.isolated_schema
def test_reference_index_migration_indexes_existing_content_and_is_reversible(
    _database,
):
    previous = (
        "0261-Reconvert-Zulip-quotes-with-recipient-and-channel-context-1adfa4.py"
    )
    migration = (
        "0263-Index-Workspace-entity-references-in-canonical-message-content-4272f7.py"
    )
    with conftest.isolated_migration_database(previous) as url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        with psycopg.connect(url, autocommit=True) as connection:
            user, project, message = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conftest.seed_workspace_user(
                connection, user, f"reference-migration-{user}"
            )
            connection.execute(
                "SELECT messenger_v2_register_project_user(%s,%s)", (project, user)
            )
            target = f"urn:topic:{uuid.uuid4()}"
            connection.execute(
                "INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload) VALUES(%s,%s,%s,%s)",
                (
                    message,
                    project,
                    user,
                    psycopg.types.json.Jsonb(
                        {"kind": "markdown", "content": f"[history]({target})"}
                    ),
                ),
            )
            before = connection.execute(
                "SELECT payload,content_hash,content_updated_at FROM messenger_messages WHERE uuid=%s",
                (message,),
            ).fetchone()
            engine.apply_migration(migration)
            assert connection.execute(
                "SELECT uuid FROM messenger_messages WHERE project_id=%s AND deleted_at IS NULL "
                "AND messenger_message_references(payload) @> %s::text[]",
                (project, [target]),
            ).fetchall() == [(message,)]
            assert (
                connection.execute(
                    "SELECT payload,content_hash,content_updated_at FROM messenger_messages WHERE uuid=%s",
                    (message,),
                ).fetchone()
                == before
            )
            engine.rollback_migration(migration)
            assert connection.execute(
                "SELECT to_regclass('messenger_messages_entity_references_idx')"
            ).fetchone() == (None,)
            engine.apply_migration(migration)
