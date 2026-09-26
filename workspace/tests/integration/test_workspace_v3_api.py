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
from restalchemy.common import contexts as ra_contexts

from workspace.messenger_api import file_storage
from workspace.messenger_api import topic_summarization
from workspace.messenger_api.api import sql_canonical_store, store_factory
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import v3_store
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
FOLDER_ITEMS = f"{V1}/folder_items/"
FILES = f"{V1}/files/"
TOPIC_SUMMARY_ENDPOINTS = f"{V1}/topic_summary_endpoints/"
TOPIC_SUMMARY_SETTINGS = f"{V1}/topic_summary_settings/"
TOPIC_SUMMARY_ENDPOINT_MANAGE = (topic_summarization.ENDPOINT_MANAGE_PERMISSION,)
TOPIC_SUMMARY_SETTINGS_MANAGE = (topic_summarization.SETTINGS_MANAGE_PERMISSION,)


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


def _run_database_operation(callback):
    with ra_contexts.Context().session_manager() as session:
        return callback(session)


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


def test_v3_rejects_cross_stream_topics_for_messages_and_drafts(api, db):
    first_stream = _create_stream(api, "First stream")
    second_stream = _create_stream(api, "Second stream")

    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": first_stream["uuid"],
            "topic_uuid": second_stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "wrong topic"},
        },
    )
    draft_uuid = sys_uuid.uuid4()
    draft = api.post(
        DRAFTS,
        json={
            "uuid": str(draft_uuid),
            "stream_uuid": first_stream["uuid"],
            "topic_uuid": second_stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "wrong topic"},
        },
    )

    assert message.status_code == 400, message.text
    assert draft.status_code == 400, draft.text
    assert (
        db.execute(
            "SELECT count(*) FROM workspace_v3.drafts WHERE uuid = %s",
            (draft_uuid,),
        ).fetchone()[0]
        == 0
    )


