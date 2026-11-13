# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.tests.integration import legacy_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2


STREAMS = "/v1/messenger/streams/"
MESSAGES = "/v1/messenger/messages/"
ENCRYPTION_MIGRATION = "0186-Add-stream-encryption-metadata-17aa3c.py"
KEY_SHARING_MIGRATION = "0301-Add-stream-key-sharing-policy-58fcbc.py"
KEY_SHARING_FIELD = "allow_key_sharing_by_holders"


@pytest.fixture
def encryption_migration_database(_database):
    with conftest.isolated_migration_database(ENCRYPTION_MIGRATION) as database_url:
        with psycopg.connect(database_url, autocommit=True) as connection:
            yield connection


@pytest.fixture(autouse=True)
def _production_v2_store(_database):
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(legacy_store.LegacyMessengerStoreFactory())


def test_created_stream_returns_explicit_encryption_defaults(api):
    response = api.post(STREAMS, json={"name": "Encryption defaults"})

    assert response.status_code == 201, response.text
    stream = response.json()
    assert stream["encryption"] is False
    assert stream["current_encryption_key_uuid"] is None
    assert stream[KEY_SHARING_FIELD] is False


def test_owner_changes_key_sharing_policy_in_stream_responses_and_events(
    api, workspace_api, db
):
    response = api.post(
        STREAMS,
        json={
            "name": "Key sharing policy",
            "encryption": True,
            "current_encryption_key_uuid": str(sys_uuid.uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()[KEY_SHARING_FIELD] is False
    stream_uuid = response.json()["uuid"]
    path = f"{STREAMS}{stream_uuid}"
    workspace_api.project_id = api.project_id
    for user, role in (
        (workspace_api.user_uuid, "member"),
        (sys_uuid.uuid4(), "administrator"),
        (sys_uuid.uuid4(), "owner"),
    ):
        conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, user, role)
        response = api.put(path, user=user, json={KEY_SHARING_FIELD: True})
        assert response.status_code == 403, response.text
    for invalid in ("true", 1, None):
        response = api.put(path, json={KEY_SHARING_FIELD: invalid})
        assert response.status_code == 400, response.text

    for enabled in (True, False):
        response = api.put(path, json={KEY_SHARING_FIELD: enabled})
        assert response.status_code == 200, response.text
        assert response.json()[KEY_SHARING_FIELD] is enabled
        response = workspace_api.get(path)
        assert response.status_code == 200, response.text
        assert response.json()[KEY_SHARING_FIELD] is enabled
        response = workspace_api.get(STREAMS, params={"uuid": stream_uuid})
        assert response.status_code == 200, response.text
        assert response.json()[0][KEY_SHARING_FIELD] is enabled
        with contexts.Context().session_manager() as session:
            v2_projection.drain_projection_queue(
                session, f"key-sharing-test:{sys_uuid.uuid4()}"
            )
        response = workspace_api.get("/v1/events/")
        assert response.status_code == 200, response.text
        updates = [
            event["payload"]
            for event in response.json()
            if event["payload"]["kind"] == "stream.updated"
            and event["payload"]["uuid"] == stream_uuid
        ]
        assert updates
        assert updates[-1][KEY_SHARING_FIELD] is enabled


def test_key_sharing_migration_defaults_existing_streams_and_rolls_back(_database):
    with conftest.isolated_migration_database(KEY_SHARING_MIGRATION) as database_url:
        engine = ra_migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        engine.rollback_migration(KEY_SHARING_MIGRATION)
        with psycopg.connect(database_url, autocommit=True) as conn:
            project, user = sys_uuid.uuid4(), sys_uuid.uuid4()
            stream = conftest.seed_user_stream(conn, project, user, "Existing stream")
            conn.execute(
                "ALTER VIEW messenger_api_user_streams_v1 SET (security_barrier=true)"
            )
            conn.execute(
                "GRANT SELECT (name) ON messenger_api_user_streams_v1 TO PUBLIC"
            )
            view_name_acl = """
                SELECT attacl FROM pg_attribute
                WHERE attrelid = 'messenger_api_user_streams_v1'::regclass
                  AND attname = 'name'
            """
            view_metadata = """
                SELECT pg_get_viewdef(oid), reloptions, relowner
                FROM pg_class
                WHERE oid = 'messenger_api_user_streams_v1'::regclass
            """
            original = conn.execute(view_metadata).fetchone()
            original_acl = conn.execute(view_name_acl).fetchone()
            engine.apply_migration(KEY_SHARING_MIGRATION)
            assert conn.execute(view_metadata).fetchone()[1:] == original[1:]
            assert conn.execute(view_name_acl).fetchone() == original_acl
            for relation in ("messenger_streams", "messenger_api_user_streams_v1"):
                assert conn.execute(
                    f"SELECT {KEY_SHARING_FIELD} FROM {relation} WHERE uuid = %s",
                    (stream,),
                ).fetchone() == (False,)
            engine.rollback_migration(KEY_SHARING_MIGRATION)
            assert conn.execute(view_metadata).fetchone() == original
            assert conn.execute(
                "SELECT name FROM messenger_api_user_streams_v1 WHERE uuid = %s",
                (stream,),
            ).fetchone() == ("Existing stream",)
            assert (
                conn.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name IN ('messenger_streams', 'messenger_api_user_streams_v1') "
                    "AND column_name = %s",
                    (KEY_SHARING_FIELD,),
                ).fetchall()
                == []
            )


@pytest.mark.parametrize("encryption", [True, False])
def test_stored_stream_encryption_is_consistent_in_reads_and_events(
    api, workspace_api, db, encryption
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    response = api.post(STREAMS, json={"name": "Encryption metadata"})
    assert response.status_code == 201, response.text
    stream_uuid = response.json()["uuid"]
    key_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_streams
            SET encryption = %s, current_encryption_key_uuid = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (encryption, key_uuid, api.project_id, stream_uuid),
        )
        for view in ("messenger_api_user_streams_v1",):
            cursor.execute(
                f"SELECT encryption, current_encryption_key_uuid FROM {view} "
                "WHERE project_id = %s AND uuid = %s AND user_uuid = %s",
                (api.project_id, stream_uuid, api.user_uuid),
            )
            assert cursor.fetchone() == (encryption, key_uuid)

    for client, collection in (
        (api, STREAMS),
        (workspace_api, "/v1/messenger/streams/"),
    ):
        response = client.get(f"{collection}{stream_uuid}")
        assert response.status_code == 200, response.text
        assert response.json()["encryption"] is encryption
        assert response.json()["current_encryption_key_uuid"] == str(key_uuid)
        response = client.get(collection, params={"uuid": stream_uuid})
        assert response.status_code == 200, response.text
        assert response.json()[0]["encryption"] is encryption
        assert response.json()[0]["current_encryption_key_uuid"] == str(key_uuid)

    response = api.put(f"{STREAMS}{stream_uuid}", json={"name": "Updated metadata"})
    assert response.status_code == 200, response.text
    with contexts.Context().session_manager() as session:
        v2_projection.drain_projection_queue(
            session,
            f"encryption-test:{sys_uuid.uuid4()}",
        )
        failures = session.execute(
            "SELECT task_kind, last_error FROM messenger_projection_tasks "
            "WHERE status IN ('failed', 'dead_letter')",
            (),
        ).fetchall()
        assert failures == []
    response = workspace_api.get("/v1/events/")
    assert response.status_code == 200, response.text
    updates = [
        event["payload"]
        for event in response.json()
        if event["payload"]["kind"] == "stream.updated"
        and event["payload"]["uuid"] == stream_uuid
    ]
    assert updates
    assert updates[-1]["encryption"] is encryption
    assert updates[-1]["current_encryption_key_uuid"] == str(key_uuid)


@pytest.mark.pre_retirement_schema
def test_encryption_migration_defaults_existing_streams_and_restores_views(
    api, encryption_migration_database
):
    db = encryption_migration_database
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(ENCRYPTION_MIGRATION)
    try:
        stream_uuid = conftest.seed_user_stream(
            db,
            api.project_id,
            api.user_uuid,
            "Before encryption migration",
        )
        with db.cursor() as cursor:
            definitions = {}
            for view in ("m_workspace_user_streams", "messenger_api_user_streams_v1"):
                cursor.execute("SELECT pg_get_viewdef(%s::regclass, true)", (view,))
                definitions[view] = cursor.fetchone()[0]

        engine.apply_migration(ENCRYPTION_MIGRATION)
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT encryption, current_encryption_key_uuid "
                "FROM messenger_streams WHERE uuid = %s",
                (stream_uuid,),
            )
            assert cursor.fetchone() == (False, None)
            cursor.execute("SELECT COUNT(*) FROM m_folder_all_items_view")
            assert cursor.fetchone()[0] > 0

        engine.rollback_migration(ENCRYPTION_MIGRATION)
        with db.cursor() as cursor:
            for view, expected in definitions.items():
                cursor.execute("SELECT pg_get_viewdef(%s::regclass, true)", (view,))
                assert cursor.fetchone()[0] == expected
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'messenger_streams' "
                "AND column_name IN ('encryption', 'current_encryption_key_uuid')"
            )
            assert cursor.fetchone()[0] == 0
    finally:
        engine.apply_migration(ENCRYPTION_MIGRATION)


