# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import datetime
import json
import socket
import time
import uuid
from unittest import mock

import pytest
import websockets

from workspace.bridges.zulip import incoming_operations
from workspace.messenger_api import websocket_service
from workspace.services import zulip_incoming
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming


def seed_notification_bot(incoming):
    incoming_tests.apply(
        incoming,
        {
            "type": "realm_user",
            "op": "add",
            "person": {
                "user_id": 6,
                "full_name": "Notification Bot",
                "email": "notification-bot@zulip.com",
                "is_bot": True,
            },
        },
    )


@pytest.mark.parametrize("precreate", ["before_admission", "after_admission"])
@pytest.mark.parametrize("subjects", [("B", "C", "D"), ("✔ A", "A", "✔ A", "A")])
def test_repeated_rename_keeps_history_with_delayed_personal_events_and_notices(
    incoming, precreate, subjects
):
    api, db, account, http, url = incoming
    seed_notification_bot(incoming)
    for identifier in (101, 102, 103):
        message = incoming_tests.event(f"Original {identifier}", identifier)
        message["message"].update(subject="A", timestamp=1000)
        incoming_tests.apply(incoming, message)
    source = db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:A'",
        (account["project_id"],),
    ).fetchone()[0]
    original = db.execute(
        "SELECT uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s ORDER BY uuid",
        (account["project_id"],),
    ).fetchall()

    async def run():
        previous = "A"
        for index, subject in enumerate(subjects, 1):
            # Recreate the worker/registry between transitions, as on an upgrade.
            service = zulip_incoming.ZulipIncomingService(url)
            await service.db.open()
            service.http = http
            service.operations = incoming_operations.Operations(service.db, http)
            personal = {
                "type": "user_topic",
                "stream_id": 42,
                "topic_name": subject,
                "visibility_policy": 3,
            }
            try:
                if precreate == "before_admission":
                    await asyncio.to_thread(incoming_tests.apply, incoming, personal)
                async with service.admission() as (connection, stage):
                    await service.append(
                        connection,
                        account,
                        {"queue_id": "repeated"},
                        [
                            {
                                "id": index,
                                **rename(
                                    subject,
                                    orig_subject=previous,
                                    message_ids=[101, 102, 103],
                                    edit_timestamp=2000 + index,
                                ),
                            }
                        ],
                        stage,
                    )
                if precreate == "after_admission":
                    await asyncio.to_thread(incoming_tests.apply, incoming, personal)
                # Zulip also sends a fresh automatic topic move notification.
                notice = incoming_tests.event("Topic moved", 200 + index)
                notice["message"].update(
                    subject=subject, timestamp=2000 + index, sender_id=6
                )
                await asyncio.to_thread(incoming_tests.apply, incoming, notice)
                await incoming_tests.drain_incoming(service)
                await asyncio.to_thread(incoming_tests.publish)
                current = api.get(f"/v1/messenger/stream_topics/{source}")
                assert current.status_code == 200, current.text
                assert (current.json()["name"], current.json()["is_done"]) == (
                    subject.removeprefix("✔ "),
                    subject.startswith("✔ "),
                )
                assert db.execute(
                    "SELECT notification_mode FROM messenger_user_topic_bindings "
                    "WHERE project_id=%s AND topic_uuid=%s AND user_uuid=%s",
                    (account["project_id"], source, account["owner_user_uuid"]),
                ).fetchone() == ("follow",)
                assert (
                    db.execute(
                        "SELECT uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s "
                        "AND message_uuid=ANY(%s) ORDER BY uuid",
                        (account["project_id"], source, [row[1] for row in original]),
                    ).fetchall()
                    == original
                )
                # An old personal preference can arrive after the rename. It
                # must not change the identity/name of the topic with history.
                await asyncio.to_thread(
                    incoming_tests.apply,
                    incoming,
                    {
                        **personal,
                        "topic_name": previous,
                        "visibility_policy": 0,
                    },
                )
                current = api.get(f"/v1/messenger/stream_topics/{source}").json()
                assert (current["name"], current["is_done"]) == (
                    subject.removeprefix("✔ "),
                    subject.startswith("✔ "),
                )
                assert not db.execute(
                    "SELECT provider_identity FROM messenger_topics WHERE project_id=%s "
                    "GROUP BY provider_uuid,stream_uuid,provider_identity HAVING count(*)>1",
                    (account["project_id"],),
                ).fetchall()
                previous = subject
            finally:
                await service.db.close()

    asyncio.run(run())