def test_v3_topic_without_default_is_legacy_boolean(api, db):
    stream = _create_stream(api, "Provider topic compatibility")
    db.execute(
        """
        UPDATE workspace_v3.streams
        SET default_topic_uuid = NULL
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, stream["uuid"]),
    )

    topics = api.get(TOPICS)

    assert topics.status_code == 200, topics.text
    topic = next(
        row for row in topics.json() if row["uuid"] == stream["default_topic_uuid"]
    )
    assert topic["is_default"] is False


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


def test_v3_rejects_deleting_system_folders_and_automatic_items(api, db):
    stream = _create_stream(api, "Protected automatic folders")
    _drain_projections(db)
    response = api.get(FOLDERS)
    assert response.status_code == 200, response.text
    folders = response.json()
    system_folder = next(folder for folder in folders if folder["system_type"] == "all")
    system_update = api.put(
        f"{FOLDERS}{system_folder['uuid']}",
        json={"title": "Renamed built-in folder"},
    )
    assert system_update.status_code == 400, system_update.text
    system_delete = api.delete(f"{FOLDERS}{system_folder['uuid']}")
    assert system_delete.status_code == 400, system_delete.text

    manual_item = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": system_folder["uuid"],
            "stream_uuid": stream["uuid"],
            "chat_type": "channel",
        },
    )
    assert manual_item.status_code == 400, manual_item.text

    automatic_item = next(
        item
        for item in system_folder["folder_items"]
        if item["stream_uuid"] == stream["uuid"]
    )
    item_delete = api.delete(f"{FOLDER_ITEMS}{automatic_item['uuid']}")
    assert item_delete.status_code == 400, item_delete.text


def test_v3_direct_message_creation_is_idempotent_and_membership_is_immutable(api, db):
    peer_uuid = sys_uuid.uuid4()
    assert api.get(f"{V1}/me/", user=peer_uuid).status_code == 200
    created = api.post(
        STREAMS,
        json={
            "name": "Protected direct message",
            "direct_user_uuid": str(peer_uuid),
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert created.status_code == 201, created.text
    repeated = api.post(
        STREAMS,
        json={
            "name": "Protected direct message",
            "direct_user_uuid": str(peer_uuid),
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["uuid"] == created.json()["uuid"]

    binding_uuid = db.execute(
        """
        SELECT uuid FROM workspace_v3.stream_bindings
        WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
        """,
        (api.project_id, created.json()["uuid"], api.user_uuid),
    ).fetchone()[0]
    binding_update = api.put(
        f"{STREAM_BINDINGS}{binding_uuid}",
        json={"notification_mode": "mute"},
    )
    assert binding_update.status_code == 400, binding_update.text
    binding_delete = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert binding_delete.status_code == 400, binding_delete.text

    deleted = api.delete(f"{STREAMS}{created.json()['uuid']}")

    assert deleted.status_code == 400, deleted.text
    assert api.get(f"{STREAMS}{created.json()['uuid']}").status_code == 200


def test_v3_private_non_direct_stream_keeps_null_direct_user_in_rest_and_events(
    api, workspace_api
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    created = api.post(
        STREAMS,
        json={"name": "Private channel", "private": True},
    )

    assert created.status_code == 201, created.text
    assert created.json()["direct_user_uuid"] is None
    events = workspace_api.get(f"{EVENTS}?epoch_version%3E=0&page_limit=100")
    assert events.status_code == 200, events.text
    stream_created = next(
        event
        for event in events.json()
        if event["payload"].get("kind") == "stream.created"
        and event["payload"].get("uuid") == created.json()["uuid"]
    )
    assert stream_created["payload"]["direct_user_uuid"] is None


def test_v3_private_stream_is_not_mistaken_for_direct_message(api):
    stream = api.post(
        STREAMS,
        json={
            "name": "Private but not direct",
            "private": True,
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream.status_code == 201, stream.text

    deleted = api.delete(f"{STREAMS}{stream.json()['uuid']}")

    assert deleted.status_code == 204, deleted.text
    assert api.get(f"{STREAMS}{stream.json()['uuid']}").status_code == 404


def test_v3_membership_delete_and_readd_rebuilds_user_state(api, db):
    stream = _create_stream(api, "Membership")
    historical = _create_message(api, stream, "history")
    peer_uuid = sys_uuid.uuid4()
    own_user = api.get(f"{V1}/me/", user=peer_uuid)
    assert own_user.status_code == 200, own_user.text
    before_add = db.execute(
        """
        SELECT COALESCE(max(epoch_version), 0)
        FROM workspace_v3.events
        WHERE project_id = %s
        """,
        (api.project_id,),
    ).fetchone()[0]

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
    membership_events = db.execute(
        """
        SELECT event.payload ->> 'kind', audience.consumer_uuid
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s
          AND event.entity_uuid = %s
          AND event.epoch_version > %s
          AND audience.consumer_type = 'user'
        """,
        (api.project_id, stream["uuid"], before_add),
    ).fetchall()
    assert ("stream.created", peer_uuid) in membership_events
    assert ("stream_bindings.created", peer_uuid) not in membership_events
    assert (
        "stream_bindings.created",
        sys_uuid.UUID(str(api.user_uuid)),
    ) in membership_events

    removed = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert removed.status_code == 204, removed.text
    removed_stream_event = db.execute(
        """
        SELECT event.payload ->> 'kind'
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s
          AND event.entity_uuid = %s
          AND event.payload ->> 'kind' = 'stream.deleted'
          AND audience.consumer_type = 'user'
          AND audience.consumer_uuid = %s
        """,
        (api.project_id, stream["uuid"], peer_uuid),
    ).fetchone()
    assert removed_stream_event == ("stream.deleted",)
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


def test_v3_stream_binding_updates_role_and_notification_mode(api):
    stream = _create_stream(api, "Mutable membership")
    invalid_notification = api.post(
        f"{STREAMS}{stream['uuid']}/actions/notifications/invoke",
        json={"notification_mode": "default"},
    )
    assert invalid_notification.status_code == 400, invalid_notification.text
    assert api.get(f"{STREAMS}{stream['uuid']}").json()["notification_mode"] == (
        "all_messages"
    )
    peer_uuid = sys_uuid.uuid4()
    assert api.get(f"{V1}/me/", user=peer_uuid).status_code == 200
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    binding_uuid = added.json()[0]["uuid"]

    role = api.put(f"{STREAM_BINDINGS}{binding_uuid}", json={"role": "administrator"})
    assert role.status_code == 200, role.text
    assert role.json()["role"] == "administrator"
    notification = api.put(
        f"{STREAM_BINDINGS}{binding_uuid}",
        json={"notification_mode": "muted"},
        user=peer_uuid,
    )
    assert notification.status_code == 200, notification.text
    assert notification.json()["notification_mode"] == "muted"
    assert notification.json()["notification_updated_at"] is not None


def test_v3_restricted_history_starts_at_membership(api, db):
    stream = _create_stream(api, "Restricted history")
    historical = _create_message(api, stream, "before join")
    historical_reaction = api.post(
        REACTIONS,
        json={"message_uuid": historical["uuid"], "emoji_name": "eyes"},
    )
    assert historical_reaction.status_code == 201, historical_reaction.text
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
    hidden_reactions = api.get(REACTIONS, user=peer_uuid)
    assert hidden_reactions.status_code == 200, hidden_reactions.text
    assert hidden_reactions.json() == []

    current = _create_message(api, stream, "after join")
    current_reaction = api.post(
        REACTIONS,
        json={"message_uuid": current["uuid"], "emoji_name": "rocket"},
    )
    assert current_reaction.status_code == 201, current_reaction.text
    visible = api.get(MESSAGES, user=peer_uuid)
    assert visible.status_code == 200, visible.text
    assert [message["uuid"] for message in visible.json()] == [current["uuid"]]
    visible_reactions = api.get(REACTIONS, user=peer_uuid)
    assert visible_reactions.status_code == 200, visible_reactions.text
    assert [reaction["uuid"] for reaction in visible_reactions.json()] == [
        current_reaction.json()["uuid"]
    ]
    assert historical["uuid"] != current["uuid"]


def test_v3_deleting_default_topic_emits_updated_stream(api, workspace_api, db):
    stream = _create_stream(api, "Default topic lifecycle")
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id

    deleted = api.delete(f"{TOPICS}{stream['default_topic_uuid']}")

    assert deleted.status_code == 204, deleted.text
    refreshed = api.get(f"{STREAMS}{stream['uuid']}")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["default_topic_uuid"] is None
    events = workspace_api.get("/v1/events/?epoch_version%3E=0&page_limit=100")
    assert events.status_code == 200, events.text
    assert any(
        event["payload"].get("kind") == "stream.updated"
        and event["payload"].get("uuid") == stream["uuid"]
        and event["payload"].get("default_topic_uuid") is None
        for event in events.json()
    )


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


def test_v3_provider_consumer_receives_only_its_source_events(api, db):
    provider_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.provider_consumers (
            uuid, project_id, name, iam_user_uuid
        ) VALUES (%s, %s, 'zulip', %s)
        """,
        (provider_uuid, api.project_id, sys_uuid.uuid4()),
    )
    zulip_stream = _create_stream(api, "Zulip source")
    db.execute(
        """
        UPDATE workspace_v3.streams
        SET source_name = 'zulip'
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, zulip_stream["uuid"]),
    )
    provider_message = _create_message(api, zulip_stream, "provider visible")
    starred = api.post(
        f"{MESSAGES}{provider_message['uuid']}/actions/star/invoke",
        json={},
    )
    assert starred.status_code == 200, starred.text
    native_stream = _create_stream(api, "Native source")
    native_message = _create_message(api, native_stream, "provider hidden")

    provider_rows = db.execute(
        """
        SELECT event.entity_uuid, event.payload ->> 'kind', recipient.payload
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
         AND audience.consumer_type = 'provider'
         AND audience.consumer_uuid = %s
        LEFT JOIN workspace_v3.event_recipient_payloads AS recipient
          ON recipient.project_id = event.project_id
         AND recipient.event_uuid = event.uuid
         AND recipient.consumer_type = 'provider'
         AND recipient.consumer_uuid = %s
        WHERE event.project_id = %s
        ORDER BY event.epoch_version
        """,
        (provider_uuid, provider_uuid, api.project_id),
    ).fetchall()

    assert len(provider_rows) == 2
    assert str(provider_rows[0][0]) == provider_message["uuid"]
    assert provider_rows[0][1] == "message.created"
    assert provider_rows[0][2] == {
        "author_uuid": str(api.user_uuid),
        "created_at": provider_message["created_at"],
        "payload": provider_message["payload"],
        "stream_uuid": provider_message["stream_uuid"],
        "topic_uuid": provider_message["topic_uuid"],
    }
    flag_uuid = db.execute(
        """
        SELECT uuid FROM workspace_v3.message_flags
        WHERE project_id = %s AND message_uuid = %s AND user_uuid = %s
        """,
        (api.project_id, provider_message["uuid"], api.user_uuid),
    ).fetchone()[0]
    assert provider_rows[1][0] == flag_uuid
    assert provider_rows[1][1] == "message_flag.updated"
    assert provider_rows[1][2] == {
        "mentioned": False,
        "message_uuid": provider_message["uuid"],
        "pinned": False,
        "read": True,
        "starred": True,
        "stream_uuid": provider_message["stream_uuid"],
        "user_uuid": str(api.user_uuid),
    }
    assert native_message["uuid"] not in {str(row[0]) for row in provider_rows}


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


def test_v3_event_epoch_filters_are_applied_before_page_limit(api, workspace_api):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    stream = _create_stream(api, "Filtered event page")
    for number in range(3):
        _create_message(api, stream, f"event {number}")
    all_events = workspace_api.get(
        EVENTS,
        params={"epoch_version>": 0, "page_limit": 100},
    )
    assert all_events.status_code == 200, all_events.text
    versions = [event["epoch_version"] for event in all_events.json()]
    assert len(versions) >= 3
    cutoff = versions[-1]

    filtered = workspace_api.get(
        EVENTS,
        params={
            "epoch_version<": cutoff,
            "sort_key": "epoch_version",
            "sort_dir": "desc",
            "page_limit": 1,
        },
    )

    assert filtered.status_code == 200, filtered.text
    assert [event["epoch_version"] for event in filtered.json()] == [versions[-2]]


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


def test_v3_topic_summary_prompt_requires_owner_or_administrator(api):
    stream = _create_stream(api, "V3 summary permissions")
    member_uuid = sys_uuid.uuid4()
    administrator_uuid = sys_uuid.uuid4()
    for user_uuid in (member_uuid, administrator_uuid):
        assert api.get(f"{V1}/me/", user=user_uuid).status_code == 200
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(member_uuid), str(administrator_uuid)]},
    )
    assert added.status_code == 200, added.text
    administrator_binding = next(
        binding
        for binding in added.json()
        if binding["user_uuid"] == str(administrator_uuid)
    )
    promoted = api.put(
        f"{STREAM_BINDINGS}{administrator_binding['uuid']}",
        json={"role": "administrator"},
    )
    assert promoted.status_code == 200, promoted.text
    action = f"{TOPICS}{stream['default_topic_uuid']}/actions/set_summary_prompt/invoke"

    forbidden = api.post(
        action,
        user=member_uuid,
        json={"summary_system_prompt": "Member prompt."},
    )
    assert forbidden.status_code == 403, forbidden.text
    invalid = api.post(
        action,
        json={"summary_reasoning_effort": "ultra"},
    )
    assert invalid.status_code == 400, invalid.text
    administrator_update = api.post(
        action,
        user=administrator_uuid,
        json={
            "summary_system_prompt": "Focus on decisions.",
            "summary_reasoning_effort": "off",
            "summary_enabled": False,
        },
    )
    assert administrator_update.status_code == 200, administrator_update.text
    assert administrator_update.json()["summary_enabled"] is False
    owner_update = api.post(
        action,
        json={"summary_enabled": True},
    )
    assert owner_update.status_code == 200, owner_update.text
    assert owner_update.json()["summary_enabled"] is True


def test_v3_topic_summary_worker_uses_v3_rows(api):
    endpoint_uuid = sys_uuid.uuid4()
    endpoint = api.post(
        TOPIC_SUMMARY_ENDPOINTS,
        permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
        json={
            "uuid": str(endpoint_uuid),
            "name": "v3-summary",
            "base_url": "https://llm.example.invalid/v1",
            "model": "summary-model",
            "api_key": "summary-secret",
            "priority": 10,
        },
    )
    assert endpoint.status_code == 201, endpoint.text
    settings = api.put(
        f"{TOPIC_SUMMARY_SETTINGS}{api.project_id}",
        permissions=TOPIC_SUMMARY_SETTINGS_MANAGE,
        json={"global_enabled": True, "project_enabled": True},
    )
    assert settings.status_code == 200, settings.text
    stream = _create_stream(api, "V3 summary worker")
    message = _create_message(api, stream, "Decision: release tomorrow.")
    now = datetime.datetime.now(datetime.timezone.utc)

    work = _run_database_operation(
        lambda session: topic_summarization.claim_summary_work(
            session,
            now=now,
            key_material="integration-test-topic-summary-key",
            topic_claim_seconds=60,
            endpoint_claim_seconds=60,
            storage_backend="v3",
        )
    )
    assert work is not None
    assert work.storage_backend == "v3"
    assert work.boundary_message_uuid == sys_uuid.UUID(message["uuid"])
    assert [item.content for item in work.messages] == ["Decision: release tomorrow."]
    _run_database_operation(
        lambda session: topic_summarization.complete_summary_work(
            session,
            work,
            "Release is planned for tomorrow.",
            now=now,
        )
    )

    topic = api.get(f"{TOPICS}{stream['default_topic_uuid']}")
    assert topic.status_code == 200, topic.text
    assert topic.json()["summary"] == "Release is planned for tomorrow."
    assert topic.json()["summary_last_message_uuid"] == message["uuid"]
    assert topic.json()["summary_has_new_messages"] is False


def test_v3_message_delete_restores_previous_topic_summary(api, db):
    stream = _create_stream(api, "V3 summary deletion")
    first = _create_message(api, stream, "first decision")
    second = _create_message(api, stream, "second decision")

    def store_summary(message_uuid, summary):
        return _run_database_operation(
            lambda session: v3_store.MessengerV3Store(
                api.project_id,
                api.user_uuid,
            ).set_topic_summary(
                sys_uuid.UUID(stream["default_topic_uuid"]),
                summary,
                sys_uuid.UUID(message_uuid),
            )
        )

    store_summary(first["uuid"], "First summary.")
    store_summary(second["uuid"], "Second summary.")
    deleted = api.delete(f"{MESSAGES}{second['uuid']}")
    assert deleted.status_code == 204, deleted.text

    restored = api.get(f"{TOPICS}{stream['default_topic_uuid']}").json()
    assert restored["summary"] == "First summary."
    assert restored["summary_last_message_uuid"] == first["uuid"]
    invalidated = db.execute(
        """
        SELECT count(*)
        FROM m_workspace_topic_summary_journal
        WHERE topic_uuid = %s AND invalidated_at IS NOT NULL
        """,
        (stream["default_topic_uuid"],),
    ).fetchone()[0]
    assert invalidated == 1


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


def test_v3_projection_owns_star_and_reaction_update_events(api, db):
    stream = _create_stream(api, "Single projection event")
    message = _create_message(api, stream)
    reaction = api.post(
        REACTIONS,
        json={"message_uuid": message["uuid"], "emoji_name": "eyes"},
    )
    assert reaction.status_code == 201, reaction.text
    _drain_projections(db)
    before_epoch = db.execute(
        """
        SELECT COALESCE(max(epoch_version), 0)
        FROM workspace_v3.events
        WHERE project_id = %s
        """,
        (api.project_id,),
    ).fetchone()[0]

    starred = api.post(f"{MESSAGES}{message['uuid']}/actions/star/invoke", json={})
    repeated_star = api.post(
        f"{MESSAGES}{message['uuid']}/actions/star/invoke",
        json={},
    )
    updated_reaction = api.put(
        f"{REACTIONS}{reaction.json()['uuid']}",
        json={"emoji_name": "heart"},
    )
    assert starred.status_code == 200, starred.text
    assert repeated_star.status_code == 200, repeated_star.text
    assert updated_reaction.status_code == 200, updated_reaction.text
    metrics = _drain_projections(db)
    assert metrics["failed"] == 0

    event_kinds = db.execute(
        """
        SELECT payload ->> 'kind', count(*)
        FROM workspace_v3.events
        WHERE project_id = %s AND epoch_version > %s
          AND (
              (entity_uuid = %s AND payload ->> 'kind' = 'message.updated')
              OR (
                  entity_uuid = %s
                  AND payload ->> 'kind' = 'message_reaction.updated'
              )
          )
        GROUP BY payload ->> 'kind'
        """,
        (
            api.project_id,
            before_epoch,
            message["uuid"],
            reaction.json()["uuid"],
        ),
    ).fetchall()
    assert dict(event_kinds) == {
        "message.updated": 1,
        "message_reaction.updated": 1,
    }


def test_v3_message_edit_emits_one_event_when_mentions_change(api, db):
    stream = _create_stream(api, "Mention edit")
    peer_uuid = sys_uuid.uuid4()
    assert api.get(f"{V1}/me/", user=peer_uuid).status_code == 200
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    message = _create_message(api, stream, "before")
    _drain_projections(db)
    before_epoch = db.execute(
        """
        SELECT COALESCE(max(epoch_version), 0)
        FROM workspace_v3.events
        WHERE project_id = %s
        """,
        (api.project_id,),
    ).fetchone()[0]

    updated = api.put(
        f"{MESSAGES}{message['uuid']}",
        json={
            "payload": {
                "kind": "markdown",
                "content": f"hello urn:user:{peer_uuid}",
            }
        },
    )
    assert updated.status_code == 200, updated.text
    metrics = _drain_projections(db)
    assert metrics["failed"] == 0

    peer_events = db.execute(
        """
        SELECT event.payload ->> 'kind'
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s
          AND event.entity_uuid = %s
          AND event.epoch_version > %s
          AND audience.consumer_type = 'user'
          AND audience.consumer_uuid = %s
        ORDER BY event.epoch_version
        """,
        (api.project_id, message["uuid"], before_epoch, peer_uuid),
    ).fetchall()
    assert peer_events == [("message.updated",)]
    assert (
        api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid).json()["mentioned"]
        is True
    )


def test_v3_reaction_creation_is_idempotent(api, db):
    stream = _create_stream(api, "Idempotent reactions")
    message = _create_message(api, stream)
    first = api.post(
        REACTIONS,
        json={"message_uuid": message["uuid"], "emoji_name": "eyes"},
    )
    repeated = api.post(
        REACTIONS,
        json={"message_uuid": message["uuid"], "emoji_name": "eyes"},
    )

    assert first.status_code == 201, first.text
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["uuid"] == first.json()["uuid"]
    assert (
        db.execute(
            """
        SELECT count(*) FROM workspace_v3.message_reactions
        WHERE project_id = %s AND message_uuid = %s
          AND user_uuid = %s AND emoji_name = 'eyes'
        """,
            (api.project_id, message["uuid"], api.user_uuid),
        ).fetchone()[0]
        == 1
    )


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


def test_v3_reaction_delete_emits_one_complete_legacy_event(
    api,
    workspace_api,
    db,
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    stream = _create_stream(api, "Reaction delete events")
    message = _create_message(api, stream)
    reaction = api.post(
        REACTIONS,
        json={"message_uuid": message["uuid"], "emoji_name": "eyes"},
    )
    assert reaction.status_code == 201, reaction.text
    _drain_projections(db)
    before_delete = workspace_api.get(EPOCH).json()

    deleted = api.delete(f"{REACTIONS}{reaction.json()['uuid']}")
    assert deleted.status_code == 204, deleted.text
    metrics = _drain_projections(db)
    assert metrics["failed"] == 0

    response = workspace_api.get(
        EVENTS,
        params={
            "epoch_version>": before_delete["current_epoch_version"],
            "epoch_generation": before_delete["epoch_generation"],
            "page_limit": 100,
        },
    )
    assert response.status_code == 200, response.text
    reaction_events = [
        event
        for event in response.json()
        if event["payload"]["kind"] == "message_reaction.deleted"
    ]
    assert [event["payload"] for event in reaction_events] == [
        {
            "kind": "message_reaction.deleted",
            "uuid": reaction.json()["uuid"],
            "project_id": str(api.project_id),
            "message_uuid": message["uuid"],
            "user_uuid": str(api.user_uuid),
            "emoji_name": "eyes",
            "source_name": "native",
            "source": {"kind": "native"},
        }
    ]


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
    assert api.get(f"{V1}/me/").status_code == 200
    other_project_uuid = sys_uuid.uuid4()
    other_user_uuid = sys_uuid.uuid4()
    other_stream_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.users (
            uuid, created_at, updated_at, username, source, status, avatar
        ) VALUES (
            %s, NOW(), NOW(), %s, 'iam', 'active',
            'urn:gravatar:00000000000000000000000000000000'
        )
        """,
        (other_user_uuid, f"other-{other_user_uuid}"),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.streams (
            uuid, project_id, name, owner_uuid
        ) VALUES (%s, %s, 'Shared identity', %s)
        """,
        (other_stream_uuid, other_project_uuid, api.user_uuid),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.stream_bindings (
            uuid, project_id, stream_uuid, user_uuid, who_uuid, role
        ) VALUES
            (gen_random_uuid(), %s, %s, %s, %s, 'owner'),
            (gen_random_uuid(), %s, %s, %s, %s, 'member')
        """,
        (
            other_project_uuid,
            other_stream_uuid,
            api.user_uuid,
            api.user_uuid,
            other_project_uuid,
            other_stream_uuid,
            other_user_uuid,
            api.user_uuid,
        ),
    )
    db.execute(
        "DELETE FROM workspace_v3.events WHERE entity_uuid = %s",
        (api.user_uuid,),
    )
    data = b"\x89PNG\r\n\x1a\nworkspace-v3-avatar"
    uploaded = api.post(
        f"{V1}/users/{api.user_uuid}/actions/avatar_upload/invoke",
        files={"file": ("avatar.png", io.BytesIO(data), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    event_projects = {
        row[0]
        for row in db.execute(
            """
            SELECT DISTINCT project_id
            FROM workspace_v3.events
            WHERE entity_uuid = %s AND payload ->> 'kind' = 'user.updated'
            """,
            (api.user_uuid,),
        ).fetchall()
    }
    assert event_projects == {
        sys_uuid.UUID(api.project_id),
        other_project_uuid,
    }
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


def test_v3_avatar_replacement_keeps_a_cross_project_previous_file(
    api,
    db,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    assert api.get(f"{V1}/me/").status_code == 200
    previous_file_uuid = sys_uuid.uuid4()
    other_project_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.files (
            uuid, project_id, user_uuid, acl_mode, name, content_type,
            size_bytes, hash, storage_type, storage_object_id
        ) VALUES (%s, %s, %s, 'public', 'other-avatar.png', 'image/png',
                  3, 'other-avatar', 'test', 'other-avatar')
        """,
        (previous_file_uuid, other_project_uuid, api.user_uuid),
    )
    db.execute(
        "UPDATE workspace_v3.users SET avatar = %s WHERE uuid = %s",
        (f"urn:image:{previous_file_uuid}", api.user_uuid),
    )

    uploaded = api.post(
        f"{V1}/users/{api.user_uuid}/actions/avatar_upload/invoke",
        files={
            "file": (
                "replacement.png",
                io.BytesIO(b"\x89PNG\r\n\x1a\nreplacement"),
                "image/png",
            )
        },
    )

    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["avatar"] != f"urn:image:{previous_file_uuid}"
    assert (
        db.execute(
            "SELECT count(*) FROM workspace_v3.files WHERE project_id = %s AND uuid = %s",
            (other_project_uuid, previous_file_uuid),
        ).fetchone()[0]
        == 1
    )


def test_v3_owner_files_stay_scoped_to_the_current_project(api, db):
    assert api.get(f"{V1}/me/").status_code == 200
    own_file_uuid = sys_uuid.uuid4()
    other_owner_file_uuid = sys_uuid.uuid4()
    public_file_uuid = sys_uuid.uuid4()
    other_project_id = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.files (
                uuid, project_id, user_uuid, acl_mode, name, content_type,
                size_bytes, hash, storage_type, storage_object_id
            ) VALUES
                (%s, %s, %s, 'owner', 'own.txt', 'text/plain',
                 3, 'own', 'test', 'own'),
                (%s, %s, %s, 'owner', 'other.txt', 'text/plain',
                 5, 'other', 'test', 'other'),
                (%s, %s, %s, 'public', 'public.txt', 'text/plain',
                 6, 'public', 'test', 'public')
            """,
            (
                own_file_uuid,
                api.project_id,
                api.user_uuid,
                other_owner_file_uuid,
                other_project_id,
                api.user_uuid,
                public_file_uuid,
                other_project_id,
                api.user_uuid,
            ),
        )

    response = api.get(FILES)

    assert response.status_code == 200, response.text
    visible = {row["uuid"] for row in response.json()}
    assert str(own_file_uuid) in visible
    assert str(public_file_uuid) in visible
    assert str(other_owner_file_uuid) not in visible

    cross_project_update = api.put(
        f"{FILES}{public_file_uuid}",
        json={"name": "must-not-change.txt"},
    )
    assert cross_project_update.status_code == 404, cross_project_update.text
    cross_project_delete = api.delete(f"{FILES}{public_file_uuid}")
    assert cross_project_delete.status_code == 404, cross_project_delete.text
    assert db.execute(
        "SELECT name FROM workspace_v3.files WHERE uuid = %s",
        (public_file_uuid,),
    ).fetchone() == ("public.txt",)
    db.execute(
        "DELETE FROM workspace_v3.files WHERE uuid = ANY(%s::uuid[])",
        ([own_file_uuid, other_owner_file_uuid, public_file_uuid],),
    )


def test_v3_presence_action_accepts_public_resource_uuid_and_broadcasts(api, db):
    stream = _create_stream(api, "Presence audience")
    peer_uuid = sys_uuid.uuid4()
    assert api.get(f"{V1}/me/", user=peer_uuid).status_code == 200
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    other_project_uuid = sys_uuid.uuid4()
    other_stream_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.streams (
            uuid, project_id, name, owner_uuid
        ) VALUES (%s, %s, 'Other project presence', %s)
        """,
        (other_stream_uuid, other_project_uuid, api.user_uuid),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.stream_bindings (
            uuid, project_id, stream_uuid, user_uuid, who_uuid, role
        ) VALUES (gen_random_uuid(), %s, %s, %s, %s, 'owner'),
                 (gen_random_uuid(), %s, %s, %s, %s, 'member')
        """,
        (
            other_project_uuid,
            other_stream_uuid,
            api.user_uuid,
            api.user_uuid,
            other_project_uuid,
            other_stream_uuid,
            peer_uuid,
            api.user_uuid,
        ),
    )

    response = api.post(
        f"{V1}/users/{api.user_uuid}/actions/presence/invoke",
        json={"status": "active"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["uuid"] == api.user_uuid
    assert response.json()["status"] == "active"
    peer_event = db.execute(
        """
        SELECT event.payload ->> 'kind'
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s
          AND event.entity_uuid = %s
          AND event.payload ->> 'kind' = 'user.updated'
          AND event.payload ->> 'status' = 'active'
          AND audience.consumer_type = 'user'
          AND audience.consumer_uuid = %s
        """,
        (api.project_id, api.user_uuid, peer_uuid),
    ).fetchone()
    assert peer_event == ("user.updated",)
    assert (
        db.execute(
            """
            SELECT count(*)
            FROM workspace_v3.events
            WHERE project_id = %s AND entity_uuid = %s
              AND payload ->> 'kind' = 'user.updated'
            """,
            (other_project_uuid, api.user_uuid),
        ).fetchone()[0]
        == 0
    )


def test_v3_user_mutation_refreshes_owning_provider_and_notifies_all(api, db):
    assert api.get(f"{V1}/me/").status_code == 200
    provider_uuids = tuple(sorted((sys_uuid.uuid4(), sys_uuid.uuid4())))
    owner_provider_uuid = provider_uuids[1]
    stream_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    source_names = ("alpha", "beta")
    db.execute(
        """
        INSERT INTO workspace_v3.provider_consumers (
            uuid, project_id, name, iam_user_uuid
        ) VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)
        """,
        (
            provider_uuids[0],
            api.project_id,
            source_names[0],
            sys_uuid.uuid4(),
            provider_uuids[1],
            api.project_id,
            source_names[1],
            sys_uuid.uuid4(),
        ),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.streams (
            uuid, project_id, name, owner_uuid, source_name
        ) VALUES (%s, %s, 'Alpha stream', %s, %s),
                 (%s, %s, 'Beta stream', %s, %s)
        """,
        (
            stream_uuids[0],
            api.project_id,
            api.user_uuid,
            source_names[0],
            stream_uuids[1],
            api.project_id,
            api.user_uuid,
            source_names[1],
        ),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.stream_bindings (
            uuid, project_id, stream_uuid, user_uuid, who_uuid, role
        ) VALUES (gen_random_uuid(), %s, %s, %s, %s, 'owner'),
                 (gen_random_uuid(), %s, %s, %s, %s, 'owner')
        """,
        (
            api.project_id,
            stream_uuids[0],
            api.user_uuid,
            api.user_uuid,
            api.project_id,
            stream_uuids[1],
            api.user_uuid,
            api.user_uuid,
        ),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.provider_entity_states (
            project_id, provider_uuid, entity_type, entity_uuid,
            content_hash, source_content_hash, source_updated_at
        ) VALUES (
            %s, %s, 'user', %s,
            decode(repeat('00', 32), 'hex'),
            decode(repeat('11', 32), 'hex'),
            clock_timestamp()
        )
        """,
        (api.project_id, owner_provider_uuid, api.user_uuid),
    )

    response = api.post(
        f"{V1}/users/{api.user_uuid}/actions/presence/invoke",
        json={"status": "active", "text": "shared provider user"},
    )

    assert response.status_code == 200, response.text
    state = db.execute(
        """
        SELECT provider_uuid, content_hash,
               encode(source_content_hash, 'hex')
        FROM workspace_v3.provider_entity_states
        WHERE project_id = %s AND entity_type = 'user' AND entity_uuid = %s
        """,
        (api.project_id, api.user_uuid),
    ).fetchone()
    assert state[0] == owner_provider_uuid
    assert bytes(state[1]) != bytes(32)
    assert state[2] == "11" * 32
    notified_providers = db.execute(
        """
        SELECT DISTINCT audience.consumer_uuid
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s AND event.entity_uuid = %s
          AND event.payload ->> 'kind' = 'user.updated'
          AND audience.consumer_type = 'provider'
        """,
        (api.project_id, api.user_uuid),
    ).fetchall()
    assert {row[0] for row in notified_providers} == set(provider_uuids)


def test_v3_user_mutation_ignores_unaffected_provider_when_owner_is_absent(api, db):
    assert api.get(f"{V1}/me/").status_code == 200
    affected_provider_uuid = sys_uuid.uuid4()
    owner_provider_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.provider_consumers (
            uuid, project_id, name, iam_user_uuid
        ) VALUES (%s, %s, 'affected', %s), (%s, %s, 'owner', %s)
        """,
        (
            affected_provider_uuid,
            api.project_id,
            sys_uuid.uuid4(),
            owner_provider_uuid,
            api.project_id,
            sys_uuid.uuid4(),
        ),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.streams (
            uuid, project_id, name, owner_uuid, source_name
        ) VALUES (%s, %s, 'Affected stream', %s, 'affected')
        """,
        (stream_uuid, api.project_id, api.user_uuid),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.stream_bindings (
            uuid, project_id, stream_uuid, user_uuid, who_uuid, role
        ) VALUES (gen_random_uuid(), %s, %s, %s, %s, 'owner')
        """,
        (api.project_id, stream_uuid, api.user_uuid, api.user_uuid),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.provider_entity_states (
            project_id, provider_uuid, entity_type, entity_uuid,
            content_hash, source_content_hash, source_updated_at
        ) VALUES (
            %s, %s, 'user', %s,
            decode(repeat('00', 32), 'hex'),
            decode(repeat('11', 32), 'hex'),
            clock_timestamp()
        )
        """,
        (api.project_id, owner_provider_uuid, api.user_uuid),
    )

    response = api.post(
        f"{V1}/users/{api.user_uuid}/actions/presence/invoke",
        json={"status": "active", "text": "owner no longer affected"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    state = db.execute(
        """
        SELECT provider_uuid, encode(content_hash, 'hex'),
               encode(source_content_hash, 'hex')
        FROM workspace_v3.provider_entity_states
        WHERE project_id = %s AND entity_type = 'user' AND entity_uuid = %s
        """,
        (api.project_id, api.user_uuid),
    ).fetchone()
    assert state == (owner_provider_uuid, "00" * 32, "11" * 32)
