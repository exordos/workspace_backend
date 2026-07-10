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

import hashlib
import importlib.util
import io
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models as messenger_models
from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/streams/"
STREAM_BINDINGS = f"{V1}/stream_bindings/"
FOLDERS = f"{V1}/folders/"
FILES = f"{V1}/files/"
FOLDER_ITEMS = f"{V1}/folder_items/"
STREAM_TOPICS = f"{V1}/stream_topics/"
MESSAGES = f"{V1}/messages/"
MESSAGE_REACTIONS = f"{V1}/message_reactions/"
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
    assert user["avatar"] == (
        messenger_models.build_workspace_user_default_avatar(user_uuid)
    )

    resp = api.get(USERS, params={"username": username})
    assert resp.status_code == 200, resp.text
    assert [user["uuid"] for user in resp.json()] == [str(user_uuid)]


def test_own_message_read_backfill_migration(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "own-message-backfill"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general",
        is_default=True,
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )
    message_uuid = sys_uuid.uuid4()
    messenger_dm_helpers.create_workspace_user_message(
        uuid=message_uuid,
        project_id=sys_uuid.UUID(api.project_id),
        user_uuid=sys_uuid.UUID(api.user_uuid),
        stream_uuid=sys_uuid.UUID(stream_uuid),
        topic_uuid=sys_uuid.UUID(topic_uuid),
        payload=message_payloads.MarkdownPayload(content="backfill me"),
    )
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_user_message_flags
            SET read = FALSE
            WHERE uuid = %s
                AND user_uuid = %s
            """,
            (message_uuid, api.user_uuid),
        )

    migration_path = (
        conftest.MIGRATIONS_DIR / "0094-mark-own-messages-read-8413a3.py"
    )
    spec = importlib.util.spec_from_file_location(
        "mark_own_messages_read_migration",
        migration_path,
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migration_step.upgrade(db)

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


def test_user_presence_action_updates_current_user_presence(api, db):
    username = f"user-{api.user_uuid}"
    event_recipient_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, api.user_uuid, username)
    conftest.seed_workspace_user(
        db,
        event_recipient_uuid,
        f"user-{event_recipient_uuid}",
    )

    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={
            "status": "idle",
            "emoji": "coffee",
            "text": "Focusing",
        },
    )
    assert resp.status_code == 200, resp.text
    user = resp.json()
    assert user["uuid"] == str(api.user_uuid)
    assert user["avatar"] == (
        messenger_models.build_workspace_user_default_avatar(api.user_uuid)
    )
    assert user["status"] == "idle"
    assert user["status_emoji"] == "coffee"
    assert user["status_text"] == "Focusing"
    assert user["last_ping_at"] is not None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT status, status_emoji, status_text, last_ping_at
            FROM m_workspace_users
            WHERE uuid = %s
            """,
            (str(api.user_uuid),),
        )
        row = cur.fetchone()
    assert row[0] == "idle"
    assert row[1] == "coffee"
    assert row[2] == "Focusing"
    assert row[3] is not None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'user.updated'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, str(api.user_uuid)),
        )
        event_rows = cur.fetchall()
    event_recipient_uuids = {str(row[0]) for row in event_rows}
    assert str(api.user_uuid) in event_recipient_uuids
    assert str(event_recipient_uuid) in event_recipient_uuids
    for _, payload in event_rows:
        assert payload["username"] == username
        assert payload["avatar"] == (
            messenger_models.build_workspace_user_default_avatar(api.user_uuid)
        )
        assert payload["status"] == "idle"
        assert payload["status_emoji"] == "coffee"
        assert payload["status_text"] == "Focusing"
        assert payload["last_ping_at"] is not None

    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={"status": "active"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
    assert resp.json()["status_emoji"] == "coffee"
    assert resp.json()["status_text"] == "Focusing"

    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={"status": "idle", "emoji": None, "text": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status_emoji") is None
    assert resp.json().get("status_text") is None
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT status_emoji, status_text
            FROM m_workspace_users
            WHERE uuid = %s
            """,
            (str(api.user_uuid),),
        )
        row = cur.fetchone()
    assert row == (None, None)

    other_user_uuid = sys_uuid.uuid4()
    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        user=other_user_uuid,
        json={"status": "active"},
    )
    assert resp.status_code == 404, resp.text


def test_user_presence_action_skips_event_for_heartbeat(api, db):
    username = f"user-{api.user_uuid}"
    conftest.seed_workspace_user(db, api.user_uuid, username)
    heartbeat_api = conftest.ApiClient(
        base_url=api.base_url,
        user_uuid=api.user_uuid,
        project_id=api.project_id,
    )

    resp = heartbeat_api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={"status": "idle"},
    )
    assert resp.status_code == 200, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), max(last_ping_at)
            FROM m_workspace_events AS events
            JOIN m_workspace_users AS users
                ON users.uuid = %s
            WHERE events.project_id = %s
                AND events.payload->>'kind' = 'user.updated'
                AND events.payload->>'uuid' = %s
            """,
            (str(api.user_uuid), api.project_id, str(api.user_uuid)),
        )
        first_event_count, first_ping_at = cur.fetchone()

    resp = heartbeat_api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={"status": "idle"},
    )
    assert resp.status_code == 200, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), max(last_ping_at)
            FROM m_workspace_events AS events
            JOIN m_workspace_users AS users
                ON users.uuid = %s
            WHERE events.project_id = %s
                AND events.payload->>'kind' = 'user.updated'
                AND events.payload->>'uuid' = %s
            """,
            (str(api.user_uuid), api.project_id, str(api.user_uuid)),
        )
        second_event_count, second_ping_at = cur.fetchone()

    assert second_event_count == first_event_count
    assert second_ping_at >= first_ping_at


def test_user_status_is_offline_when_last_ping_is_stale(api, db):
    user_uuid = sys_uuid.uuid4()
    event_recipient_uuid = sys_uuid.uuid4()
    username = f"user-{user_uuid}"
    conftest.seed_workspace_user(db, user_uuid, username)
    conftest.seed_workspace_user(
        db,
        event_recipient_uuid,
        f"user-{event_recipient_uuid}",
    )
    conftest.seed_user_stream(db, api.project_id, api.user_uuid, "status-team")

    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_users
            SET status = 'active',
                last_ping_at = NOW() - INTERVAL '2 minutes'
            WHERE uuid = %s
            """,
            (str(user_uuid),),
        )

    messenger_dm_helpers.mark_stale_workspace_users_offline()

    resp = api.get(f"{USERS}{user_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "offline"
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT status
            FROM m_workspace_users
            WHERE uuid = %s
            """,
            (str(user_uuid),),
        )
        assert cur.fetchone()[0] == "offline"

        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'user.updated'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, str(user_uuid)),
        )
        event_rows = cur.fetchall()
    event_recipient_uuids = {str(row[0]) for row in event_rows}
    assert str(user_uuid) in event_recipient_uuids
    assert str(event_recipient_uuid) in event_recipient_uuids
    for _, payload in event_rows:
        assert payload["username"] == username
        assert payload["status"] == "offline"

    messenger_dm_helpers.mark_stale_workspace_users_offline()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'user.updated'
                AND payload->>'uuid' = %s
            """,
            (api.project_id, str(user_uuid)),
        )
        assert cur.fetchone()[0] == len(event_rows)

    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_users
            SET status = 'do_not_disturb',
                last_ping_at = NOW()
            WHERE uuid = %s
            """,
            (str(user_uuid),),
        )

    resp = api.get(f"{USERS}{user_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "do_not_disturb"


def test_workspace_event_payload_identity_backfill_migration(_database, db):
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    clean_user_uuid = sys_uuid.uuid4()
    damaged_user_uuid = sys_uuid.uuid4()
    for workspace_user_uuid in (
        user_uuid,
        clean_user_uuid,
        damaged_user_uuid,
    ):
        conftest.seed_workspace_user(
            db,
            workspace_user_uuid,
            f"user-{workspace_user_uuid}",
        )

    def run_migration(filename, module_name):
        migration_path = conftest.MIGRATIONS_DIR / filename
        spec = importlib.util.spec_from_file_location(module_name, migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migration_step.upgrade(db)

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_events
                (uuid, project_id, user_uuid, schema_version, object_type, action,
                 payload, created_at, updated_at)
            VALUES (
                %s,
                %s,
                %s,
                1,
                'message',
                'created',
                jsonb_build_object(
                    'kind', 'message.created',
                    'uuid', %s::text,
                    'stream_uuid', %s::text,
                    'topic_uuid', %s::text,
                    'author_uuid', %s::text,
                    'payload', jsonb_build_object(
                        'kind', 'markdown',
                        'content', 'legacy event'
                    ),
                    'created_at', '2026-07-02 12:00:00.000000',
                    'updated_at', '2026-07-02 12:00:00.000000'
                ),
                NOW(),
                NOW()
            )
            RETURNING epoch_version
            """,
            (
                str(sys_uuid.uuid4()),
                str(project_id),
                str(user_uuid),
                str(message_uuid),
                str(stream_uuid),
                str(topic_uuid),
                str(user_uuid),
            ),
        )
        message_epoch_version = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO m_workspace_events
                (uuid, project_id, user_uuid, schema_version, object_type, action,
                 payload, created_at, updated_at)
            VALUES (
                %s,
                %s,
                %s,
                1,
                'user',
                'updated',
                jsonb_build_object(
                    'kind', 'user.updated',
                    'uuid', %s::text,
                    'created_at', '2026-07-02 12:00:00.000000',
                    'updated_at', '2026-07-02 12:00:00.000000',
                    'username', 'clean-user',
                    'source', 'iam',
                    'status', 'active',
                    'avatar', 'urn:gavatar:' || %s::text,
                    'last_ping_at', '2026-07-02 12:00:00.000000'
                ),
                NOW(),
                NOW()
            )
            RETURNING epoch_version
            """,
            (
                str(sys_uuid.uuid4()),
                str(project_id),
                str(clean_user_uuid),
                str(clean_user_uuid),
                str(clean_user_uuid),
            ),
        )
        clean_user_epoch_version = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO m_workspace_events
                (uuid, project_id, user_uuid, schema_version, object_type, action,
                 payload, created_at, updated_at)
            VALUES (
                %s,
                %s,
                %s,
                1,
                'user',
                'updated',
                jsonb_build_object(
                    'kind', 'user.updated',
                    'project_id', %s::text,
                    'uuid', %s::text,
                    'created_at', '2026-07-02 12:00:00.000000',
                    'updated_at', '2026-07-02 12:00:00.000000',
                    'username', 'damaged-user',
                    'source', 'iam',
                    'status', 'active',
                    'avatar', 'urn:gavatar:' || %s::text,
                    'last_ping_at', '2026-07-02 12:00:00.000000'
                ),
                NOW(),
                NOW()
            )
            RETURNING epoch_version
            """,
            (
                str(sys_uuid.uuid4()),
                str(project_id),
                str(damaged_user_uuid),
                str(project_id),
                str(damaged_user_uuid),
                str(damaged_user_uuid),
            ),
        )
        damaged_user_epoch_version = cur.fetchone()[0]

        cur.execute(
            """
            SELECT payload->>'project_id', payload->>'user_uuid'
            FROM m_workspace_events
            WHERE epoch_version = %s
            """,
            (message_epoch_version,),
        )
        assert cur.fetchone() == (None, None)

    run_migration(
        "0061-backfill-workspace-event-payload-identity-fields-f25144.py",
        "migration_0061",
    )

    event = messenger_models.WorkspaceEvent.objects.get_one(
        filters={"epoch_version": dm_filters.EQ(message_epoch_version)},
    )
    assert event.payload["project_id"] == str(project_id)
    assert event.payload["user_uuid"] == str(user_uuid)

    event = messenger_models.WorkspaceEvent.objects.get_one(
        filters={"epoch_version": dm_filters.EQ(clean_user_epoch_version)},
    )
    assert event.payload["username"] == "clean-user"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload->>'project_id', payload->>'user_uuid'
            FROM m_workspace_events
            WHERE epoch_version = %s
            """,
            (message_epoch_version,),
        )
        assert cur.fetchone() == (str(project_id), str(user_uuid))

        cur.execute(
            """
            SELECT payload->>'project_id'
            FROM m_workspace_events
            WHERE epoch_version = %s
            """,
            (clean_user_epoch_version,),
        )
        assert cur.fetchone()[0] is None

    run_migration(
        "0062-clean-invalid-workspace-event-payload-project-ids-82eab5.py",
        "migration_0062",
    )

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload->>'project_id'
            FROM m_workspace_events
            WHERE epoch_version = %s
            """,
            (damaged_user_epoch_version,),
        )
        assert cur.fetchone()[0] is None

    event = messenger_models.WorkspaceEvent.objects.get_one(
        filters={"epoch_version": dm_filters.EQ(damaged_user_epoch_version)},
    )
    assert event.payload["username"] == "damaged-user"
    assert project_id in messenger_dm_helpers._get_workspace_event_project_ids()


