# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Native Messenger v2 cutover tests through the unchanged HTTP contract."""

import concurrent.futures
import datetime
import itertools
import json
import threading
import urllib.parse
import uuid as sys_uuid

import psycopg
import pytest
from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.common import contexts
from restalchemy.storage.sql import engines
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.common import database
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.messenger_api.dm import models as messenger_models
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/messenger/streams/"
STREAM_BINDINGS = f"{V1}/messenger/stream_bindings/"
STREAM_TOPICS = f"{V1}/messenger/stream_topics/"
MESSAGES = f"{V1}/messenger/messages/"
MESSAGE_REACTIONS = f"{V1}/messenger/message_reactions/"
FOLDERS = f"{V1}/messenger/folders/"
FOLDER_ITEMS = f"{V1}/messenger/folder_items/"
FILES = f"{V1}/messenger/files/"
DRAFTS = f"{V1}/messenger/drafts/"
EVENTS = f"{V1}/events/"
TOMBSTONE_MIRROR_MIGRATION = "0188-Skip-tombstoned-streams-in-binding-mirrors-fd5982.py"


def _truncate_messenger_test_data():
    with contexts.Context().session_manager() as session:
        session.execute("""
            CREATE TEMP TABLE messenger_v2_test_system_users
                (LIKE m_workspace_users INCLUDING ALL) ON COMMIT DROP;
            INSERT INTO messenger_v2_test_system_users SELECT * FROM m_workspace_users
                WHERE uuid='00000000-0000-0000-0000-000000000000';
            TRUNCATE TABLE m_workspace_users, m_workspace_event_audience_snapshots_v1,
                m_external_accounts_v2 RESTART IDENTITY CASCADE;
            INSERT INTO m_workspace_users SELECT * FROM messenger_v2_test_system_users;
        """)


@pytest.fixture(scope="module", autouse=True)
def _isolate_v2_module(_database):
    """Fence this module's data without rolling back the current API schema."""
    _truncate_messenger_test_data()
    try:
        yield
    finally:
        _truncate_messenger_test_data()


@pytest.fixture(autouse=True)
def _production_v2_store():
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(store_factory.build_store_factory())


@pytest.fixture
def migration_engine(_database):
    """Restore HEAD after checking a historical schema, even on failure."""
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    try:
        yield engine
    finally:
        _truncate_messenger_test_data()
        engine.apply_migration(engine.get_latest_migration())


@pytest.fixture(autouse=True)
def _isolate_projection_queue(db):
    """Keep each v2 worker scenario independent from earlier scale fixtures."""
    with db.cursor() as cursor:
        cursor.execute(
            """
            DO $cleanup$
            BEGIN
                IF to_regclass('messenger_projection_scope_leases') IS NOT NULL THEN
                    DELETE FROM messenger_projection_scope_leases;
                END IF;
                IF to_regclass('messenger_domain_outbox_events') IS NOT NULL THEN
                    DELETE FROM messenger_domain_outbox_events;
                END IF;
            END
            $cleanup$;
            """
        )


def _drain() -> int:
    with contexts.Context().session_manager() as session:
        processed = v2_projection.drain_projection_queue(
            session,
            f"integration:{sys_uuid.uuid4()}",
        )
        failures = session.execute(
            """
            SELECT task_kind, status, last_error
            FROM messenger_projection_tasks
            WHERE status IN ('failed', 'dead_letter')
            ORDER BY created_at, uuid
            """,
            (),
        ).fetchall()
        assert failures == []
        return processed


def _register_project_user(db, project_uuid, user_uuid):
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT messenger_v2_register_project_user(%s, %s)",
            (project_uuid, user_uuid),
        )


