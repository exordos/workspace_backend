# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import uuid as sys_uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.messenger_api import topic_summarization
from workspace.tests.integration import legacy_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from migrations import legacy_messenger_read_state as read_state
from workspace.tests.integration import conftest
from workspace.tests.integration import test_key_announcements
from workspace.tests.integration import test_messenger_api
from workspace.tests.integration import test_messenger_v2
from workspace.tests.unit import test_messenger_message_payloads


STREAMS = "/v1/messenger/streams/"
MESSAGES = "/v1/messenger/messages/"
MIGRATION = "0187-Process-only-Markdown-message-content-90458e.py"


@pytest.fixture
def content_migration_database(_database, http_server):
    with conftest.isolated_migration_database(MIGRATION) as database_url:
        with psycopg.connect(database_url, autocommit=True) as connection:
            client = conftest.ApiClient(
                base_url=http_server,
                user_uuid=sys_uuid.uuid4(),
                project_id=sys_uuid.uuid4(),
                user_seeder=lambda value: conftest.seed_workspace_user(
                    connection, value, f"user-{value}"
                ),
            )
            yield client, connection


@pytest.fixture(autouse=True)
def _production_v2_store(_database):
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(legacy_store.LegacyMessengerStoreFactory())


def _payloads(content):
    result = []
    for example in test_messenger_message_payloads.PAYLOAD_EXAMPLES:
        payload = example.copy()
        for field in ("content", "public_key", "device_name"):
            if field in payload:
                payload[field] = content
        result.append(payload)
    return result


def _post_message(api, stream_uuid, payload):
    response = api.post(MESSAGES, json={"stream_uuid": stream_uuid, "payload": payload})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("store_kind", ["v2"])
def test_opaque_payloads_pass_through_history_and_events_without_mentions(
    api, workspace_api, db, store_kind
):
    stream = test_key_announcements._create_stream(api)
    peer_uuid = test_key_announcements._add_member(api, db, stream["uuid"])
    notifications = api.post(
        f"{STREAMS}{stream['uuid']}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "mentions_only"},
    )
    assert notifications.status_code == 200, notifications.text
    test_messenger_v2._drain()
    test_key_announcements._use_store(store_kind)
    content = (
        f"[Author](urn:user:{api.user_uuid}) [Peer](urn:user:{peer_uuid}) "
        f"![Image](urn:image:{sys_uuid.uuid4()}) search needle"
    )
    payloads = _payloads(content)
    messages = [_post_message(api, stream["uuid"], payload) for payload in payloads]
    assert [message["mentioned"] for message in messages] == [True] + [False] * 5
    test_messenger_v2._drain()
    workspace_api.project_id = api.project_id
    for user_uuid in (api.user_uuid, peer_uuid):
        history = api.get(
            MESSAGES, user=user_uuid, params={"stream_uuid": stream["uuid"]}
        )
        assert history.status_code == 200, history.text
        assert [row["payload"] for row in history.json()] == payloads
        assert [row["mentioned"] for row in history.json()] == [True] + [False] * 5
        mentioned = api.get(
            MESSAGES,
            user=user_uuid,
            params={"stream_uuid": stream["uuid"], "mentioned": "true"},
        )
        assert mentioned.status_code == 200, mentioned.text
        assert [row["uuid"] for row in mentioned.json()] == [messages[0]["uuid"]]
        workspace_api.user_uuid = str(user_uuid)
        events = workspace_api.get("/v1/events/")
        assert events.status_code == 200, events.text
        created = [
            event["payload"]
            for event in events.json()
            if event["payload"]["kind"] == "message.created"
        ]
        assert [event["payload"] for event in created] == payloads
        assert [event["mentioned"] for event in created] == [True] + [False] * 5
    peer_stream = api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()
    assert peer_stream["unread_count"] == 6
    assert peer_stream["active_unread_count"] == 1
    assert peer_stream["passive_unread_count"] == 5
    rebuild = api.post(
        f"/v1/messenger/stream_topics/{stream['default_topic_uuid']}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "follow"},
    )
    assert rebuild.status_code == 200, rebuild.text
    test_messenger_v2._drain()
    history = api.get(MESSAGES, user=peer_uuid, params={"stream_uuid": stream["uuid"]})
    assert [row["mentioned"] for row in history.json()] == [True] + [False] * 5


