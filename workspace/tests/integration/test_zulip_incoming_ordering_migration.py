# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import concurrent.futures
import importlib.util
import json
import queue
import time
import uuid

import psycopg
from psycopg import rows
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip import incoming_operations
from workspace.messenger_api.dm import external_models
from workspace.services import zulip_incoming
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming
LEGACY_HEAD = "0265-Replace-outbound-Zulip-tasks-with-SQL-intents-9cafda.py"
PUBLISHED_HEAD = (
    "0275-Qualify-immutable-content-digest-functions-for-backup-restore-67692f.py"
)
REPAIR = "0279-Restore-migrated-Zulip-inbox-message-ordering-keys-7f3b75.py"


def migration(number):
    path = next(conftest.MIGRATIONS_DIR.glob(f"{number:04}-*.py"))
    spec = importlib.util.spec_from_file_location("inbox_order_repair", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.migration_step


def test_published_cutover_repair_reconstructs_keys_without_changing_durable_events():
    payloads = [
        {"type": "message", "message": {"id": 501}},
        {"type": "update_message", "message_id": 501},
        {"type": "delete_message", "message_ids": [501, 502, 501]},
        {"type": "reaction", "message_id": 501},
        {"type": "update_message_flags", "messages": [502, 501, 502]},
        {"type": "update_message_flags", "messages": [None, 501, None]},
        {"type": "update_message_flags", "messages": [], "all": False},
        {"type": "update_message_flags", "messages": [], "all": True},
        {"type": "update_message_flags", "messages": [501], "all": True},
        {"type": "update_message", "message_ids": [501], "message_id": 502},
        {"type": "delete_message", "message_ids": [], "messages": [501]},
        {"type": "reaction", "message_id": "501"},
        {"type": "subscription", "op": "add", "subscriptions": []},
    ]
    project = uuid.uuid4()
    with (
        conftest.isolated_migration_database(LEGACY_HEAD) as url,
        psycopg.connect(url, autocommit=True, row_factory=rows.dict_row) as db,
    ):
        for index, (project_id, hostname) in enumerate(
            [
                (project, "first.example.test"),
                (project, "second.example.test"),
                (uuid.uuid4(), "first.example.test"),
            ]
        ):
            owner = uuid.uuid4()
            conftest.seed_workspace_user(db, owner, f"ordering-{owner}")
            account = external_models.ExternalAccount(
                project_id=project_id,
                owner_user_uuid=owner,
                provider="zulip",
                external_account_id="7",
                status="active",
                disabled=True,
                settings={
                    "server_url": f"https://{hostname}",
                    "api_key": "test-key",
                },
            )
            account.insert()
            queue, generation = f"cassi-legacy-order-{index}", uuid.uuid4()
            db.execute(
                "INSERT INTO bridge_zulip_v5_subscriptions "
                "(uuid,project_id,external_account_uuid,generation,queue_id,"
                "last_event_id,account_revision,longpoll_timeout) "
                "VALUES (%s,%s,%s,%s,%s,99,'retained',90)",
                (uuid.uuid4(), project_id, account.uuid, generation, queue),
            )
            for event_id, payload in enumerate([*payloads, payloads[0]], 1):
                db.execute(
                    "INSERT INTO bridge_zulip_v5_events "
                    "(uuid,project_id,external_account_uuid,generation,queue_id,"
                    "event_id,source,payload,processed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'zulip',%s::jsonb,"
                    "CASE WHEN %s THEN NOW() AT TIME ZONE 'UTC' END)",
                    (
                        uuid.uuid4(),
                        project_id,
                        account.uuid,
                        generation,
                        queue,
                        event_id,
                        json.dumps(payload),
                        event_id > len(payloads),
                    ),
                )
        engine = migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR))
        engine.apply_migration(PUBLISHED_HEAD)
        assert (
            db.execute(
                "SELECT count(*) AS n FROM zulip_incoming_events "
                "WHERE payload->>'type'='message' AND processed_at IS NULL "
                "AND ordering_keys='{}'"
            ).fetchone()["n"]
            == 3
        )
        db.execute(
            "UPDATE zulip_incoming_events SET attempts=3,error='retained retry',"
            "next_retry_at=NOW()+interval '1 hour' WHERE processed_at IS NULL"
        )
        # A current-admission row must not be rewritten unnecessarily.
        db.execute(
            "UPDATE zulip_incoming_events SET ordering_keys=ARRAY['message:501'] "
            "WHERE queue_id='cassi-legacy-order-0' AND event_id=2"
        )
        before = db.execute(
            "SELECT id,to_jsonb(event)-'ordering_keys' AS stable,ordering_keys,"
            "xmin::text AS revision FROM zulip_incoming_events event ORDER BY id"
        ).fetchall()
        subscriptions = db.execute(
            "SELECT to_jsonb(subscription) AS value FROM zulip_incoming_subscriptions "
            "subscription ORDER BY external_account_uuid"
        ).fetchall()
        engine.apply_migration(REPAIR)
        after = db.execute(
            "SELECT id,to_jsonb(event)-'ordering_keys' AS stable,ordering_keys,"
            "xmin::text AS revision FROM zulip_incoming_events event ORDER BY id"
        ).fetchall()
        for old, repaired in zip(before, after, strict=True):
            assert repaired["stable"] == old["stable"]
            event = old["stable"]
            expected = (
                zulip_incoming.ZulipIncomingService.ordering_keys(
                    {"uuid": event["external_account_uuid"]}, event["payload"]
                )
                if event["processed_at"] is None
                and event["payload"]["type"] != "subscription"
                else old["ordering_keys"]
            )
            assert repaired["ordering_keys"] == expected
            if expected == old["ordering_keys"]:
                assert repaired["revision"] == old["revision"]
        with db.transaction():
            migration(279).upgrade(db)
        assert (
            db.execute(
                "SELECT id,to_jsonb(event)-'ordering_keys' AS stable,ordering_keys,"
                "xmin::text AS revision FROM zulip_incoming_events event ORDER BY id"
            ).fetchall()
            == after
        )
        assert (
            db.execute(
                "SELECT to_jsonb(subscription) AS value FROM zulip_incoming_subscriptions "
                "subscription ORDER BY external_account_uuid"
            ).fetchall()
            == subscriptions
        )
        # A retry blocks only its own provider and project, even when IDs match.
        db.execute(
            "UPDATE zulip_incoming_events SET processed_at=NOW() WHERE event_id>2"
        )
        db.execute(
            "UPDATE zulip_incoming_events SET next_retry_at=NOW() "
            "WHERE event_id=2 OR queue_id<>'cassi-legacy-order-0'"
        )
        eligible = db.execute(zulip_incoming.SELECT_EVENT_SQL, ([], 10)).fetchall()
        assert {(row["queue_id"], row["event_id"]) for row in eligible} == {
            ("cassi-legacy-order-1", 1),
            ("cassi-legacy-order-2", 1),
        }