@pytest.mark.parametrize("duplicate", ["source", "destination"])
def test_rename_selects_history_instead_of_empty_duplicate_identity(
    incoming, duplicate
):
    api, db, account, _http, _url = incoming
    source = seed_messages(incoming, size=3)
    # The old implementation can leave two rows with the same provider name.
    db.execute(
        "INSERT INTO messenger_topics SELECT (jsonb_populate_record(NULL::messenger_topics, "
        "to_jsonb(topic)||jsonb_build_object('uuid',%s::text,'provider_identity',%s::text))).* "
        "FROM messenger_topics topic WHERE project_id=%s AND uuid=%s",
        (
            str(uuid.UUID(int=1)),
            "42:General" if duplicate == "source" else "42:Renamed",
            account["project_id"],
            source,
        ),
    )
    incoming_tests.apply(incoming, rename(message_ids=[101, 102, 103]))
    assert api.get(f"/v1/messenger/stream_topics/{source}").json()["name"] == "Renamed"
    assert db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:Renamed'",
        (account["project_id"],),
    ).fetchall() == [(source,)]


@pytest.fixture
def stale_statistics(incoming):
    _api, db, *_ = incoming
    tables = (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
    )
    try:
        # Keep the tiny pre-import estimates throughout a 10,000-message rename.
        for table in tables:
            db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
            db.execute(f"ANALYZE {table}")
        yield
    finally:
        for table in tables:
            db.execute(f"ALTER TABLE {table} RESET (autovacuum_enabled)")


def test_delayed_rename_batches_reconcile_precreated_aliases(incoming):
    api, db, account, http, url = incoming
    seed_notification_bot(incoming)
    initial = incoming_tests.event()
    initial["message"].update(subject="A", timestamp=1000)
    incoming_tests.apply(incoming, initial)
    source = db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:A'",
        (account["project_id"],),
    ).fetchone()[0]

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            previous = "A"
            for index, subject in enumerate(("B", "C", "D"), 1):
                await asyncio.to_thread(
                    incoming_tests.apply,
                    incoming,
                    {
                        "type": "user_topic",
                        "stream_id": 42,
                        "topic_name": subject,
                        "visibility_policy": 3,
                    },
                )
                notice = incoming_tests.event("Move notice", 200 + index)
                notice["message"].update(
                    subject=subject, timestamp=2000 + index, sender_id=6
                )
                await asyncio.to_thread(incoming_tests.apply, incoming, notice)
                async with service.admission() as (connection, stage):
                    await service.append(
                        connection,
                        account,
                        {"queue_id": "delayed"},
                        [
                            {
                                "id": index,
                                **rename(
                                    subject,
                                    orig_subject=previous,
                                    message_ids=[101, *range(201, 200 + index)],
                                    edit_timestamp=2000 + index,
                                ),
                            }
                        ],
                        stage,
                    )
                previous = subject
            # All batches are admitted before processing, with the same worker
            # ordering and publication path as live provider delivery.
            for subject in ("B", "C", "D"):
                ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 128))
                assert len(ready) == 1
                await service.process(ready[0])
                await asyncio.to_thread(incoming_tests.publish)
                assert (
                    api.get(f"/v1/messenger/stream_topics/{source}").json()["name"]
                    == subject
                )
                assert db.execute(
                    "SELECT p.topic_uuid FROM messenger_message_placements p JOIN messenger_messages m "
                    "ON m.project_id=p.project_id AND m.uuid=p.message_uuid "
                    "WHERE m.project_id=%s AND m.provider_identity='101'",
                    (account["project_id"],),
                ).fetchone() == (source,)
            assert not db.execute(
                "SELECT 1 FROM zulip_incoming_events WHERE processed_at IS NULL"
            ).fetchall()
        finally:
            await service.db.close()

    asyncio.run(run())