@pytest.mark.parametrize("store_kind", ["v2"])
@pytest.mark.parametrize("kind", ["e2ee", "key_grant"])
def test_editing_between_markdown_and_opaque_content_replaces_mentions(
    api, workspace_api, db, store_kind, kind
):
    stream = test_key_announcements._create_stream(api)
    peer_uuid = test_key_announcements._add_member(api, db, stream["uuid"])
    test_messenger_v2._drain()
    test_key_announcements._use_store(store_kind)
    content = f"Hello [Peer](urn:user:{peer_uuid})"
    message = _post_message(
        api, stream["uuid"], {"kind": "markdown", "content": content}
    )
    test_messenger_v2._drain()
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT uuid FROM messenger_message_placements "
            "WHERE project_id = %s AND COALESCE(legacy_public_uuid, uuid) = %s",
            (api.project_id, message["uuid"]),
        )
        event_message_uuid = str(cursor.fetchone()[0])
    payload = next(payload for payload in _payloads(content) if payload["kind"] == kind)
    workspace_api.project_id = api.project_id
    workspace_api.user_uuid = str(peer_uuid)
    for values, expected in [
        (payload, False),
        ({"kind": "markdown", "content": content}, True),
    ]:
        edited = api.put(f"{MESSAGES}{message['uuid']}", json={"payload": values})
        assert edited.status_code == 200, edited.text
        test_messenger_v2._drain()
        history = api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid)
        assert history.status_code == 200, history.text
        assert history.json()["payload"] == values
        assert history.json()["mentioned"] is expected
        events = workspace_api.get("/v1/events/")
        updates = [
            event["payload"]
            for event in events.json()
            if event["payload"]["kind"] == "message.updated"
            and event["payload"]["uuid"] == event_message_uuid
        ]
        assert updates[-1]["payload"] == values
        assert updates[-1]["mentioned"] is expected


@pytest.mark.parametrize("store_kind", ["v2"])
def test_summary_selection_counts_only_markdown_messages(
    api, db, store_kind, monkeypatch
):
    stream = test_key_announcements._create_stream(api)
    test_key_announcements._use_store(store_kind)
    opaque = _payloads(f"![Secret](urn:image:{sys_uuid.uuid4()})")[1:]
    for payload in opaque:
        _post_message(api, stream["uuid"], payload)
    key = "integration-test-topic-summary-key"
    endpoint = api.post(
        "/v1/messenger/topic_summary_endpoints/",
        permissions=(topic_summarization.ENDPOINT_MANAGE_PERMISSION,),
        json={
            "uuid": str(sys_uuid.uuid4()),
            "name": "markdown-only",
            "base_url": "https://llm.example.invalid/v1",
            "model": "summary-model",
            "api_key": "summary-secret",
        },
    )
    assert endpoint.status_code == 201, endpoint.text
    settings = api.put(
        f"/v1/messenger/topic_summary_settings/{api.project_id}",
        permissions=(topic_summarization.SETTINGS_MANAGE_PERMISSION,),
        json={"global_enabled": True, "project_enabled": True},
    )
    assert settings.status_code == 200, settings.text
    monkeypatch.setattr(topic_summarization, "MAX_MESSAGES_PER_SUMMARY", 2)

    def claim():
        with contexts.Context().session_manager() as session:
            return topic_summarization.claim_summary_work(
                session,
                now=datetime.datetime.now(datetime.timezone.utc),
                key_material=key,
                topic_claim_seconds=60,
                endpoint_claim_seconds=60,
            )

    def complete(work):
        with contexts.Context().session_manager() as session:
            topic_summarization.complete_summary_work(
                session,
                work,
                "Markdown summary",
                now=datetime.datetime.now(datetime.timezone.utc),
            )

    try:
        assert claim() is None
        markdown = []
        for index in range(3):
            markdown.append(
                _post_message(
                    api,
                    stream["uuid"],
                    {"kind": "markdown", "content": f"Decision {index}"},
                )
            )
            for payload in opaque:
                _post_message(api, stream["uuid"], payload)
        first = claim()
        assert first is not None
        assert [str(message.uuid) for message in first.messages] == [
            message["uuid"] for message in markdown[:2]
        ]
        assert [message.content for message in first.messages] == [
            "Decision 0",
            "Decision 1",
        ]
        assert first.boundary_message_uuid == sys_uuid.UUID(markdown[1]["uuid"])
        assert first.images == ()
        assert first.requires_vision is False
        complete(first)
        second = claim()
        assert second is not None
        assert [str(message.uuid) for message in second.messages] == [
            markdown[2]["uuid"]
        ]
        complete(second)
        assert claim() is None
    finally:
        api.put(
            f"/v1/messenger/topic_summary_settings/{api.project_id}",
            permissions=(topic_summarization.SETTINGS_MANAGE_PERMISSION,),
            json={"global_enabled": False, "project_enabled": False},
        )
        api.delete(
            f"/v1/messenger/topic_summary_endpoints/{endpoint.json()['uuid']}",
            permissions=(topic_summarization.ENDPOINT_MANAGE_PERMISSION,),
        )


