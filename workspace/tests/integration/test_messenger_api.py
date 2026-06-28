#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""End-to-end messenger API tests against a real server + test database."""

import uuid as sys_uuid

from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import message_payloads
from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/streams/"
STREAM_BINDINGS = f"{V1}/stream_bindings/"
FOLDERS = f"{V1}/folders/"
FOLDER_ITEMS = f"{V1}/folder_items/"
STREAM_TOPICS = f"{V1}/stream_topics/"
MESSAGES = f"{V1}/messages/"
EVENTS = f"{V1}/events/"
EPOCH = f"{V1}/epoch/"
USERS = f"{V1}/users/"


# --------------------------------------------------------------------------- #
# Smoke
# --------------------------------------------------------------------------- #


def test_root_endpoint_is_served(api):
    resp = api.get(f"{V1}/")
    assert resp.status_code == 200, resp.text


def test_user_get_by_uuid_uses_global_user_table(api, db):
    user_uuid = sys_uuid.uuid4()
    username = f"user-{user_uuid}"
    conftest.seed_workspace_user(db, user_uuid, username)

    resp = api.get(f"{USERS}{user_uuid}")
    assert resp.status_code == 200, resp.text
    user = resp.json()
    assert user["uuid"] == str(user_uuid)
    assert user["username"] == username

    resp = api.get(USERS, params={"username": username})
    assert resp.status_code == 200, resp.text
    assert [user["uuid"] for user in resp.json()] == [str(user_uuid)]


# --------------------------------------------------------------------------- #
# Folders: full write path through the real ORM
# --------------------------------------------------------------------------- #