def test_first_request_materializes_iam_user_before_native_write(http_server, db):
    user_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    client = conftest.ApiClient(http_server, user_uuid, project_uuid)

    response = client.post(
        STREAMS,
        json={
            "name": "First request",
            "description": "",
            "source_name": "native",
        },
    )

    assert response.status_code == 201, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT workspace_user.username, project_user.user_uuid
            FROM m_workspace_users AS workspace_user
            JOIN messenger_project_users AS project_user
              ON project_user.user_uuid = workspace_user.uuid
            WHERE workspace_user.uuid = %s AND project_user.project_id = %s
            """,
            (user_uuid, project_uuid),
        )
        assert cursor.fetchone() == (f"user-{user_uuid}", user_uuid)


def test_concurrent_first_requests_share_one_iam_user_import(db, monkeypatch):
    user_uuid = sys_uuid.uuid4()
    username = f"user-{user_uuid}"
    first_lookup = threading.Barrier(2)
    thread_state = threading.local()
    objects = messenger_models.WorkspaceUser.objects
    original_get_one_or_none = objects.get_one_or_none

    def synchronized_get_one_or_none(*args, **kwargs):
        result = original_get_one_or_none(*args, **kwargs)
        if not getattr(thread_state, "initial_lookup_complete", False):
            thread_state.initial_lookup_complete = True
            first_lookup.wait(timeout=5)
        return result

    monkeypatch.setattr(objects, "get_one_or_none", synchronized_get_one_or_none)

    def import_user():
        with contexts.Context().session_manager():
            return messenger_models.WorkspaceUser.sync_iam_identity(
                user_uuid=user_uuid,
                username=username,
                first_name=None,
                last_name=None,
                email=None,
            ).uuid

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: import_user(), range(2)))

    assert results == (user_uuid, user_uuid)
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM m_workspace_users WHERE uuid = %s",
            (user_uuid,),
        )
        assert cursor.fetchone()[0] == 1


@pytest.mark.parametrize("timezone", ("UTC", "Europe/Moscow", "America/New_York"))
def test_native_message_timestamps_use_utc(api, timezone):
    parts = urllib.parse.urlsplit(conftest.TEST_DB_URL)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["options"] = f"{query.get('options', '')} -c timezone={timezone}".strip()
    connection_url = urllib.parse.urlunsplit(
        parts._replace(
            query=urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        )
    )
    with psycopg.connect(connection_url) as connection:
        assert connection.execute("SHOW TIME ZONE").fetchone() == (timezone,)

    conf = cfg.ConfigOpts()
    ra_config_opts.register_posgresql_db_opts(conf)
    conf.set_override("connection_url", connection_url, group="db")
    engines.engine_factory.destroy_all_engines()
    try:
        database.configure_postgresql("workspace-api", conf=conf)
        response = api.post(STREAMS, json={"name": "UTC timestamps"})
        assert response.status_code == 201, response.text
        stream = response.json()

        before = datetime.datetime.now(datetime.timezone.utc)
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "payload": {"kind": "markdown", "content": "UTC timestamp check"},
            },
        )
        after = datetime.datetime.now(datetime.timezone.utc)
        assert response.status_code == 201, response.text
        message = response.json()
        for field in ("created_at", "updated_at"):
            value = datetime.datetime.fromisoformat(
                message[field].replace("Z", "+00:00")
            )
            assert before <= value <= after

        before = datetime.datetime.now(datetime.timezone.utc)
        response = api.put(
            f"{MESSAGES}{message['uuid']}",
            json={"payload": {"kind": "markdown", "content": "Edited UTC check"}},
        )
        after = datetime.datetime.now(datetime.timezone.utc)
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["created_at"] == message["created_at"]
        assert (
            before
            <= datetime.datetime.fromisoformat(
                updated["updated_at"].replace("Z", "+00:00")
            )
            <= after
        )
        assert api.get(f"{MESSAGES}{message['uuid']}").json() == updated
        assert _drain() >= 1
    finally:
        engines.engine_factory.destroy_all_engines()
        engines.engine_factory.configure_factory(db_url=conftest.TEST_DB_URL)


def test_native_v2_cutover_preserves_v1_http_and_event_contract(api, workspace_api, db):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)

    response = api.post(
        STREAMS,
        json={
            "name": "Native v2",
            "description": "Canonical native Messenger",
            "source_name": "native",
        },
    )
    assert response.status_code == 201, response.text
    stream = response.json()
    assert stream.get("provider_uuid") is None
    assert _drain() >= 3

    response = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["user_uuid"] == str(peer_uuid)
    assert _drain() >= 3

    response = api.post(
        STREAM_TOPICS,
        json={
            "stream_uuid": stream["uuid"],
            "name": "Architecture",
        },
    )
    assert response.status_code == 201, response.text
    topic = response.json()
    assert _drain() >= 1

    canonical_uuid = sys_uuid.uuid4()
    response = api.post(
        MESSAGES,
        json={
            "uuid": str(canonical_uuid),
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic["uuid"],
            "payload": {
                "kind": "markdown",
                "content": f"Hello [peer](urn:user:{peer_uuid})",
            },
        },
    )
    assert response.status_code == 201, response.text
    message = response.json()
    expected_public_uuid = sys_uuid.uuid5(
        sys_uuid.UUID(topic["uuid"]),
        str(canonical_uuid),
    )
    assert message["uuid"] == str(expected_public_uuid)
    assert message["read"] is True

    pending_peer = api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid)
    assert pending_peer.status_code == 404, pending_peer.text
    assert _drain() >= 2
    peer_message = api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid)
    assert peer_message.status_code == 200, peer_message.text
    assert peer_message.json()["mentioned"] is True
    assert peer_message.json()["read"] is False

    reaction_uuid = sys_uuid.uuid4()
    response = api.post(
        MESSAGE_REACTIONS,
        json={
            "uuid": str(reaction_uuid),
            "message_uuid": message["uuid"],
            "emoji_name": "thumbs_up",
        },
    )
    assert response.status_code == 201, response.text
    assert _drain() >= 1
    reloaded = api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid)
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["reactions"] == {"thumbs_up": 1}

    response = api.post(
        f"{MESSAGES}{message['uuid']}/actions/read/invoke",
        user=peer_uuid,
    )
    assert response.status_code == 200, response.text
    assert response.json()["read"] is True
    assert _drain() >= 1

    response = api.put(
        f"{MESSAGES}{message['uuid']}",
        json={"payload": {"kind": "markdown", "content": "Edited"}},
    )
    assert response.status_code == 200, response.text
    assert _drain() >= 2
    assert (
        api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid).json()["payload"][
            "content"
        ]
        == "Edited"
    )

    events = workspace_api.get(EVENTS, params={"page_limit": 100}, user=peer_uuid)
    assert events.status_code == 200, events.text
    kinds = [event["payload"]["kind"] for event in events.json()]
    assert "stream.created" in kinds
    assert "topic.created" in kinds
    assert "message.created" in kinds
    assert "message.updated" in kinds
    assert "message.read" in kinds

    response = api.delete(f"{MESSAGES}{message['uuid']}")
    assert response.status_code == 204, response.text
    assert _drain() >= 1
    assert api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid).status_code == 404

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM messenger_projection_tasks
            WHERE project_id = %s AND status <> 'completed'
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT count(*) FROM messenger_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, canonical_uuid),
        )
        assert cursor.fetchone()[0] == 0


def test_native_v2_topic_deletion_drains_derived_projections(api, db):
    response = api.post(STREAMS, json={"name": "Topic deletion projections"})
    assert response.status_code == 201, response.text
    stream = response.json()
    assert _drain() >= 1
    topic_uuid = stream["default_topic_uuid"]

    response = api.delete(f"{STREAM_TOPICS}{topic_uuid}")
    assert response.status_code == 204, response.text
    assert _drain() >= 1

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM messenger_topics WHERE project_id = %s AND uuid = %s",
            (api.project_id, topic_uuid),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT count(*) FROM messenger_projection_tasks "
            "WHERE project_id = %s AND status <> 'completed'",
            (api.project_id,),
        )
        assert cursor.fetchone() == (0,)


@pytest.mark.parametrize("resource", ("message", "topic"))
def test_native_v2_deletion_before_stream_purge_drains_snapshots(
    api, workspace_api, db, resource
):
    response = api.post(STREAMS, json={"name": "Delivery snapshot deletion"})
    assert response.status_code == 201, response.text
    stream = response.json()
    topic_uuid = stream["default_topic_uuid"]
    assert _drain() >= 1
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "Synthetic last message"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    assert _drain() >= 1
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT last_message_uuid FROM messenger_stream_bindings "
            "WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s",
            (api.project_id, stream["uuid"], api.user_uuid),
        )
        assert cursor.fetchone() == (sys_uuid.UUID(message_uuid),)
    if resource == "message":
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE messenger_topics SET summary = 'Synthetic summary', "
                "summary_last_message_uuid = %s WHERE project_id = %s AND uuid = %s",
                (message_uuid, api.project_id, topic_uuid),
            )

    target = (
        f"{MESSAGES}{message_uuid}"
        if resource == "message"
        else f"{STREAM_TOPICS}{topic_uuid}"
    )
    response = api.delete(target)
    assert response.status_code == 204, response.text
    response = api.delete(f"{STREAMS}{stream['uuid']}")
    assert response.status_code == 204, response.text
    assert api.get(target).status_code == 404
    assert api.get(f"{STREAMS}{stream['uuid']}").status_code == 404

    assert _drain() >= 1

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT task_kind, payload->>'source_kind', status, attempts,
                   next_retry_at, lease_expires_at, last_error
            FROM messenger_projection_tasks
            WHERE project_id = %s AND status <> 'completed'
            ORDER BY created_at, uuid
            """,
            (api.project_id,),
        )
        assert cursor.fetchall() == []
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM messenger_streams WHERE project_id = %s),
                (SELECT count(*) FROM messenger_topics WHERE project_id = %s),
                (SELECT count(*) FROM messenger_messages WHERE project_id = %s)
            """,
            (api.project_id,) * 3,
        )
        assert cursor.fetchone() == (0, 0, 0)

    response = workspace_api.get(EVENTS, user=api.user_uuid, project=api.project_id)
    assert response.status_code == 200, response.text
    kinds = {event["payload"]["kind"] for event in response.json()}
    assert {"stream.deleted", "folder.updated"} <= kinds


def test_native_v2_deleted_message_supersedes_pending_projection_work(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Deleted pending projection",
            "description": "",
            "source_name": "native",
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    assert _drain() >= 1

    topic_response = api.post(
        STREAM_TOPICS,
        json={
            "stream_uuid": stream["uuid"],
            "name": "Delete before fanout",
        },
    )
    assert topic_response.status_code == 201, topic_response.text
    topic = topic_response.json()
    assert _drain() >= 1

    message_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic["uuid"],
            "payload": {"kind": "markdown", "content": "create"},
        },
    )
    assert message_response.status_code == 201, message_response.text
    message_uuid = message_response.json()["uuid"]
    updated = api.put(
        f"{MESSAGES}{message_uuid}",
        json={"payload": {"kind": "markdown", "content": "updated"}},
    )
    assert updated.status_code == 200, updated.text
    deleted = api.delete(f"{MESSAGES}{message_uuid}")
    assert deleted.status_code == 204, deleted.text

    assert _drain() >= 3
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND status <> 'completed'
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == 0


def test_native_v2_direct_stream_is_idempotent_and_closed(api, db):
    peer_uuid = sys_uuid.uuid4()
    third_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    conftest.seed_workspace_user(db, third_uuid, f"user-{third_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    _register_project_user(db, api.project_id, third_uuid)

    body = {
        "name": "Direct",
        "description": "",
        "source_name": "native",
        "direct_user_uuid": str(peer_uuid),
    }
    first = api.post(STREAMS, json=body)
    assert first.status_code == 201, first.text
    second = api.post(STREAMS, json=body)
    assert second.status_code in (200, 201), second.text
    assert second.json()["uuid"] == first.json()["uuid"]

    reciprocal = api.post(
        STREAMS,
        user=peer_uuid,
        json={**body, "direct_user_uuid": str(api.user_uuid)},
    )
    assert reciprocal.status_code in (200, 201), reciprocal.text
    assert reciprocal.json()["uuid"] == first.json()["uuid"]
    assert reciprocal.json()["direct_user_uuid"] == str(api.user_uuid)

    stream_uuid = first.json()["uuid"]
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT uuid, user_uuid, role FROM messenger_stream_bindings "
            "WHERE project_id = %s AND stream_uuid = %s AND active ORDER BY user_uuid",
            (api.project_id, stream_uuid),
        )
        bindings = cursor.fetchall()
    assert {str(row[1]) for row in bindings} == {api.user_uuid, str(peer_uuid)}
    assert all(row[2] == "owner" for row in bindings)

    for actor in (api.user_uuid, peer_uuid):
        rejected = api.post(
            f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
            user=actor,
            json={"member": [str(third_uuid)]},
        )
        assert rejected.status_code == 400, rejected.text
        for binding_uuid, _user_uuid, _role in bindings:
            rejected = api.put(
                f"{STREAM_BINDINGS}{binding_uuid}",
                user=actor,
                json={"role": "member"},
            )
            assert rejected.status_code == 400, rejected.text
            rejected = api.delete(f"{STREAM_BINDINGS}{binding_uuid}", user=actor)
            assert rejected.status_code == 400, rejected.text

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT uuid, user_uuid, role FROM messenger_stream_bindings "
            "WHERE project_id = %s AND stream_uuid = %s AND active ORDER BY user_uuid",
            (api.project_id, stream_uuid),
        )
        assert cursor.fetchall() == bindings


@pytest.mark.parametrize("peer", ({}, {"direct_user_uuid": None}))
def test_native_v2_private_stream_requires_peer(api, db, peer):
    stream_uuid = sys_uuid.uuid4()
    body = {
        "uuid": str(stream_uuid),
        "name": "Invalid private stream",
        "description": "",
        "private": True,
        "source_name": "native",
        **peer,
    }
    for _ in range(2):
        response = api.post(STREAMS, json=body)
        assert response.status_code == 400, response.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM messenger_streams WHERE uuid = %s",
            (stream_uuid,),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT count(*) FROM messenger_stream_bindings WHERE stream_uuid = %s",
            (stream_uuid,),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT count(*) FROM messenger_projection_tasks WHERE project_id = %s",
            (api.project_id,),
        )
        assert cursor.fetchone() == (0,)


@pytest.mark.parametrize("with_peer", (False, True))
def test_native_v2_ordinary_stream_cannot_be_converted_to_private(api, with_peer):
    response = api.post(STREAMS, json={"name": "Ordinary stream"})
    assert response.status_code == 201, response.text
    stream = response.json()
    update = {"name": "Private conversion", "private": True}
    if with_peer:
        update["direct_user_uuid"] = api.user_uuid
    response = api.put(f"{STREAMS}{stream['uuid']}", json=update)
    assert response.status_code == 400, response.text
    reloaded = api.get(f"{STREAMS}{stream['uuid']}")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["name"] == stream["name"]
    assert reloaded.json()["private"] is False
    assert reloaded.json().get("direct_user_uuid") is None


def test_native_v2_legacy_private_stream_without_peer_rejects_membership_changes(
    api, db
):
    response = api.post(STREAMS, json={"name": "Legacy invalid private stream"})
    assert response.status_code == 201, response.text
    stream_uuid = response.json()["uuid"]
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE messenger_streams SET private = true "
            "WHERE project_id = %s AND uuid = %s",
            (api.project_id, stream_uuid),
        )
        cursor.execute(
            "SELECT uuid FROM messenger_stream_bindings "
            "WHERE project_id = %s AND stream_uuid = %s",
            (api.project_id, stream_uuid),
        )
        binding_uuid = cursor.fetchone()[0]
    third_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, third_uuid, f"user-{third_uuid}")
    _register_project_user(db, api.project_id, third_uuid)

    added = api.post(
        f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
        json={"member": [str(third_uuid)]},
    )
    assert added.status_code == 400, added.text
    updated = api.put(f"{STREAM_BINDINGS}{binding_uuid}", json={"role": "member"})
    assert updated.status_code == 400, updated.text
    deleted = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert deleted.status_code == 400, deleted.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT uuid, role FROM messenger_stream_bindings "
            "WHERE project_id = %s AND stream_uuid = %s AND active",
            (api.project_id, stream_uuid),
        )
        assert cursor.fetchall() == [(binding_uuid, "owner")]
        cursor.execute(
            "UPDATE messenger_streams SET private = false "
            "WHERE project_id = %s AND uuid = %s",
            (api.project_id, stream_uuid),
        )


def test_native_v2_fanout_is_bounded_and_outbox_is_one_to_one(api, db):
    peers = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    for peer_uuid in peers:
        conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
        _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Bounded fanout",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid) for peer_uuid in peers]},
    )
    assert added.status_code == 200, added.text
    _drain()

    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "bounded"},
        },
    )
    assert message.status_code == 201, message.text
    with contexts.Context().session_manager() as session:
        processed = v2_projection.drain_projection_queue(
            session,
            f"integration:{sys_uuid.uuid4()}",
            fanout_batch_size=2,
        )
    assert processed >= 3

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM messenger_api_user_messages_v1
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message.json()["uuid"]),
        )
        assert cursor.fetchone()[0] == 3
        cursor.execute(
            """
            SELECT count(*), min(batch_size), max(batch_size)
            FROM messenger_fanout_batch_tasks
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone() == (2, 1, 2)
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM messenger_domain_outbox_events
                 WHERE project_id = %s),
                (SELECT count(*) FROM messenger_projection_tasks
                 WHERE project_id = %s),
                (SELECT count(DISTINCT outbox_event_uuid)
                 FROM messenger_projection_tasks WHERE project_id = %s)
            """,
            (api.project_id, api.project_id, api.project_id),
        )
        outbox_count, task_count, distinct_task_count = cursor.fetchone()
    assert outbox_count == task_count == distinct_task_count


def test_native_v2_membership_generation_fences_fanout_and_rebuilds_history(api, db):
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Membership generations",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    historical = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "before membership"},
        },
    ).json()
    _drain()

    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    binding_uuid = added.json()[0]["uuid"]
    assert api.get(f"{MESSAGES}{historical['uuid']}", user=peer_uuid).status_code == 404
    _drain()
    peer_historical = api.get(f"{MESSAGES}{historical['uuid']}", user=peer_uuid)
    assert peer_historical.status_code == 200, peer_historical.text
    assert peer_historical.json()["read"] is True

    starred = api.post(
        f"{MESSAGES}{historical['uuid']}/actions/star/invoke",
        user=peer_uuid,
    )
    assert starred.status_code == 200, starred.text
    assert starred.json()["starred"] is True

    delivered_while_member = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "old generation event"},
        },
    ).json()
    _drain()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid
            FROM m_workspace_visible_events
            WHERE project_id = %s AND user_uuid = %s
              AND object_type = 'message' AND action = 'created'
              AND payload->>'uuid' = %s
            """,
            (api.project_id, peer_uuid, delivered_while_member["uuid"]),
        )
        old_generation_event_uuid = cursor.fetchone()[0]

    sent_while_member = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "fenced fanout"},
        },
    ).json()
    removed = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert removed.status_code == 204, removed.text
    _drain()
    assert api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).status_code == 404
    assert (
        api.get(f"{MESSAGES}{sent_while_member['uuid']}", user=peer_uuid).status_code
        == 404
    )

    readded = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert readded.status_code == 200, readded.text
    assert readded.json()[0]["uuid"] == binding_uuid
    _drain()
    for message_uuid in (
        historical["uuid"],
        delivered_while_member["uuid"],
        sent_while_member["uuid"],
    ):
        reloaded = api.get(f"{MESSAGES}{message_uuid}", user=peer_uuid)
        assert reloaded.status_code == 200, reloaded.text
        assert reloaded.json()["read"] is True
        assert reloaded.json()["starred"] is False
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT active, membership_generation
            FROM messenger_stream_bindings
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, binding_uuid),
        )
        assert cursor.fetchone() == (True, 3)
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM m_workspace_visible_events
                 WHERE uuid = %s AND user_uuid = %s),
                (SELECT membership_generation
                 FROM messenger_event_membership_guards
                 WHERE event_uuid = %s AND user_uuid = %s)
            """,
            (
                old_generation_event_uuid,
                peer_uuid,
                old_generation_event_uuid,
                peer_uuid,
            ),
        )
        assert cursor.fetchone() == (0, 1)
        cursor.execute(
            """
            DELETE FROM m_workspace_broadcast_message_events_v1
            WHERE uuid = %s
            """,
            (old_generation_event_uuid,),
        )
        cursor.execute(
            """
            SELECT count(*) FROM messenger_event_membership_guards
            WHERE event_uuid = %s
            """,
            (old_generation_event_uuid,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT count(*)
            FROM messenger_user_message_bindings AS binding
            JOIN messenger_user_message_states AS state
              ON state.project_id = binding.project_id
             AND state.placement_uuid = binding.placement_uuid
             AND state.user_uuid = binding.user_uuid
            WHERE binding.project_id = %s AND binding.user_uuid = %s
              AND binding.membership_generation = 3
              AND state.membership_generation = 3
            """,
            (api.project_id, peer_uuid),
        )
        assert cursor.fetchone()[0] == 3