# --------------------------------------------------------------------------- #
# Files: metadata and local storage
# --------------------------------------------------------------------------- #


def test_file_json_crud_scopes_access_and_deletes_access_rows(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "files-team"
    )
    stream_user = sys_uuid.uuid4()
    outsider_user = sys_uuid.uuid4()
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, stream_user
    )

    resp = api.post(
        FILES,
        json={
            "stream_uuid": stream_uuid,
            "name": "example.txt",
            "description": "Example",
            "content_type": "text/plain",
            "size_bytes": 12,
            "hash": "abc",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    file = resp.json()
    file_uuid = file["uuid"]
    assert file["name"] == "example.txt"
    assert file["stream_uuid"] == stream_uuid
    assert file["user_uuid"] == str(api.user_uuid)
    assert "project_id" not in file

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid
            FROM m_workspace_file_accesses
            WHERE file_uuid = %s
            """,
            (file_uuid,),
        )
        access_user_uuids = {str(row[0]) for row in cur.fetchall()}
    assert access_user_uuids == {str(api.user_uuid), str(stream_user)}

    resp = api.get(FILES)
    assert resp.status_code == 200, resp.text
    assert [item["uuid"] for item in resp.json()] == [file_uuid]

    resp = api.get(FILES, user=stream_user)
    assert resp.status_code == 200, resp.text
    assert [item["uuid"] for item in resp.json()] == [file_uuid]

    resp = api.get(f"{FILES}{file_uuid}", user=stream_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == file_uuid

    resp = api.get(FILES, user=outsider_user)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    resp = api.get(f"{FILES}{file_uuid}", user=outsider_user)
    assert resp.status_code == 404, resp.text
    resp = api.get(
        f"{FILES}{file_uuid}/actions/download", user=outsider_user
    )
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_file_accesses
                (uuid, project_id, file_uuid, user_uuid, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (project_id, file_uuid, user_uuid) DO NOTHING
            """,
            (
                str(sys_uuid.uuid4()), api.project_id, file_uuid,
                str(outsider_user),
            ),
        )

    resp = api.get(f"{FILES}{file_uuid}", user=outsider_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == file_uuid

    resp = api.put(
        f"{FILES}{file_uuid}",
        user=outsider_user,
        json={"name": "not-owner.txt"},
    )
    assert resp.status_code == 404, resp.text

    resp = api.put(
        f"{FILES}{file_uuid}",
        json={"name": "renamed.txt", "description": "Updated"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "renamed.txt"
    assert resp.json()["description"] == "Updated"

    resp = api.delete(f"{FILES}{file_uuid}")
    assert resp.status_code in (200, 204), resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_workspace_files WHERE uuid = %s),
                (SELECT COUNT(*)
                 FROM m_workspace_file_accesses
                 WHERE file_uuid = %s)
            """,
            (file_uuid, file_uuid),
        )
        file_count, access_count = cur.fetchone()

    assert file_count == 0
    assert access_count == 0


def test_file_multipart_upload_writes_local_file(
    api, db, tmp_path, monkeypatch
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "file-upload-team"
    )
    stream_user = sys_uuid.uuid4()
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, stream_user
    )
    data = b"uploaded file data"

    resp = api.post(
        FILES,
        data={"stream_uuid": stream_uuid},
        files={"file": ("upload.txt", io.BytesIO(data), "text/plain")},
    )
    assert resp.status_code in (200, 201), resp.text
    file = resp.json()

    path = file_storage.get_workspace_file_path(
        file_uuid=file["uuid"],
        storage_path=tmp_path,
    )
    assert path.read_bytes() == data
    assert file["name"] == "upload.txt"
    assert file["storage_type"] == "file"
    assert "storage_id" not in file
    assert "storage_object_id" not in file
    resp = api.get(f"{FILES}{file['uuid']}/actions/download")
    assert resp.status_code == 200, resp.text
    assert resp.content == data
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert 'filename="upload.txt"' in resp.headers["Content-Disposition"]

    resp = api.get(
        f"{FILES}{file['uuid']}/actions/download", user=stream_user
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == data

    assert file["size_bytes"] == len(data)
    assert file["hash"] == hashlib.sha256(data).hexdigest()

    resp = api.delete(f"{FILES}{file['uuid']}")
    assert resp.status_code in (200, 204), resp.text
    assert not path.exists()


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


def test_system_folders_exist_for_user_without_streams(api, db):
    conftest.seed_workspace_user(
        db,
        api.user_uuid,
        f"user-{api.user_uuid}",
    )
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_external_accounts
                (uuid, project_id, user_uuid, server_url, account_type,
                 status, account_settings, created_at, updated_at)
            VALUES
                (%s, %s, %s, 'https://iam.example.local', 'iam', 'active',
                 %s::jsonb, NOW(), NOW())
            """,
            (
                str(sys_uuid.uuid4()),
                api.project_id,
                api.user_uuid,
                (
                    '{"kind": "iam", "credentials": {"kind": "iam", '
                    '"username": "admin", "access_token": "token"}}'
                ),
            ),
        )

    resp = api.get(FOLDERS)
    assert resp.status_code == 200, resp.text
    folders_by_uuid = {
        folder["uuid"]: folder
        for folder in resp.json()
    }
    expected_folders = {
        str(messenger_dm_helpers.ALL_CHATS_FOLDER_UUID): "All chats",
        str(messenger_dm_helpers.PERSONAL_FOLDER_UUID): "Personal",
        str(messenger_dm_helpers.CHANNELS_FOLDER_UUID): "Channels",
    }
    assert {
        uuid: folders_by_uuid[uuid]["title"]
        for uuid in expected_folders
    } == expected_folders
    assert all(
        folders_by_uuid[uuid]["folder_items"] == []
        for uuid in expected_folders
    )
    assert all(
        folders_by_uuid[uuid]["background_color_value"] == 11184810
        for uuid in expected_folders
    )

    for folder_uuid, title in expected_folders.items():
        resp = api.get(f"{FOLDERS}{folder_uuid}")
        assert resp.status_code == 200, resp.text
        folder = resp.json()
        assert folder["title"] == title
        assert folder["background_color_value"] == 11184810
        assert folder["folder_items"] == []


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
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.created"
    assert event["payload"]["uuid"] == folder["uuid"]
    assert event["payload"]["title"] == "Inbox"


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
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.updated"
    assert event["payload"]["uuid"] == folder["uuid"]
    assert event["payload"]["title"] == "Archive"


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
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.deleted"
    assert event["payload"] == payload


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
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.updated"
    assert event["payload"]["uuid"] == folder["uuid"]
    assert event["payload"]["folder_items"][0]["stream_uuid"] == stream_uuid


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
    assert event["object_type"] == "folder_item"
    assert event["payload"]["kind"] == "folder_item.deleted"
    assert event["payload"] == payload


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
    assert item.get("pinned_at") is None

    resp = api.post(f"{FOLDER_ITEMS}{item['uuid']}/actions/pin/invoke")
    assert resp.status_code == 200, resp.text
    pinned_item = resp.json()
    assert pinned_item["uuid"] == item["uuid"]
    assert pinned_item["pinned_at"] is not None

    resp = api.post(f"{FOLDER_ITEMS}{item['uuid']}/actions/unpin/invoke")
    assert resp.status_code == 200, resp.text
    unpinned_item = resp.json()
    assert unpinned_item["uuid"] == item["uuid"]
    assert unpinned_item.get("pinned_at") is None

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
    assert unpin_payload["folder_items"][0].get("pinned_at") is None

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": unpin_payload,
        }
    )
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.updated"
    assert event["payload"]["folder_items"][0].get("pinned_at") is None


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
    assert unpinned_item.get("pinned_at") is None

    resp = api.get(f"{FOLDERS}{messenger_dm_helpers.ALL_CHATS_FOLDER_UUID}")
    assert resp.status_code == 200, resp.text
    folder_item = [
        item for item in resp.json()["folder_items"]
        if item["uuid"] == item_uuid
    ][0]
    assert folder_item.get("pinned_at") is None

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
    system_folder_titles = {"All chats", "Personal", "Channels"}

    api.post(FOLDERS, json={"title": "mine"})
    api.post(FOLDERS, json={"title": "theirs"}, user=other_user)

    titles = [
        f["title"]
        for f in api.get(FOLDERS).json()
        if f["title"] not in system_folder_titles
    ]
    assert titles == ["mine"]

    other_titles = [
        f["title"]
        for f in api.get(FOLDERS, user=other_user).json()
        if f["title"] not in system_folder_titles
    ]
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

    assert len(rows) == 4
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
    assert stream.get("last_message_uuid") is None
    assert payload.get("last_message_uuid") is None
    assert 0 <= stream["color"] <= 0xFFFFFF
    assert payload["color"] == stream["color"]
    assert payload["source_name"] == "native"
    assert payload["source"] == {"kind": "native"}

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["object_type"] == "stream"
    assert event["payload"]["kind"] == "stream.created"
    assert event["payload"]["uuid"] == stream["uuid"]
    assert event["payload"]["name"] == "Engineering"
    assert event["payload"]["role"] == "owner"
    assert event["payload"]["notification_mode"] == "all_messages"
    assert event["payload"]["color"] == stream["color"]
    assert event["payload"].get("last_message_uuid") is None

    topic_epoch_version, topic_user_uuid, topic_payload = rows[3]
    assert str(topic_user_uuid) == str(api.user_uuid)
    assert topic_payload["kind"] == "topic.created"
    assert topic_payload["name"] == "General Topic"
    assert topic_payload["stream_uuid"] == stream["uuid"]
    assert topic_payload["user_uuid"] == str(api.user_uuid)
    assert topic_payload["project_id"] == str(api.project_id)
    assert topic_payload["is_default"] is True
    assert topic_payload["is_done"] is False
    assert topic_payload["unread_count"] == 0
    assert topic_payload["notification_mode"] == "default"
    assert topic_payload.get("last_message_uuid") is None
    assert 0 <= topic_payload["color"] <= 0xFFFFFF

    topic_event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": topic_epoch_version,
            "user_uuid": api.user_uuid,
            "payload": topic_payload,
        }
    )
    assert topic_event["object_type"] == "topic"
    assert topic_event["payload"]["kind"] == "topic.created"
    assert topic_event["payload"]["uuid"] == topic_payload["uuid"]
    assert topic_event["payload"]["name"] == "General Topic"
    assert topic_event["payload"]["is_default"] is True

    folder_events = [row[2] for row in rows[1:3]]
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
    assert event["object_type"] == "stream"
    assert event["payload"]["kind"] == "stream.updated"
    assert event["payload"]["notification_mode"] == "mentions_only"


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
    message = message_resp.json()
    message_uuid = message["uuid"]
    assert message["reactions"] == {}
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_message_reactions
                (uuid, project_id, created_at, updated_at, message_uuid,
                 user_uuid, emoji_name)
            VALUES (%s, %s, NOW(), NOW(), %s, %s, 'thumbs_up')
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
    conftest.seed_workspace_user(
        db,
        direct_user_uuid,
        f"user-{direct_user_uuid}",
    )
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
    for target_uuid in (target_user_uuid, second_target_user_uuid):
        conftest.seed_workspace_user(
            db,
            target_uuid,
            f"user-{target_uuid}",
        )
    file_resp = api.post(
        FILES,
        json={
            "stream_uuid": stream_uuid,
            "name": "roadmap.txt",
            "description": "Roadmap",
            "content_type": "text/plain",
            "size_bytes": 7,
            "hash": "hash",
        },
    )
    assert file_resp.status_code in (200, 201), file_resp.text
    file_uuid = file_resp.json()["uuid"]

    resp = api.get(f"{FILES}{file_uuid}", user=target_user_uuid)
    assert resp.status_code == 404, resp.text

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
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid
            FROM m_workspace_file_accesses
            WHERE file_uuid = %s
            """,
            (file_uuid,),
        )
        access_user_uuids = {str(row[0]) for row in cur.fetchall()}
    assert access_user_uuids == {
        str(api.user_uuid),
        str(target_user_uuid),
        str(second_target_user_uuid),
    }

    for target_uuid in (target_user_uuid, second_target_user_uuid):
        resp = api.get(f"{FILES}{file_uuid}", user=target_uuid)
        assert resp.status_code == 200, resp.text
        assert resp.json()["uuid"] == file_uuid

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
    assert owner_events[0]["uuid"] == stream_uuid
    assert [
        binding["user_uuid"]
        for binding in owner_events[0]["items"]
    ] == [
        str(target_user_uuid),
        str(second_target_user_uuid),
    ]
    assert {
        binding["who_uuid"]
        for binding in owner_events[0]["items"]
    } == {str(api.user_uuid)}
    assert {
        binding["role"]
        for binding in owner_events[0]["items"]
    } == {"member"}
    assert {
        binding["notification_mode"]
        for binding in owner_events[0]["items"]
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

    file_resp = api.post(
        FILES,
        json={
            "stream_uuid": stream_uuid,
            "name": "handoff.txt",
            "description": "Handoff",
            "content_type": "text/plain",
            "size_bytes": 8,
            "hash": "hash",
        },
    )
    assert file_resp.status_code in (200, 201), file_resp.text
    file_uuid = file_resp.json()["uuid"]

    resp = api.get(f"{FILES}{file_uuid}", user=target_user_uuid)
    assert resp.status_code == 200, resp.text

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
    resp = api.get(f"{FILES}{file_uuid}", user=target_user_uuid)
    assert resp.status_code == 404, resp.text
    resp = api.get(f"{FILES}{file_uuid}")
    assert resp.status_code == 200, resp.text

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
            SELECT COUNT(*)
            FROM m_workspace_file_accesses
            WHERE file_uuid = %s
                AND user_uuid = %s
            """,
            (file_uuid, str(target_user_uuid)),
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
    for event in events[1:]:
        assert all(
            item["stream_uuid"] != stream_uuid
            for item in event["folder_items"]
        )


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
    assert 0 <= topic["color"] <= 0xFFFFFF
    assert topic.get("last_message_uuid") is None
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
        assert payload["color"] == topic["color"]
        assert payload.get("last_message_uuid") is None
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
    assert event["object_type"] == "topic"
    assert event["payload"]["kind"] == "topic.created"
    assert event["payload"]["uuid"] == topic["uuid"]
    assert event["payload"]["name"] == "planning"
    assert event["payload"]["color"] == topic["color"]
    assert event["payload"].get("last_message_uuid") is None
    assert event["payload"]["notification_mode"] == "default"


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

    resp = api.put(
        f"{STREAM_TOPICS}{topic_uuid}",
        json={"name": "retros", "color": 0xABCDEF},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "retros"
    assert resp.json()["color"] == 0xABCDEF

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "retros"
    assert resp.json()["color"] == 0xABCDEF

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
        assert payload["color"] == 0xABCDEF


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


def test_external_folder_and_binding_events_follow_stream_visibility(api, db):
    conftest.seed_workspace_user(
        db,
        api.user_uuid,
        f"user-{api.user_uuid}",
    )
    stream_uuid = sys_uuid.uuid4()
    server_url = "https://zulip-hidden.example.test"
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
              AND user_uuid = %s
            """,
            (api.project_id, api.user_uuid),
        )
        before_epoch = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO m_workspace_streams
                (uuid, name, description, source_name, source,
                 user_uuid, project_id, created_at, updated_at)
            VALUES (
                %s,
                'hidden-zulip',
                'hidden',
                'zulip',
                jsonb_build_object(
                    'kind', 'zulip',
                    'server_url', %s::text,
                    'stream_id', 42
                ),
                %s,
                %s,
                NOW(),
                NOW()
            )
            """,
            (
                str(stream_uuid),
                server_url,
                api.user_uuid,
                api.project_id,
            ),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_stream_bindings
                (uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'member', NOW(), NOW())
            """,
            (
                str(sys_uuid.uuid4()),
                api.project_id,
                str(stream_uuid),
                api.user_uuid,
                api.user_uuid,
            ),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_events
                (uuid, project_id, user_uuid, schema_version, object_type,
                 action, payload, created_at, updated_at)
            VALUES (
                %s,
                %s,
                %s,
                1,
                'folder',
                'updated',
                jsonb_build_object(
                    'kind', 'folder.updated',
                    'uuid', '00000000-0000-0000-0000-000000000000',
                    'title', 'All chats',
                    'system_type', 'all',
                    'unread_count', 7,
                    'folder_items', jsonb_build_array(jsonb_build_object(
                        'uuid', %s::text,
                        'folder', '00000000-0000-0000-0000-000000000000',
                        'project_id', %s::text,
                        'user_uuid', %s::text,
                        'stream_uuid', %s::text,
                        'chat_type', 'stream',
                        'unread_count', 7
                    )),
                    'created_at', '2026-07-10T00:00:00.000000Z',
                    'updated_at', '2026-07-10T00:00:00.000000Z'
                ),
                NOW(),
                NOW()
            )
            RETURNING epoch_version
            """,
            (
                str(sys_uuid.uuid4()),
                api.project_id,
                api.user_uuid,
                str(sys_uuid.uuid4()),
                api.project_id,
                api.user_uuid,
                str(stream_uuid),
            ),
        )
        folder_epoch = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO m_workspace_events
                (uuid, project_id, user_uuid, schema_version, object_type,
                 action, payload, created_at, updated_at)
            VALUES (
                %s,
                %s,
                %s,
                1,
                'stream_binding',
                'created',
                jsonb_build_object(
                    'kind', 'stream_bindings.created',
                    'uuid', %s::text,
                    'items', jsonb_build_array(jsonb_build_object(
                        'uuid', %s::text,
                        'project_id', %s::text,
                        'user_uuid', %s::text,
                        'stream_uuid', %s::text,
                        'who_uuid', %s::text,
                        'role', 'member',
                        'notification_mode', 'all_messages'
                    ))
                ),
                NOW(),
                NOW()
            )
            RETURNING epoch_version
            """,
            (
                str(sys_uuid.uuid4()),
                api.project_id,
                api.user_uuid,
                str(stream_uuid),
                str(sys_uuid.uuid4()),
                api.project_id,
                api.user_uuid,
                str(stream_uuid),
                api.user_uuid,
            ),
        )
        binding_epoch = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_visible_events
            WHERE epoch_version IN (%s, %s)
            """,
            (folder_epoch, binding_epoch),
        )
        assert cur.fetchone()[0] == 0

    resp = api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version>", before_epoch),
        ],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_external_accounts
                (uuid, project_id, user_uuid, server_url, account_type,
                 status, account_settings, source_scope, access_status,
                 access_checked_at, access_confirmed_at, created_at,
                 updated_at)
            VALUES (
                %s,
                %s,
                %s,
                %s,
                'zulip',
                'active',
                jsonb_build_object(
                    'kind', 'zulip',
                    'credentials', jsonb_build_object(
                        'kind', 'zulip',
                        'email', 'agent@example.test',
                        'api_key', 'token'
                    )
                ),
                %s,
                'confirmed',
                NOW(),
                NOW(),
                NOW(),
                NOW()
            )
            """,
            (
                str(sys_uuid.uuid4()),
                api.project_id,
                api.user_uuid,
                server_url,
                server_url,
            ),
        )
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_visible_events
            WHERE epoch_version IN (%s, %s)
            """,
            (folder_epoch, binding_epoch),
        )
        assert cur.fetchone()[0] == 2

    resp = api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version>", before_epoch),
        ],
    )
    assert resp.status_code == 200, resp.text
    assert [
        event["epoch_version"]
        for event in resp.json()
    ] == [folder_epoch, binding_epoch]


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
    assert message["reactions"] == {}

    other_message_resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert other_message_resp.status_code == 200, other_message_resp.text
    other_message = other_message_resp.json()
    assert other_message["read"] is False
    assert other_message["is_own"] is False
    assert other_message["reactions"] == {}

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
            SELECT last_message_uuid
            FROM m_workspace_user_streams
            WHERE project_id = %s
                AND uuid = %s
                AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, api.user_uuid),
        )
        assert str(cur.fetchone()[0]) == message_uuid
        cur.execute(
            """
            SELECT last_message_uuid
            FROM m_workspace_user_topics_view
            WHERE project_id = %s
                AND uuid = %s
                AND user_uuid = %s
            """,
            (api.project_id, topic_uuid, api.user_uuid),
        )
        assert str(cur.fetchone()[0]) == message_uuid

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
    assert author_payload["reactions"] == {}
    assert other_payload["user_uuid"] == str(other_user)
    assert other_payload["project_id"] == str(api.project_id)
    assert other_payload["read"] is False
    assert other_payload["pinned"] is False
    assert other_payload["starred"] is False
    assert other_payload["is_own"] is False
    assert other_payload["reactions"] == {}
    packed_author_payload = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": 1,
            "user_uuid": api.user_uuid,
            "payload": author_payload,
        }
    )["payload"]
    packed_other_payload = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": 2,
            "user_uuid": other_user,
            "payload": other_payload,
        }
    )["payload"]
    assert packed_author_payload["kind"] == "message.created"
    assert packed_other_payload["kind"] == "message.created"
    assert {
        key: value for key, value in packed_author_payload.items()
        if key != "kind"
    } == message
    assert {
        key: value for key, value in packed_other_payload.items()
        if key != "kind"
    } == other_message

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
    assert event["payload"]["reactions"] == {}

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
    assert other_event["payload"]["reactions"] == {}
    assert other_events[1]["payload"]["last_message_uuid"] == message_uuid
    assert other_events[2]["payload"]["last_message_uuid"] == message_uuid

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
        "message.read",
        "topic.updated",
        "stream.updated",
        "folder.updated",
        "folder.updated",
    ]
    assert read_events[0]["uuid"] == message_uuid
    assert read_events[0]["read"] is True
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
            SELECT s.last_message_uuid, t.last_message_uuid
            FROM m_workspace_user_streams AS s
            JOIN m_workspace_user_topics_view AS t
                ON t.stream_uuid = s.uuid
                AND t.project_id = s.project_id
                AND t.user_uuid = s.user_uuid
            WHERE s.project_id = %s
                AND s.uuid = %s
                AND t.uuid = %s
                AND s.user_uuid = %s
            """,
            (api.project_id, stream_uuid, topic_uuid, api.user_uuid),
        )
        assert cur.fetchone() == (None, None)
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


def test_message_reaction_crud_is_user_scoped_and_writes_message_events(api, db):
    other_user = sys_uuid.uuid4()
    outsider_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "reaction-crud-team"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general",
        is_default=True,
    )

    message_resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "react to this",
            },
        },
    )
    assert message_resp.status_code == 201, message_resp.text
    message_uuid = message_resp.json()["uuid"]

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_reactions_epoch = cur.fetchone()[0]

    reaction_uuid = str(sys_uuid.uuid4())
    resp = api.post(
        MESSAGE_REACTIONS,
        json={
            "uuid": reaction_uuid,
            "message_uuid": message_uuid,
            "emoji_name": "thumbs_up",
        },
    )
    assert resp.status_code == 201, resp.text
    reaction = resp.json()
    assert reaction["uuid"] == reaction_uuid
    assert reaction["project_id"] == str(api.project_id)
    assert reaction["user_uuid"] == str(api.user_uuid)
    assert reaction["message_uuid"] == message_uuid
    assert reaction["emoji_name"] == "thumbs_up"
    assert "status" not in reaction

    resp = api.get(f"{MESSAGE_REACTIONS}{reaction_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["emoji_name"] == "thumbs_up"

    resp = api.get(f"{MESSAGE_REACTIONS}{reaction_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_uuid"] == str(api.user_uuid)

    resp = api.get(f"{MESSAGE_REACTIONS}{reaction_uuid}", user=outsider_user)
    assert resp.status_code == 404, resp.text

    resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactions"] == {"thumbs_up": 1}

    duplicate_resp = api.post(
        MESSAGE_REACTIONS,
        json={
            "message_uuid": message_uuid,
            "emoji_name": "thumbs_up",
        },
    )
    assert duplicate_resp.status_code == 409, duplicate_resp.text

    second_resp = api.post(
        MESSAGE_REACTIONS,
        json={
            "message_uuid": message_uuid,
            "emoji_name": "eyes",
        },
    )
    assert second_resp.status_code == 201, second_resp.text
    second_reaction_uuid = second_resp.json()["uuid"]

    other_resp = api.post(
        MESSAGE_REACTIONS,
        user=other_user,
        json={
            "message_uuid": message_uuid,
            "emoji_name": "thumbs_up",
        },
    )
    assert other_resp.status_code == 201, other_resp.text
    other_reaction_uuid = other_resp.json()["uuid"]

    resp = api.get(f"{MESSAGES}{message_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactions"] == {
        "eyes": 1,
        "thumbs_up": 2,
    }

    resp = api.get(MESSAGE_REACTIONS, params={"message_uuid": message_uuid})
    assert resp.status_code == 200, resp.text
    expected_reactions = {
        ("eyes", str(api.user_uuid)),
        ("thumbs_up", str(api.user_uuid)),
        ("thumbs_up", str(other_user)),
    }
    assert {
        (item["emoji_name"], item["user_uuid"])
        for item in resp.json()
    } == expected_reactions

    other_filter_resp = api.get(
        MESSAGE_REACTIONS,
        user=other_user,
        params={"message_uuid": message_uuid},
    )
    assert other_filter_resp.status_code == 200, other_filter_resp.text
    assert {
        (item["emoji_name"], item["user_uuid"])
        for item in other_filter_resp.json()
    } == expected_reactions

    user_filter_resp = api.get(
        MESSAGE_REACTIONS,
        params={"message_uuid": message_uuid, "user_uuid": str(other_user)},
    )
    assert user_filter_resp.status_code == 200, user_filter_resp.text
    assert [item["emoji_name"] for item in user_filter_resp.json()] == [
        "thumbs_up",
    ]

    outsider_filter_resp = api.get(
        MESSAGE_REACTIONS,
        user=outsider_user,
        params={"message_uuid": message_uuid},
    )
    assert outsider_filter_resp.status_code == 200, outsider_filter_resp.text
    assert outsider_filter_resp.json() == []

    resp = api.put(
        f"{MESSAGE_REACTIONS}{reaction_uuid}",
        json={"emoji_name": "heart"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["emoji_name"] == "heart"

    resp = api.get(f"{MESSAGES}{message_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactions"] == {
        "eyes": 1,
        "heart": 1,
        "thumbs_up": 1,
    }

    resp = api.delete(f"{MESSAGE_REACTIONS}{other_reaction_uuid}")
    assert resp.status_code == 404, resp.text

    resp = api.delete(f"{MESSAGE_REACTIONS}{second_reaction_uuid}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{MESSAGE_REACTIONS}{second_reaction_uuid}")
    assert resp.status_code == 404, resp.text

    resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactions"] == {
        "heart": 1,
        "thumbs_up": 1,
    }

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT emoji_name, user_uuid
            FROM m_workspace_message_reactions
            WHERE project_id = %s
                AND message_uuid = %s
            ORDER BY emoji_name, user_uuid
            """,
            (api.project_id, message_uuid),
        )
        stored_reactions = [
            (emoji_name, str(user_uuid))
            for emoji_name, user_uuid in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT object_type, action, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_reactions_epoch),
        )
        reaction_event_rows = [
            (object_type, action, str(user_uuid), payload)
            for object_type, action, user_uuid, payload in cur.fetchall()
        ]

    assert stored_reactions == [
        ("heart", str(api.user_uuid)),
        ("thumbs_up", str(other_user)),
    ]
    expected_event_users = {str(api.user_uuid), str(other_user)}
    expected_reaction_snapshots = [
        {"thumbs_up": 1},
        {"eyes": 1, "thumbs_up": 1},
        {"eyes": 1, "thumbs_up": 2},
        {"eyes": 1, "heart": 1, "thumbs_up": 1},
        {"heart": 1, "thumbs_up": 1},
    ]
    message_event_rows = [
        row for row in reaction_event_rows
        if row[0] == "message"
    ]
    reaction_state_event_rows = [
        row for row in reaction_event_rows
        if row[0] == "message_reaction"
    ]
    assert len(message_event_rows) == len(expected_reaction_snapshots) * 2
    assert all(
        action == "updated" and payload["kind"] == "message.updated"
        for _, action, _, payload in message_event_rows
    )
    assert all(
        payload["kind"] == "message.updated"
        for _, _, _, payload in message_event_rows
    )
    assert all(
        payload["uuid"] == message_uuid
        for _, _, _, payload in message_event_rows
    )
    for index, expected_reactions in enumerate(expected_reaction_snapshots):
        group = message_event_rows[index * 2:index * 2 + 2]
        assert {user_uuid for _, _, user_uuid, _ in group} == expected_event_users
        assert all(
            payload["reactions"] == expected_reactions
            for _, _, _, payload in group
        )

    expected_reaction_events = [
        ("created", str(api.user_uuid), reaction_uuid, "thumbs_up"),
        ("created", str(api.user_uuid), second_reaction_uuid, "eyes"),
        ("created", str(other_user), other_reaction_uuid, "thumbs_up"),
        ("updated", str(api.user_uuid), reaction_uuid, "heart"),
        ("deleted", str(api.user_uuid), second_reaction_uuid, "eyes"),
    ]
    assert len(reaction_state_event_rows) == len(expected_reaction_events)
    for event_row, expected in zip(
        reaction_state_event_rows,
        expected_reaction_events,
    ):
        _, action, event_user_uuid, payload = event_row
        expected_action, expected_user_uuid, expected_uuid, expected_emoji = (
            expected
        )
        assert action == expected_action
        assert event_user_uuid == expected_user_uuid
        assert payload["kind"] == f"message_reaction.{expected_action}"
        assert payload["uuid"] == expected_uuid
        assert payload["message_uuid"] == message_uuid
        assert payload["user_uuid"] == expected_user_uuid
        assert payload["emoji_name"] == expected_emoji
        assert payload["source_name"] == "native"
        assert payload["source"]["kind"] == "native"
        if expected_action == "updated":
            assert payload["old_message_uuid"] == message_uuid
            assert payload["old_emoji_name"] == "thumbs_up"
            assert payload["old_source_name"] == "native"
            assert payload["old_source"]["kind"] == "native"
        else:
            assert "old_message_uuid" not in payload
            assert "old_emoji_name" not in payload


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