def rename(subject="Renamed", **changes):
    return {
        "type": "update_message",
        "message_id": 101,
        "message_ids": list(range(101, 10101)),
        "stream_id": 42,
        "orig_subject": "General",
        "subject": subject,
        "propagate_mode": "change_all",
        "edit_timestamp": 2000000000,
        **changes,
    }


def seed_messages(incoming, size=10000):
    _api, db, account, *_ = incoming
    incoming_tests.apply(incoming, incoming_tests.event())
    # Reuse the real import's contents, placement and personal state. Unknown
    # provider history in the rename array must remain unmaterialized.
    for table, replacements in (
        (
            "messenger_messages",
            "'uuid',messenger_uuid_v5(m.uuid,n::text),'provider_identity',n::text",
        ),
        (
            "messenger_message_placements",
            "'uuid',messenger_uuid_v5(m.uuid,n::text),'message_uuid',messenger_uuid_v5(m.message_uuid,n::text)",
        ),
        (
            "messenger_user_message_bindings",
            "'uuid',messenger_uuid_v5(m.uuid,n::text),'placement_uuid',messenger_uuid_v5(m.placement_uuid,n::text)",
        ),
    ):
        db.execute(
            f"INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table}, "
            f"to_jsonb(m)||jsonb_build_object({replacements}))).* "
            f"FROM {table} m CROSS JOIN generate_series(102,%s) n "
            "WHERE m.project_id=%s",
            (100 + size, account["project_id"]),
        )
    return db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:General'",
        (account["project_id"],),
    ).fetchone()[0]


def fingerprint(db, project, excluded=()):
    return db.execute(
        "SELECT md5(string_agg(row_to_json(m)::text,'' ORDER BY m.uuid)) "
        "FROM messenger_messages m WHERE project_id=%s AND NOT COALESCE(provider_identity=ANY(%s::text[]),false)",
        (project, list(excluded)),
    ).fetchone()[0]


