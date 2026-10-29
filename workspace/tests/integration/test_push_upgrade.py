# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import json
import copy
import pathlib
import uuid

import pytest
from gcl_sdk import migrations as sdk_migrations
from gcl_sdk.common import utils as sdk_utils
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations

from workspace.messenger_api import push_events
from workspace.tests.integration import conftest


@pytest.mark.pre_sparse_schema
@pytest.mark.parametrize("signal_status", ["NEW", "IN_PROGRESS"])
def test_deployed_sdk_head_does_not_skip_new_master_dependencies(
    api, db, signal_status
):
    """Reproduce the real upgrade from the feature's previous lab image."""
    fixture = pathlib.Path(__file__).parents[1] / "fixtures/topic_push_events.json"
    body = json.loads(fixture.read_text())["event"]
    with contexts.Context().session_manager() as session:
        sdk = sdk_utils.MigrationEngine._get_migration_engine(sdk_migrations)
        sdk.apply_migration("0000-init-events-table-2cfd220e.py", session)
        push_events._insert(body, uuid.UUID(body["exchange"]["user_uuid"]))
        signal = copy.deepcopy(body)
        signal["uuid"] = str(uuid.uuid4())
        signal["event_params"]["devices"] = []
        signal["_topic_snapshot"] = {
            "source_uuid": str(uuid.uuid4()),
            "unread_count": 1,
            "active_unread_count": 1,
            "previous_active_count": 0,
        }
        push_events._insert(signal, uuid.UUID(body["exchange"]["user_uuid"]))
        session.execute(
            "UPDATE gcl_sdk_events SET status=%s WHERE uuid=%s",
            (signal_status, signal["uuid"]),
        )
        session.execute(
            "INSERT INTO ra_migrations(uuid,applied) VALUES (%s,true)",
            ("4408ccb7-46a5-4cd1-9101-876958bc79df",),
        )
        session.execute(
            "INSERT INTO ra_migrations(uuid,applied) VALUES (%s,true)",
            ("bbf1bb0c-978e-43b2-a747-037fd605d5fb",),
        )
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.apply_migration(engine.get_latest_migration())
    with contexts.Context().session_manager() as session:
        assert (
            session.execute(
                "SELECT to_regprocedure('messenger_queue_counters(uuid,uuid,uuid,uuid,boolean)') AS function",
                (),
            ).fetchone()["function"]
            is not None
        )
        stored = session.execute(
            "SELECT event_data->'body' AS body FROM gcl_sdk_events WHERE uuid=%s",
            (body["uuid"],),
        ).fetchone()
        assert stored["body"] == body
        converted = session.execute(
            "SELECT status,event_data FROM gcl_sdk_events WHERE uuid=%s",
            (signal["uuid"],),
        ).fetchone()
        assert converted["status"] == signal_status
        assert converted["event_data"] == {
            "kind": "WorkspaceTopicSignal",
            "body": {
                "project_id": signal["project_id"],
                "user_uuid": signal["exchange"]["user_uuid"],
                "stream_uuid": signal["event_params"]["stream_uuid"],
                "topic_uuid": signal["event_params"]["topic_uuid"],
                "source_uuid": signal["_topic_snapshot"]["source_uuid"],
                "previous_active_count": 0,
            },
        }
        resumed = push_events.fetch_events(
            ("NEW", "IN_PROGRESS"), "WorkspaceTopicSignal"
        )
        assert [str(event.uuid) for event in resumed] == [signal["uuid"]]
        assert session.execute(
            "SELECT to_regclass('zulip_history_batches') AS batches", ()
        ).fetchone()["batches"]

    response = api.post(
        "/v1/messenger/streams/", json={"name": "SDK upgrade regression"}
    )
    assert response.status_code == 201, response.text
    topic = response.json()["default_topic_uuid"]
    changed = api.post(
        f"/v1/messenger/stream_topics/{topic}/actions/notifications/invoke",
        json={"notification_mode": "default"},
    )
    assert changed.status_code == 200, changed.text