def test_message_create_uses_stream_default_topic(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "default-topic-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general",
        is_default=True,
    )
    message_uuid = str(sys_uuid.uuid4())

    resp = api.post(
        MESSAGES,
        json={
            "uuid": message_uuid,
            "stream_uuid": stream_uuid,
            "payload": {
                "kind": "markdown",
                "content": "missing topic",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["topic_uuid"] == topic_uuid

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT topic_uuid
            FROM m_workspace_messages
            WHERE uuid = %s
            """,
            (message_uuid,),
        )
        stored_topic_uuid = cur.fetchone()[0]
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'message.created'
                AND payload->>'uuid' = %s
            """,
            (api.project_id, message_uuid),
        )
        event_payload = cur.fetchone()[0]

    assert str(stored_topic_uuid) == topic_uuid
    assert event_payload["topic_uuid"] == topic_uuid


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


def test_zulip_message_flag_sync_keeps_author_read(api, db):
    other_user = sys_uuid.uuid4()
    server_url = "https://zulip.example.test"
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "zulip-own-message"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general",
        is_default=True,
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, stream_uuid, other_user
    )
    with db.cursor() as cur:
        for user_uuid in (api.user_uuid, other_user):
            cur.execute(
                """
                INSERT INTO m_external_accounts
                    (uuid, project_id, user_uuid, server_url, account_type,
                     status, account_settings, source_scope, access_status,
                     access_checked_at, access_confirmed_at, created_at,
                     updated_at)
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    'zulip',
                    'active',
                    jsonb_build_object(
                        'kind', 'zulip',
                        'credentials', jsonb_build_object(
                            'kind', 'zulip',
                            'login', 'agent@example.test',
                            'token', 'token'
                        )
                    ),
                    %s,
                    'confirmed',
                    NOW(),
                    NOW(),
                    NOW(),
                    NOW()
                )
                """,
                (
                    str(sys_uuid.uuid4()),
                    api.project_id,
                    str(user_uuid),
                    server_url,
                    server_url,
                ),
            )
    message_uuid = sys_uuid.uuid4()
    message = messenger_dm_helpers.create_workspace_user_message(
        uuid=message_uuid,
        project_id=sys_uuid.UUID(api.project_id),
        user_uuid=sys_uuid.UUID(api.user_uuid),
        stream_uuid=sys_uuid.UUID(stream_uuid),
        topic_uuid=sys_uuid.UUID(topic_uuid),
        payload=message_payloads.MarkdownPayload(content="sent through Zulip"),
        source_name=messenger_models.SourceName.ZULIP.value,
        source=messenger_models.ZulipSource(
            stream_id=42,
            server_url=server_url,
            topic_name="general",
            message_id=123,
        ),
    )

    assert message.read is True
    message = messenger_dm_helpers.sync_workspace_user_message_flags(
        project_id=sys_uuid.UUID(api.project_id),
        user_uuid=sys_uuid.UUID(api.user_uuid),
        message_uuid=message_uuid,
        values={"read": False},
    )
    assert message.read is True

    other_message = messenger_dm_helpers.get_workspace_user_message(
        project_id=sys_uuid.UUID(api.project_id),
        user_uuid=other_user,
        message_uuid=message_uuid,
    )
    assert other_message.read is False

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