@pytest.mark.parametrize("subject,done", [("Renamed", False), ("✔ General", True)])
def test_large_rename_is_metadata_only_and_visible_within_one_second(
    incoming, subject, done, record_property, stale_statistics
):
    api, db, account, http, _url = incoming
    topic = seed_messages(incoming)
    before = fingerprint(db, account["project_id"])
    placements_before = db.execute(
        "SELECT md5(string_agg(row_to_json(p)::text,'' ORDER BY uuid)) FROM messenger_message_placements p WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone()
    http.request.reset_mock()
    http.request.side_effect = AssertionError(
        "A subject-only change must not fetch message bodies"
    )
    started = time.monotonic()
    incoming_tests.apply(incoming, rename(subject))
    response = api.get(f"/v1/messenger/stream_topics/{topic}")
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.text
    assert (response.json()["name"], response.json()["is_done"]) == (
        subject.removeprefix("✔ "),
        done,
    )
    record_property("rename_to_http_seconds", round(elapsed, 4))
    assert elapsed < 1, elapsed
    http.request.assert_not_awaited()
    assert fingerprint(db, account["project_id"]) == before
    assert (
        db.execute(
            "SELECT md5(string_agg(row_to_json(p)::text,'' ORDER BY uuid)) FROM messenger_message_placements p WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()
        == placements_before
    )
    assert db.execute(
        "SELECT count(DISTINCT topic_uuid) FROM messenger_message_placements WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (1,)
    assert any(
        e["kind"] == "topic.updated" for e in incoming_tests.client_events(incoming)
    )
    if done:
        started = time.monotonic()
        incoming_tests.apply(
            incoming, rename("General", orig_subject=subject, edit_timestamp=2000000001)
        )
        reopened = api.get(f"/v1/messenger/stream_topics/{topic}").json()
        record_property("reopen_to_http_seconds", round(time.monotonic() - started, 4))
        assert time.monotonic() - started < 1
        assert reopened["name"] == "General" and reopened["is_done"] is False
        assert fingerprint(db, account["project_id"]) == before


def test_rename_ignores_unloaded_history_and_keeps_personal_flags(incoming):
    api, db, account, http, _url = incoming
    incoming_tests.apply(incoming, incoming_tests.event())
    incoming_tests.apply(
        incoming,
        {
            "type": "update_message_flags",
            "messages": [101],
            "flag": "starred",
            "op": "add",
        },
    )
    http.request.side_effect = AssertionError(
        "Unloaded history is not a rename dependency"
    )
    incoming_tests.apply(incoming, rename())
    messages = api.get("/v1/messenger/messages/").json()
    assert len(messages) == 1
    assert messages[0]["payload"]["content"] == "Hello"
    assert messages[0]["starred"]
    assert db.execute(
        "SELECT provider_identity FROM messenger_user_topic_bindings WHERE project_id=%s AND provider_identity<>'42::7'",
        (account["project_id"],),
    ).fetchall() == [("42:Renamed:7",)]


def test_shared_rename_copies_restart_and_rename_back(incoming, record_property):
    api, db, account, http, url = incoming
    peers = [
        incoming_tests.peer_account(incoming, identifier) for identifier in range(8, 30)
    ]
    for _linked, peer in peers:
        incoming_tests.apply(
            peer,
            {
                "type": "subscription",
                "op": "add",
                "subscriptions": [{"stream_id": 42, "name": "Channel"}],
            },
        )
    incoming_tests.apply(incoming, incoming_tests.event())
    http.request.side_effect = AssertionError("Shared renames do not reload messages")

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            for index, owner in enumerate(
                [account, *[peer[2] for _linked, peer in peers]]
            ):
                async with service.admission() as (connection, stage):
                    await service.append(
                        connection,
                        owner,
                        {"queue_id": "rename"},
                        [
                            {
                                "id": 1,
                                **rename(),
                                **(
                                    {"flags": ["read"]}
                                    if index == 0
                                    else {"flags": ["starred"]}
                                    if index == 1
                                    else {}
                                ),
                            }
                        ],
                        stage,
                    )
            pending = await service.db.sql(
                "SELECT * FROM zulip_incoming_events WHERE processed_at IS NULL ORDER BY id"
            )
            assert len(pending) == 23
            assert sum(bool(e["payload"].get("_personal_only")) for e in pending) == 22
            restored = zulip_incoming.incoming_registry.Registry()
            restored.restore(pending)
            service.registry = restored
            with mock.patch.object(
                service.operations,
                "rename_topic",
                wraps=service.operations.rename_topic,
            ) as shared:
                started = time.monotonic()
                selected = await service.db.sql(
                    zulip_incoming.SELECT_EVENT_SQL, ([], 128)
                )
                assert len(selected) == 1
                await service.process(selected[0])
                incoming_tests.publish()
                topic = db.execute(
                    "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:Renamed'",
                    (account["project_id"],),
                ).fetchone()[0]
                assert (
                    api.get(f"/v1/messenger/stream_topics/{topic}").json()["name"]
                    == "Renamed"
                )
                record_property(
                    "23_copy_first_visible_seconds",
                    round(time.monotonic() - started, 4),
                )
                assert time.monotonic() - started < 1
                await incoming_tests.drain_incoming(service)
                assert shared.await_count == 1
            # A later occurrence with identical names is still a new change.
            for index, old, new in (
                (2, "Renamed", "General"),
                (3, "General", "Renamed"),
            ):
                async with service.admission() as (connection, stage):
                    await service.append(
                        connection,
                        account,
                        {"queue_id": "rename"},
                        [
                            {
                                "id": index,
                                **rename(
                                    new,
                                    orig_subject=old,
                                    edit_timestamp=2000000000 + index,
                                ),
                            }
                        ],
                        stage,
                    )
                await incoming_tests.drain_incoming(service)
        finally:
            await service.db.close()

    try:
        asyncio.run(run())
        assert db.execute(
            "SELECT name FROM messenger_topics WHERE project_id=%s AND provider_identity<>'42:'",
            (account["project_id"],),
        ).fetchall() == [("Renamed",)]
        states = db.execute(
            "SELECT user_uuid,read,starred FROM messenger_api_user_messages_v1 WHERE project_id=%s",
            (account["project_id"],),
        ).fetchall()
        assert (account["owner_user_uuid"], True, False) in states
        assert (peers[0][1][2]["owner_user_uuid"], False, True) in states
    finally:
        for linked, _peer in peers:
            linked.delete()


def test_merge_commits_bounded_batches_keeps_realtime_and_resumes_after_restart(
    incoming, record_property
):
    api, db, account, http, url = incoming
    source = seed_messages(incoming, size=250)
    destination = incoming_tests.event("Destination message", 50001)
    destination["message"]["subject"] = "Destination"
    incoming_tests.apply(incoming, destination)
    incoming_tests.apply(
        incoming,
        {
            "type": "subscription",
            "op": "add",
            "subscriptions": [{"stream_id": 43, "name": "Other channel"}],
        },
    )
    initial = incoming_tests.event("Other topic seed", 50002)
    initial["message"].update(stream_id=43, subject="Other")
    incoming_tests.apply(incoming, initial)
    http.request.reset_mock()
    http.request.side_effect = AssertionError(
        "Topic merges must not fetch unchanged message bodies"
    )
    original = fingerprint(db, account["project_id"])
    target = db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:Destination'",
        (account["project_id"],),
    ).fetchone()[0]
    bulk = rename("Destination", message_ids=list(range(101, 351)))
    fresh = incoming_tests.event("Unrelated realtime marker", 60001)
    fresh["message"].update(stream_id=43, subject="Other")
    batches = []

    async def process(service, row):
        before = time.monotonic()
        await service.process(row)
        batches.append(time.monotonic() - before)

    async def run():
        first = zulip_incoming.ZulipIncomingService(url)
        await first.db.open()
        first.http = http
        first.operations = incoming_operations.Operations(first.db, http)
        try:
            async with first.admission() as (connection, stage):
                await first.append(
                    connection,
                    account,
                    {"queue_id": "bounded-merge"},
                    [{"id": 1, **bulk}, {"id": 2, **fresh}],
                    stage,
                )
            pending = await first.db.sql(
                "SELECT * FROM zulip_incoming_events WHERE processed_at IS NULL ORDER BY id"
            )
            await process(first, pending[0])
            assert db.execute(
                "SELECT count(*) FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s",
                (account["project_id"], source),
            ).fetchone() == (150,)
            assert db.execute(
                "SELECT processed_at FROM zulip_incoming_events WHERE id=%s",
                (pending[0]["id"],),
            ).fetchone() == (None,)
            started = time.monotonic()
            await first.process(pending[1])
            incoming_tests.publish()
            assert any(
                m["payload"]["content"] == "Unrelated realtime marker"
                for m in api.get("/v1/messenger/messages/").json()
            )
            record_property(
                "unrelated_message_to_http_seconds",
                round(time.monotonic() - started, 4),
            )
            assert time.monotonic() - started < 1
            assert not db.execute(
                "SELECT processed_at FROM zulip_incoming_events WHERE id=%s",
                (pending[0]["id"],),
            ).fetchone()[0]
        finally:
            await first.db.close()
        restarted = zulip_incoming.ZulipIncomingService(url)
        await restarted.db.open()
        restarted.http = http
        restarted.operations = incoming_operations.Operations(restarted.db, http)
        try:
            pending = await restarted.db.sql(
                "SELECT * FROM zulip_incoming_events WHERE processed_at IS NULL ORDER BY id"
            )
            restarted.registry.restore(pending)
            await incoming_tests.drain_incoming(restarted)
        finally:
            await restarted.db.close()

    asyncio.run(run())
    incoming_tests.drain()
    assert max(batches) < 1, batches
    assert not db.execute(
        "SELECT 1 FROM messenger_topics WHERE project_id=%s AND uuid=%s",
        (account["project_id"], source),
    ).fetchall()
    assert db.execute(
        "SELECT count(*) FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s",
        (account["project_id"], target),
    ).fetchone() == (251,)
    assert fingerprint(db, account["project_id"], excluded=["60001"]) == original
    assert db.execute(
        "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (253,)
    http.request.assert_not_awaited()
    moved = [
        e
        for e in incoming_tests.client_events(incoming)
        if e["kind"] == "message.updated" and e["topic_uuid"] == str(target)
    ]
    assert len(moved) == 250


def test_shared_rename_recovers_after_primary_account_is_disabled(incoming):
    _api, db, account, http, url = incoming
    incoming_tests.apply(incoming, incoming_tests.event())
    linked, peer = incoming_tests.peer_account(incoming)
    incoming_tests.apply(peer, incoming_tests.event())
    http.request.side_effect = AssertionError("Recovery must retain the metadata event")

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            for owner in (account, peer[2]):
                async with service.admission() as (connection, stage):
                    await service.append(
                        connection,
                        owner,
                        {"queue_id": "failed-primary"},
                        [{"id": 1, **rename(message_ids=[101])}],
                        stage,
                    )
            db.execute(
                "UPDATE m_external_accounts_v2 SET disabled=true WHERE uuid=%s",
                (account["uuid"],),
            )
            await incoming_tests.drain_incoming(service)
        finally:
            await service.db.close()
            db.execute(
                "UPDATE m_external_accounts_v2 SET disabled=false WHERE uuid=%s",
                (account["uuid"],),
            )

    try:
        asyncio.run(run())
        assert db.execute(
            "SELECT name FROM messenger_topics WHERE project_id=%s AND provider_identity='42:Renamed'",
            (account["project_id"],),
        ).fetchone() == ("Renamed",)
    finally:
        linked.delete()


@pytest.mark.parametrize("subject", ["Renamed", "✔ General"])
def test_fresh_destination_keeps_history_identity_and_reaches_websocket_within_one_second(
    incoming, record_property, subject, stale_statistics
):
    _api, db, account, http, url = incoming
    source = seed_messages(incoming)
    edit_time = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        async with service.admission() as (connection, stage):
            await service.append(
                connection,
                account,
                {"queue_id": "fresh-destination"},
                [{"id": 1, **rename(subject, edit_timestamp=edit_time)}],
                stage,
            )
        # This independent fresh message can be imported before the bulk rename.
        fresh = incoming_tests.event("After the rename", 60001)
        fresh["message"].update(subject=subject, timestamp=edit_time + 1)
        await asyncio.to_thread(incoming_tests.apply, incoming, fresh)
        original = fingerprint(db, account["project_id"])
        http.request.reset_mock()
        http.request.side_effect = AssertionError("A rename cannot reload old messages")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = websocket_service.MessengerEventsWebsocketServer(
            db_url=url,
            iam_engine_driver=mock.Mock(),
            heartbeat_interval=25,
            client_timeout=5,
            catchup_limit=500,
            send_queue_limit=32,
            poll_interval=0.01,
        )
        server._authenticator.authenticate = lambda token: (
            account["owner_user_uuid"],
            account["project_id"],
        )
        task = asyncio.create_task(server.serve("127.0.0.1", port))
        client = None
        try:
            for _ in range(100):
                try:
                    client = await websockets.connect(
                        f"ws://127.0.0.1:{port}/v1/events/ws",
                        subprotocols=["workspace.events.v1", "bearer.test"],
                    )
                    break
                except OSError:
                    await asyncio.sleep(0.01)
            assert client is not None
            while json.loads(await client.recv()).get("type") != "ready":
                pass
            started = time.monotonic()
            pending = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 128))
            selected_at = time.monotonic()
            await service.process(pending[0])
            applied_at = time.monotonic()
            await asyncio.to_thread(incoming_tests.publish)
            published_at = time.monotonic()
            async with asyncio.timeout(1):
                while True:
                    frame = json.loads(await client.recv())
                    if (
                        frame.get("object_type") == "topic"
                        and frame.get("action") == "updated"
                        and frame["payload"]["uuid"] == str(source)
                        and frame["payload"]["name"] == subject.removeprefix("✔ ")
                        and frame["payload"]["is_done"] == subject.startswith("✔ ")
                    ):
                        break
            elapsed = time.monotonic() - started
            record_property("durable_inbox_to_websocket_seconds", round(elapsed, 4))
            record_property("select_seconds", round(selected_at - started, 4))
            record_property("apply_seconds", round(applied_at - selected_at, 4))
            record_property("publish_seconds", round(published_at - applied_at, 4))
            record_property(
                "websocket_seconds", round(time.monotonic() - published_at, 4)
            )
            assert elapsed < 1, elapsed
            assert not db.execute(
                "SELECT 1 FROM zulip_incoming_events WHERE processed_at IS NULL"
            ).fetchall()
            assert db.execute(
                "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity=%s",
                (account["project_id"], f"42:{subject}"),
            ).fetchone() == (source,)
            assert db.execute(
                "SELECT count(*) FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s",
                (account["project_id"], source),
            ).fetchone() == (10001,)
            for table in (
                "messenger_stream_last_messages",
                "messenger_topic_last_messages",
            ):
                assert db.execute(
                    f"SELECT message.provider_identity FROM {table} last "
                    "JOIN messenger_message_placements placement ON placement.project_id=last.project_id AND placement.uuid=last.placement_uuid "
                    "JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid "
                    "WHERE last.project_id=%s",
                    (account["project_id"],),
                ).fetchall() == [("60001",)]
            assert fingerprint(db, account["project_id"]) == original
            http.request.assert_not_awaited()
        finally:
            if client is not None:
                await client.close()
            server.stop()
            await task
            await service.db.close()

    asyncio.run(run())


def test_conflicting_content_edit_waits_for_every_merge_batch(incoming):
    api, db, account, http, url = incoming
    seed_messages(incoming, size=150)
    destination = incoming_tests.event("Destination", 50001)
    destination["message"]["subject"] = "Destination"
    incoming_tests.apply(incoming, destination)
    http.request.reset_mock()
    http.request.return_value = {
        "message": {
            **incoming_tests.event()["message"],
            "subject": "Destination",
            "content": "After merge",
            "last_edit_timestamp": 2000000001,
        }
    }

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            async with service.admission() as (connection, stage):
                await service.append(
                    connection,
                    account,
                    {"queue_id": "conflicting-edit"},
                    [
                        {
                            "id": 1,
                            **rename("Destination", message_ids=list(range(101, 251))),
                        },
                        {
                            "id": 2,
                            "type": "update_message",
                            "message_id": 101,
                            "edit_timestamp": 2000000001,
                            "content": "After merge",
                        },
                    ],
                    stage,
                )
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 128))
            assert [row["event_id"] for row in ready] == [1]
            await service.process(ready[0])
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 128))
            assert [row["event_id"] for row in ready] == [1]
            assert http.request.await_count == 0
            await incoming_tests.drain_incoming(service)
        finally:
            await service.db.close()

    asyncio.run(run())
    http.request.assert_awaited_once()
    placement = db.execute(
        "SELECT placement.uuid FROM messenger_message_placements placement JOIN messenger_messages message "
        "ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid "
        "WHERE message.project_id=%s AND message.provider_identity='101'",
        (account["project_id"],),
    ).fetchone()[0]
    response = api.get(f"/v1/messenger/messages/{placement}")
    assert response.status_code == 200, response.text
    assert response.json()["payload"]["content"] == "After merge"


