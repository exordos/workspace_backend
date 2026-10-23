# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models as zulip_models
from workspace.messenger_api.dm import synchronization
from workspace.messenger_api.dm import v2_models
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_message_states as message_tests


message_sources = message_tests.message_sources
MIGRATION = "0264-Add-synchronization-status-to-provider-entities-ca9efd.py"
BASE = "0263-Normalize-provider-identities-and-remove-source-payloads-b52ac4.py"
TABLES = (
    "messenger_streams",
    "messenger_stream_bindings",
    "messenger_topics",
    "messenger_user_topic_bindings",
    "messenger_messages",
    "messenger_user_message_states",
    "messenger_message_reaction_facts",
    "m_workspace_files",
)


@pytest.fixture
def prepared(message_sources):
    account, source = message_sources()
    message_tests._prepare(account)
    with contexts.Context().session_manager():
        zulip_models.ZulipMessageState.reconcile_batch(100)
    return account, source


def test_imported_entities_and_native_api_records_are_ready(prepared, db, api):
    account, _ = prepared
    for table in TABLES[:6]:
        assert db.execute(
            f"SELECT status FROM {table} WHERE project_id=%s", (account.project_id,)
        ).fetchall() == [("active",)], table
    api.project_id, api.user_uuid = (
        str(account.project_id),
        str(account.owner_user_uuid),
    )
    message = message_tests._placements(account)[0]
    assert (
        api.get(f"/v1/messenger/messages/{message.uuid}").json()["status"] == "active"
    )
    stream = api.post("/v1/messenger/streams/", json={"name": "Ready"})
    assert stream.status_code == 201, stream.text
    assert stream.json()["status"] == "active"


def test_status_is_managed_by_workers_and_native_status_is_constrained(api, db):
    root = "/v1/messenger/streams/"
    response = api.post(root, json={"name": "Forged", "status": "error"})
    assert response.status_code == 400, response.text
    response = api.post(root, json={"name": "Native"})
    assert response.status_code == 201, response.text
    path = root + response.json()["uuid"]
    response = api.put(path, json={"status": "syncing"})
    assert response.status_code == 400, response.text
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "UPDATE messenger_streams SET status='error' WHERE uuid=%s",
            (path.rsplit("/", 1)[1],),
        )


def test_status_transitions_do_not_enqueue_domain_work(prepared, db):
    account, source = prepared
    canonical = uuid.uuid5(account.project_id, str(source.message_uuid))

    def pending():
        return db.execute(
            "SELECT message_uuid,revision FROM messenger_message_changes WHERE project_id=%s ORDER BY message_uuid",
            (account.project_id,),
        ).fetchall()

    before = pending()
    with contexts.Context().session_manager() as session:
        with synchronization.synchronize(
            v2_models.WorkspaceMessage, account.project_id, uuid=canonical
        ):
            assert (
                session.execute(
                    "SELECT status FROM messenger_messages WHERE uuid=%s", (canonical,)
                ).fetchone()["status"]
                == "syncing"
            )
    assert pending() == before
    assert db.execute(
        "SELECT status FROM messenger_messages WHERE uuid=%s", (canonical,)
    ).fetchone() == ("active",)


def test_worker_records_entity_failure_after_rollback_and_recovers(
    prepared, db, monkeypatch
):
    account, source = prepared
    canonical = uuid.uuid5(account.project_id, str(source.message_uuid))
    state = zulip_models.ZulipMessageState(
        project_id=account.project_id,
        message_uuid=canonical,
        source_uuid=source.uuid,
        action="update",
    )
    original = zulip_models.ZulipMessageState.update_content
    seen = []

    def update(current):
        session = contexts.Context().get_session()
        seen.append(
            session.execute(
                "SELECT status FROM messenger_messages WHERE uuid=%s", (canonical,)
            ).fetchone()["status"]
        )
        raise RuntimeError("Entity synchronization failed")

    def execute(_content, _task):
        state.do()

    monkeypatch.setattr(zulip_models.ReconciliationContent, "execute", execute)
    monkeypatch.setattr(zulip_models.ZulipMessageState, "update_content", update)
    task = zulip_models.Task(content=zulip_models.ReconciliationContent())
    task.insert()
    with contexts.Context().session_manager():
        task.execute()
    assert seen == ["syncing"]
    assert task.status == "retry"
    assert db.execute(
        "SELECT status FROM messenger_messages WHERE uuid=%s", (canonical,)
    ).fetchone() == ("error",)
    assert db.execute(
        "SELECT status FROM messenger_streams WHERE project_id=%s",
        (account.project_id,),
    ).fetchone() == ("active",)
    monkeypatch.setattr(zulip_models.ZulipMessageState, "update_content", original)
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done"
    assert db.execute(
        "SELECT status FROM messenger_messages WHERE uuid=%s", (canonical,)
    ).fetchone() == ("active",)
    task.delete()


def test_sync_status_migration_preserves_existing_rows_and_round_trips(_database):
    with conftest.isolated_migration_database(BASE) as url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        user_uuid, project, stream_uuid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with psycopg.connect(url, autocommit=True) as db:
            conftest.seed_workspace_user(db, user_uuid, "sync-status-migration")
            db.execute(
                "INSERT INTO messenger_project_users(project_id,user_uuid) VALUES (%s,%s)",
                (project, user_uuid),
            )
            db.execute(
                "INSERT INTO messenger_streams(uuid,project_id,owner_uuid,name) VALUES (%s,%s,%s,'Ready')",
                (stream_uuid, project, user_uuid),
            )
            engine.apply_migration(MIGRATION)
            assert db.execute(
                "SELECT status FROM messenger_streams WHERE uuid=%s", (stream_uuid,)
            ).fetchone() == ("active",)
            assert db.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_name=ANY(%s) AND column_name='status'",
                (list(TABLES),),
            ).fetchone() == (8,)
            engine.rollback_migration(MIGRATION)
            assert db.execute(
                "SELECT name FROM messenger_streams WHERE uuid=%s", (stream_uuid,)
            ).fetchone() == ("Ready",)
            assert db.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_name=ANY(%s) AND column_name='status'",
                (list(TABLES),),
            ).fetchone() == (0,)
            engine.apply_migration(MIGRATION)
            assert db.execute(
                "SELECT status FROM messenger_streams WHERE uuid=%s", (stream_uuid,)
            ).fetchone() == ("active",)
