# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Old Messenger HTTP contract backed by the clean Workspace v3 store."""

import datetime
import io
import uuid as sys_uuid

import psycopg
import pytest

from workspace.messenger_api import file_storage
from workspace.messenger_api.api import sql_canonical_store, store_factory
from workspace.messenger_api.api import store as api_store
from workspace.tests.integration import conftest
from workspace.workspace_v3 import projections

V1 = "/v1"
STREAMS = f"{V1}/streams/"
STREAM_BINDINGS = f"{V1}/stream_bindings/"
TOPICS = f"{V1}/stream_topics/"
MESSAGES = f"{V1}/messages/"
DRAFTS = f"{V1}/drafts/"
EVENTS = f"{V1}/events/"
EPOCH = f"{V1}/epoch/"
REACTIONS = f"{V1}/message_reactions/"
FOLDERS = f"{V1}/folders/"


@pytest.fixture(autouse=True)
def _v3_store():
    api_store.configure_store_factory(store_factory.build_v3_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(
            sql_canonical_store.SQLCanonicalMessengerStoreFactory()
        )


def _create_stream(api, name="V3 API"):
    response = api.post(
        STREAMS,
        json={
            "name": name,
            "description": "clean backend",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_message(api, stream, content="hello"):
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": content},
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _drain_projections(db):
    metrics = {"completed": 0.0, "failed": 0.0, "events": 0.0}
    while True:
        with db.transaction():
            tasks = projections.claim_projection_tasks(
                db,
                "integration:v3:api",
                batch_size=1000,
            )
        if not tasks:
            return metrics
        with db.transaction():
            batch = projections.process_claimed_projection_tasks(
                db,
                "integration:v3:api",
                tasks,
            )
        for name in metrics:
            metrics[name] += batch[name]


def test_v3_api_keeps_routes_and_reduces_provider_projection(api):
    stream = _create_stream(api)
    assert stream["source_name"] == "native"
    assert stream["source"] == {"kind": "native"}
    assert "provider" not in stream
    assert "delivery" not in stream
    assert stream["created_at"].endswith("Z")
    assert stream["updated_at"].endswith("Z")

    topic = api.get(f"{TOPICS}{stream['default_topic_uuid']}")
    assert topic.status_code == 200, topic.text
    assert topic.json()["is_default"] is True

    first = _create_message(api, stream, "first")
    second = _create_message(api, stream, "second")
    page = api.get(
        MESSAGES,
        params={"sort_key": "created_at", "sort_dir": "asc", "page_limit": 1},
    )
    assert page.status_code == 200, page.text
    assert [row["uuid"] for row in page.json()] == [first["uuid"]]
    marker = page.headers["X-Pagination-Marker"]
    next_page = api.get(
        MESSAGES,
        params={
            "sort_key": "created_at",
            "sort_dir": "asc",
            "page_limit": 1,
            "page_marker": marker,
        },
    )
    assert next_page.status_code == 200, next_page.text
    assert [row["uuid"] for row in next_page.json()] == [second["uuid"]]
    assert second["created_at"].endswith("Z")


def test_v3_initializes_the_three_legacy_automatic_folders(api):
    response = api.get(FOLDERS)

    assert response.status_code == 200, response.text
    assert {
        (folder["uuid"], folder["title"], folder["system_type"])
        for folder in response.json()
    } == {
        ("00000000-0000-0000-0000-000000000000", "All chats", "all"),
        ("00000000-0000-0000-0000-000000000001", "Personal", "created"),
        ("00000000-0000-0000-0000-000000000002", "Channels", "created"),
    }


def test_v3_membership_delete_and_readd_rebuilds_user_state(api, db):
    stream = _create_stream(api, "Membership")
    historical = _create_message(api, stream, "history")
    peer_uuid = sys_uuid.uuid4()
    own_user = api.get(f"{V1}/me/", user=peer_uuid)
    assert own_user.status_code == 200, own_user.text

    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    binding_uuid = added.json()[0]["uuid"]
    peer_messages = api.get(MESSAGES, user=peer_uuid)
    assert peer_messages.status_code == 200, peer_messages.text
    assert peer_messages.json()[0]["uuid"] == historical["uuid"]
    assert peer_messages.json()[0]["read"] is True

    removed = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert removed.status_code == 204, removed.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM workspace_v3.topic_bindings
                 WHERE project_id = %s AND user_uuid = %s),
                (SELECT count(*) FROM workspace_v3.message_flags
                 WHERE project_id = %s AND user_uuid = %s),
                (SELECT count(*) FROM workspace_v3.folder_items
                 WHERE project_id = %s AND user_uuid = %s
                   AND stream_uuid = %s)
            """,
            (
                api.project_id,
                peer_uuid,
                api.project_id,
                peer_uuid,
                api.project_id,
                peer_uuid,
                stream["uuid"],
            ),
        )
        assert cursor.fetchone() == (0, 0, 0)

    added_again = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added_again.status_code == 200, added_again.text
    assert added_again.json()[0]["uuid"] != binding_uuid
    reloaded = api.get(MESSAGES, user=peer_uuid)
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()[0]["read"] is True


def test_v3_restricted_history_starts_at_membership(api, db):
    stream = _create_stream(api, "Restricted history")
    historical = _create_message(api, stream, "before join")
    peer_uuid = sys_uuid.uuid4()
    assert api.get(f"{V1}/me/", user=peer_uuid).status_code == 200
    db.execute(
        """
        UPDATE workspace_v3.streams
        SET history_public_to_subscribers = false
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, stream["uuid"]),
    )

    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    assert (
        db.execute(
            """
        SELECT last_message_uuid
        FROM workspace_v3.stream_bindings
        WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
        """,
            (api.project_id, stream["uuid"], peer_uuid),
        ).fetchone()[0]
        is None
    )
    hidden = api.get(MESSAGES, user=peer_uuid)
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == []

    current = _create_message(api, stream, "after join")
    visible = api.get(MESSAGES, user=peer_uuid)
    assert visible.status_code == 200, visible.text
    assert [message["uuid"] for message in visible.json()] == [current["uuid"]]
    assert historical["uuid"] != current["uuid"]


