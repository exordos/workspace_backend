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
FOLDERS = f"{V1}/folders/"
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
    assert event["folder"]["uuid"] == folder["uuid"]
    assert event["folder"]["title"] == "Archive"


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
# Stream topics: rename
# --------------------------------------------------------------------------- #


def test_stream_topic_rename(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "team-chat"
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


def test_stream_topic_is_done_flag(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "team-chat"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "standups"
    )

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is False

    resp = api.post(f"{STREAM_TOPICS}{topic_uuid}/toggle_done/")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is True

    resp = api.post(f"{STREAM_TOPICS}{topic_uuid}/toggle_done/")
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
        event_rows = {str(row[0]): row[1] for row in cur.fetchall()}
    assert set(event_rows) == {str(api.user_uuid), str(other_user)}
    author_payload = event_rows[str(api.user_uuid)]
    other_payload = event_rows[str(other_user)]
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
    assert len(other_events) == 1
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