def test_encrypted_stream_creation_persists_metadata_in_responses_and_events(
    api, workspace_api, db
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    key_uuid = str(sys_uuid.uuid4())
    response = api.post(
        STREAMS,
        json={
            "name": "Secret chat",
            "encryption": True,
            "current_encryption_key_uuid": key_uuid,
        },
    )
    assert response.status_code == 201, response.text
    stream_uuid = response.json()["uuid"]
    assert response.json()["encryption"] is True
    assert response.json()["current_encryption_key_uuid"] == key_uuid
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT encryption, current_encryption_key_uuid FROM messenger_streams "
            "WHERE project_id = %s AND uuid = %s",
            (api.project_id, stream_uuid),
        )
        assert cursor.fetchone() == (True, sys_uuid.UUID(key_uuid))
    read = workspace_api.get(f"/v1/messenger/streams/{stream_uuid}")
    assert read.status_code == 200, read.text
    assert read.json()["encryption"] is True
    assert read.json()["current_encryption_key_uuid"] == key_uuid
    test_messenger_v2._drain()
    response = workspace_api.get("/v1/events/")
    assert response.status_code == 200, response.text
    created = [
        event["payload"]
        for event in response.json()
        if event["payload"]["kind"] == "stream.created"
        and event["payload"]["uuid"] == stream_uuid
    ]
    assert created
    assert created[0]["encryption"] is True
    assert created[0]["current_encryption_key_uuid"] == key_uuid