def test_repaired_create_retry_keeps_edits_and_deletes_ordered(incoming):
    api, db, account, http, url = incoming
    incoming_tests.apply(incoming, incoming_tests.event("Existing topic", 100))
    http.request.return_value = {
        "message": incoming_tests.event("Created", 501)["message"]
    }

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.operations = incoming_operations.Operations(service.db, http)
        values = [
            {"id": 1, **incoming_tests.event("Created", 501)},
            {
                "id": 2,
                "type": "update_message",
                "message_id": 501,
                "content": "Edited",
                "edit_timestamp": 2000000000,
            },
            {"id": 3, "type": "delete_message", "message_ids": [501]},
            {"id": 4, **incoming_tests.event("Independent", 502)},
        ]
        try:
            async with service.db.transaction() as connection:
                await service.append(
                    connection, account, {"queue_id": "cassi-repair-fifo"}, values
                )
            db.execute(
                "UPDATE zulip_incoming_events SET ordering_keys=ARRAY['*'] "
                "WHERE queue_id='cassi-repair-fifo'"
            )
            with db.transaction():
                migration(270).upgrade(db)
                migration(279).upgrade(db)
            db.execute(
                zulip_incoming.RETRY_EVENT_SQL,
                (
                    "Waiting for dependency",
                    3600,
                    db.execute(
                        "SELECT id FROM zulip_incoming_events WHERE queue_id='cassi-repair-fifo' AND event_id=1"
                    ).fetchone()[0],
                ),
            )
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
            assert [row["event_id"] for row in ready] == [4]
            await service.process(ready[0])
            db.execute(
                "UPDATE zulip_incoming_events SET next_retry_at=NOW() "
                "WHERE queue_id='cassi-repair-fifo' AND event_id=1"
            )
            for expected in (1, 2, 3):
                ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
                assert [row["event_id"] for row in ready] == [expected]
                await service.process(ready[0])
                if expected == 2:
                    assert db.execute(
                        "SELECT payload->>'content' FROM messenger_messages_with_content_v1 "
                        "WHERE project_id=%s AND provider_uuid=%s AND provider_identity='501'",
                        (account["project_id"], account["provider_uuid"]),
                    ).fetchone() == ("Edited",)
            assert not await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
        finally:
            await service.db.close()

    asyncio.run(run())
    assert {
        message["payload"]["content"]
        for message in api.get("/v1/messenger/messages/").json()
    } == {"Existing topic", "Independent"}
    assert db.execute(
        "SELECT event_id,error FROM zulip_incoming_events WHERE queue_id='cassi-repair-fifo' ORDER BY id"
    ).fetchall() == [(1, None), (2, None), (3, None), (4, None)]


def test_repair_does_not_rewrite_an_event_completed_while_waiting_for_its_lock(
    incoming,
):
    _api, db, account, _http, url = incoming
    event_id = db.execute(
        "INSERT INTO zulip_incoming_events "
        "(project_id,external_account_uuid,provider_uuid,owner_user_uuid,"
        "account_identity,queue_id,event_id,payload,ordering_keys) "
        "VALUES (%s,%s,%s,%s,%s,'cassi-completed-during-repair',1,%s,'{}') RETURNING id",
        (
            account["project_id"],
            account["uuid"],
            account["provider_uuid"],
            account["owner_user_uuid"],
            account["external_account_id"],
            incoming_operations.json_value(incoming_tests.event("Completed", 501)),
        ),
    ).fetchone()[0]
    backend = queue.Queue()

    def repair():
        with psycopg.connect(url, autocommit=True) as connection:
            backend.put(connection.info.backend_pid)
            migration(279).upgrade(connection)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        with db.transaction():
            db.execute(
                "SELECT 1 FROM zulip_incoming_events WHERE id=%s FOR UPDATE",
                (event_id,),
            )
            result = executor.submit(repair)
            pid = backend.get(timeout=5)
            deadline = time.monotonic() + 5
            while not db.execute(
                "SELECT %s=ANY(pg_blocking_pids(%s))", (db.info.backend_pid, pid)
            ).fetchone()[0]:
                assert time.monotonic() < deadline, (
                    "Migration did not reach the held event row"
                )
                time.sleep(0.01)
            db.execute(zulip_incoming.COMPLETE_EVENT_SQL, (event_id,))
        result.result(timeout=5)
    assert db.execute(
        "SELECT ordering_keys,processed_at IS NOT NULL FROM zulip_incoming_events WHERE id=%s",
        (event_id,),
    ).fetchone() == ([], True)