def test_native_v2_canonical_message_supports_multiple_placements(
    api, workspace_api, db
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Canonical placements",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    first_topic = api.post(
        STREAM_TOPICS,
        json={"stream_uuid": stream["uuid"], "name": "First"},
    ).json()
    second_topic = api.post(
        STREAM_TOPICS,
        json={"stream_uuid": stream["uuid"], "name": "Second"},
    ).json()
    _drain()

    canonical_uuid = sys_uuid.uuid4()
    message_body = {
        "uuid": str(canonical_uuid),
        "stream_uuid": stream["uuid"],
        "payload": {"kind": "markdown", "content": "shared content"},
    }
    first = api.post(
        MESSAGES,
        json={**message_body, "topic_uuid": first_topic["uuid"]},
    )
    second = api.post(
        MESSAGES,
        json={**message_body, "topic_uuid": second_topic["uuid"]},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["uuid"] != second.json()["uuid"]
    assert first.json()["uuid"] == str(
        sys_uuid.uuid5(sys_uuid.UUID(first_topic["uuid"]), str(canonical_uuid))
    )
    assert second.json()["uuid"] == str(
        sys_uuid.uuid5(sys_uuid.UUID(second_topic["uuid"]), str(canonical_uuid))
    )
    _drain()

    reaction = api.post(
        MESSAGE_REACTIONS,
        json={
            "message_uuid": first.json()["uuid"],
            "emoji_name": "rocket",
        },
    )
    assert reaction.status_code == 201, reaction.text
    _drain()
    for placement_uuid in (first.json()["uuid"], second.json()["uuid"]):
        visible = api.get(f"{MESSAGES}{placement_uuid}", user=peer_uuid)
        assert visible.status_code == 200, visible.text
        assert visible.json()["reactions"] == {"rocket": 1}

    updated = api.put(
        f"{MESSAGES}{first.json()['uuid']}",
        json={"payload": {"kind": "markdown", "content": "updated once"}},
    )
    assert updated.status_code == 200, updated.text
    _drain()
    assert (
        api.get(f"{MESSAGES}{second.json()['uuid']}", user=peer_uuid).json()["payload"][
            "content"
        ]
        == "updated once"
    )
    events = workspace_api.get(EVENTS, params={"page_limit": 500}, user=peer_uuid)
    updated_message_uuids = {
        event["payload"]["uuid"]
        for event in events.json()
        if event["payload"]["kind"] == "message.updated"
    }
    assert {first.json()["uuid"], second.json()["uuid"]} <= updated_message_uuids

    deleted = api.delete(f"{MESSAGES}{first.json()['uuid']}")
    assert deleted.status_code == 204, deleted.text
    _drain()
    assert api.get(f"{MESSAGES}{first.json()['uuid']}").status_code == 404
    assert api.get(f"{MESSAGES}{second.json()['uuid']}").status_code == 404
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM messenger_messages
                 WHERE project_id = %s AND uuid = %s),
                (SELECT count(*) FROM messenger_message_reaction_facts
                 WHERE project_id = %s AND canonical_message_uuid = %s)
            """,
            (api.project_id, canonical_uuid, api.project_id, canonical_uuid),
        )
        assert cursor.fetchone() == (0, 0)


def test_native_v2_actions_converge_counters_notifications_and_events(
    api, workspace_api, db
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Actions and counters",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    _drain()
    topic_uuid = stream["default_topic_uuid"]
    message_uuids = []
    for content in ("first", "second"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
        _drain()

    peer_topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()
    peer_stream = api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()
    assert (
        peer_topic["unread_count"],
        peer_topic["active_unread_count"],
        peer_topic["passive_unread_count"],
    ) == (2, 2, 0)
    assert (
        peer_stream["unread_count"],
        peer_stream["active_unread_count"],
        peer_stream["passive_unread_count"],
    ) == (2, 2, 0)

    muted = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "mute"},
    )
    assert muted.status_code == 200, muted.text
    _drain()
    peer_topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()
    peer_stream = api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()
    assert (peer_topic["active_unread_count"], peer_topic["passive_unread_count"]) == (
        0,
        2,
    )
    assert (
        peer_stream["active_unread_count"],
        peer_stream["passive_unread_count"],
    ) == (0, 2)

    followed = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "follow"},
    )
    assert followed.status_code == 200, followed.text
    _drain()
    assert (
        api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()[
            "active_unread_count"
        ]
        == 2
    )

    read_up_to = api.post(
        f"{MESSAGES}{message_uuids[0]}/actions/read_up_to/invoke",
        user=peer_uuid,
    )
    assert read_up_to.status_code == 200, read_up_to.text
    _drain()
    assert (
        api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()["unread_count"]
        == 1
    )
    read_topic = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/read/invoke",
        user=peer_uuid,
    )
    assert read_topic.status_code == 200, read_topic.text
    _drain()
    assert (
        api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()["unread_count"]
        == 0
    )
    assert (
        api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()["unread_count"]
        == 0
    )

    toggled = api.post(f"{STREAM_TOPICS}{topic_uuid}/actions/toggle_done/invoke")
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["is_done"] is True
    _drain()
    assert (
        api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()["is_done"]
        is True
    )
    archived = api.post(f"{STREAMS}{stream['uuid']}/actions/archive/invoke")
    assert archived.status_code == 200, archived.text
    assert archived.json()["is_archived"] is True
    _drain()
    unarchived = api.post(f"{STREAMS}{stream['uuid']}/actions/unarchive/invoke")
    assert unarchived.status_code == 200, unarchived.text
    _drain()

    events = workspace_api.get(EVENTS, params={"page_limit": 500}, user=peer_uuid)
    kinds = [event["payload"]["kind"] for event in events.json()]
    assert "message.read" in kinds
    assert "topic.updated" in kinds
    assert "stream.updated" in kinds
    assert "folder.updated" in kinds


def test_native_v2_idempotent_reads_repair_stale_counters(api, db):
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Idempotent read repair",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    _drain()
    topic_uuid = stream["default_topic_uuid"]
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "read repair"},
        },
    )
    assert message.status_code == 201, message.text
    message_uuid = message.json()["uuid"]
    _drain()

    initial_read = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/read/invoke",
        user=peer_uuid,
    )
    assert initial_read.status_code == 200, initial_read.text
    _drain()

    def corrupt_ready_counters():
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messenger_stream_bindings
                SET unread_count=7, active_unread_count=7,
                    passive_unread_count=0
                WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s
                """,
                (api.project_id, stream["uuid"], peer_uuid),
            )
            cursor.execute(
                """
                UPDATE messenger_user_topic_bindings
                SET unread_count=7, active_unread_count=7,
                    passive_unread_count=0
                WHERE project_id=%s AND topic_uuid=%s AND user_uuid=%s
                """,
                (api.project_id, topic_uuid, peer_uuid),
            )
        db.commit()

    actions = (
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        f"{MESSAGES}{message_uuid}/actions/read_up_to/invoke",
        f"{STREAM_TOPICS}{topic_uuid}/actions/read/invoke",
        f"{STREAMS}{stream['uuid']}/actions/read/invoke",
    )
    expected_source_kinds = (
        "message.read",
        "messages.read",
        "topic.read",
        "stream.read",
    )
    for path, source_kind in zip(actions, expected_source_kinds, strict=True):
        corrupt_ready_counters()
        repaired = api.post(path, user=peer_uuid)
        assert repaired.status_code == 200, repaired.text
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM messenger_domain_outbox_events
                WHERE project_id=%s
                  AND event_kind='read_counters'
                  AND payload->>'source_kind'=%s
                  AND payload->>'user_uuid'=%s
                """,
                (api.project_id, source_kind, str(peer_uuid)),
            )
            assert cursor.fetchone()[0] >= 2
        _drain()
        topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()
        current_stream = api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()
        assert topic["unread_count"] == 0
        assert current_stream["unread_count"] == 0


def test_projection_claim_bounds_interactive_read_priority(
    api,
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    stream = api.post(
        STREAMS,
        json={
            "name": "Interactive read claim priority",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    old_event_uuid = sys_uuid.uuid4()
    read_event_uuid = sys_uuid.uuid4()
    now = datetime.datetime.now(datetime.UTC)
    background_created_at = now - datetime.timedelta(minutes=10)
    with db.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                (
                    old_event_uuid,
                    api.project_id,
                    "topic_state_projection",
                    "topic",
                    f"{api.project_id}:{stream['default_topic_uuid']}",
                    json.dumps(
                        {
                            "source_kind": "topic.updated",
                            "topic_uuid": stream["default_topic_uuid"],
                        }
                    ),
                    background_created_at,
                    background_created_at,
                ),
                (
                    read_event_uuid,
                    api.project_id,
                    "read_counters",
                    "user-topic",
                    f"{api.project_id}:{api.user_uuid}:{stream['default_topic_uuid']}",
                    json.dumps(
                        {
                            "source_kind": "topic.read",
                            "user_uuid": str(api.user_uuid),
                            "stream_uuid": stream["uuid"],
                            "topic_uuid": stream["default_topic_uuid"],
                        }
                    ),
                    now,
                    now,
                ),
            ),
        )
    db.commit()
    with contexts.Context().session_manager() as session:
        assert v2_projection.derive_projection_tasks(session) == 0
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = CASE
                    WHEN outbox_event_uuid = %s THEN %s ELSE %s
                END,
                updated_at = NOW()
            WHERE outbox_event_uuid IN (%s, %s)
            """,
            (
                old_event_uuid,
                background_created_at,
                now,
                old_event_uuid,
                read_event_uuid,
            ),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'completed', updated_at = NOW()
            WHERE outbox_event_uuid NOT IN (%s, %s)
            """,
            (old_event_uuid, read_event_uuid),
        )
        claimed = v2_projection._claim_task(session, "integration:read-priority", 30)
        assert claimed is not None
        assert claimed["outbox_event_uuid"] == read_event_uuid
        assert claimed["payload"]["source_kind"] == "topic.read"


@pytest.mark.parametrize("blocked_by", ("predecessor", "scope_lease"))
def test_projection_claim_ignores_unclaimable_overdue_tasks(
    api, blocked_by, monkeypatch
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    streams = []
    for name in ("Blocked overdue scope", "Claimable background scope"):
        streams.append(
            api.post(
                STREAMS,
                json={
                    "name": name,
                    "description": "",
                    "source_name": "native",
                },
            ).json()
        )
        _drain()

    now = datetime.datetime.now(datetime.UTC)
    blocked_event_uuid = sys_uuid.uuid4()
    background_event_uuid = sys_uuid.uuid4()
    read_event_uuid = sys_uuid.uuid4()
    predecessor_event_uuid = sys_uuid.uuid4() if blocked_by == "predecessor" else None
    blocked_stream, background_stream = streams
    events = []
    if predecessor_event_uuid is not None:
        events.append(
            (
                predecessor_event_uuid,
                "topic_state_projection",
                "topic",
                f"{api.project_id}:{blocked_stream['default_topic_uuid']}",
                {
                    "source_kind": "topic.updated",
                    "topic_uuid": blocked_stream["default_topic_uuid"],
                },
                now - datetime.timedelta(seconds=42),
            )
        )
    events.extend(
        (
            (
                blocked_event_uuid,
                "topic_state_projection",
                "topic",
                f"{api.project_id}:{blocked_stream['default_topic_uuid']}",
                {
                    "source_kind": "topic.updated",
                    "topic_uuid": blocked_stream["default_topic_uuid"],
                },
                now - datetime.timedelta(seconds=41),
            ),
            (
                background_event_uuid,
                "topic_state_projection",
                "topic",
                f"{api.project_id}:{background_stream['default_topic_uuid']}",
                {
                    "source_kind": "topic.updated",
                    "topic_uuid": background_stream["default_topic_uuid"],
                },
                now - datetime.timedelta(seconds=10),
            ),
            (
                read_event_uuid,
                "read_counters",
                "user-topic",
                (
                    f"{api.project_id}:{api.user_uuid}:"
                    f"{background_stream['default_topic_uuid']}"
                ),
                {
                    "source_kind": "topic.read",
                    "user_uuid": str(api.user_uuid),
                    "stream_uuid": background_stream["uuid"],
                    "topic_uuid": background_stream["default_topic_uuid"],
                },
                now,
            ),
        )
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT input.uuid, %s, input.event_kind, input.scope_kind,
                   input.scope_key, input.payload::jsonb,
                   input.created_at, input.created_at
            FROM unnest(
                %s::uuid[], %s::text[], %s::text[], %s::text[],
                %s::text[], %s::timestamptz[]
            ) AS input(
                uuid, event_kind, scope_kind, scope_key, payload, created_at
            )
            """,
            (
                api.project_id,
                [event[0] for event in events],
                [event[1] for event in events],
                [event[2] for event in events],
                [event[3] for event in events],
                [json.dumps(event[4]) for event in events],
                [event[5] for event in events],
            ),
        )
        assert v2_projection.derive_projection_tasks(session) == 0
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = NOW()
            FROM messenger_domain_outbox_events AS event
            WHERE task.project_id = %s
              AND event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
              AND event.uuid = ANY(%s::uuid[])
            """,
            (api.project_id, [event[0] for event in events]),
        )
        if predecessor_event_uuid is not None:
            session.execute(
                """
                UPDATE messenger_projection_tasks
                SET status = 'failed', next_retry_at = NOW() + interval '1 hour',
                    updated_at = NOW()
                WHERE project_id = %s AND outbox_event_uuid = %s
                """,
                (api.project_id, predecessor_event_uuid),
            )
        else:
            session.execute(
                """
                INSERT INTO messenger_projection_scope_leases (
                    uuid, project_id, scope_kind, scope_key, owner,
                    fencing_token, lease_expires_at
                ) VALUES (%s, %s, 'topic', %s, 'competing-worker', 1,
                          NOW() + interval '1 hour')
                ON CONFLICT (project_id, scope_kind, scope_key) DO UPDATE SET
                    owner = EXCLUDED.owner,
                    fencing_token = messenger_projection_scope_leases.fencing_token + 1,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    updated_at = NOW()
                """,
                (
                    sys_uuid.uuid4(),
                    api.project_id,
                    f"{api.project_id}:{blocked_stream['default_topic_uuid']}",
                ),
            )

        claimed = v2_projection._claim_task(
            session,
            "integration:claimable-overdue",
            30,
        )
        assert claimed is not None
        assert claimed["outbox_event_uuid"] == read_event_uuid


def test_native_v2_keeps_folder_file_and_draft_contracts(api, db):
    peer_uuid = sys_uuid.uuid4()
    outsider_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    conftest.seed_workspace_user(db, outsider_uuid, f"user-{outsider_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Adjacent contracts",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    binding_uuid = added.json()[0]["uuid"]
    _drain()

    file_response = api.post(
        FILES,
        json={
            "stream_uuid": stream["uuid"],
            "name": "architecture.txt",
            "description": "Native v2",
            "content_type": "text/plain",
            "size_bytes": 12,
            "hash": "v2-hash",
        },
    )
    assert file_response.status_code == 201, file_response.text
    file_uuid = file_response.json()["uuid"]
    assert api.get(f"{FILES}{file_uuid}", user=peer_uuid).status_code == 200
    assert api.get(f"{FILES}{file_uuid}", user=outsider_uuid).status_code == 404

    system_item_uuid = f"00{stream['uuid'][2:]}"
    system_item = api.get(f"{FOLDER_ITEMS}{system_item_uuid}")
    assert system_item.status_code == 200, system_item.text
    system_pinned = api.post(f"{FOLDER_ITEMS}{system_item_uuid}/actions/pin/invoke")
    assert system_pinned.status_code == 200, system_pinned.text
    assert system_pinned.json()["pinned_at"] is not None
    _drain()
    all_chats = api.get(f"{FOLDERS}00000000-0000-0000-0000-000000000000")
    assert all_chats.status_code == 200, all_chats.text
    nested_system_item = next(
        value
        for value in all_chats.json()["folder_items"]
        if value["uuid"] == system_item_uuid
    )
    assert nested_system_item["pinned_at"] is not None
    system_unpinned = api.post(f"{FOLDER_ITEMS}{system_item_uuid}/actions/unpin/invoke")
    assert system_unpinned.status_code == 200, system_unpinned.text
    _drain()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM messenger_folder_items
            WHERE project_id = %s AND user_uuid = %s
              AND folder_uuid = %s AND stream_uuid = %s AND pinned_at IS NOT NULL
            """,
            (
                api.project_id,
                api.user_uuid,
                "00000000-0000-0000-0000-000000000000",
                stream["uuid"],
            ),
        )
        assert cursor.fetchone()[0] == 0

    folder = api.post(FOLDERS, json={"title": "V2 review"})
    assert folder.status_code == 201, folder.text
    item = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder.json()["uuid"],
            "stream_uuid": stream["uuid"],
            "chat_type": "stream",
        },
    )
    assert item.status_code == 201, item.text
    pinned = api.post(f"{FOLDER_ITEMS}{item.json()['uuid']}/actions/pin/invoke")
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["pinned_at"] is not None
    _drain()
    reloaded_folder = api.get(f"{FOLDERS}{folder.json()['uuid']}")
    assert reloaded_folder.status_code == 200, reloaded_folder.text
    nested_items = reloaded_folder.json()["folder_items"]
    assert len(nested_items) == 1
    assert nested_items[0]["uuid"] == item.json()["uuid"]
    assert nested_items[0]["folder_uuid"] == folder.json()["uuid"]
    assert nested_items[0]["stream_uuid"] == stream["uuid"]
    assert nested_items[0]["pinned_at"] is not None
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM messenger_user_folder_bindings
                 WHERE project_id = %s AND user_uuid = %s),
                (SELECT count(*) FROM messenger_folder_items
                 WHERE project_id = %s AND user_uuid = %s),
                (SELECT jsonb_array_length(folder_items_snapshot)
                 FROM messenger_user_folder_bindings
                 WHERE project_id = %s AND user_uuid = %s
                   AND folder_uuid = %s)
            """,
            (
                api.project_id,
                api.user_uuid,
                api.project_id,
                api.user_uuid,
                api.project_id,
                api.user_uuid,
                folder.json()["uuid"],
            ),
        )
        binding_count, item_count, snapshot_count = cursor.fetchone()
    assert binding_count >= 4
    assert item_count >= 3
    assert snapshot_count == 1

    draft_uuid = sys_uuid.uuid4()
    draft_body = {
        "uuid": str(draft_uuid),
        "stream_uuid": stream["uuid"],
        "topic_uuid": stream["default_topic_uuid"],
        "payload": {"kind": "markdown", "content": "  native draft  "},
    }
    draft = api.post(DRAFTS, json=draft_body)
    assert draft.status_code == 201, draft.text
    assert draft.headers["ETag"] == '"1"'
    assert draft.json()["payload"]["content"] == "native draft"
    retry = api.post(DRAFTS, json=draft_body)
    assert retry.status_code == 200, retry.text
    updated = api.put(
        f"{DRAFTS}{draft_uuid}",
        headers={"If-Match": '"1"'},
        json={"payload": {"kind": "markdown", "content": "updated"}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.headers["ETag"] == '"2"'

    removed = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert removed.status_code == 204, removed.text
    _drain()
    assert api.get(f"{FILES}{file_uuid}", user=peer_uuid).status_code == 404
    deleted_draft = api.delete(f"{DRAFTS}{draft_uuid}", headers={"If-Match": '"2"'})
    assert deleted_draft.status_code == 204, deleted_draft.text


def test_native_v2_isolates_projects_and_rejects_non_members(api, workspace_api, db):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    other_project = sys_uuid.uuid4()
    other_user = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, other_user, f"user-{other_user}")
    _register_project_user(db, other_project, other_user)
    current_stream = api.post(
        STREAMS,
        json={
            "name": "Current project",
            "description": "",
            "source_name": "native",
        },
    )
    assert current_stream.status_code == 201, current_stream.text
    rejected_member = api.post(
        f"{STREAMS}{current_stream.json()['uuid']}/actions/add_users/invoke",
        json={"member": [str(other_user)]},
    )
    assert rejected_member.status_code == 400, rejected_member.text
    rejected_direct = api.post(
        STREAMS,
        json={
            "name": "Cross-project direct",
            "description": "",
            "source_name": "native",
            "direct_user_uuid": str(other_user),
        },
    )
    assert rejected_direct.status_code == 400, rejected_direct.text
    other_stream = api.post(
        STREAMS,
        project=other_project,
        json={
            "name": "Other project",
            "description": "",
            "source_name": "native",
        },
    )
    assert other_stream.status_code == 201, other_stream.text
    _drain()
    other_message = api.post(
        MESSAGES,
        project=other_project,
        json={
            "stream_uuid": other_stream.json()["uuid"],
            "topic_uuid": other_stream.json()["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "isolated"},
        },
    )
    assert other_message.status_code == 201, other_message.text
    _drain()

    assert api.get(f"{STREAMS}{other_stream.json()['uuid']}").status_code == 404
    assert api.get(f"{MESSAGES}{other_message.json()['uuid']}").status_code == 404
    assert (
        api.get(
            f"{MESSAGES}{other_message.json()['uuid']}", project=other_project
        ).status_code
        == 200
    )
    current_events = workspace_api.get(EVENTS, params={"page_limit": 500})
    assert current_events.status_code == 200, current_events.text
    assert all(event["project_id"] == api.project_id for event in current_events.json())


def test_native_v2_scheduler_preserves_fifo_within_fanout_lane(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    stream = api.post(
        STREAMS,
        json={
            "name": "Newest first",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    first = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "older"},
        },
    ).json()
    second = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "newer"},
        },
    ).json()
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            UPDATE messenger_domain_outbox_events
            SET payload = jsonb_set(
                payload,
                '{message_created_at}',
                to_jsonb(NOW() + interval '1 minute')
            )
            WHERE project_id = %s AND event_kind = 'fanout'
              AND payload->>'placement_uuid' = %s
            """,
            (api.project_id, second["uuid"]),
        )
        v2_projection.derive_projection_tasks(session)
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = NOW(), updated_at = NOW()
            WHERE project_id = %s
              AND payload->>'placement_uuid' IN (%s, %s)
            """,
            (api.project_id, first["uuid"], second["uuid"]),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'completed', updated_at = NOW()
            WHERE project_id <> %s
               OR COALESCE(payload->>'placement_uuid', '') NOT IN (%s, %s)
            """,
            (api.project_id, first["uuid"], second["uuid"]),
        )
        claimed = v2_projection._claim_task(session, "integration:fifo", 30)
        assert claimed["task_kind"] == "fanout"
        assert str(claimed["payload"]["placement_uuid"]) == first["uuid"]
        assert str(claimed["payload"]["placement_uuid"]) != second["uuid"]