def test_folder_crud_roundtrip(api):
    # create
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    folder_uuid = folder["uuid"]
    assert folder["title"] == "Inbox"
    # hidden fields must not leak
    assert "user_uuid" not in folder
    assert "project_id" not in folder

    # get
    resp = api.get(f"{FOLDERS}{folder_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == folder_uuid

    # update
    resp = api.put(f"{FOLDERS}{folder_uuid}", json={"title": "Archive"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Archive"

    # delete
    resp = api.delete(f"{FOLDERS}{folder_uuid}")
    assert resp.status_code in (200, 204), resp.text

    # gone
    resp = api.get(f"{FOLDERS}{folder_uuid}")
    assert resp.status_code == 404, resp.text


def test_folder_create_writes_realtime_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    epoch_version, user_uuid, payload = rows[0]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["kind"] == "folder.created"
    assert payload["uuid"] == folder["uuid"]
    assert payload["title"] == "Inbox"
    assert payload["user_uuid"] == str(api.user_uuid)
    assert payload["project_id"] == str(api.project_id)
    assert payload["unread_count"] == 0
    assert payload["folder_items"] == []

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["type"] == "folder"
    assert event["kind"] == "folder.created"
    assert event["folder"]["uuid"] == folder["uuid"]
    assert event["folder"]["title"] == "Inbox"


def test_folder_update_writes_realtime_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()

    resp = api.put(f"{FOLDERS}{folder['uuid']}", json={"title": "Archive"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Archive"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    epoch_version, user_uuid, payload = rows[1]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["kind"] == "folder.updated"
    assert payload["uuid"] == folder["uuid"]
    assert payload["title"] == "Archive"
    assert payload["user_uuid"] == str(api.user_uuid)
    assert payload["project_id"] == str(api.project_id)
    assert payload["unread_count"] == 0
    assert payload["folder_items"] == []

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["type"] == "folder"
    assert event["kind"] == "folder.updated"
    assert event["folder"]["uuid"] == folder["uuid"]
    assert event["folder"]["title"] == "Archive"


def test_folder_delete_writes_realtime_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()

    resp = api.delete(f"{FOLDERS}{folder['uuid']}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{FOLDERS}{folder['uuid']}")
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    epoch_version, user_uuid, payload = rows[1]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload == {
        "kind": "folder.deleted",
        "uuid": folder["uuid"],
    }

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["type"] == "folder"
    assert event["kind"] == "folder.deleted"
    assert event["folder"] == {"uuid": folder["uuid"]}


def test_folder_item_create_writes_folder_updated_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "standups"
    )

    resp = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder["uuid"],
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    item = resp.json()
    assert item["folder_uuid"] == folder["uuid"]
    assert item["stream_uuid"] == stream_uuid

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    epoch_version, user_uuid, payload = rows[1]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["kind"] == "folder.updated"
    assert payload["uuid"] == folder["uuid"]
    assert payload["title"] == "Inbox"
    assert payload["user_uuid"] == str(api.user_uuid)
    assert payload["project_id"] == str(api.project_id)
    assert payload["unread_count"] == 0
    assert len(payload["folder_items"]) == 1
    assert payload["folder_items"][0]["uuid"] == item["uuid"]
    assert payload["folder_items"][0]["folder_uuid"] == folder["uuid"]
    assert payload["folder_items"][0]["stream_uuid"] == stream_uuid

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["type"] == "folder"
    assert event["kind"] == "folder.updated"
    assert event["folder"]["uuid"] == folder["uuid"]
    assert event["folder"]["folder_items"][0]["stream_uuid"] == stream_uuid


def test_folder_item_delete_writes_deleted_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "standups"
    )
    resp = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder["uuid"],
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    item = resp.json()

    resp = api.delete(f"{FOLDER_ITEMS}{item['uuid']}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{FOLDER_ITEMS}{item['uuid']}")
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 3
    epoch_version, user_uuid, payload = rows[2]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload == {
        "kind": "folder_item.deleted",
        "uuid": item["uuid"],
    }

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["type"] == "folder_item"
    assert event["kind"] == "folder_item.deleted"
    assert event["folder_item"] == {"uuid": item["uuid"]}


def test_folder_item_pin_unpin_actions_write_folder_updated_events(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "standups"
    )
    resp = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder["uuid"],
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    item = resp.json()
    assert item["pinned_at"] is None

    resp = api.post(f"{FOLDER_ITEMS}{item['uuid']}/actions/pin/invoke")
    assert resp.status_code == 200, resp.text
    pinned_item = resp.json()
    assert pinned_item["uuid"] == item["uuid"]
    assert pinned_item["pinned_at"] is not None

    resp = api.post(f"{FOLDER_ITEMS}{item['uuid']}/actions/unpin/invoke")
    assert resp.status_code == 200, resp.text
    unpinned_item = resp.json()
    assert unpinned_item["uuid"] == item["uuid"]
    assert unpinned_item["pinned_at"] is None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 4
    _, user_uuid, pin_payload = rows[2]
    assert str(user_uuid) == str(api.user_uuid)
    assert pin_payload["kind"] == "folder.updated"
    assert pin_payload["uuid"] == folder["uuid"]
    assert pin_payload["folder_items"][0]["uuid"] == item["uuid"]
    assert pin_payload["folder_items"][0]["pinned_at"] is not None

    epoch_version, user_uuid, unpin_payload = rows[3]
    assert str(user_uuid) == str(api.user_uuid)
    assert unpin_payload["kind"] == "folder.updated"
    assert unpin_payload["uuid"] == folder["uuid"]
    assert unpin_payload["folder_items"][0]["uuid"] == item["uuid"]
    assert unpin_payload["folder_items"][0]["pinned_at"] is None

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": unpin_payload,
        }
    )
    assert event["type"] == "folder"
    assert event["kind"] == "folder.updated"
    assert event["folder"]["folder_items"][0]["pinned_at"] is None


def test_system_folder_item_pin_unpin_actions_materialize_user_item(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "system-pins"
    )
    item_uuid = f"00{stream_uuid[2:]}"

    resp = api.post(f"{FOLDER_ITEMS}{item_uuid}/actions/pin/invoke")
    assert resp.status_code == 200, resp.text
    pinned_item = resp.json()
    assert pinned_item["uuid"] == item_uuid
    assert pinned_item["stream_uuid"] == stream_uuid
    assert pinned_item["folder_uuid"] == str(
        messenger_dm_helpers.ALL_CHATS_FOLDER_UUID
    )
    assert pinned_item["pinned_at"] is not None

    resp = api.get(f"{FOLDERS}{messenger_dm_helpers.ALL_CHATS_FOLDER_UUID}")
    assert resp.status_code == 200, resp.text
    folder_item = [
        item for item in resp.json()["folder_items"]
        if item["uuid"] == item_uuid
    ][0]
    assert folder_item["pinned_at"] is not None

    resp = api.post(f"{FOLDER_ITEMS}{item_uuid}/actions/unpin/invoke")
    assert resp.status_code == 200, resp.text
    unpinned_item = resp.json()
    assert unpinned_item["uuid"] == item_uuid
    assert unpinned_item["pinned_at"] is None

    resp = api.get(f"{FOLDERS}{messenger_dm_helpers.ALL_CHATS_FOLDER_UUID}")
    assert resp.status_code == 200, resp.text
    folder_item = [
        item for item in resp.json()["folder_items"]
        if item["uuid"] == item_uuid
    ][0]
    assert folder_item["pinned_at"] is None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_folder_items
            WHERE uuid = %s
                AND project_id = %s
                AND user_uuid = %s
                AND folder_uuid = %s
                AND stream_uuid = %s
            """,
            (
                item_uuid,
                api.project_id,
                api.user_uuid,
                str(messenger_dm_helpers.ALL_CHATS_FOLDER_UUID),
                stream_uuid,
            ),
        )
        item_count = cur.fetchone()[0]

    assert item_count == 1


def test_folders_are_scoped_to_the_authenticated_user(api):
    other_user = sys_uuid.uuid4()

    api.post(FOLDERS, json={"title": "mine"})
    api.post(FOLDERS, json={"title": "theirs"}, user=other_user)

    titles = [f["title"] for f in api.get(FOLDERS).json()]
    assert titles == ["mine"]

    other_titles = [f["title"] for f in api.get(FOLDERS, user=other_user).json()]
    assert other_titles == ["theirs"]


# --------------------------------------------------------------------------- #
# Streams: composite primary key controller (read paths)
# --------------------------------------------------------------------------- #


def test_streams_list_is_scoped_to_user(api, db):
    other_user = sys_uuid.uuid4()
    for i in range(3):
        conftest.seed_user_stream(db, api.project_id, api.user_uuid, f"mine-{i}")
    for i in range(2):
        conftest.seed_user_stream(db, api.project_id, other_user, f"other-{i}")

    resp = api.get(STREAMS)
    assert resp.status_code == 200, resp.text
    names = sorted(s["name"] for s in resp.json())
    assert names == ["mine-0", "mine-1", "mine-2"]


def test_stream_get_by_uuid_is_scoped(api, db):
    other_user = sys_uuid.uuid4()
    mine = conftest.seed_user_stream(db, api.project_id, api.user_uuid, "mine")
    theirs = conftest.seed_user_stream(db, api.project_id, other_user, "theirs")

    # own row is visible
    resp = api.get(f"{STREAMS}{mine}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == mine
    assert resp.json()["name"] == "mine"

    # another user's row, addressed by its real uuid, is not found
    resp = api.get(f"{STREAMS}{theirs}")
    assert resp.status_code == 404, resp.text


def test_stream_create_writes_realtime_event(api, db):
    resp = api.post(
        STREAMS,
        json={
            "name": "Engineering",
            "description": "Engineering workspace",
            "source_name": "native",
            "source": {"kind": "native"},
            "invite_only": False,
            "announce": False,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    stream = resp.json()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 3
    epoch_version, user_uuid, payload = rows[0]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["kind"] == "stream.created"
    assert payload["uuid"] == stream["uuid"]
    assert payload["name"] == "Engineering"
    assert payload["description"] == "Engineering workspace"
    assert payload["user_uuid"] == str(api.user_uuid)
    assert payload["project_id"] == str(api.project_id)
    assert payload["owner"] == str(api.user_uuid)
    assert payload["role"] == "owner"
    assert payload["notification_mode"] == "all_messages"
    assert payload["unread_count"] == 0
    assert payload["source_name"] == "native"
    assert payload["source"] == {"kind": "native"}

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["type"] == "stream"
    assert event["kind"] == "stream.created"
    assert event["stream"]["uuid"] == stream["uuid"]
    assert event["stream"]["name"] == "Engineering"
    assert event["stream"]["role"] == "owner"
    assert event["stream"]["notification_mode"] == "all_messages"

    folder_events = [row[2] for row in rows[1:]]
    assert [payload["kind"] for payload in folder_events] == [
        "folder.updated",
        "folder.updated",
    ]
    assert [payload["uuid"] for payload in folder_events] == [
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-000000000002",
    ]
    assert [payload["title"] for payload in folder_events] == [
        "All chats",
        "Channels",
    ]
    assert all(
        payload["user_uuid"] == str(api.user_uuid)
        for payload in folder_events
    )


def test_stream_notifications_are_user_scoped_and_write_event(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "notifications-team"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )

    resp = api.get(f"{STREAMS}{stream_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "all_messages"

    resp = api.get(f"{STREAMS}{stream_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "all_messages"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_epoch = cur.fetchone()[0]

    resp = api.post(
        f"{STREAMS}{stream_uuid}/actions/notifications/invoke",
        json={"notification_mode": "mentions_only"},
    )
    assert resp.status_code == 200, resp.text
    stream = resp.json()
    assert stream["notification_mode"] == "mentions_only"

    resp = api.get(f"{STREAMS}{stream_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "all_messages"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, notification_mode
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
                AND stream_uuid = %s
            ORDER BY user_uuid
            """,
            (api.project_id, stream_uuid),
        )
        bindings = cur.fetchall()
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND epoch_version > %s
                AND payload->>'kind' = 'stream.updated'
                AND payload->>'uuid' = %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_epoch, stream_uuid),
        )
        event_rows = cur.fetchall()

    assert dict((str(user_uuid), mode) for user_uuid, mode in bindings) == {
        str(api.user_uuid): "mentions_only",
        str(other_user): "all_messages",
    }
    assert len(event_rows) == 1
    epoch_version, user_uuid, payload = event_rows[0]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["notification_mode"] == "mentions_only"

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": user_uuid,
            "payload": payload,
        }
    )
    assert event["type"] == "stream"
    assert event["kind"] == "stream.updated"
    assert event["stream"]["notification_mode"] == "mentions_only"


