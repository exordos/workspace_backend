# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest


STREAMS = "/v1/streams/"
ENCRYPTION_MIGRATION = "0186-Add-stream-encryption-metadata-17aa3c.py"


@pytest.fixture(autouse=True)
def _production_v2_store(_database):
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(
            sql_canonical_store.SQLCanonicalMessengerStoreFactory()
        )


def test_created_stream_returns_explicit_encryption_defaults(api):
    response = api.post(STREAMS, json={"name": "Encryption defaults"})

    assert response.status_code == 201, response.text
    stream = response.json()
    assert stream["encryption"] is False
    assert stream["current_encryption_key_uuid"] is None


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
        for view in ("m_workspace_user_streams", "messenger_api_user_streams_v1"):
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


def test_encryption_migration_defaults_existing_streams_and_restores_views(api, db):
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