def test_native_v2_scheduler_drains_aged_tasks_before_fresh_fanout(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    stream = api.post(
        STREAMS,
        json={
            "name": "Aged before fresh",
            "description": "",
            "source_name": "native",
        },
    ).json()
    _drain()
    aged = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "aged"},
        },
    ).json()
    fresh = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "fresh"},
        },
    ).json()
    with contexts.Context().session_manager() as session:
        v2_projection.derive_projection_tasks(session)
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = CASE
                    WHEN payload->>'placement_uuid' = %s
                    THEN NOW() - interval '10 seconds'
                    ELSE NOW()
                END,
                updated_at = NOW()
            WHERE project_id = %s
              AND payload->>'placement_uuid' IN (%s, %s)
            """,
            (aged["uuid"], api.project_id, aged["uuid"], fresh["uuid"]),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'completed', updated_at = NOW()
            WHERE project_id <> %s
               OR COALESCE(payload->>'placement_uuid', '') NOT IN (%s, %s)
            """,
            (api.project_id, aged["uuid"], fresh["uuid"]),
        )
        claimed = v2_projection._claim_task(session, "integration:aged", 30)
        assert str(claimed["payload"]["placement_uuid"]) == aged["uuid"]


def test_native_v2_projection_failure_is_retryable_and_dead_letters(api, db):
    event_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                %s, %s, 'folder_projection', 'user-folder', 'invalid',
                '{"source_kind":"stream.created"}'::jsonb
            )
            """,
            (event_uuid, api.project_id),
        )
    with contexts.Context().session_manager() as session:
        v2_projection.derive_projection_tasks(session)
        assert v2_projection.process_one_projection_task(
            session,
            "integration:failure",
            max_attempts=2,
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, last_error
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
        status, attempts, last_error = cursor.fetchone()
        assert (status, attempts) == ("failed", 1)
        assert "folder_uuid" in last_error
        cursor.execute(
            """
            UPDATE messenger_projection_tasks SET next_retry_at = NOW()
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:failure",
            max_attempts=2,
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, lease_owner
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
        assert cursor.fetchone() == ("dead_letter", 2, None)