@pytest.mark.parametrize(
    "metadata",
    [
        {"encryption": True},
        {"encryption": True, "current_encryption_key_uuid": None},
        {"current_encryption_key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20"},
        {
            "encryption": False,
            "current_encryption_key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
        },
        {"encryption": True, "current_encryption_key_uuid": "invalid"},
        {
            "encryption": "true",
            "current_encryption_key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
        },
        {"encryption": "false"},
        {"encryption": None},
        {
            "source_name": "zulip",
            "encryption": True,
            "current_encryption_key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
        },
    ],
)
def test_invalid_stream_encryption_leaves_no_stream_or_outbox(api, db, metadata):
    response = api.post(STREAMS, json={"name": "Invalid encryption", **metadata})

    assert response.status_code == 400, response.text
    with db.cursor() as cursor:
        for table in (
            "messenger_streams",
            "messenger_topics",
            "messenger_stream_bindings",
            "messenger_domain_outbox_events",
        ):
            cursor.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = %s", (api.project_id,)
            )
            assert cursor.fetchone() == (0,)


@pytest.mark.parametrize("encryption", [False, True])
@pytest.mark.parametrize("field", ["encryption", "current_encryption_key_uuid"])
def test_stream_put_rejects_encryption_fields_without_renaming(
    api, db, encryption, field
):
    metadata = {"encryption": encryption}
    if encryption:
        metadata["current_encryption_key_uuid"] = str(sys_uuid.uuid4())
    created = api.post(STREAMS, json={"name": "Immutable encryption", **metadata})
    assert created.status_code == 201, created.text
    stream = created.json()
    attempted = False if field == "encryption" else str(sys_uuid.uuid4())
    response = api.put(
        f"{STREAMS}{stream['uuid']}", json={"name": "Invalid rename", field: attempted}
    )

    assert response.status_code == 400, response.text
    read = api.get(f"{STREAMS}{stream['uuid']}")
    assert read.status_code == 200, read.text
    assert read.json() == stream