def test_stream_delete_cascades_data_and_writes_realtime_events(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "delete-me"
    )
    conftest.seed_user_stream(db, api.project_id, api.user_uuid, "keep-owner")
    conftest.seed_user_stream(db, api.project_id, other_user, "keep-other")
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general", is_default=True
    )
    conftest.seed_stream_topic_flags(
        db, topic_uuid, api.user_uuid, api.project_id
    )

    folder_resp = api.post(FOLDERS, json={"title": "Pinned"})
    assert folder_resp.status_code in (200, 201), folder_resp.text
    folder_uuid = folder_resp.json()["uuid"]
    item_resp = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder_uuid,
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert item_resp.status_code in (200, 201), item_resp.text

    message_resp = api.post(
        MESSAGES,
        json={
            "uuid": str(sys_uuid.uuid4()),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "delete cascade check",
            },
        },
    )
    assert message_resp.status_code == 201, message_resp.text
    message_uuid = message_resp.json()["uuid"]
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_message_reactions
                (uuid, project_id, created_at, updated_at, message_uuid,
                 user_uuid, emoji_name, status)
            VALUES (%s, %s, NOW(), NOW(), %s, %s, 'thumbs_up', 'active')
            """,
            (str(sys_uuid.uuid4()), api.project_id, message_uuid, api.user_uuid),
        )
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_delete_epoch = cur.fetchone()[0]

    resp = api.delete(f"{STREAMS}{stream_uuid}")
    assert resp.status_code in (200, 204), resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_workspace_streams
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_stream_topics
                 WHERE stream_uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_topic_flags
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_messages
                 WHERE stream_uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_message_flags
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_message_reactions
                 WHERE message_uuid = %s),
                (SELECT COUNT(*) FROM m_folder_items
                 WHERE stream_uuid = %s)
            """,
            (
                stream_uuid,
                stream_uuid,
                topic_uuid,
                stream_uuid,
                message_uuid,
                message_uuid,
                stream_uuid,
            ),
        )
        counts = cur.fetchone()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_delete_epoch),
        )
        event_rows = cur.fetchall()

    assert counts == (0, 0, 0, 0, 0, 0, 0)
    events_by_user = {}
    for user_uuid, payload in event_rows:
        events_by_user.setdefault(str(user_uuid), []).append(payload)

    assert set(events_by_user) == {str(api.user_uuid), str(other_user)}
    assert [event["kind"] for event in events_by_user[str(api.user_uuid)]] == [
        "stream.deleted",
        "folder.updated",
        "folder.updated",
        "folder.updated",
    ]
    assert [event["kind"] for event in events_by_user[str(other_user)]] == [
        "stream.deleted",
        "folder.updated",
        "folder.updated",
    ]
    assert events_by_user[str(api.user_uuid)][0]["uuid"] == stream_uuid
    assert events_by_user[str(other_user)][0]["uuid"] == stream_uuid

    owner_folder_events = events_by_user[str(api.user_uuid)][1:]
    other_folder_events = events_by_user[str(other_user)][1:]
    assert [event["uuid"] for event in owner_folder_events] == [
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-000000000002",
        folder_uuid,
    ]
    assert [event["uuid"] for event in other_folder_events] == [
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-000000000002",
    ]
    for event in owner_folder_events + other_folder_events:
        assert all(
            item["stream_uuid"] != stream_uuid
            for item in event["folder_items"]
        )