def test_rename_into_default_topic_never_removes_its_identity(incoming):
    _api, db, account, http, url = incoming
    incoming_tests.apply(incoming, incoming_tests.event())
    default = db.execute(
        "SELECT default_topic_uuid FROM messenger_streams WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone()[0]
    http.request.side_effect = AssertionError(
        "Renaming into the default topic is metadata work"
    )

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            async with service.admission() as (connection, stage):
                await service.append(
                    connection,
                    account,
                    {"queue_id": "default-destination"},
                    [
                        {
                            "id": 1,
                            **rename(
                                "",
                                message_ids=[101],
                                edit_timestamp=int(time.time()) - 60,
                            ),
                        }
                    ],
                    stage,
                )
            # A delayed event can predate local creation of the default topic.
            await service.db.sql(
                "UPDATE zulip_incoming_events SET received_at=now()-interval '1 minute'"
            )
            await incoming_tests.drain_incoming(service)
        finally:
            await service.db.close()

    asyncio.run(run())
    assert db.execute(
        "SELECT uuid,name FROM messenger_topics WHERE project_id=%s AND provider_identity='42:'",
        (account["project_id"],),
    ).fetchone() == (default, "General")
    assert db.execute(
        "SELECT topic_uuid FROM messenger_message_placements WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (default,)


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize(
    "subject", ["Renamed default history", "✔ Renamed default history"]
)
def test_rename_from_default_keeps_empty_subject_identity_and_new_default_messages(
    incoming, subject, durable
):
    api, db, account, http, url = incoming
    initial = incoming_tests.event()
    initial["message"]["subject"] = ""
    incoming_tests.apply(incoming, initial)
    default = db.execute(
        "SELECT default_topic_uuid FROM messenger_streams WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone()[0]
    canonical = db.execute(
        "SELECT uuid FROM messenger_messages WHERE project_id=%s AND provider_identity='101'",
        (account["project_id"],),
    ).fetchone()[0]
    http.request.side_effect = AssertionError(
        "Default-topic moves must not fetch message bodies"
    )
    change = rename(subject, orig_subject="", message_ids=[101])
    if durable:

        async def run():
            service = zulip_incoming.ZulipIncomingService(url)
            await service.db.open()
            service.http = http
            service.operations = incoming_operations.Operations(service.db, http)
            try:
                async with service.admission() as (connection, stage):
                    await service.append(
                        connection,
                        account,
                        {"queue_id": "default-source"},
                        [{"id": 1, **change}],
                        stage,
                    )
                await incoming_tests.drain_incoming(service)
            finally:
                await service.db.close()

        asyncio.run(run())
    else:
        incoming_tests.apply(incoming, change)
    assert db.execute(
        "SELECT default_topic_uuid FROM messenger_streams WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (default,)
    assert db.execute(
        "SELECT provider_identity,is_done FROM messenger_topics WHERE project_id=%s AND uuid=%s",
        (account["project_id"], default),
    ).fetchone() == ("42:", False)
    moved = db.execute(
        "SELECT topic_uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s AND message_uuid=%s",
        (account["project_id"], canonical),
    ).fetchone()
    assert moved[0] != default and moved[1] == canonical
    following = incoming_tests.event("Fresh default message", 102)
    following["message"]["subject"] = ""
    incoming_tests.apply(incoming, following)
    default_messages = api.get(
        "/v1/messenger/messages/", params={"topic_uuid": str(default)}
    ).json()
    assert [message["payload"]["content"] for message in default_messages] == [
        "Fresh default message"
    ]
    target = api.get(f"/v1/messenger/stream_topics/{moved[0]}").json()
    assert target["name"] == subject.removeprefix("✔ ") and target[
        "is_done"
    ] == subject.startswith("✔ ")
