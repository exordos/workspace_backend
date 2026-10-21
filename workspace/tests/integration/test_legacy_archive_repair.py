# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Repair proven carrier-archive leaks without undoing later user choices."""

import json
import uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations

from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest

BASELINE = (
    "0248-Snapshot-outgoing-Zulip-deletion-tasks-before-tombstone-cleanup-e378c7.py"
)
REPAIR = (
    "0249-Restore-active-legacy-direct-conversations-after-archive-merging-079b28.py"
)


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("active", False),
        ("archived", True),
        ("later_edit", True),
        ("unselected", True),
        ("conflicting", True),
        ("other_project", True),
        ("channel", True),
    ],
)
def test_repair_requires_unambiguous_unchanged_active_native_snapshot(
    http_server, scenario, expected
):
    with conftest.isolated_migration_database(BASELINE) as url:
        project, owner, peer, target, original, carrier = (
            uuid.uuid4() for _ in range(6)
        )
        saved_project = uuid.uuid4() if scenario == "other_project" else project
        with psycopg.connect(url, autocommit=True) as db:
            for user in (owner, peer):
                conftest.seed_workspace_user(db, user, str(user))
                db.execute(
                    "SELECT messenger_v2_register_project_user(%s,%s)", (project, user)
                )
            db.execute(
                "INSERT INTO messenger_streams(uuid,project_id,owner_uuid,name,private,direct_user_uuid,source_name,is_archived,updated_at) "
                "VALUES(%s,%s,%s,'Merged DM',%s,%s,'zulip',true,%s)",
                (
                    target,
                    project,
                    owner,
                    scenario != "channel",
                    None if scenario == "channel" else peer,
                    "2026-01-03" if scenario == "later_edit" else "2026-01-01",
                ),
            )
            for user in (owner, peer):
                db.execute(
                    "INSERT INTO messenger_stream_bindings(uuid,project_id,stream_uuid,user_uuid,who_uuid) VALUES(%s,%s,%s,%s,%s)",
                    (uuid.uuid4(), project, target, user, owner),
                )
            db.execute(
                "CREATE TABLE workspace_zulip_v5_legacy_archive(kind text,identity_key text,data jsonb,PRIMARY KEY(kind,identity_key))"
            )
            for stream, archived, source in (
                (original, scenario == "archived", "native"),
                (carrier, True, "zulip"),
            ):
                saved = {
                    "uuid": str(stream),
                    "project_id": str(saved_project),
                    "source_name": source,
                    "private": scenario != "channel",
                    "direct_user_uuid": str(peer),
                    "is_archived": archived,
                    "updated_at": "2026-01-02T00:00:00",
                }
                mapping = {
                    "kind": "stream",
                    "old_uuid": str(stream),
                    "new_uuid": str(target),
                    "project_id": str(saved_project),
                }
                for kind, value in (("stream", saved), ("mapping", mapping)):
                    db.execute(
                        "INSERT INTO workspace_zulip_v5_legacy_archive VALUES(%s,%s,%s::jsonb)",
                        (kind, str(stream), json.dumps(value)),
                    )
            refs = [original, carrier] if scenario == "conflicting" else [original]
            for stream in refs:
                chat = {
                    "project_id": str(saved_project),
                    "projection_stream_uuid": str(stream),
                    "selected": scenario != "unselected",
                }
                db.execute(
                    "INSERT INTO workspace_zulip_v5_legacy_archive VALUES('chat',%s,%s::jsonb)",
                    (str(stream), json.dumps(chat)),
                )
        engine = migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR))
        engine.apply_migration(REPAIR)
        response = conftest.ApiClient(http_server, owner, project).get(
            "/v1/messenger/streams/" + str(target)
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_archived"] is expected
        with psycopg.connect(url) as db:
            outbox_count = db.execute(
                "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
                (project,),
            ).fetchone()[0]
            assert outbox_count == (0 if expected else 2)
        if not expected:
            for _ in range(10):
                with contexts.Context().session_manager() as session:
                    processed = v2_projection.drain_projection_queue(
                        session, "cassi-archive-repair-test"
                    )
                if not processed:
                    break
            for user in (owner, peer):
                events = conftest.ApiClient(http_server, user, project).get(
                    "/v1/events/"
                )
                assert events.status_code == 200, events.text
                snapshots = [
                    event["payload"]
                    for event in events.json()
                    if event["payload"]["kind"] == "stream.updated"
                    and event["payload"]["uuid"] == str(target)
                ]
                assert len(snapshots) == 1
                assert snapshots[0]["is_archived"] is False
                assert any(
                    event["payload"]["kind"] == "folder.updated"
                    for event in events.json()
                )
            with psycopg.connect(url, autocommit=True) as db:
                before = db.execute(
                    "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
                    (project,),
                ).fetchone()[0]
                db.execute(
                    "DELETE FROM ra_migrations WHERE uuid='079b287d-436b-4208-81c0-ec315f5fb595'"
                )
            engine.apply_migration(REPAIR)
            with psycopg.connect(url) as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
                        (project,),
                    ).fetchone()[0]
                    == before
                )