def test_direct_stream_create_is_idempotent_and_creates_owner_bindings(api, db):
    direct_user_uuid = sys_uuid.uuid4()
    expected_index = ":".join(
        sorted([str(api.user_uuid), str(direct_user_uuid)])
    )
    payload = {
        "name": "Direct",
        "description": "Private workspace",
        "source_name": "native",
        "source": {"kind": "native"},
        "direct_user_uuid": str(direct_user_uuid),
    }

    first_resp = api.post(STREAMS, json=payload)
    assert first_resp.status_code in (200, 201), first_resp.text
    first_stream = first_resp.json()

    second_resp = api.post(STREAMS, json=payload)
    assert second_resp.status_code in (200, 201), second_resp.text
    second_stream = second_resp.json()

    assert second_stream["uuid"] == first_stream["uuid"]
    assert first_stream["private"] is True
    assert first_stream["direct_user_uuid"] == str(direct_user_uuid)
    assert "private_index" not in first_stream

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT private_index
            FROM m_workspace_streams
            WHERE project_id = %s
                AND uuid = %s
            """,
            (api.project_id, first_stream["uuid"]),
        )
        stored_private_index = cur.fetchone()[0]
        cur.execute(
            """
            SELECT user_uuid, role
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
                AND stream_uuid = %s
            ORDER BY user_uuid
            """,
            (api.project_id, first_stream["uuid"]),
        )
        bindings = cur.fetchall()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'stream.created'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, first_stream["uuid"]),
        )
        events = cur.fetchall()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'folder.updated'
            ORDER BY user_uuid, payload->>'uuid'
            """,
            (api.project_id,),
        )
        folder_events = cur.fetchall()

    assert stored_private_index == expected_index
    assert [(str(user_uuid), role) for user_uuid, role in bindings] == [
        (user_uuid, "owner")
        for user_uuid in sorted([str(api.user_uuid), str(direct_user_uuid)])
    ]
    assert [str(user_uuid) for user_uuid, _payload in events] == sorted(
        [str(api.user_uuid), str(direct_user_uuid)]
    )
    assert [
        (str(user_uuid), payload["uuid"], payload["title"])
        for user_uuid, payload in folder_events
    ] == [
        (user_uuid, folder_uuid, title)
        for user_uuid in sorted([str(api.user_uuid), str(direct_user_uuid)])
        for folder_uuid, title in (
            ("00000000-0000-0000-0000-000000000000", "All chats"),
            ("00000000-0000-0000-0000-000000000001", "Personal"),
        )
    ]


def test_stream_binding_create_notifies_added_user(api, db):
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Engineering",
    )
    target_user_uuid = sys_uuid.uuid4()
    second_target_user_uuid = sys_uuid.uuid4()

    resp = api.post(
        f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
        json={
            "member": [
                str(target_user_uuid),
                str(second_target_user_uuid),
            ],
        },
    )
    assert resp.status_code in (200, 201), resp.text

    for target_uuid in (target_user_uuid, second_target_user_uuid):
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT payload
                FROM m_workspace_events
                WHERE project_id = %s
                    AND user_uuid = %s
                ORDER BY epoch_version
                """,
                (api.project_id, target_uuid),
            )
            events = [row[0] for row in cur.fetchall()]

        assert [event["kind"] for event in events] == [
            "stream.created",
            "folder.updated",
            "folder.updated",
        ]
        assert events[0]["uuid"] == stream_uuid
        assert events[0]["user_uuid"] == str(target_uuid)
        assert events[0]["role"] == "member"
        assert events[0]["notification_mode"] == "all_messages"
        assert [(event["uuid"], event["title"]) for event in events[1:]] == [
            ("00000000-0000-0000-0000-000000000000", "All chats"),
            ("00000000-0000-0000-0000-000000000002", "Channels"),
        ]

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND user_uuid = %s
            ORDER BY epoch_version
            """,
            (api.project_id, api.user_uuid),
        )
        owner_events = [row[0] for row in cur.fetchall()]

    assert [event["kind"] for event in owner_events] == [
        "stream_bindings.created",
    ]
    assert owner_events[0]["stream_uuid"] == stream_uuid
    assert [
        binding["user_uuid"]
        for binding in owner_events[0]["stream_bindings"]
    ] == [
        str(target_user_uuid),
        str(second_target_user_uuid),
    ]
    assert {
        binding["who_uuid"]
        for binding in owner_events[0]["stream_bindings"]
    } == {str(api.user_uuid)}
    assert {
        binding["role"]
        for binding in owner_events[0]["stream_bindings"]
    } == {"member"}
    assert {
        binding["notification_mode"]
        for binding in owner_events[0]["stream_bindings"]
    } == {"all_messages"}