def test_native_v2_reclaims_an_expired_running_projection_task(api, db):
    folder = api.post(FOLDERS, json={"title": "Lease recovery"})
    assert folder.status_code == 201, folder.text
    with contexts.Context().session_manager() as session:
        v2_projection.drain_projection_queue(
            session,
            f"integration:{sys_uuid.uuid4()}",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid, attempts
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'folder_projection'
              AND payload->>'folder_uuid' = %s
            ORDER BY created_at DESC, uuid DESC
            LIMIT 1
            """,
            (api.project_id, folder.json()["uuid"]),
        )
        task_uuid, attempts = cursor.fetchone()
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'running', lease_owner = 'stale-worker',
                lease_expires_at = NOW() - interval '1 second'
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, task_uuid),
        )
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:reaper",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, lease_owner
            FROM messenger_projection_tasks
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, task_uuid),
        )
        assert cursor.fetchone() == ("completed", attempts + 1, None)


def test_native_v2_coalesces_legacy_folder_snapshot_bursts(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Legacy folder snapshot burst",
            "description": "",
            "source_name": "native",
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream_uuid = stream_response.json()["uuid"]
    folder_response = api.post(FOLDERS, json={"title": "Legacy snapshot target"})
    assert folder_response.status_code == 201, folder_response.text
    folder_uuid = folder_response.json()["uuid"]
    _drain()
    event_uuids = [sys_uuid.uuid4() for _ in range(3)]
    scope_key = f"{api.project_id}:{api.user_uuid}:{folder_uuid}"
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT snapshot_version
            FROM messenger_user_folder_bindings
            WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
            """,
            (api.project_id, api.user_uuid, folder_uuid),
        )
        snapshot_version = cursor.fetchone()[0]
        cursor.executemany(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                %s, %s, 'folder_projection', 'user-folder', %s, %s::jsonb
            )
            """,
            [
                (
                    event_uuid,
                    api.project_id,
                    scope_key,
                    json.dumps(
                        {
                            "source_kind": source_kind,
                            "user_uuid": api.user_uuid,
                            "stream_uuid": stream_uuid,
                            "folder_uuid": folder_uuid,
                        }
                    ),
                )
                for event_uuid, source_kind in zip(
                    event_uuids,
                    (
                        "legacy_message_state.updated",
                        "legacy_message_state.deleted",
                        "legacy_message_state.updated",
                    ),
                    strict=True,
                )
            ],
        )
    with contexts.Context().session_manager() as session:
        assert v2_projection.derive_projection_tasks(session) == 0
    competing_claim = psycopg.connect(conftest.TEST_DB_URL)
    try:
        with competing_claim.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM messenger_projection_tasks
                WHERE project_id = %s AND outbox_event_uuid = ANY(%s::uuid[])
                ORDER BY created_at DESC, ordering_created_at DESC,
                         outbox_event_uuid DESC
                LIMIT 1
                FOR UPDATE
                """,
                (api.project_id, event_uuids),
            )
            assert cursor.fetchone() is not None
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL lock_timeout = '100ms'")
            assert v2_projection.process_one_projection_task(
                session,
                "integration:legacy-folder-coalesce",
            )
    finally:
        competing_claim.rollback()
        competing_claim.close()
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:legacy-folder-coalesce",
        )
        assert not v2_projection.process_one_projection_task(
            session,
            "integration:legacy-folder-coalesce",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE attempts = 1),
                   count(*) FILTER (WHERE attempts = 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
        assert cursor.fetchone() == (3, 2, 1)
        cursor.execute(
            """
            SELECT snapshot_version
            FROM messenger_user_folder_bindings
            WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
            """,
            (api.project_id, api.user_uuid, folder_uuid),
        )
        assert cursor.fetchone()[0] == snapshot_version + 2


def test_native_v2_coalesces_snapshot_only_read_counter_bursts(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Read counter snapshot burst",
            "description": "",
            "source_name": "native",
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    _drain()
    event_uuids = [sys_uuid.uuid4() for _ in range(4)]
    scope_key = f"{api.project_id}:{api.user_uuid}:{stream['uuid']}"
    common_payload = {
        "user_uuid": api.user_uuid,
        "stream_uuid": stream["uuid"],
        "topic_uuid": stream["default_topic_uuid"],
    }
    payloads = [
        {**common_payload, "source_kind": "message.created"},
        {**common_payload, "source_kind": "message.updated"},
        {**common_payload, "source_kind": "stream_binding.created"},
        {
            **common_payload,
            "source_kind": "message.read",
            "placement_uuid": str(sys_uuid.uuid4()),
            "emit_message_read": True,
        },
    ]
    with db.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES (
                %s, %s, 'read_counters', 'user-stream', %s, %s::jsonb,
                NOW() + %s * INTERVAL '1 second',
                NOW() + %s * INTERVAL '1 second'
            )
            """,
            [
                (
                    event_uuid,
                    api.project_id,
                    scope_key,
                    json.dumps(payload),
                    position,
                    position,
                )
                for position, (event_uuid, payload) in enumerate(
                    zip(event_uuids, payloads, strict=True)
                )
            ],
        )
    with contexts.Context().session_manager() as session:
        assert v2_projection.derive_projection_tasks(session) == 0
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = CASE
                WHEN (payload->>'emit_message_read')::boolean
                    THEN NOW()
                ELSE NOW() - INTERVAL '1 hour'
            END
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
    competing_claim = psycopg.connect(conftest.TEST_DB_URL)
    try:
        with competing_claim.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM messenger_projection_tasks
                WHERE project_id = %s
                  AND outbox_event_uuid = ANY(%s::uuid[])
                  AND COALESCE(
                      (payload->>'emit_message_read')::boolean,
                      FALSE
                  ) = FALSE
                ORDER BY created_at DESC, ordering_created_at DESC,
                         outbox_event_uuid DESC
                LIMIT 1
                FOR UPDATE
                """,
                (api.project_id, event_uuids),
            )
            assert cursor.fetchone() is not None
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL lock_timeout = '100ms'")
            assert v2_projection.process_one_projection_task(
                session,
                "integration:read-counter-coalesce",
            )
    finally:
        competing_claim.rollback()
        competing_claim.close()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE status = 'pending'),
                   count(*) FILTER (WHERE attempts = 1),
                   count(*) FILTER (WHERE attempts = 0),
                   count(*) FILTER (
                       WHERE status = 'pending'
                         AND (payload->>'emit_message_read')::boolean
                   )
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
        # The overdue snapshot batch gets one bounded turn before the fresh
        # interactive read. Its unlocked sibling coalesces while the row held
        # by a competing claim remains pending without blocking this worker.
        assert cursor.fetchone() == (2, 2, 1, 3, 1)
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:read-counter-coalesce",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE status = 'pending'),
                   count(*) FILTER (WHERE attempts = 1),
                   count(*) FILTER (WHERE attempts = 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
        assert cursor.fetchone() == (3, 1, 2, 2)
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:read-counter-coalesce",
        )
        # Atomic enqueue also schedules folder snapshots for these reads.
        v2_projection.drain_projection_queue(
            session,
            "integration:read-counter-coalesce",
        )
        assert not v2_projection.process_one_projection_task(
            session,
            "integration:read-counter-coalesce",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE attempts = 1),
                   count(*) FILTER (WHERE attempts = 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
        assert cursor.fetchone() == (4, 3, 1)
    # Current migrations enqueue the resulting folder snapshots atomically.
    _drain()
    with contexts.Context().session_manager() as session:
        assert not v2_projection.process_one_projection_task(
            session,
            "integration:read-counter-coalesce",
        )


def test_native_v2_fanout_supports_more_than_five_thousand_recipients(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Large native audience",
            "description": "",
            "source_name": "native",
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    with contexts.Context().session_manager() as session:
        v2_projection.drain_projection_queue(
            session,
            f"integration:{sys_uuid.uuid4()}",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            WITH generated AS (
                SELECT md5(%s::text || ':fanout-user:' || value::text)::uuid
                           AS user_uuid,
                       value
                FROM generate_series(1, 5001) AS value
            )
            INSERT INTO m_workspace_users (
                uuid, username, source, status, avatar, last_ping_at,
                created_at, updated_at
            )
            SELECT user_uuid,
                   'fanout-' || value::text || '-' || %s::text,
                   'iam', 'active', 'urn:gravatar:' || md5(user_uuid::text),
                   NOW(), NOW(), NOW()
            FROM generated
            """,
            (api.project_id, api.project_id),
        )
        cursor.execute(
            """
            WITH generated AS (
                SELECT md5(%s::text || ':fanout-user:' || value::text)::uuid
                           AS user_uuid
                FROM generate_series(1, 5001) AS value
            )
            INSERT INTO messenger_project_users (project_id, user_uuid)
            SELECT %s, user_uuid FROM generated
            """,
            (api.project_id, api.project_id),
        )
        cursor.execute(
            """
            WITH generated AS (
                SELECT md5(%s::text || ':fanout-user:' || value::text)::uuid
                           AS user_uuid,
                       md5(%s::text || ':fanout-binding:' || value::text)::uuid
                           AS binding_uuid
                FROM generate_series(1, 5001) AS value
            )
            INSERT INTO messenger_stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid,
                active, membership_generation, role, notification_mode,
                notification_updated_at, created_at, updated_at
            )
            SELECT binding_uuid, %s, %s, user_uuid, %s,
                   true, 1, 'member', 'all_messages', NOW(), NOW(), NOW()
            FROM generated
            """,
            (
                api.project_id,
                api.project_id,
                api.project_id,
                stream["uuid"],
                api.user_uuid,
            ),
        )
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "large fanout"},
        },
    )
    assert message.status_code == 201, message.text
    for _ in range(100):
        with contexts.Context().session_manager() as session:
            v2_projection.derive_projection_tasks(session)
            assert v2_projection.process_one_projection_task(
                session,
                f"integration:{sys_uuid.uuid4()}",
                fanout_batch_size=1000,
            )
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT status
                FROM messenger_fanout_roots
                WHERE project_id = %s AND placement_uuid = %s
                """,
                (api.project_id, message.json()["uuid"]),
            )
            root = cursor.fetchone()
        if root is not None and root[0] == "completed":
            break
    assert root == ("completed",)
    with db.cursor() as cursor:
        # This fixture inserts thousands of rows before autovacuum can refresh
        # statistics, especially after earlier tests truncated the same tables.
        for table in (
            "messenger_user_message_bindings",
            "messenger_user_message_states",
            "messenger_stream_bindings",
            "messenger_message_placements",
            "messenger_messages",
            "messenger_streams",
            "messenger_topics",
        ):
            cursor.execute(f"ANALYZE {table}")
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM messenger_api_user_messages_v1
                 WHERE project_id = %s AND uuid = %s),
                (SELECT count(*) FROM messenger_fanout_batch_tasks
                 WHERE project_id = %s),
                (SELECT max(batch_size) FROM messenger_fanout_batch_tasks
                 WHERE project_id = %s),
                (SELECT bool_or(payload ? 'audience')
                 FROM messenger_domain_outbox_events
                 WHERE project_id = %s AND event_kind = 'fanout')
            """,
            (
                api.project_id,
                message.json()["uuid"],
                api.project_id,
                api.project_id,
                api.project_id,
            ),
        )
        message_count, batch_count, max_batch_size, embeds_audience = cursor.fetchone()
    assert message_count == 5002
    assert batch_count == 6
    assert max_batch_size == 1000
    assert embeds_audience is False