def test_v3_private_stream_rejects_third_member_with_controlled_4xx(api):
    created = api.post(
        STREAMS,
        json={
            "name": "Private pair",
            "description": "",
            "private": True,
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert created.status_code == 201, created.text
    peer_uuid = sys_uuid.uuid4()
    third_uuid = sys_uuid.uuid4()
    assert api.get(f"{V1}/me/", user=peer_uuid).status_code == 200
    assert api.get(f"{V1}/me/", user=third_uuid).status_code == 200
    added = api.post(
        f"{STREAMS}{created.json()['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text

    rejected = api.post(
        f"{STREAMS}{created.json()['uuid']}/actions/add_users/invoke",
        json={"member": [str(third_uuid)]},
    )

    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["type"] == "PrivateStreamMemberLimitError"


def test_v3_events_notify_websocket_listener_after_commit(api):
    listener = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
    try:
        listener.execute("LISTEN workspace_events")
        _create_stream(api, "Live notification")
        notification = next(listener.notifies(timeout=1, stop_after=1), None)
    finally:
        listener.close()

    assert notification is not None
    assert int(notification.payload) > 0


def test_v3_drafts_and_event_cursor_keep_old_contract(api, workspace_api, db):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    stream = _create_stream(api, "Drafts and events")
    message = _create_message(api, stream)
    draft_uuid = sys_uuid.uuid4()
    body = {
        "uuid": str(draft_uuid),
        "stream_uuid": stream["uuid"],
        "topic_uuid": stream["default_topic_uuid"],
        "payload": {"kind": "markdown", "content": "draft"},
    }
    created = api.post(DRAFTS, json=body)
    assert created.status_code == 201, created.text
    assert created.headers["ETag"] == '"1"'
    updated = api.put(
        f"{DRAFTS}{draft_uuid}",
        headers={"If-Match": '"1"'},
        json={"payload": {"kind": "markdown", "content": "changed"}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.headers["ETag"] == '"2"'

    epoch = workspace_api.get(EPOCH)
    assert epoch.status_code == 200, epoch.text
    cursor = epoch.json()
    assert cursor["epoch_version"] >= 3
    events = workspace_api.get(f"{EVENTS}?epoch_version%3E=0&page_limit=100")
    assert events.status_code == 200, events.text
    kinds = [event["payload"]["kind"] for event in events.json()]
    assert "stream.created" in kinds
    assert "topic.created" in kinds
    assert "message.created" in kinds
    message_event = next(
        event
        for event in events.json()
        if event["payload"]["kind"] == "message.created"
    )
    assert message_event["payload"]["uuid"] == message["uuid"]
    assert message_event["payload"]["read"] is True
    base_payload, recipient_payload = db.execute(
        """
        SELECT event.payload, recipient.payload
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_recipient_payloads AS recipient
          ON recipient.project_id = event.project_id
         AND recipient.event_uuid = event.uuid
        WHERE event.project_id = %s AND event.entity_uuid = %s
          AND recipient.consumer_type = 'user'
          AND recipient.consumer_uuid = %s
        """,
        (api.project_id, message["uuid"], api.user_uuid),
    ).fetchone()
    assert "user_uuid" not in base_payload
    assert "read" not in base_payload
    assert recipient_payload["user_uuid"] == str(api.user_uuid)
    assert recipient_payload["read"] is True
    assert message_event["created_at"].endswith("Z")

    deleted = api.delete(f"{DRAFTS}{draft_uuid}", headers={"If-Match": '"2"'})
    assert deleted.status_code == 204, deleted.text


def test_v3_user_actions_keep_scope_events_and_global_topic_done(api, db):
    stream = _create_stream(api, "Actions")
    peer_uuid = sys_uuid.uuid4()
    assert api.get(f"{V1}/me/", user=peer_uuid).status_code == 200
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    topic_response = api.post(
        TOPICS,
        json={
            "stream_uuid": stream["uuid"],
            "name": "Second",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert topic_response.status_code == 201, topic_response.text
    second_topic = topic_response.json()
    first = _create_message(api, stream, "first")
    second = _create_message(api, stream, "second")
    other_topic_message_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": second_topic["uuid"],
            "payload": {"kind": "markdown", "content": "other topic"},
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert other_topic_message_response.status_code == 201
    other_topic_message = other_topic_message_response.json()
    _drain_projections(db)

    read = api.post(
        f"{MESSAGES}{second['uuid']}/actions/read_up_to/invoke",
        user=peer_uuid,
    )
    assert read.status_code == 200, read.text
    starred = api.post(
        f"{MESSAGES}{second['uuid']}/actions/star/invoke",
        user=peer_uuid,
    )
    assert starred.status_code == 200, starred.text
    assert starred.json()["starred"] is True

    invalid_unmute = api.post(
        f"{TOPICS}{second_topic['uuid']}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "unmute"},
    )
    assert invalid_unmute.status_code == 400, invalid_unmute.text
    muted = api.post(
        f"{STREAMS}{stream['uuid']}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "muted"},
    )
    assert muted.status_code == 200, muted.text
    unmuted_topic = api.post(
        f"{TOPICS}{second_topic['uuid']}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "unmute"},
    )
    assert unmuted_topic.status_code == 200, unmuted_topic.text
    normalized = api.post(
        f"{STREAMS}{stream['uuid']}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "all_messages"},
    )
    assert normalized.status_code == 200, normalized.text
    topic_for_peer = api.get(f"{TOPICS}{second_topic['uuid']}", user=peer_uuid)
    assert topic_for_peer.json()["notification_mode"] == "default"

    toggled = api.post(
        f"{TOPICS}{second_topic['uuid']}/actions/toggle_done/invoke",
    )
    assert toggled.status_code == 200, toggled.text
    assert (
        api.get(f"{TOPICS}{second_topic['uuid']}", user=peer_uuid).json()["is_done"]
        is True
    )
    defaulted = api.post(
        f"{TOPICS}{second_topic['uuid']}/actions/set_default/invoke",
    )
    assert defaulted.status_code == 200, defaulted.text
    assert defaulted.json()["is_default"] is True

    metrics = _drain_projections(db)
    assert metrics["failed"] == 0
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT message_uuid, read, starred
            FROM workspace_v3.message_flags
            WHERE project_id = %s AND user_uuid = %s
              AND message_uuid = ANY(%s::uuid[])
            ORDER BY message_uuid
            """,
            (
                api.project_id,
                peer_uuid,
                [first["uuid"], second["uuid"], other_topic_message["uuid"]],
            ),
        )
        flags = {str(row[0]): row[1:] for row in cursor.fetchall()}
    assert flags[first["uuid"]] == (True, False)
    assert flags[second["uuid"]] == (True, True)
    assert flags[other_topic_message["uuid"]] == (False, False)


def test_v3_reaction_worker_updates_denormalized_message(api, db):
    stream = _create_stream(api, "Reactions")
    message = _create_message(api, stream)
    _drain_projections(db)
    reaction = api.post(
        REACTIONS,
        json={"message_uuid": message["uuid"], "emoji_name": "eyes"},
    )
    assert reaction.status_code == 201, reaction.text
    before = api.get(f"{MESSAGES}{message['uuid']}").json()
    assert before["reactions"] == {}

    metrics = _drain_projections(db)
    assert metrics["failed"] == 0
    assert metrics["events"] >= 2
    after = api.get(f"{MESSAGES}{message['uuid']}").json()
    assert after["reactions"] == {"eyes": 1}
    assert after["reaction_users"] == {"eyes": [str(api.user_uuid)]}


def test_v3_projection_events_keep_flat_v2_public_payloads(
    api,
    workspace_api,
    db,
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    stream = _create_stream(api, "Complete events")
    message = _create_message(api, stream)
    reaction = api.post(
        REACTIONS,
        json={"message_uuid": message["uuid"], "emoji_name": "eyes"},
    )
    assert reaction.status_code == 201, reaction.text
    assert reaction.json()["source_name"] == "native"
    assert reaction.json()["source"] == {"kind": "native"}

    metrics = _drain_projections(db)
    assert metrics["failed"] == 0
    response = workspace_api.get(f"{EVENTS}?epoch_version%3E=0&page_limit=100")
    assert response.status_code == 200, response.text
    events = response.json()

    stream_created = next(
        event for event in events if event["payload"]["kind"] == "stream.created"
    )
    assert stream_created["payload"]["uuid"] == stream["uuid"]
    assert "private_index" not in stream_created["payload"]
    assert stream_created["payload"]["created_at"].endswith("Z")

    reaction_created = next(
        event
        for event in events
        if event["payload"]["kind"] == "message_reaction.created"
    )
    assert reaction_created["payload"] == {
        "kind": "message_reaction.created",
        "uuid": reaction.json()["uuid"],
        "project_id": str(api.project_id),
        "message_uuid": message["uuid"],
        "user_uuid": str(api.user_uuid),
        "emoji_name": "eyes",
        "source_name": "native",
        "source": {"kind": "native"},
    }

    message_updated = next(
        event
        for event in events
        if event["payload"]["kind"] == "message.updated"
        and event["payload"]["uuid"] == message["uuid"]
    )
    assert message_updated["payload"]["reactions"] == {"eyes": 1}
    assert message_updated["payload"]["read"] is True
    assert message_updated["payload"]["updated_at"].endswith("Z")

    folder_events = [
        event for event in events if event["payload"]["kind"].startswith("folder.")
    ]
    assert {event["payload"]["kind"] for event in folder_events} == {"folder.updated"}
    assert {event["payload"]["system_type"] for event in folder_events} == {
        "all",
        "created",
    }
    assert all("folder_items" in event["payload"] for event in folder_events)
    assert all(event["payload"]["updated_at"].endswith("Z") for event in folder_events)


def test_v3_event_retention_advances_reconnect_floor(api, workspace_api, db):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    _create_stream(api, "Retention")
    cursor = workspace_api.get(EPOCH).json()
    with db.cursor() as database_cursor:
        database_cursor.execute(
            """
            UPDATE workspace_v3.events
            SET created_at = %s
            WHERE project_id = %s
            """,
            (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=2),
                api.project_id,
            ),
        )
    assert projections.prune_event_journal(db, retention_hours=1) == 2
    expired = workspace_api.get(
        EVENTS,
        params={
            "epoch_version>": 0,
            "page_limit": 100,
            "epoch_generation": cursor["epoch_generation"],
        },
    )
    assert expired.status_code == 410, expired.text
    assert expired.json()["reason"] == "epoch_pruned"


def test_v3_avatar_file_lifecycle_uses_v3_metadata(api, db, tmp_path, monkeypatch):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    data = b"\x89PNG\r\n\x1a\nworkspace-v3-avatar"
    uploaded = api.post(
        f"{V1}/users/{api.user_uuid}/actions/avatar_upload/invoke",
        files={"file": ("avatar.png", io.BytesIO(data), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    file_uuid = uploaded.json()["avatar"].removeprefix("urn:image:")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT acl_mode, stream_uuid
            FROM workspace_v3.files
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, file_uuid),
        )
        assert cursor.fetchone() == ("public", None)

    reset = api.post(
        f"{V1}/users/{api.user_uuid}/actions/avatar_reset/invoke",
        json={},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["avatar"].startswith("urn:gravatar:")
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM workspace_v3.files WHERE uuid = %s",
            (file_uuid,),
        )
        assert cursor.fetchone()[0] == 0