def test_stream_binding_delete_notifies_removed_user(api, db):
    target_user_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Remove user team",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        target_user_uuid,
    )

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT uuid
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
                AND stream_uuid = %s
                AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, str(target_user_uuid)),
        )
        binding_uuid = cur.fetchone()[0]

    resp = api.post(
        FOLDERS,
        user=target_user_uuid,
        json={"title": "Watched"},
    )
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    resp = api.post(
        FOLDER_ITEMS,
        user=target_user_uuid,
        json={
            "folder_uuid": folder["uuid"],
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert resp.status_code in (200, 201), resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_delete_epoch = cur.fetchone()[0]

    resp = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{STREAMS}{stream_uuid}", user=target_user_uuid)
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_bindings
            WHERE uuid = %s
            """,
            (binding_uuid,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_delete_epoch),
        )
        event_rows = cur.fetchall()

    assert [str(row[0]) for row in event_rows] == [
        str(target_user_uuid),
        str(target_user_uuid),
        str(target_user_uuid),
        str(target_user_uuid),
    ]
    events = [row[1] for row in event_rows]
    assert [event["kind"] for event in events] == [
        "stream.deleted",
        "folder.updated",
        "folder.updated",
        "folder.updated",
    ]
    assert events[0]["uuid"] == stream_uuid
    assert [(event["uuid"], event["title"]) for event in events[1:]] == [
        ("00000000-0000-0000-0000-000000000000", "All chats"),
        ("00000000-0000-0000-0000-000000000002", "Channels"),
        (folder["uuid"], "Watched"),
    ]
    assert events[3]["folder_items"] == []


def test_streams_cursor_pagination_with_composite_pk(api, db):
    seeded = {
        conftest.seed_user_stream(db, api.project_id, api.user_uuid, f"s-{i}")
        for i in range(5)
    }
    # noise that must never appear in this user's pages
    other_user = sys_uuid.uuid4()
    for i in range(3):
        conftest.seed_user_stream(db, api.project_id, other_user, f"noise-{i}")

    collected = []
    pages = 0
    marker = None
    while True:
        params = {"page_limit": 2}
        if marker:
            params["page_marker"] = marker
        resp = api.get(STREAMS, params=params)
        assert resp.status_code == 200, resp.text
        assert resp.headers["X-Pagination-Limit"] == "2"

        page = resp.json()
        collected.extend(item["uuid"] for item in page)
        pages += 1

        marker = resp.headers.get("X-Pagination-Marker")
        if marker is None:
            break
        assert len(page) == 2
        assert marker == page[-1]["uuid"]
        assert pages < 10  # safety net against an infinite loop

    # every seeded row returned exactly once, nothing from the other user
    assert sorted(collected) == sorted(seeded)
    assert len(collected) == len(set(collected)) == 5
    assert pages == 3  # 2 + 2 + 1


# --------------------------------------------------------------------------- #
# Stream topics: CRUD
# --------------------------------------------------------------------------- #


def test_stream_topic_create_is_visible_to_stream_users(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-create-team"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )

    resp = api.post(
        STREAM_TOPICS,
        json={
            "name": "planning",
            "stream_uuid": stream_uuid,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    topic = resp.json()
    assert topic["name"] == "planning"
    assert topic["stream_uuid"] == stream_uuid
    assert topic["is_default"] is False
    assert topic["is_done"] is False
    assert topic["notification_mode"] == "default"

    resp = api.get(f"{STREAM_TOPICS}{topic['uuid']}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "planning"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_topic_flags
            WHERE uuid = %s
                AND project_id = %s
                AND user_uuid IN (%s, %s)
            """,
            (topic["uuid"], api.project_id, api.user_uuid, other_user),
        )
        flags_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'topic.created'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, topic["uuid"]),
        )
        event_rows = cur.fetchall()

    assert flags_count == 2
    assert {str(row[1]) for row in event_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    for _, _, payload in event_rows:
        assert payload["kind"] == "topic.created"
        assert payload["uuid"] == topic["uuid"]
        assert payload["name"] == "planning"
        assert payload["stream_uuid"] == stream_uuid
        assert payload["unread_count"] == 0
        assert payload["is_default"] is False
        assert payload["is_done"] is False
        assert payload["notification_mode"] == "default"

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": event_rows[0][0],
            "user_uuid": event_rows[0][1],
            "payload": event_rows[0][2],
        }
    )
    assert event["type"] == "topic"
    assert event["kind"] == "topic.created"
    assert event["topic"]["uuid"] == topic["uuid"]
    assert event["topic"]["name"] == "planning"
    assert event["topic"]["notification_mode"] == "default"