@pytest.mark.parametrize("direct", [False, True])
def test_repeated_stream_creation_cannot_reset_encryption(api, db, direct):
    metadata = {
        "name": "Repeat secret chat",
        "encryption": True,
        "current_encryption_key_uuid": str(sys_uuid.uuid4()),
    }
    if direct:
        peer_uuid = sys_uuid.uuid4()
        conftest.seed_workspace_user(db, peer_uuid, "encrypted-peer")
        test_messenger_v2._register_project_user(db, api.project_id, peer_uuid)
        metadata["direct_user_uuid"] = str(peer_uuid)
    else:
        metadata["uuid"] = str(sys_uuid.uuid4())
    first = api.post(STREAMS, json=metadata)
    assert first.status_code == 201, first.text
    repeated = api.post(STREAMS, json=metadata)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["uuid"] == first.json()["uuid"]
    for changed in (
        {"encryption": False, "current_encryption_key_uuid": None},
        {"current_encryption_key_uuid": str(sys_uuid.uuid4())},
    ):
        response = api.post(STREAMS, json={**metadata, **changed})
        assert response.status_code == 400, response.text
    read = api.get(f"{STREAMS}{first.json()['uuid']}")
    assert read.status_code == 200, read.text
    assert read.json()["encryption"] is True
    assert (
        read.json()["current_encryption_key_uuid"]
        == metadata["current_encryption_key_uuid"]
    )


@pytest.mark.parametrize("encryption", [False, True])
def test_native_stream_accepts_e2ee_with_an_old_key(api, db, encryption):
    metadata = {"name": "Native encrypted message", "encryption": encryption}
    if encryption:
        metadata["current_encryption_key_uuid"] = str(sys_uuid.uuid4())
    created = api.post(STREAMS, json=metadata)
    assert created.status_code == 201, created.text
    payload = {
        "kind": "e2ee",
        "key_uuid": str(sys_uuid.uuid4()),
        "content": "opaque ciphertext",
    }
    response = api.post(
        MESSAGES, json={"stream_uuid": created.json()["uuid"], "payload": payload}
    )

    assert response.status_code == 201, response.text
    assert response.json()["payload"] == payload
    test_messenger_v2._drain()
    read = api.get(f"{MESSAGES}{response.json()['uuid']}")
    assert read.status_code == 200, read.text
    assert read.json()["payload"] == payload


@pytest.mark.parametrize("other_source", ["native"])
def test_encrypting_a_message_does_not_change_its_independent_copy(
    api, db, other_source
):
    streams = []
    for name in ("Original stream", "Other placement stream"):
        response = api.post(STREAMS, json={"name": name})
        assert response.status_code == 201, response.text
        streams.append(response.json())
    if other_source == "zulip":
        test_messenger_v2._seed_v2_provider_route(
            db, api.project_id, api.user_uuid, streams[1]["uuid"]
        )
    plaintext = {"kind": "markdown", "content": "Shared message"}
    placements = []
    for stream in streams:
        response = api.post(
            MESSAGES,
            json={
                "uuid": str(sys_uuid.uuid4()),
                "stream_uuid": stream["uuid"],
                "payload": plaintext,
            },
        )
        assert response.status_code == 201, response.text
        placements.append(response.json()["uuid"])
    encrypted = {
        "kind": "e2ee",
        "key_uuid": str(sys_uuid.uuid4()),
        "content": "opaque ciphertext",
    }
    response = api.put(f"{MESSAGES}{placements[0]}", json={"payload": encrypted})

    assert response.status_code == (200 if other_source == "native" else 400), (
        response.text
    )
    expected = encrypted if other_source == "native" else plaintext
    for index, placement_uuid in enumerate(placements):
        read = api.get(f"{MESSAGES}{placement_uuid}")
        assert read.status_code == 200, read.text
        assert read.json()["payload"] == (expected if index == 0 else plaintext)