@pytest.mark.pre_retirement_schema
def test_content_migration_round_trip_repairs_stored_mentions(
    content_migration_database,
):
    api, db = content_migration_database
    stream = test_key_announcements._create_stream(api)
    peer_uuid = test_key_announcements._add_member(api, db, stream["uuid"])
    content = f"[Peer](urn:user:{peer_uuid})"
    payload = _payloads(content)[1]
    message = _post_message(api, stream["uuid"], payload)
    test_messenger_v2._drain()
    definitions = {}
    with db.cursor() as cursor:
        for name in (
            "m_workspace_user_messages_view",
            "m_workspace_user_unread_messages_base_v1",
            "m_workspace_user_topic_unread_counts_v1",
        ):
            cursor.execute("SELECT pg_get_viewdef(%s::regclass)", (name,))
            definitions[name] = cursor.fetchone()[0]
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(MIGRATION)
    try:
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE messenger_user_message_states SET mentioned = true "
                "WHERE project_id = %s AND placement_uuid = %s",
                (api.project_id, message["uuid"]),
            )
            cursor.execute(
                "UPDATE messenger_stream_bindings "
                "SET notification_mode = 'mentions_only', active_unread_count = 1, "
                "passive_unread_count = 0 "
                "WHERE project_id = %s AND user_uuid = %s AND stream_uuid = %s",
                (api.project_id, peer_uuid, stream["uuid"]),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_message_mentions_v1 (
                    message_uuid, user_uuid, project_id, stream_uuid, topic_uuid,
                    ingest_sequence, created_at
                )
                SELECT uuid, %s, project_id, stream_uuid, topic_uuid,
                       ingest_sequence, NOW()
                FROM m_workspace_messages WHERE uuid = %s
                """,
                (peer_uuid, message["uuid"]),
            )
            cursor.execute(
                "SELECT to_regprocedure('workspace_message_markdown_content_v1(jsonb)')"
            )
            assert cursor.fetchone() == (None,)
        with contexts.Context().session_manager() as session:
            read_state.sync_stream_mentions_for_user(
                session, api.project_id, peer_uuid, stream["uuid"]
            )
    finally:
        engine.apply_migration(MIGRATION)
    test_messenger_v2._drain()
    history = api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid)
    assert history.status_code == 200, history.text
    assert history.json()["payload"] == payload
    assert history.json()["mentioned"] is False
    peer_stream = api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()
    assert peer_stream["active_unread_count"] == 0
    assert peer_stream["passive_unread_count"] == 1
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_message_mentions_v1 WHERE message_uuid = %s",
            (message["uuid"],),
        )
        assert cursor.fetchone() == (0,)
        for name, definition in definitions.items():
            cursor.execute("SELECT pg_get_viewdef(%s::regclass)", (name,))
            assert cursor.fetchone() == (definition,)


@pytest.mark.pre_retirement_schema
def test_compaction_rebuilds_only_markdown_mentions(api, db):
    stream = test_key_announcements._create_stream(api)
    peer_uuid = test_key_announcements._add_member(api, db, stream["uuid"])
    test_messenger_v2._drain()
    test_key_announcements._use_store("legacy")
    test_messenger_api._set_workspace_read_mode(
        db, api.project_id, read_state.PROJECT_MODE_LEGACY
    )
    content = f"[Peer](urn:user:{peer_uuid})"
    messages = [
        _post_message(api, stream["uuid"], payload) for payload in _payloads(content)
    ]
    with contexts.Context().session_manager() as session:
        read_state.begin_compaction(session, api.project_id)
    for _iteration in range(50):
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT mode FROM m_workspace_read_state_projects_v1 WHERE project_id = %s",
                (api.project_id,),
            )
            if cursor.fetchone()[0] == read_state.PROJECT_MODE_COMPACT:
                break
        with contexts.Context().session_manager() as session:
            read_state.compact_legacy_batch(session, api.project_id, batch_size=2)
    else:
        pytest.fail("Compact read-state cutover did not complete")
    mentioned = api.get(
        MESSAGES,
        user=peer_uuid,
        params={"stream_uuid": stream["uuid"], "mentioned": "true"},
    )
    assert mentioned.status_code == 200, mentioned.text
    assert [message["uuid"] for message in mentioned.json()] == [messages[0]["uuid"]]
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT message_uuid FROM m_workspace_message_mentions_v1 "
            "WHERE project_id = %s AND user_uuid = %s",
            (api.project_id, peer_uuid),
        )
        assert cursor.fetchall() == [(sys_uuid.UUID(messages[0]["uuid"]),)]