def test_stream_topic_rename(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "team-chat"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "standups"
    )

    resp = api.put(f"{STREAM_TOPICS}{topic_uuid}", json={"name": "retros"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "retros"

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "retros"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'topic.updated'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, topic_uuid),
        )
        event_rows = cur.fetchall()

    assert {str(row[0]) for row in event_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    for _, payload in event_rows:
        assert payload["name"] == "retros"
        assert payload["stream_uuid"] == stream_uuid


def test_stream_topic_notifications_follow_stream_mute_rules(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-notifications-team"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "standups"
    )

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "default"

    resp = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        json={"notification_mode": "follow"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "follow"

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "default"

    resp = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        json={"notification_mode": "unmute"},
    )
    assert resp.status_code == 400, resp.text

    resp = api.post(
        f"{STREAMS}{stream_uuid}/actions/notifications/invoke",
        json={"notification_mode": "muted"},
    )
    assert resp.status_code == 200, resp.text

    resp = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        json={"notification_mode": "unmute"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "unmute"

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "default"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, notification_mode
            FROM m_workspace_user_topics_view
            WHERE project_id = %s
                AND uuid = %s
            ORDER BY user_uuid
            """,
            (api.project_id, topic_uuid),
        )
        topic_rows = cur.fetchall()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND user_uuid = %s
                AND payload->>'kind' = 'topic.updated'
                AND payload->>'uuid' = %s
            ORDER BY epoch_version
            """,
            (api.project_id, api.user_uuid, topic_uuid),
        )
        event_rows = cur.fetchall()

    assert dict((str(user_uuid), mode) for user_uuid, mode in topic_rows) == {
        str(api.user_uuid): "unmute",
        str(other_user): "default",
    }
    assert [payload["notification_mode"] for _, payload in event_rows] == [
        "follow",
        "unmute",
    ]


def test_stream_topic_delete_cascades_topic_messages(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-delete-team"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "standups"
    )
    message_uuid = str(sys_uuid.uuid4())

    resp = api.post(
        MESSAGES,
        json={
            "uuid": message_uuid,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "delete with topic",
            },
        },
    )
    assert resp.status_code == 201, resp.text

    resp = api.delete(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_workspace_stream_topics
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_topic_flags
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_messages
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_message_flags
                 WHERE uuid = %s)
            """,
            (topic_uuid, topic_uuid, message_uuid, message_uuid),
        )
        counts = cur.fetchone()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'topic.deleted'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, topic_uuid),
        )
        event_rows = cur.fetchall()

    assert counts == (0, 0, 0, 0)
    assert {str(row[0]) for row in event_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    for _, payload in event_rows:
        assert payload["stream_uuid"] == stream_uuid


def test_stream_topic_is_done_flag(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "team-chat"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "standups"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is False

    resp = api.post(f"{STREAM_TOPICS}{topic_uuid}/toggle_done/")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is True

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is True

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'topic.updated'
                AND payload->>'uuid' = %s
                AND payload->>'is_done' = 'true'
            ORDER BY user_uuid
            """,
            (api.project_id, topic_uuid),
        )
        event_rows = cur.fetchall()

    assert {str(row[0]) for row in event_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    for _, payload in event_rows:
        assert payload["stream_uuid"] == stream_uuid
        assert payload["is_done"] is True

    resp = api.post(f"{STREAM_TOPICS}{topic_uuid}/toggle_done/")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is False

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is False


# --------------------------------------------------------------------------- #
# Message events: durable epoch/outbox delivery
# --------------------------------------------------------------------------- #


def test_epoch_is_zero_without_visible_events(api):
    resp = api.get(EPOCH)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"epoch_version": 0}


