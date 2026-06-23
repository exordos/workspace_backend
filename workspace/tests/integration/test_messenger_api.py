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

from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/streams/"
FOLDERS = f"{V1}/folders/"
STREAM_TOPICS = f"{V1}/stream_topics/"


# --------------------------------------------------------------------------- #
# Smoke
# --------------------------------------------------------------------------- #


def test_root_endpoint_is_served(api):
    resp = api.get(f"{V1}/")
    assert resp.status_code == 200, resp.text


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