def test_message_create_writes_flags_and_visible_events(api, db):
    other_user = sys_uuid.uuid4()
    outsider = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "events-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general", is_default=True
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )

    resp = api.post(
        MESSAGES,
        json={
            "uuid": str(sys_uuid.uuid4()),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "hello over epochs",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    message = resp.json()
    message_uuid = message["uuid"]
    assert message["read"] is True
    assert message["is_own"] is True

    other_message_resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert other_message_resp.status_code == 200, other_message_resp.text
    other_message = other_message_resp.json()
    assert other_message["read"] is False
    assert other_message["is_own"] is False

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, read
            FROM m_workspace_user_message_flags
            WHERE uuid = %s
            ORDER BY user_uuid
            """,
            (message_uuid,),
        )
        flags = {str(row[0]): row[1] for row in cur.fetchall()}
    assert flags == {
        str(api.user_uuid): True,
        str(other_user): False,
    }

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        event_rows = cur.fetchall()
    events_by_user = {}
    for user_uuid, payload in event_rows:
        events_by_user.setdefault(str(user_uuid), []).append(payload)

    assert set(events_by_user) == {str(api.user_uuid), str(other_user)}
    assert [payload["kind"] for payload in events_by_user[str(api.user_uuid)]] == [
        "message.created",
    ]
    assert [payload["kind"] for payload in events_by_user[str(other_user)]] == [
        "message.created",
        "topic.updated",
        "stream.updated",
        "folder.updated",
        "folder.updated",
    ]
    author_payload = events_by_user[str(api.user_uuid)][0]
    other_payload = events_by_user[str(other_user)][0]
    assert author_payload["kind"] == "message.created"
    assert author_payload["uuid"] == message_uuid
    assert author_payload["stream_uuid"] == stream_uuid
    assert author_payload["topic_uuid"] == topic_uuid
    assert author_payload["author_uuid"] == str(api.user_uuid)
    assert author_payload["payload"] == {
        "kind": "markdown",
        "content": "hello over epochs",
    }
    assert author_payload["user_uuid"] == str(api.user_uuid)
    assert author_payload["project_id"] == str(api.project_id)
    assert author_payload["read"] is True
    assert author_payload["pinned"] is False
    assert author_payload["starred"] is False
    assert author_payload["is_own"] is True
    assert other_payload["user_uuid"] == str(other_user)
    assert other_payload["project_id"] == str(api.project_id)
    assert other_payload["read"] is False
    assert other_payload["pinned"] is False
    assert other_payload["starred"] is False
    assert other_payload["is_own"] is False
    assert messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": 1,
            "user_uuid": api.user_uuid,
            "payload": author_payload,
        }
    )["message"] == message
    assert messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": 2,
            "user_uuid": other_user,
            "payload": other_payload,
        }
    )["message"] == other_message

    author_resp = api.get(EVENTS, params={"page_limit": 100})
    assert author_resp.status_code == 200, author_resp.text
    author_events = author_resp.json()
    assert len(author_events) == 1
    event = author_events[0]
    assert event["project_id"] == str(api.project_id)
    assert event["user_uuid"] == str(api.user_uuid)
    assert event["payload"]["kind"] == "message.created"
    assert event["payload"]["uuid"] == message_uuid
    assert event["payload"]["stream_uuid"] == stream_uuid
    assert event["payload"]["topic_uuid"] == topic_uuid
    assert event["payload"]["author_uuid"] == str(api.user_uuid)
    assert event["payload"]["payload"]["content"] == "hello over epochs"
    assert event["payload"]["user_uuid"] == str(api.user_uuid)
    assert event["payload"]["project_id"] == str(api.project_id)
    assert event["payload"]["read"] is True
    assert event["payload"]["is_own"] is True

    other_events = api.get(
        EVENTS,
        user=other_user,
        params={"page_limit": 100},
    ).json()
    assert [event["payload"]["kind"] for event in other_events] == [
        "message.created",
        "topic.updated",
        "stream.updated",
        "folder.updated",
        "folder.updated",
    ]
    other_event = other_events[0]
    assert other_event["payload"]["uuid"] == message_uuid
    assert other_event["payload"]["kind"] == "message.created"
    assert other_event["payload"]["user_uuid"] == str(other_user)
    assert other_event["payload"]["project_id"] == str(api.project_id)
    assert other_event["payload"]["read"] is False
    assert other_event["payload"]["is_own"] is False

    outsider_events = api.get(
        EVENTS,
        user=outsider,
        params={"page_limit": 100},
    ).json()
    assert outsider_events == []

    next_page = api.get(
        EVENTS,
        params={
            "page_limit": 100,
            "page_marker": event["epoch_version"],
        },
    ).json()
    assert next_page == []


def test_message_update_read_delete_write_realtime_events(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "message-crud-team"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general",
        is_default=True,
    )

    resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "first version",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    message_uuid = resp.json()["uuid"]

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_read_epoch = cur.fetchone()[0]

    resp = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["read"] is True

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT read
            FROM m_workspace_user_message_flags
            WHERE uuid = %s
                AND project_id = %s
                AND user_uuid = %s
            """,
            (message_uuid, api.project_id, str(other_user)),
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND user_uuid = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, str(other_user), before_read_epoch),
        )
        read_events = [row[0] for row in cur.fetchall()]

    assert [event["kind"] for event in read_events] == [
        "messages.read",
        "topic.updated",
        "stream.updated",
        "folder.updated",
        "folder.updated",
    ]
    assert read_events[0]["message_uuids"] == [message_uuid]
    assert read_events[1]["unread_count"] == 0
    assert read_events[2]["unread_count"] == 0
    assert [event["unread_count"] for event in read_events[3:]] == [0, 0]

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_update_epoch = cur.fetchone()[0]

    resp = api.put(
        f"{MESSAGES}{message_uuid}",
        json={
            "payload": {
                "kind": "markdown",
                "content": "edited version",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["payload"]["content"] == "edited version"

    resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["payload"]["content"] == "edited version"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_update_epoch),
        )
        update_rows = cur.fetchall()

    assert {str(row[0]) for row in update_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    assert [row[1]["kind"] for row in update_rows] == [
        "message.updated",
        "message.updated",
    ]
    assert all(
        row[1]["payload"]["content"] == "edited version"
        for row in update_rows
    )

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_delete_epoch = cur.fetchone()[0]

    resp = api.delete(f"{MESSAGES}{message_uuid}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_workspace_messages
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_message_flags
                 WHERE uuid = %s)
            """,
            (message_uuid, message_uuid),
        )
        assert cur.fetchone() == (0, 0)
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_delete_epoch),
        )
        delete_rows = cur.fetchall()

    assert {str(row[0]) for row in delete_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    assert [row[1]["kind"] for row in delete_rows] == [
        "message.deleted",
        "message.deleted",
    ]
    assert all(row[1]["uuid"] == message_uuid for row in delete_rows)
    assert all(row[1]["stream_uuid"] == stream_uuid for row in delete_rows)
    assert all(row[1]["topic_uuid"] == topic_uuid for row in delete_rows)


def test_stream_topic_and_message_read_actions_mark_expected_messages(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "read-actions-team"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general",
        is_default=True,
    )
    other_topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "random"
    )

    message_uuids = []
    for topic, content in (
        (topic_uuid, "first"),
        (topic_uuid, "second"),
        (topic_uuid, "third"),
        (other_topic_uuid, "other topic"),
    ):
        resp = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic,
                "payload": {
                    "kind": "markdown",
                    "content": content,
                },
            },
        )
        assert resp.status_code == 201, resp.text
        message_uuids.append(resp.json()["uuid"])

    def other_user_flags():
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT uuid, read
                FROM m_workspace_user_message_flags
                WHERE project_id = %s
                    AND user_uuid = %s
                    AND uuid IN (%s, %s, %s, %s)
                """,
                (api.project_id, str(other_user), *message_uuids),
            )
            return {str(uuid): read for uuid, read in cur.fetchall()}

    assert other_user_flags() == {
        message_uuids[0]: False,
        message_uuids[1]: False,
        message_uuids[2]: False,
        message_uuids[3]: False,
    }

    resp = api.post(
        f"{MESSAGES}{message_uuids[1]}/actions/read/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == message_uuids[1]
    assert resp.json()["read"] is True
    assert other_user_flags() == {
        message_uuids[0]: False,
        message_uuids[1]: True,
        message_uuids[2]: False,
        message_uuids[3]: False,
    }

    resp = api.post(
        f"{MESSAGES}{message_uuids[1]}/actions/read_up_to/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == message_uuids[1]
    assert resp.json()["read"] is True
    assert other_user_flags() == {
        message_uuids[0]: True,
        message_uuids[1]: True,
        message_uuids[2]: False,
        message_uuids[3]: False,
    }

    resp = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/read/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == topic_uuid
    assert resp.json()["unread_count"] == 0
    assert other_user_flags() == {
        message_uuids[0]: True,
        message_uuids[1]: True,
        message_uuids[2]: True,
        message_uuids[3]: False,
    }

    resp = api.post(
        f"{STREAMS}{stream_uuid}/actions/read/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == stream_uuid
    assert resp.json()["unread_count"] == 0
    assert other_user_flags() == {
        message_uuids[0]: True,
        message_uuids[1]: True,
        message_uuids[2]: True,
        message_uuids[3]: True,
    }


def test_unbound_user_cannot_send_message(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, other_user, "private-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, other_user, "general", is_default=True
    )

    resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "nope",
            },
        },
    )
    assert resp.status_code == 400, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_messages
            WHERE project_id = %s
              AND user_uuid = %s
              AND stream_uuid = %s
            """,
            (api.project_id, api.user_uuid, stream_uuid),
        )
        assert cur.fetchone()[0] == 0


def test_message_create_requires_topic(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-required-team"
    )

    resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "payload": {
                "kind": "markdown",
                "content": "missing topic",
            },
        },
    )
    assert resp.status_code == 400, resp.text


def test_message_helper_writes_visible_event(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "helper-events-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general", is_default=True
    )
    message_uuid = sys_uuid.uuid4()
    message = messenger_dm_helpers.create_workspace_user_message(
        uuid=message_uuid,
        project_id=sys_uuid.UUID(api.project_id),
        user_uuid=sys_uuid.UUID(api.user_uuid),
        stream_uuid=sys_uuid.UUID(stream_uuid),
        topic_uuid=sys_uuid.UUID(topic_uuid),
        payload=message_payloads.MarkdownPayload(content="created through model"),
    )

    resp = api.get(EVENTS, params={"page_limit": 100})
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    assert events[0]["payload"]["uuid"] == str(message_uuid)
    assert events[0]["payload"]["kind"] == "message.created"
    assert events[0]["payload"]["user_uuid"] == str(message.user_uuid)
    assert events[0]["payload"]["read"] is True


def test_events_filter_by_epoch_range(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "range-events-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general", is_default=True
    )
    message_uuids = []
    for content in ("first through model", "second through model"):
        message_uuid = sys_uuid.uuid4()
        message_uuids.append(str(message_uuid))
        messenger_dm_helpers.create_workspace_user_message(
            uuid=message_uuid,
            project_id=sys_uuid.UUID(api.project_id),
            user_uuid=sys_uuid.UUID(api.user_uuid),
            stream_uuid=sys_uuid.UUID(stream_uuid),
            topic_uuid=sys_uuid.UUID(topic_uuid),
            payload=message_payloads.MarkdownPayload(content=content),
        )

    resp = api.get(EVENTS, params={"page_limit": 100})
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert [
        event["payload"]["uuid"]
        for event in events
    ] == message_uuids
    first_epoch = events[0]["epoch_version"]
    second_epoch = events[1]["epoch_version"]

    after_resp = api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version=>", first_epoch),
        ],
    )
    assert after_resp.status_code == 200, after_resp.text
    assert [
        event["epoch_version"]
        for event in after_resp.json()
    ] == [first_epoch, second_epoch]

    strict_after_resp = api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version>", first_epoch),
        ],
    )
    assert strict_after_resp.status_code == 200, strict_after_resp.text
    assert [
        event["epoch_version"]
        for event in strict_after_resp.json()
    ] == [second_epoch]

    before_resp = api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version=<", first_epoch),
        ],
    )
    assert before_resp.status_code == 200, before_resp.text
    assert [event["epoch_version"] for event in before_resp.json()] == [first_epoch]

    strict_before_resp = api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version<", second_epoch),
        ],
    )
    assert strict_before_resp.status_code == 200, strict_before_resp.text
    assert [
        event["epoch_version"]
        for event in strict_before_resp.json()
    ] == [first_epoch]

    exact_resp = api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version=>", second_epoch),
            ("epoch_version=<", second_epoch),
        ],
    )
    assert exact_resp.status_code == 200, exact_resp.text
    assert [event["epoch_version"] for event in exact_resp.json()] == [second_epoch]
