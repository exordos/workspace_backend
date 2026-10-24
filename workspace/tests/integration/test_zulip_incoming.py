# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import datetime
import concurrent.futures
import time
import uuid
from unittest import mock

import psycopg
from psycopg import rows
import pytest
from restalchemy.common import contexts

from workspace.bridges.zulip import incoming_operations
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.services import zulip_incoming
from workspace.services import zulip_outgoing
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest


@pytest.fixture
def incoming(api, db, _test_database_url):
    conftest.seed_workspace_user(db, uuid.UUID(api.user_uuid), f"user-{api.user_uuid}")
    account = external_models.ExternalAccount(
        project_id=uuid.UUID(api.project_id),
        owner_user_uuid=uuid.UUID(api.user_uuid),
        provider="zulip",
        external_account_id="7",
        status="active",
        settings={
            "server_url": "https://zulip.example.com",
            "email": "user@example.com",
            "api_key": "test-key",
        },
    )
    account.insert()
    db.execute(
        incoming_operations.REGISTER_USER_SQL,
        (account.project_id, account.owner_user_uuid),
    )
    with db.cursor(row_factory=rows.dict_row) as cursor:
        cursor.execute(zulip_incoming.SELECT_ACCOUNTS_SQL)
        data = next(row for row in cursor.fetchall() if row["uuid"] == account.uuid)
    http = mock.Mock(request=mock.AsyncMock())
    http.request.return_value = {
        "stream": {"stream_id": 42, "name": "Channel", "description": ""}
    }
    yield api, db, data, http, _test_database_url
    db.execute("DELETE FROM m_external_accounts_v2 WHERE uuid=%s", (account.uuid,))


def event(content="Hello", identifier=101):
    return {
        "type": "message",
        "message": {
            "id": identifier,
            "sender_id": 7,
            "content": content,
            "type": "stream",
            "stream_id": 42,
            "subject": "General",
            "flags": [],
            "timestamp": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        },
    }


def apply(incoming, *events):
    api, db, account, http, database_url = incoming

    async def run():
        database = zulip_outgoing.Database(database_url)
        await database.open()
        operations = incoming_operations.Operations(database, http)
        try:
            for value in events:
                prepared = await prepare_dependencies(
                    operations, database, account, value
                )
                async with database.transaction() as connection:
                    await connection.execute(zulip_incoming.INCOMING_TRANSACTION_SQL)
                    await operations.apply(connection, account, prepared)
        finally:
            await database.close()

    asyncio.run(run())


async def prepare_dependencies(operations, database, account, value):
    while True:
        try:
            return await operations.prepare(account, value)
        except incoming_operations.MissingDependency as error:
            prepared = await prepare_dependencies(
                operations,
                database,
                account,
                {"type": "ensure_dependency", "dependency": error.spec},
            )
            async with database.transaction() as connection:
                await connection.execute(zulip_incoming.INCOMING_TRANSACTION_SQL)
                await operations.apply(connection, account, prepared)


def drain():
    with contexts.Context().session_manager() as session:
        v2_projection.drain_projection_queue(session, f"incoming-test:{uuid.uuid4()}")
        failures = session.execute(
            "SELECT task_kind,last_error FROM messenger_projection_tasks WHERE status IN ('failed','dead_letter')"
        ).fetchall()
        assert not failures, failures


def test_message_create_edit_delete_produces_client_events(incoming):
    api, db, account, http, database_url = incoming
    apply(incoming, event())
    drain()
    response = api.get("/v1/messenger/messages/")
    assert response.status_code == 200, response.text
    message = response.json()[0]
    assert message["payload"]["content"] == "Hello"
    assert message["provider_identity"] == "101"
    http.request.return_value = {
        "message": {
            **event("Edited")["message"],
            "last_edit_timestamp": int(
                datetime.datetime.now(datetime.timezone.utc).timestamp()
            )
            + 1,
        }
    }
    apply(incoming, {"type": "update_message", "message_id": 101})
    drain()
    assert (
        api.get(f"/v1/messenger/messages/{message['uuid']}").json()["payload"][
            "content"
        ]
        == "Edited"
    )
    apply(incoming, {"type": "delete_message", "message_ids": [101]})
    drain()
    kinds = [
        row[0]
        for row in db.execute(
            "SELECT payload->>'kind' FROM m_workspace_visible_events WHERE project_id=%s AND user_uuid=%s ORDER BY epoch_version",
            (account["project_id"], account["owner_user_uuid"]),
        )
    ]
    assert "message.created" in kinds
    assert "message.updated" in kinds
    assert "message.deleted" in kinds
    assert (
        db.execute(
            "SELECT count(*) FROM zulip_outgoing_intents WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("newer_snapshot", [False, True])
def test_edit_event_wins_over_stale_message_detail(incoming, newer_snapshot):
    api, _db, _account, http, _url = incoming
    original = event()
    apply(incoming, original)
    stamp = original["message"]["timestamp"]
    http.request.return_value = {
        "message": {
            **original["message"],
            "content": "Newer detail" if newer_snapshot else "Hello",
            "last_edit_timestamp": stamp + 20 if newer_snapshot else stamp,
        }
    }
    apply(
        incoming,
        {
            "type": "update_message",
            "message_id": 101,
            "content": "Edited event",
            "edit_timestamp": stamp + 10,
        },
    )
    drain()
    assert api.get("/v1/messenger/messages/").json()[0]["payload"]["content"] == (
        "Newer detail" if newer_snapshot else "Edited event"
    )


def client_events(incoming):
    _api, db, account, _http, _url = incoming
    return [
        row[0]
        for row in db.execute(
            "SELECT payload FROM m_workspace_visible_events WHERE project_id=%s AND user_uuid=%s ORDER BY epoch_version",
            (account["project_id"], account["owner_user_uuid"]),
        )
    ]


def test_repeated_events_are_silent_and_old_message_changes_are_published(incoming):
    old = event()
    old["message"]["timestamp"] = 1000000000
    apply(incoming, old)
    drain()
    before = len(client_events(incoming))
    apply(incoming, old)
    drain()
    assert len(client_events(incoming)) == before
    apply(
        incoming,
        {
            "type": "update_message_flags",
            "messages": [101],
            "flag": "read",
            "op": "add",
        },
    )
    drain()
    assert "message.read" in [row["kind"] for row in client_events(incoming)[before:]]


def test_reactions_read_and_starred_flags_update_domain_and_clients(incoming):
    api, db, account, http, _url = incoming
    apply(incoming, event())
    reaction = {
        "type": "reaction",
        "op": "add",
        "message_id": 101,
        "user_id": 7,
        "emoji_name": "+1",
        "emoji_code": "1f44d",
        "reaction_type": "unicode_emoji",
    }
    apply(
        incoming,
        reaction,
        {
            "type": "update_message_flags",
            "messages": [101],
            "flag": "starred",
            "op": "add",
        },
    )
    drain()
    message = api.get("/v1/messenger/messages/").json()[0]
    assert message["starred"] is True
    assert message["reactions"]
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_message_reaction_facts WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        == 1
    )
    apply(incoming, {**reaction, "op": "remove"})
    drain()
    kinds = [item["kind"] for item in client_events(incoming)]
    assert "message_reaction.created" in kinds
    assert "message_reaction.deleted" in kinds
    assert not api.get("/v1/messenger/messages/").json()[0]["reactions"]


def test_channel_and_topic_preferences_emit_client_events(incoming):
    api, db, account, _http, _url = incoming
    apply(incoming, event())
    apply(
        incoming,
        {
            "type": "stream",
            "op": "update",
            "stream_id": 42,
            "property": "name",
            "value": "Renamed",
        },
        {
            "type": "subscription",
            "op": "update",
            "stream_id": 42,
            "property": "is_muted",
            "value": True,
        },
        {
            "type": "user_topic",
            "stream_id": 42,
            "topic_name": "General",
            "visibility_policy": 3,
        },
    )
    drain()
    stream = api.get("/v1/messenger/streams/").json()[0]
    assert stream["name"] == "Renamed"
    assert (
        db.execute(
            "SELECT notification_mode FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s",
            (account["project_id"], uuid.UUID(stream["uuid"])),
        ).fetchone()[0]
        == "mentions_only"
    )
    kinds = [item["kind"] for item in client_events(incoming)]
    assert "stream.updated" in kinds
    assert "stream_binding.updated" in kinds
    assert "topic.updated" in kinds
    apply(incoming, {"type": "subscription", "op": "remove", "stream_ids": [42]})
    drain()
    assert "stream.deleted" in [item["kind"] for item in client_events(incoming)]


def test_downloaded_file_is_visible_and_announced(incoming, monkeypatch, tmp_path):
    api, db, account, http, _url = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    http.download = mock.AsyncMock(return_value=(b"example file", "text/plain"))
    apply(incoming, event("[file](/user_uploads/1/a/example.txt)"))
    drain()
    message = api.get("/v1/messenger/messages/").json()[0]
    assert "urn:file:" in message["payload"]["content"]
    file = db.execute(
        "SELECT uuid FROM m_workspace_files WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone()[0]
    response = api.get(f"/v1/messenger/files/{file}/actions/download")
    assert response.status_code == 200, response.text
    assert response.content == b"example file"
    assert "file.created" in [item["kind"] for item in client_events(incoming)]


def test_receive_cursor_and_inbox_are_atomic_and_deduplicated(incoming):
    _api, db, account, _http, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        subscription = {
            "queue_id": "atomic-test",
            "last_event_id": -1,
            "longpoll_timeout": 90,
        }
        try:
            async with service.db.transaction() as connection:
                await connection.execute(
                    zulip_incoming.SAVE_SUBSCRIPTION_SQL,
                    (
                        account["uuid"],
                        account["project_id"],
                        account["provider_uuid"],
                        account["owner_user_uuid"],
                        "7",
                        account["settings"]["server_url"],
                        "atomic-test",
                        -1,
                        90,
                    ),
                )
            with pytest.raises(RuntimeError):
                async with service.db.transaction() as connection:
                    await service.append(
                        connection, account, subscription, [{"id": 2, **event()}]
                    )
                    raise RuntimeError("rollback")
            assert not await service.db.sql(
                "SELECT id FROM zulip_incoming_events WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
            assert (
                await service.db.sql(
                    "SELECT last_event_id FROM zulip_incoming_subscriptions WHERE external_account_uuid=%s",
                    (account["uuid"],),
                )
            )[0]["last_event_id"] == -1
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    subscription,
                    [{"id": 2, **event()}, {"id": 2, **event()}],
                )
            assert (
                len(
                    await service.db.sql(
                        "SELECT id FROM zulip_incoming_events WHERE external_account_uuid=%s",
                        (account["uuid"],),
                    )
                )
                == 1
            )
            assert (
                await service.db.sql(
                    "SELECT last_event_id FROM zulip_incoming_subscriptions WHERE external_account_uuid=%s",
                    (account["uuid"],),
                )
            )[0]["last_event_id"] == 2
        finally:
            await service.db.close()

    asyncio.run(run())


def test_failed_application_keeps_domain_and_client_events_unchanged(incoming):
    _api, db, account, http, url = incoming

    async def run():
        database = zulip_outgoing.Database(url)
        await database.open()
        operations = incoming_operations.Operations(database, http)
        prepared = await prepare_dependencies(operations, database, account, event())
        try:
            with pytest.raises(RuntimeError):
                async with database.transaction() as connection:
                    await connection.execute(zulip_incoming.INCOMING_TRANSACTION_SQL)
                    await operations.apply(connection, account, prepared)
                    raise RuntimeError("rollback")
        finally:
            await database.close()

    asyncio.run(run())
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        == 0
    )


def test_pending_zulip_echo_reuses_root_and_fills_canonical_identity(incoming):
    api, db, account, _http, _url = incoming
    apply(incoming, event())
    drain()
    stream = api.get("/v1/messenger/streams/").json()[0]
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "**Original**"},
        },
    )
    assert response.status_code == 201, response.text
    placement = response.json()
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (uuid.UUID(placement["uuid"]),),
    ).fetchone()[0]
    delivery = uuid.uuid4()
    db.execute(zulip_incoming.INCOMING_TRANSACTION_SQL)
    db.execute(
        """INSERT INTO bridge_zulip_v5_deliveries
        (uuid,project_id,message_uuid,stream_uuid,external_account_uuid,hostname,external_account_id,recipient,topic_name,sending)
        VALUES (%s,%s,%s,%s,%s,%s,'7','{}','General',true)""",
        (
            delivery,
            account["project_id"],
            canonical,
            uuid.UUID(stream["uuid"]),
            account["uuid"],
            account["hostname"],
        ),
    )
    db.execute(
        incoming_operations.outgoing_operations.INSERT_CONTENT_SNAPSHOT_SQL,
        (uuid.uuid4(), delivery, "**Original**", "*Original*"),
    )
    db.execute(
        "UPDATE zulip_outgoing_intents SET status='done' WHERE project_id=%s",
        (account["project_id"],),
    )
    echo = event("*Original*", 202)
    echo["local_message_id"] = str(canonical)
    echo["message"]["timestamp"] += 5
    apply(incoming, echo)
    drain()
    root = db.execute(
        "SELECT source_name,provider_uuid,provider_identity,payload FROM messenger_messages WHERE project_id=%s AND uuid=%s",
        (account["project_id"], canonical),
    ).fetchone()
    assert root == (
        "zulip",
        account["provider_uuid"],
        "202",
        {"kind": "markdown", "content": "**Original**"},
    )
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        == 2
    )
    assert not db.execute(
        "SELECT 1 FROM zulip_entity_links WHERE project_id=%s AND entity_uuid=%s",
        (account["project_id"], canonical),
    ).fetchone()


@pytest.mark.parametrize("missing_binding", [False, True])
@pytest.mark.parametrize("catalog_event", [False, True])
def test_user_topic_and_message_echo_complete_pending_topic_identity(
    incoming, missing_binding, catalog_event
):
    api, db, account, _http, _url = incoming
    apply(incoming, event())
    drain()
    stream = api.get("/v1/messenger/streams/").json()[0]
    response = api.post(
        "/v1/messenger/stream_topics/",
        json={"stream_uuid": stream["uuid"], "name": "Native topic"},
    )
    assert response.status_code == 201, response.text
    topic = response.json()
    if missing_binding:
        db.execute(
            "DELETE FROM messenger_user_topic_bindings "
            "WHERE project_id=%s AND topic_uuid=%s",
            (account["project_id"], uuid.UUID(topic["uuid"])),
        )
    if catalog_event:
        apply(
            incoming,
            {
                "type": "user_topic",
                "stream_id": 42,
                "topic_name": "Native topic",
                "visibility_policy": 0,
            },
        )
    following = event("Following native-topic message", 202)
    following["message"]["subject"] = "Native topic"
    apply(incoming, following)
    drain()
    assert db.execute(
        "SELECT source_name,provider_uuid,provider_identity FROM messenger_topics "
        "WHERE project_id=%s AND uuid=%s",
        (account["project_id"], uuid.UUID(topic["uuid"])),
    ).fetchone() == ("zulip", account["provider_uuid"], "42:Native topic")
    binding = db.execute(
        "SELECT source_name,provider_uuid,provider_identity "
        "FROM messenger_user_topic_bindings WHERE project_id=%s AND topic_uuid=%s",
        (account["project_id"], uuid.UUID(topic["uuid"])),
    ).fetchone()
    assert binding == ("zulip", account["provider_uuid"], "42:Native topic:7")
    received = api.get(
        "/v1/messenger/messages/", params={"topic_uuid": topic["uuid"]}
    ).json()
    assert [item["payload"]["content"] for item in received] == [
        "Following native-topic message"
    ]


def test_default_topic_binding_uses_empty_remote_subject(incoming):
    api, db, account, _http, _url = incoming
    apply(incoming, event())
    topic_uuid = db.execute(
        "SELECT default_topic_uuid FROM messenger_streams WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone()[0]
    db.execute(
        "DELETE FROM messenger_user_topic_bindings WHERE project_id=%s AND topic_uuid=%s",
        (account["project_id"], topic_uuid),
    )
    apply(
        incoming,
        {
            "type": "subscription",
            "op": "update",
            "stream_id": 42,
            "property": "is_muted",
            "value": True,
        },
    )
    assert db.execute(
        "SELECT provider_identity FROM messenger_user_topic_bindings "
        "WHERE project_id=%s AND topic_uuid=%s",
        (account["project_id"], topic_uuid),
    ).fetchone() == ("42::7",)


@pytest.mark.isolated_schema
def test_long_polling_reaches_an_actual_websocket_client(incoming, monkeypatch):
    import contextlib
    import contextvars
    import json
    import socket

    from aiohttp import web
    import websockets
    from workspace.messenger_api import websocket_service

    _api, db, account, _http, url = incoming

    async def run():
        inbox = []
        registrations = []
        acknowledgements = []
        identities = []
        expired = []
        in_transaction = contextvars.ContextVar(
            "incoming-test-transaction", default=False
        )

        async def own_user(request):
            identities.append(request.transport)
            return web.json_response(
                {"user_id": 7, "full_name": "Sender", "email": "user@example.com"}
            )

        async def register(request):
            registrations.append(dict(await request.post()))
            return web.json_response(
                {
                    "queue_id": f"live-queue-{len(registrations)}",
                    "last_event_id": -1,
                    "event_queue_longpoll_timeout_seconds": 1,
                    "realm_users": [],
                    "subscriptions": [
                        {"stream_id": 42, "name": "Channel", "description": ""}
                    ],
                }
            )

        async def events(request):
            if expired and request.query["queue_id"] == "live-queue-1":
                return web.json_response(
                    {"result": "error", "code": "BAD_EVENT_QUEUE_ID", "msg": "Expired"},
                    status=400,
                )
            cursor = int(request.query["last_event_id"])
            acknowledgements.append(cursor)
            await asyncio.sleep(0.02)
            return web.json_response(
                {"events": [item for item in inbox if item["id"] > cursor]}
            )

        app = web.Application()
        app.router.add_get("/api/v1/users/me", own_user)
        app.router.add_post("/api/v1/register", register)
        app.router.add_get("/api/v1/events", events)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        provider = external_models.Provider.register("127.0.0.1")
        settings = {**account["settings"], "server_url": f"http://127.0.0.1:{port}"}
        db.execute(
            "UPDATE m_external_accounts_v2 SET provider_uuid=%s,settings=%s WHERE uuid=%s",
            (provider.uuid, incoming_operations.json_value(settings), account["uuid"]),
        )
        service = zulip_incoming.ZulipIncomingService(url, poll_interval=0.02)
        transaction = service.db.transaction

        @contextlib.asynccontextmanager
        async def checked_transaction():
            async with transaction() as connection:
                token = in_transaction.set(True)
                try:
                    yield connection
                finally:
                    in_transaction.reset(token)

        monkeypatch.setattr(service.db, "transaction", checked_transaction)
        original_request = zulip_outgoing.Http.request

        async def checked_request(self, *args, **kwargs):
            assert not in_transaction.get(), "HTTP request inside a SQL transaction"
            return await original_request(self, *args, **kwargs)

        monkeypatch.setattr(zulip_outgoing.Http, "request", checked_request)
        receiver = asyncio.create_task(service.run())
        socket_probe = socket.socket()
        socket_probe.bind(("127.0.0.1", 0))
        websocket_port = socket_probe.getsockname()[1]
        socket_probe.close()
        server = websocket_service.MessengerEventsWebsocketServer(
            db_url=url,
            iam_engine_driver=mock.Mock(),
            heartbeat_interval=25,
            client_timeout=5,
            catchup_limit=500,
            send_queue_limit=32,
            poll_interval=0.02,
        )
        server._authenticator.authenticate = lambda token: (
            account["owner_user_uuid"],
            account["project_id"],
        )
        websocket_task = asyncio.create_task(server.serve("127.0.0.1", websocket_port))
        try:
            async with asyncio.timeout(10):
                while not registrations:
                    await asyncio.sleep(0.01)
                while (
                    db.execute(
                        "SELECT status FROM m_external_accounts_v2 WHERE uuid=%s",
                        (account["uuid"],),
                    ).fetchone()[0]
                    != "active"
                ):
                    await asyncio.sleep(0.01)
                await asyncio.to_thread(drain)
                async with websockets.connect(
                    f"ws://127.0.0.1:{websocket_port}/v1/events/ws",
                    subprotocols=["workspace.events.v1", "bearer.test"],
                ) as websocket:
                    while json.loads(await websocket.recv()).get("type") != "ready":
                        pass
                    inbox.append({"id": 3, **event("Delivered through long polling")})
                    while not db.execute(
                        "SELECT 1 FROM messenger_messages WHERE project_id=%s AND provider_identity=%s",
                        (account["project_id"], "101"),
                    ).fetchone():
                        await asyncio.sleep(0.01)
                    await asyncio.to_thread(drain)
                    while True:
                        frame = json.loads(await websocket.recv())
                        if (
                            frame.get("object_type") == "message"
                            and frame.get("action") == "created"
                        ):
                            assert (
                                frame["payload"]["payload"]["content"]
                                == "Delivered through long polling"
                            )
                            break
                    while 3 not in acknowledgements:
                        await asyncio.sleep(0.01)
                    assert len(registrations) == 1
                    assert (
                        db.execute(
                            "SELECT count(*) FROM zulip_incoming_events WHERE external_account_uuid=%s AND event_id=3",
                            (account["uuid"],),
                        ).fetchone()[0]
                        == 1
                    )
                    expired.append(True)
                    inbox.append({"id": 4, **event("After queue renewal", 102)})
                    while len(registrations) < 2:
                        await asyncio.sleep(0.01)
                    while not db.execute(
                        "SELECT 1 FROM messenger_messages WHERE project_id=%s AND provider_identity='102'",
                        (account["project_id"],),
                    ).fetchone():
                        await asyncio.sleep(0.01)
                    await asyncio.to_thread(drain)
                    while True:
                        frame = json.loads(await websocket.recv())
                        if (
                            frame.get("object_type") == "message"
                            and frame.get("action") == "created"
                        ):
                            assert (
                                frame["payload"]["payload"]["content"]
                                == "After queue renewal"
                            )
                            break
        finally:
            service.stopping.set()
            service.ready.set()
            server.stop()
            await asyncio.gather(receiver, websocket_task)
            await runner.cleanup()

    asyncio.run(run())


def peer_account(incoming):
    api, db, account, http, url = incoming
    peer = uuid.uuid4()
    conftest.seed_workspace_user(db, peer, f"peer-{peer}")
    db.execute(incoming_operations.REGISTER_USER_SQL, (account["project_id"], peer))
    linked = external_models.ExternalAccount(
        project_id=account["project_id"],
        owner_user_uuid=peer,
        provider="zulip",
        external_account_id="8",
        status="active",
        settings={**account["settings"], "email": "peer@example.com"},
    )
    linked.insert()
    with db.cursor(row_factory=rows.dict_row) as cursor:
        cursor.execute(zulip_incoming.SELECT_ACCOUNT_SQL, (linked.uuid,))
        data = cursor.fetchone()
    return linked, (api, db, data, http, url)


def test_provider_membership_does_not_grant_unobserved_messages_or_files(
    incoming, monkeypatch, tmp_path
):
    api, db, account, http, _url = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    http.download = mock.AsyncMock(return_value=(b"private file", "text/plain"))
    value = event("[private](/user_uploads/1/a/private.txt)")
    apply(incoming, value)
    drain()
    linked, peer = peer_account(incoming)
    try:
        apply(
            peer,
            {
                "type": "subscription",
                "op": "add",
                "subscriptions": [{"stream_id": 42, "name": "Channel"}],
            },
        )
        drain()
        file = db.execute(
            "SELECT uuid FROM m_workspace_files WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        assert (
            api.get("/v1/messenger/messages/", user=linked.owner_user_uuid).json() == []
        )
        assert (
            api.get(
                f"/v1/messenger/files/{file}", user=linked.owner_user_uuid
            ).status_code
            == 404
        )
        before = sum(
            item["kind"] == "message.created" for item in client_events(incoming)
        )
        apply(peer, value)
        drain()
        assert (
            len(api.get("/v1/messenger/messages/", user=linked.owner_user_uuid).json())
            == 1
        )
        assert (
            api.get(
                f"/v1/messenger/files/{file}", user=linked.owner_user_uuid
            ).status_code
            == 200
        )
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
                (account["project_id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            sum(item["kind"] == "message.created" for item in client_events(incoming))
            == before
        )
    finally:
        linked.delete()


def test_reaction_payload_preserves_its_actor_for_other_recipients(incoming):
    apply(incoming, event())
    linked, _peer = peer_account(incoming)
    try:
        apply(
            incoming,
            {
                "type": "reaction",
                "op": "add",
                "message_id": 101,
                "user_id": 8,
                "emoji_name": "+1",
                "emoji_code": "1f44d",
                "reaction_type": "unicode_emoji",
            },
        )
        drain()
        reaction = next(
            item
            for item in client_events(incoming)
            if item["kind"] == "message_reaction.created"
        )
        assert reaction["user_uuid"] == str(linked.owner_user_uuid)
    finally:
        linked.delete()


def test_topic_rename_preserves_identity_and_last_delete_removes_topic(incoming):
    api, db, account, http, _url = incoming
    apply(incoming, event())
    topic = db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:General'",
        (account["project_id"],),
    ).fetchone()[0]
    message = {**event()["message"], "subject": "Renamed topic"}
    http.request.return_value = {"message": message}
    apply(
        incoming,
        {
            "type": "update_message",
            "message_id": 101,
            "message_ids": [101],
            "stream_id": 42,
            "orig_subject": "General",
            "subject": "Renamed topic",
            "propagate_mode": "change_all",
        },
    )
    drain()
    assert (
        db.execute(
            "SELECT name FROM messenger_topics WHERE project_id=%s AND uuid=%s",
            (account["project_id"], topic),
        ).fetchone()[0]
        == "Renamed topic"
    )
    assert (
        uuid.UUID(api.get("/v1/messenger/messages/").json()[0]["topic_uuid"]) == topic
    )
    apply(incoming, {"type": "delete_message", "message_ids": [101]})
    drain()
    assert "topic.deleted" in [item["kind"] for item in client_events(incoming)]


def test_attachment_events_before_message_allow_later_file_removal(
    incoming, monkeypatch, tmp_path
):
    _api, db, account, http, _url = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    http.download = mock.AsyncMock(return_value=(b"file", "text/plain"))
    apply(
        incoming,
        {
            "type": "attachment",
            "op": "add",
            "attachment": {"id": 55, "path_id": "1/a/file.txt", "name": "file.txt"},
        },
    )
    apply(incoming, event("[file](/user_uploads/1/a/file.txt)"))
    drain()
    apply(incoming, {"type": "attachment", "op": "remove", "attachment": {"id": 55}})
    drain()
    assert (
        db.execute(
            "SELECT count(*) FROM m_workspace_files WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        == 0
    )
    assert "file.deleted" in [item["kind"] for item in client_events(incoming)]


def test_direct_messages_share_one_conversation_in_both_directions(incoming):
    api, db, account, _http, _url = incoming
    linked, peer = peer_account(incoming)
    recipients = [{"id": 7, "full_name": "Sender"}, {"id": 8, "full_name": "Peer"}]
    first = event()
    first["message"].update(
        type="private", recipient_id=1, display_recipient=recipients
    )
    first["message"].pop("stream_id")
    first["message"].pop("subject")
    second = {
        "type": "message",
        "message": {**first["message"], "id": 102, "sender_id": 8, "recipient_id": 2},
    }
    try:
        apply(incoming, first)
        apply(peer, second)
        drain()
        streams = db.execute(
            "SELECT owner_uuid,direct_user_uuid FROM messenger_streams WHERE project_id=%s",
            (account["project_id"],),
        ).fetchall()
        assert len(streams) == 1
        assert streams[0] == (account["owner_user_uuid"], linked.owner_user_uuid)
        assert len(api.get("/v1/messenger/messages/").json()) == 1
        assert (
            len(api.get("/v1/messenger/messages/", user=linked.owner_user_uuid).json())
            == 1
        )
    finally:
        linked.delete()


def test_message_move_removes_old_placement_and_publishes_both_scopes(incoming):
    api, db, account, http, _url = incoming
    apply(incoming, event())
    drain()
    previous = api.get("/v1/messenger/messages/").json()[0]
    apply(
        incoming,
        {
            "type": "subscription",
            "op": "add",
            "subscriptions": [{"stream_id": 43, "name": "Destination"}],
        },
    )
    moved = {**event()["message"], "stream_id": 43, "subject": "Moved"}
    http.request.return_value = {"message": moved}
    apply(
        incoming, {"type": "update_message", "message_ids": [101], "new_stream_id": 43}
    )
    drain()
    current = api.get("/v1/messenger/messages/").json()
    assert len(current) == 1
    assert current[0]["stream_uuid"] != previous["stream_uuid"]
    assert api.get(f"/v1/messenger/messages/{previous['uuid']}").status_code == 404
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_message_placements WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        == 1
    )
    assert any(
        item["kind"] == "message.deleted" and str(item["uuid"]) == previous["uuid"]
        for item in client_events(incoming)
    )


def test_api_changes_on_incoming_objects_capture_outgoing_routes(incoming):
    api, db, account, _http, _url = incoming
    apply(incoming, event())
    drain()
    stream = api.get("/v1/messenger/streams/").json()[0]
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "Outbound reply"},
        },
    )
    assert response.status_code == 201, response.text
    captured = db.execute(
        "SELECT entity_type,operation,data FROM zulip_outgoing_intents WHERE project_id=%s AND entity_type='message'",
        (account["project_id"],),
    ).fetchall()
    assert len(captured) == 1
    assert captured[0][:2] == ("message", "insert")
    assert captured[0][2]["stream_id"] == 42
    assert captured[0][2]["channel_type"] == "channel"
    assert captured[0][2]["account_identity"] == "7"


def test_inbox_preserves_order_across_retry_and_service_restart(incoming):
    apply(
        incoming,
        {
            "type": "subscription",
            "op": "add",
            "subscriptions": [{"stream_id": 42, "name": "Channel"}],
        },
    )
    apply(
        incoming,
        {
            "type": "user_topic",
            "stream_id": 42,
            "topic_name": "General",
            "visibility_policy": 0,
        },
    )
    _api, _db, account, http, url = incoming

    async def run():
        first = zulip_incoming.ZulipIncomingService(url)
        await first.db.open()
        try:
            async with first.db.transaction() as connection:
                await first.append(
                    connection,
                    account,
                    {"queue_id": "ordered"},
                    [
                        {"id": 1, **event("First")},
                        {"id": 2, **event("Second")},
                    ],
                )
            pending = await first.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
            assert [
                row["event_id"]
                for row in pending
                if row["external_account_uuid"] == account["uuid"]
            ] == [1]
            await first.db.sql(
                zulip_incoming.RETRY_EVENT_SQL, ("dependency", 1, pending[0]["id"])
            )
            assert not await first.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
            await first.db.sql(
                "UPDATE zulip_incoming_events SET next_retry_at=now() WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
        finally:
            await first.db.close()
        restarted = zulip_incoming.ZulipIncomingService(url)
        await restarted.db.open()
        restarted.operations = incoming_operations.Operations(restarted.db, http)
        try:
            for expected in (1, 2):
                pending = await restarted.db.sql(
                    zulip_incoming.SELECT_EVENT_SQL, ([], 10)
                )
                assert [row["event_id"] for row in pending] == [expected]
                await restarted.process(pending[0])
            assert not await restarted.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
        finally:
            await restarted.db.close()

    asyncio.run(run())


@pytest.mark.parametrize("placement_references", [False, True])
def test_cutover_keeps_live_events_and_cursor_and_removes_history_tasks(
    _database, placement_references
):
    import importlib.util
    import psycopg

    with conftest.isolated_migration_database(
        "0265-Replace-outbound-Zulip-tasks-with-SQL-intents-9cafda.py"
    ) as url:
        with psycopg.connect(
            url, autocommit=True, row_factory=rows.dict_row
        ) as connection:
            if placement_references:
                for table in ("bridge_zulip_v5_deliveries", "bridge_zulip_v5_messages"):
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN placement_event_uuid uuid "
                        "REFERENCES bridge_zulip_v5_events(uuid) ON DELETE SET NULL"
                    )
                connection.execute(
                    "CREATE VIEW bridge_zulip_v5_retired_placement_states AS "
                    "SELECT uuid, placement_event_uuid FROM bridge_zulip_v5_messages"
                )
            owner, project = uuid.uuid4(), uuid.uuid4()
            conftest.seed_workspace_user(connection, owner, str(owner))
            account = external_models.ExternalAccount(
                project_id=project,
                owner_user_uuid=owner,
                provider="zulip",
                external_account_id="7",
                status="active",
                settings={
                    "server_url": "https://zulip.example.com",
                    "email": "user@example.com",
                },
            )
            account.insert()
            connection.execute(
                "UPDATE m_external_accounts_v2 SET status='backfill' WHERE uuid=%s",
                (account.uuid,),
            )
            generation, pending, completed, historical = [
                uuid.uuid4() for _ in range(4)
            ]
            connection.execute(
                """INSERT INTO bridge_zulip_v5_subscriptions
                (uuid,project_id,external_account_uuid,generation,queue_id,last_event_id,account_revision,longpoll_timeout)
                VALUES (%s,%s,%s,%s,'cutover-queue',6,'retired',90)""",
                (uuid.uuid4(), project, account.uuid, generation),
            )
            for identity, event_id, source in (
                (pending, 5, "zulip"),
                (completed, 6, "zulip"),
                (historical, None, "internal"),
            ):
                connection.execute(
                    """INSERT INTO bridge_zulip_v5_events
                    (uuid,project_id,external_account_uuid,generation,queue_id,event_id,source,payload,processed_at)
                    VALUES (%s,%s,%s,%s,'cutover-queue',%s,%s,'{"type":"heartbeat"}',now())""",
                    (identity, project, account.uuid, generation, event_id, source),
                )
            connection.execute(
                """INSERT INTO bridge_zulip_v5_tasks
                (uuid,project_id,external_account_uuid,realtime_event_uuid,content,status)
                VALUES (%s,%s,%s,%s,'{"kind":"import_messages"}','new'),
                       (%s,%s,%s,NULL,'{"kind":"full_backfill"}','new')""",
                (
                    uuid.uuid4(),
                    project,
                    account.uuid,
                    pending,
                    uuid.uuid4(),
                    project,
                    account.uuid,
                ),
            )
            path = next(conftest.MIGRATIONS_DIR.glob("0266-*.py"))
            spec = importlib.util.spec_from_file_location("incoming_cutover", path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            with connection.transaction():
                migration.migration_step.upgrade(connection)
            assert connection.execute(
                "SELECT queue_id,last_event_id FROM zulip_incoming_subscriptions"
            ).fetchone() == {"queue_id": "cutover-queue", "last_event_id": 6}
            received = connection.execute(
                "SELECT uuid,processed_at IS NULL AS pending FROM zulip_incoming_events ORDER BY event_id"
            ).fetchall()
            assert received == [
                {"uuid": pending, "pending": True},
                {"uuid": completed, "pending": False},
            ]
            assert (
                connection.execute(
                    "SELECT to_regclass('bridge_zulip_v5_tasks') AS name"
                ).fetchone()["name"]
                is None
            )
            assert (
                connection.execute(
                    "SELECT to_regclass('bridge_zulip_v5_events') AS name"
                ).fetchone()["name"]
                is None
            )
            assert not connection.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name IN ('bridge_zulip_v5_deliveries', 'bridge_zulip_v5_messages') "
                "AND column_name='placement_event_uuid'"
            ).fetchall()
            assert (
                connection.execute(
                    "SELECT to_regclass('bridge_zulip_v5_retired_placement_states') AS name"
                ).fetchone()["name"]
                is None
            )
            assert (
                connection.execute(
                    "SELECT status FROM m_external_accounts_v2 WHERE uuid=%s",
                    (account.uuid,),
                ).fetchone()["status"]
                == "syncing"
            )


@pytest.mark.parametrize("message", ["Invalid message(s)", "Invalid message ID"])
def test_topic_edit_skips_already_deleted_messages_in_the_event(incoming, message):
    api, _db, _account, http, _url = incoming
    initial = event()
    apply(incoming, initial)
    drain()
    updated = {
        **initial["message"],
        "subject": "Renamed surviving topic",
        "content": "Surviving edit",
        "last_edit_timestamp": initial["message"]["timestamp"] + 10,
    }
    original = http.request.side_effect

    async def get_message(account, method, path, data=None, **kwargs):
        if path == "/messages/102":
            raise incoming_operations.outgoing_operations.ApiError(
                400, {"code": "BAD_REQUEST", "msg": message}, "GET"
            )
        if path == "/messages/101":
            return {"message": updated}
        if original:
            return await original(account, method, path, data, **kwargs)
        raise AssertionError(path)

    http.request.side_effect = get_message
    apply(
        incoming,
        {
            "type": "update_message",
            "message_id": 102,
            "message_ids": [101, 102],
            "stream_id": 42,
            "orig_subject": "General",
            "subject": updated["subject"],
            "propagate_mode": "change_all",
            "edit_timestamp": updated["last_edit_timestamp"],
        },
    )
    drain()
    messages = api.get("/v1/messenger/messages/").json()
    assert len(messages) == 1
    assert messages[0]["payload"]["content"] == "Surviving edit"
    assert (
        api.get("/v1/messenger/stream_topics/" + messages[0]["topic_uuid"]).json()[
            "name"
        ]
        == updated["subject"]
    )


def test_journal_read_enqueues_the_provider_flag_change(incoming):
    from oslo_config import cfg
    from workspace.common import mutation_journal_opts
    from workspace.services.messenger_workers import mutation_runtime
    from workspace.tests.integration import test_mutation_journal as journal_tests

    api, db, account, _http, _url = incoming
    unread = event()
    unread["flags"] = []
    apply(incoming, unread)
    drain()
    message = api.get("/v1/messenger/messages/").json()[0]
    assert message["read"] is False
    cfg.CONF.set_override("enabled", True, group=mutation_journal_opts.DOMAIN)
    runtime = mutation_runtime.MutationRuntime()
    try:
        response = api.post(
            f"/v1/messenger/messages/{message['uuid']}/actions/read/invoke"
        )
        assert response.status_code == 200, response.text
        journal_tests.drain(runtime, db)
        assert (
            api.get("/v1/messenger/messages/" + message["uuid"]).json()["read"] is True
        )
        intents = db.execute(
            "SELECT data->>'source_name',data->>'read_at',data->>'actor_uuid' FROM zulip_outgoing_intents WHERE entity_type='message_state' AND external_account_uuid=%s",
            (account["uuid"],),
        ).fetchall()
        assert len(intents) == 1
        assert intents[0][0] == "zulip" and intents[0][1] is not None
        assert intents[0][2] == str(account["owner_user_uuid"])
    finally:
        runtime.close()
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)


def test_flags_do_not_recreate_a_state_deleted_with_its_message(incoming):
    api, db, account, _http, database_url = incoming
    apply(incoming, event())
    drain()
    message = api.get("/v1/messenger/messages/").json()[0]
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (message["uuid"],),
    ).fetchone()[0]
    original_rows = incoming_operations.rows
    deleted = None

    def delete_message():
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM messenger_messages WHERE uuid=%s", (canonical,)
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:

        async def rows_during_delete(connection, query, parameters):
            nonlocal deleted
            result = await original_rows(connection, query, parameters)
            if query == incoming_operations.SELECT_STATES_SQL:
                deleted = pool.submit(delete_message)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (
                        deleted.done()
                        or db.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE datname=current_database() AND wait_event_type='Lock' "
                            "AND query LIKE 'DELETE FROM messenger_messages WHERE uuid=%')"
                        ).fetchone()[0]
                    ):
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("Concurrent deletion did not start")
            return result

        with mock.patch.object(incoming_operations, "rows", rows_during_delete):
            apply(
                incoming,
                {
                    "type": "update_message_flags",
                    "messages": [101],
                    "flag": "read",
                    "op": "add",
                },
            )
        assert deleted is not None
        deleted.result(timeout=5)
    assert api.get("/v1/messenger/messages/" + message["uuid"]).status_code == 404
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_user_message_states WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        == 0
    )


def test_topic_merge_uses_existing_destination_without_duplicate_identity(incoming):
    api, db, account, http, _url = incoming
    apply(incoming, event())
    destination = {**event(identifier=102)["message"], "subject": "Destination"}
    apply(incoming, {"type": "message", "message": destination})
    target = db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s "
        "AND provider_identity='42:Destination'",
        (account["project_id"],),
    ).fetchone()[0]
    http.request.return_value = {
        "message": {**event()["message"], "subject": "Destination"}
    }
    apply(
        incoming,
        {
            "type": "update_message",
            "message_ids": [101],
            "stream_id": 42,
            "orig_subject": "General",
            "subject": "Destination",
            "propagate_mode": "change_all",
        },
    )
    drain()
    topics = db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s "
        "AND deleted_at IS NULL AND provider_identity IN ('42:General','42:Destination')",
        (account["project_id"],),
    ).fetchall()
    assert topics == [(target,)]
    messages = api.get("/v1/messenger/messages/").json()
    assert len(messages) == 2
    assert all(message["topic_uuid"] == str(target) for message in messages)
    assert len({message["uuid"] for message in messages}) == 2


@pytest.mark.parametrize("flag", ["read", "starred"])
@pytest.mark.parametrize("all_messages", [None, False, True])
def test_empty_flag_list_only_applies_globally_with_explicit_all(
    incoming, flag, all_messages
):
    _api, db, account, _http, _url = incoming
    apply(incoming, event(), event(identifier=102))
    values = {"type": "update_message_flags", "messages": [], "flag": flag, "op": "add"}
    if all_messages is not None:
        values["all"] = all_messages
    before = db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone()[0]
    apply(incoming, values)
    states = db.execute(
        "SELECT read_at IS NOT NULL,starred FROM messenger_user_message_states "
        "WHERE project_id=%s",
        (account["project_id"],),
    ).fetchall()
    expected = all_messages is True
    assert len(states) == 2
    assert all(row[0 if flag == "read" else 1] is expected for row in states)
    if not expected:
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
                (account["project_id"],),
            ).fetchone()[0]
            == before
        )


def test_ready_incoming_event_reaches_clients_without_projection_drain(incoming):
    api, db, account, _http, _url = incoming
    apply(incoming, event())
    events = api.get("/v1/events/").json()
    assert any(
        row["payload"]["kind"] == "message.created"
        and row["payload"]["payload"]["content"] == "Hello"
        for row in events
    )
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_projection_tasks WHERE project_id=%s "
            "AND task_kind='delivery_snapshot_event'",
            (account["project_id"],),
        ).fetchone()[0]
        == 0
    )


def test_thousand_read_flags_use_one_exact_batch_and_one_counter_scope(incoming):
    api, db, account, _http, _url = incoming
    apply(incoming, *(event(identifier=value) for value in range(1, 1002)))
    before_events = len(client_events(incoming))
    before_counters = db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone()[0]
    with mock.patch.object(
        incoming_operations, "rows", wraps=incoming_operations.rows
    ) as queries:
        apply(
            incoming,
            {
                "type": "update_message_flags",
                "messages": list(range(1, 1001)),
                "flag": "read",
                "op": "add",
                "all": False,
            },
        )
    states = db.execute(
        "SELECT message.provider_identity,state.read_at IS NOT NULL "
        "FROM messenger_user_message_states state "
        "JOIN messenger_message_placements place ON place.uuid=state.placement_uuid "
        "JOIN messenger_messages message ON message.uuid=place.message_uuid "
        "WHERE state.project_id=%s",
        (account["project_id"],),
    ).fetchall()
    assert len(states) == 1001
    assert all(read == (identity != "1001") for identity, read in states)
    frames = client_events(incoming)[before_events:]
    assert len(frames) == 1
    assert frames[0]["kind"] == "messages.read"
    expected = {
        str(row[0])
        for row in db.execute(
            "SELECT placement_uuid FROM messenger_user_message_states "
            "WHERE project_id=%s AND read_at IS NOT NULL",
            (account["project_id"],),
        )
    }
    assert set(frames[0]["message_uuids"]) == expected
    assert queries.call_count <= 10
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()[0]
        == before_counters + 1
    )
    assert api.get("/v1/events/").status_code == 200


def test_unknown_flag_targets_do_not_fetch_or_import_history(incoming):
    api, _db, _account, http, _url = incoming
    apply(incoming, event())
    http.request.reset_mock()
    apply(
        incoming,
        {
            "type": "update_message_flags",
            "messages": [999999],
            "flag": "read",
            "op": "add",
        },
    )
    http.request.assert_not_called()
    assert len(api.get("/v1/messenger/messages/").json()) == 1


@pytest.mark.parametrize(
    "flag,present",
    [("read", True), ("read", False), ("starred", True), ("starred", False)],
)
def test_batched_flags_preserve_pending_personal_changes_and_exact_scope(
    incoming, flag, present
):
    _api, db, account, _http, _url = incoming
    initial = [] if present else [flag]
    apply(
        incoming,
        *({**event(identifier=value), "flags": initial} for value in (101, 102, 103)),
    )
    pending = db.execute(
        "SELECT uuid FROM messenger_messages WHERE project_id=%s AND provider_identity='101'",
        (account["project_id"],),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data) "
        "VALUES (%s,'message_state',%s,'pending',%s,'update',%s)",
        (
            account["project_id"],
            uuid.uuid4(),
            account["uuid"],
            incoming_operations.json_value(
                {
                    "message_uuid": str(pending),
                    "user_uuid": str(account["owner_user_uuid"]),
                }
            ),
        ),
    )
    apply(
        incoming,
        {
            "type": "update_message_flags",
            "messages": [101, 102],
            "flag": flag,
            "op": "add" if present else "remove",
        },
    )
    states = db.execute(
        "SELECT message.provider_identity,state.read_at IS NOT NULL,state.starred "
        "FROM messenger_user_message_states state "
        "JOIN messenger_message_placements place ON place.uuid=state.placement_uuid "
        "JOIN messenger_messages message ON message.uuid=place.message_uuid "
        "WHERE state.project_id=%s",
        (account["project_id"],),
    ).fetchall()
    assert all(
        row[1 if flag == "read" else 2] == (present if row[0] == "102" else not present)
        for row in states
    )


def test_incoming_publication_lock_contention_rolls_back_and_retries(incoming):
    api, db, account, _http, url = incoming
    with psycopg.connect(url, autocommit=True) as locker:
        locker.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s::text,0))",
            (account["project_id"],),
        )
        try:
            with pytest.raises(incoming_operations.outgoing_operations.Pending):
                apply(incoming, event())
            assert (
                db.execute(
                    "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
                    (account["project_id"],),
                ).fetchone()[0]
                == 0
            )
            assert not api.get("/v1/events/").json()
        finally:
            locker.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s::text,0))",
                (account["project_id"],),
            )
    apply(incoming, event())
    assert (
        sum(
            row["payload"]["kind"] == "message.created"
            for row in api.get("/v1/events/").json()
        )
        == 1
    )


def test_missing_dependency_does_not_block_another_message_in_the_same_chat(incoming):
    apply(
        incoming,
        {
            "type": "subscription",
            "op": "add",
            "subscriptions": [{"stream_id": 42, "name": "Channel"}],
        },
    )
    # Prepare the topic without creating a message.
    apply(
        incoming,
        {
            "type": "user_topic",
            "stream_id": 42,
            "topic_name": "General",
            "visibility_policy": 0,
        },
    )
    api, _db, account, http, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        operations = incoming_operations.Operations(service.db, http)
        original_prepare = operations.prepare

        async def prepare(account, payload):
            if payload.get("message", {}).get("id") == 101:
                raise incoming_operations.outgoing_operations.Pending(
                    "Dependency pending"
                )
            return await original_prepare(account, payload)

        operations.prepare = prepare
        service.operations = operations
        try:
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    {"queue_id": "independent"},
                    [
                        {"id": 1, **event("Waiting", 101)},
                        {"id": 2, **event("Independent", 102)},
                        {"id": 3, "type": "update_message", "message_id": 101},
                    ],
                )
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
            assert [row["event_id"] for row in ready] == [1, 2]
            await asyncio.gather(*(service.process(row) for row in ready))
            rows = api.get("/v1/messenger/messages/").json()
            assert [row["payload"]["content"] for row in rows] == ["Independent"]
            assert any(
                row["payload"]["kind"] == "message.created"
                for row in api.get("/v1/events/").json()
            )
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE queue_id=%s AND event_id=3 "
                "AND processed_at IS NOT NULL",
                ("independent",),
            )
        finally:
            await service.db.close()

    asyncio.run(run())


@pytest.mark.parametrize("move", [False, True])
def test_reactions_are_add_remove_only(incoming, move):
    api, _db, _account, _http, _url = incoming
    apply(incoming, event(), event(identifier=102))
    messages = api.get("/v1/messenger/messages/").json()
    reaction = api.post(
        "/v1/messenger/message_reactions/",
        json={
            "message_uuid": messages[0]["uuid"],
            "emoji_name": "thumbs_up",
        },
    )
    assert reaction.status_code == 201, reaction.text
    path = "/v1/messenger/message_reactions/" + reaction.json()["uuid"]
    values = {"message_uuid": messages[1]["uuid"]} if move else {"emoji_name": "heart"}
    assert api.put(path, json=values).status_code == 405
    assert api.get(path).json()["emoji_name"] == "thumbs_up"
    assert api.delete(path).status_code == 204
    replacement = api.post(
        "/v1/messenger/message_reactions/",
        json={
            "message_uuid": messages[0]["uuid"],
            "emoji_name": "heart",
        },
    )
    assert replacement.status_code == 201, replacement.text


@pytest.mark.parametrize("newer_remote", [False, True])
def test_message_conflicts_use_full_precision_update_time(incoming, newer_remote):
    api, db, account, http, _url = incoming
    original = event()
    apply(incoming, original)
    message = api.get("/v1/messenger/messages/").json()[0]
    updated = api.put(
        "/v1/messenger/messages/" + message["uuid"],
        json={
            "payload": {"kind": "markdown", "content": "Local edit"},
        },
    )
    assert updated.status_code == 200, updated.text
    local_time = datetime.datetime.fromtimestamp(
        original["message"]["timestamp"] + 2, datetime.timezone.utc
    ).replace(tzinfo=None, microsecond=500000)
    db.execute(
        "UPDATE messenger_messages SET content_updated_at=%s WHERE project_id=%s",
        (local_time, account["project_id"]),
    )
    assert (
        db.execute(
            "SELECT count(*) FROM zulip_outgoing_intents WHERE project_id=%s AND entity_type='message' AND status<>'done'",
            (account["project_id"],),
        ).fetchone()[0]
        > 0
    )
    remote_time = original["message"]["timestamp"] + (3 if newer_remote else 2)
    http.request.reset_mock()
    http.request.return_value = {
        "message": {
            **original["message"],
            "content": "Remote edit",
            "last_edit_timestamp": remote_time,
        }
    }
    apply(
        incoming,
        {
            "type": "update_message",
            "message_id": 101,
            "content": "Remote edit",
            "edit_timestamp": remote_time,
        },
    )
    actual = api.get("/v1/messenger/messages/" + message["uuid"]).json()["payload"][
        "content"
    ]
    assert actual == ("Remote edit" if newer_remote else "Local edit")
    if not newer_remote:
        http.request.assert_not_called()


def test_stale_queued_edit_cannot_replace_a_newer_provider_update(incoming):
    api, db, account, http, url = incoming
    original = event()
    apply(incoming, original)
    message = api.get("/v1/messenger/messages/").json()[0]
    assert (
        api.put(
            "/v1/messenger/messages/" + message["uuid"],
            json={
                "payload": {"kind": "markdown", "content": "Old pending edit"},
            },
        ).status_code
        == 200
    )
    with db.cursor(row_factory=rows.dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM zulip_outgoing_intents WHERE project_id=%s AND entity_type='message' AND operation='update'",
            (account["project_id"],),
        )
        intent = cursor.fetchone()
    assert intent
    future = datetime.datetime.now(datetime.timezone.utc).replace(
        tzinfo=None
    ) + datetime.timedelta(seconds=10)
    with db.transaction():
        db.execute("SELECT set_config('workspace.zulip_incoming','on',true)")
        db.execute(
            "UPDATE messenger_messages SET payload='{"
            + '"kind":"markdown","content":"New provider edit"'
            + "}',content_updated_at=%s WHERE project_id=%s",
            (future, account["project_id"]),
        )

    async def prepare():
        database = zulip_outgoing.Database(url)
        await database.open()
        try:
            return await incoming_operations.outgoing_operations.Operations(
                database, http
            ).prepare(intent, account)
        finally:
            await database.close()

    assert asyncio.run(prepare())["kind"] == "noop"


@pytest.mark.parametrize(
    "error,terminal",
    [
        (ValueError("Invalid event data"), True),
        (psycopg.DataError("Invalid provider value"), True),
        (psycopg.errors.NotNullViolation("Missing required provider value"), True),
        (psycopg.errors.CheckViolation("Invalid provider state"), True),
        (
            incoming_operations.outgoing_operations.ApiError(
                422, {"msg": "Invalid data"}
            ),
            True,
        ),
        (
            incoming_operations.outgoing_operations.ApiError(500, {"msg": "Try later"}),
            False,
        ),
        (
            incoming_operations.outgoing_operations.ApiError(
                429, {"msg": "Rate limited"}
            ),
            False,
        ),
        (incoming_operations.outgoing_operations.Pending("Missing dependency"), False),
    ],
)
def test_permanent_event_failure_does_not_block_the_next_event(
    incoming, error, terminal
):
    apply(
        incoming,
        {
            "type": "subscription",
            "op": "add",
            "subscriptions": [{"stream_id": 42, "name": "Channel"}],
        },
    )
    apply(
        incoming,
        {
            "type": "user_topic",
            "stream_id": 42,
            "topic_name": "General",
            "visibility_policy": 0,
        },
    )
    api, _db, account, http, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.operations = incoming_operations.Operations(service.db, http)
        prepare = service.operations.prepare

        async def poison(account, payload):
            if payload.get("invalid"):
                raise error
            return await prepare(account, payload)

        service.operations.prepare = poison
        try:
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    {"queue_id": "permanent"},
                    [
                        {"id": 1, **event(), "invalid": True},
                        {"id": 2, **event("Valid following event")},
                    ],
                )
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
            assert [row["event_id"] for row in ready] == [1]
            await service.process(ready[0])
            saved = (
                await service.db.sql(
                    "SELECT processed_at,error FROM zulip_incoming_events WHERE id=%s",
                    (ready[0]["id"],),
                )
            )[0]
            assert (saved["processed_at"] is not None) is terminal
            assert saved["error"]
            next_events = await service.db.sql(
                zulip_incoming.SELECT_EVENT_SQL, ([], 10)
            )
            if terminal:
                assert (
                    _db.execute(
                        "SELECT status FROM m_external_accounts_v2 WHERE uuid=%s",
                        (account["uuid"],),
                    ).fetchone()[0]
                    == "error"
                )
                assert [row["event_id"] for row in next_events] == [2]
                await service.process(next_events[0])
                assert (
                    api.get("/v1/messenger/messages/").json()[0]["payload"]["content"]
                    == "Valid following event"
                )
            else:
                assert not any(row["event_id"] == 2 for row in next_events)
        finally:
            await service.db.close()

    asyncio.run(run())


@pytest.mark.parametrize("reference", ["none", "file", "upload", "failure"])
def test_incoming_file_removal_cleans_storage_after_commit(
    incoming, monkeypatch, tmp_path, reference
):
    _api, db, account, http, url = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    http.download = mock.AsyncMock(return_value=(b"file", "text/plain"))
    apply(
        incoming,
        {
            "type": "attachment",
            "op": "add",
            "attachment": {"id": 55, "path_id": "1/a/file.txt", "name": "file.txt"},
        },
    )
    apply(incoming, event("[file](/user_uploads/1/a/file.txt)"))
    with db.cursor(row_factory=rows.dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM m_workspace_files WHERE project_id=%s",
            (account["project_id"],),
        )
        file = cursor.fetchone()
    blob = file_storage.get_workspace_file_path(
        file["uuid"], storage_object_id=file["storage_object_id"]
    )
    sidecar = file_storage._get_local_file_path(
        file_storage.get_workspace_file_metadata_object_id(file["uuid"])
    )
    assert blob.exists() and sidecar.exists()
    other = uuid.uuid4()
    if reference == "file":
        with db.transaction():
            db.execute("SELECT set_config('workspace.zulip_incoming','on',true)")
            db.execute(
                "INSERT INTO m_workspace_files SELECT (jsonb_populate_record(NULL::m_workspace_files,to_jsonb(file)||jsonb_build_object('uuid',%s::uuid,'provider_identity',NULL))).* FROM m_workspace_files file WHERE uuid=%s",
                (other, file["uuid"]),
            )
    elif reference == "upload":
        db.execute(
            "INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data) VALUES (%s,'file',%s,'pending-upload',%s,'insert',%s)",
            (
                account["project_id"],
                other,
                account["uuid"],
                incoming_operations.json_value(file),
            ),
        )
    apply(incoming, {"type": "attachment", "op": "remove", "attachment": {"id": 55}})
    # The blob is still present immediately after the database deletion commits.
    assert blob.exists()
    with db.cursor(row_factory=rows.dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM zulip_outgoing_intents WHERE project_id=%s AND entity_type='file' AND entity_uuid=%s AND operation='delete'",
            (account["project_id"], file["uuid"]),
        )
        cleanup = cursor.fetchone()
    assert cleanup
    http.request.reset_mock()

    async def finish():
        database = zulip_outgoing.Database(url)
        await database.open()
        operations = incoming_operations.outgoing_operations.Operations(database, http)
        try:
            prepared = await operations.prepare(cleanup, account)
            result = await operations.execute(cleanup, account, prepared, False)
            await operations.finish(cleanup, account, prepared, result)
        finally:
            await database.close()

    if reference == "upload":
        with pytest.raises(incoming_operations.outgoing_operations.Pending):
            asyncio.run(finish())
        assert blob.exists()
        assert (
            db.execute(
                "SELECT status FROM zulip_outgoing_intents WHERE id=%s",
                (cleanup["id"],),
            ).fetchone()[0]
            != "done"
        )
        db.execute(
            "UPDATE zulip_outgoing_intents SET status='done' WHERE entity_uuid=%s",
            (other,),
        )
    if reference == "failure":
        with monkeypatch.context() as patch:
            patch.setattr(
                file_storage,
                "delete_workspace_file",
                mock.Mock(side_effect=OSError("Storage unavailable")),
            )
            with pytest.raises(OSError):
                asyncio.run(finish())
        assert blob.exists()
        assert (
            db.execute(
                "SELECT status FROM zulip_outgoing_intents WHERE id=%s",
                (cleanup["id"],),
            ).fetchone()[0]
            != "done"
        )
    asyncio.run(finish())
    assert blob.exists() is (reference == "file")
    assert not sidecar.exists()
    http.request.assert_not_called()


async def drain_incoming(service, limit=100):
    for _ in range(limit):
        ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 128))
        if not ready:
            if not await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE processed_at IS NULL"
            ):
                return
            await service.db.sql(
                "UPDATE zulip_incoming_events SET next_retry_at=now() WHERE processed_at IS NULL"
            )
            continue
        await asyncio.gather(*(service.process(row) for row in ready))
        await service.db.sql(
            "UPDATE zulip_incoming_events SET next_retry_at=now() WHERE processed_at IS NULL"
        )
    pytest.fail("Incoming inbox did not drain")


def test_ready_message_does_not_write_locked_topic_binding(incoming):
    api, db, account, http, url = incoming
    apply(incoming, event(identifier=100))
    http.request.reset_mock()

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    {"queue_id": "ready-binding"},
                    [{"id": 1, **event(identifier=101)}],
                )
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
            with psycopg.connect(url) as locker:
                locker.execute(
                    "SELECT 1 FROM messenger_user_topic_bindings WHERE project_id=%s FOR UPDATE",
                    (account["project_id"],),
                )
                await asyncio.wait_for(service.process(ready[0]), 0.5)
            assert await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE id=%s AND processed_at IS NOT NULL",
                (ready[0]["id"],),
            )
        finally:
            await service.db.close()

    asyncio.run(run())
    assert len(api.get("/v1/messenger/messages/").json()) == 2


def test_missing_topic_burst_shares_one_intention_and_removes_it_on_success(incoming):
    api, db, account, http, url = incoming
    apply(incoming, event(identifier=100))
    http.request.reset_mock()

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            values = [
                {"id": index, **event(identifier=200 + index)} for index in range(1, 17)
            ]
            for value in values:
                value["message"]["subject"] = "Missing topic"
            async with service.db.transaction() as connection:
                await service.append(
                    connection, account, {"queue_id": "missing-topic"}, values
                )
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 128))
            await asyncio.gather(*(service.process(row) for row in ready))
            intentions = await service.db.sql(
                "SELECT * FROM zulip_incoming_events WHERE payload->>'type'='ensure_dependency'"
            )
            assert len(intentions) == 1
            assert intentions[0]["payload"]["dependency"]["kind"] == "topic"
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE queue_id='missing-topic' AND processed_at IS NOT NULL"
            )
            await drain_incoming(service)
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE payload->>'type'='ensure_dependency'"
            )
            topics = await service.db.sql(
                "SELECT uuid FROM messenger_topics WHERE project_id=%s AND name='Missing topic'",
                (account["project_id"],),
            )
            assert len(topics) == 1
            assert (
                len(
                    await service.db.sql(
                        "SELECT 1 FROM messenger_user_topic_bindings WHERE topic_uuid=%s",
                        (topics[0]["uuid"],),
                    )
                )
                == 1
            )
            # A later missing dependency can be ensured again after successful deletion of its intention.
            async with service.db.transaction() as connection:
                await connection.execute(zulip_incoming.INCOMING_TRANSACTION_SQL)
                await connection.execute(
                    "DELETE FROM messenger_user_topic_bindings WHERE topic_uuid=%s",
                    (topics[0]["uuid"],),
                )
                value = {"id": 17, **event(identifier=217)}
                value["message"]["subject"] = "Missing topic"
                await service.append(
                    connection, account, {"queue_id": "missing-topic"}, [value]
                )
            await drain_incoming(service)
        finally:
            await service.db.close()

    asyncio.run(run())
    assert len(api.get("/v1/messenger/messages/").json()) == 18
    http.request.assert_not_called()


def test_dependency_chain_uses_dispatcher_without_inline_catalog_creation(incoming):
    api, db, account, http, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    {"queue_id": "dependency-chain"},
                    [{"id": 1, **event()}],
                )
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
            await service.process(ready[0])
            assert not await service.db.sql(
                "SELECT 1 FROM messenger_streams WHERE project_id=%s",
                (account["project_id"],),
            )
            assert not await service.db.sql(
                "SELECT 1 FROM messenger_messages WHERE project_id=%s",
                (account["project_id"],),
            )
            http.request.assert_not_called()
            await drain_incoming(service)
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE payload->>'type'='ensure_dependency'"
            )
        finally:
            await service.db.close()

    asyncio.run(run())
    assert len(api.get("/v1/messenger/messages/").json()) == 1
    http.request.assert_awaited_once()


def test_provider_identity_collisions_do_not_cross_message_or_read_state(incoming):
    api, db, account, http, url = incoming
    other = external_models.ExternalAccount(
        project_id=account["project_id"],
        owner_user_uuid=account["owner_user_uuid"],
        provider="zulip",
        external_account_id="7",
        status="active",
        settings={
            **account["settings"],
            "server_url": "https://other-zulip.example.com",
        },
    )
    other.insert()
    with db.cursor(row_factory=rows.dict_row) as cursor:
        cursor.execute(zulip_incoming.SELECT_ACCOUNT_SQL, (other.uuid,))
        second = cursor.fetchone()
    try:
        apply(incoming, event("First provider"))
        apply((api, db, second, http, url), event("Second provider"))
        apply(
            incoming,
            {
                "type": "update_message_flags",
                "messages": [101],
                "flag": "read",
                "op": "add",
            },
        )
        found = db.execute(
            "SELECT message.provider_uuid,message.payload->>'content',state.read_at IS NOT NULL FROM messenger_messages message JOIN messenger_message_placements placement ON placement.message_uuid=message.uuid AND placement.project_id=message.project_id JOIN messenger_user_message_states state ON state.placement_uuid=placement.uuid AND state.project_id=placement.project_id WHERE message.project_id=%s",
            (account["project_id"],),
        ).fetchall()
        assert set(found) == {
            (account["provider_uuid"], "First provider", True),
            (second["provider_uuid"], "Second provider", False),
        }
        apply(incoming, {"type": "delete_message", "message_id": 101})
        assert [
            row["payload"]["content"]
            for row in api.get("/v1/messenger/messages/").json()
        ] == ["Second provider"]
    finally:
        other.delete()


def test_dependency_enqueue_and_parent_deferral_rollback_together(incoming):
    _api, db, account, http, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        try:
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    {"queue_id": "atomic-intention"},
                    [{"id": 1, **event()}],
                )
            ready = (await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10)))[0]
            with mock.patch.object(
                zulip_incoming,
                "RETRY_EVENT_SQL",
                "SELECT 1/0 FROM (SELECT %s::text,%s::float,%s::bigint) ignored",
            ):
                with pytest.raises(psycopg.errors.DivisionByZero):
                    await service.defer_dependency(
                        ready,
                        account,
                        incoming_operations.MissingDependency(
                            {"kind": "stream", "identity": "42"}
                        ),
                    )
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE payload->>'type'='ensure_dependency'"
            )
            assert (
                await service.db.sql(
                    "SELECT attempts FROM zulip_incoming_events WHERE id=%s",
                    (ready["id"],),
                )
            )[0]["attempts"] == 0
        finally:
            await service.db.close()

    asyncio.run(run())


def test_dependency_creation_and_intention_removal_rollback_together(incoming):
    _api, db, account, http, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    {"queue_id": "atomic-creation"},
                    [{"id": 1, **event()}],
                )
            parent = (await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10)))[
                0
            ]
            await service.process(parent)
            intention = (
                await service.db.sql(
                    "SELECT * FROM zulip_incoming_events WHERE payload->>'type'='ensure_dependency'"
                )
            )[0]
            original = service.operations.ensure_dependency

            async def fail_after_creation(*args):
                await original(*args)
                raise psycopg.OperationalError("Connection lost before completion")

            with mock.patch.object(
                service.operations, "ensure_dependency", fail_after_creation
            ):
                await service.process(intention)
            assert not await service.db.sql(
                "SELECT 1 FROM messenger_streams WHERE project_id=%s",
                (account["project_id"],),
            )
            assert await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE id=%s AND processed_at IS NULL",
                (intention["id"],),
            )
            await service.db.sql(
                "UPDATE zulip_incoming_events SET next_retry_at=now() WHERE id=%s",
                (intention["id"],),
            )
            await drain_incoming(service)
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE payload->>'type'='ensure_dependency'"
            )
        finally:
            await service.db.close()

    asyncio.run(run())


def test_permanent_dependency_failure_is_visible_without_retry_amplification(incoming):
    api, db, account, http, url = incoming
    apply(incoming, event(identifier=100))

    async def reply(account, method, path, *args, **kwargs):
        assert path == "/users/8"
        raise incoming_operations.outgoing_operations.ApiError(
            404, {"msg": "Unknown user"}
        )

    http.request.side_effect = reply
    http.request.reset_mock()

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.operations = incoming_operations.Operations(service.db, http)
        unknown = {"id": 1, **event("Unknown sender", 101)}
        unknown["message"]["sender_id"] = 8
        try:
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    {"queue_id": "terminal-dependency"},
                    [unknown, {"id": 2, **event("Valid event", 102)}],
                )
            await drain_incoming(service)
            failures = await service.db.sql(
                "SELECT payload,error FROM zulip_incoming_events WHERE error IS NOT NULL"
            )
            assert len(failures) == 2
            assert {row["payload"]["type"] for row in failures} == {
                "message",
                "ensure_dependency",
            }
            assert all(row["error"] for row in failures)
        finally:
            await service.db.close()

    asyncio.run(run())
    http.request.assert_awaited_once()
    assert {
        row["payload"]["content"] for row in api.get("/v1/messenger/messages/").json()
    } == {"Hello", "Valid event"}


def test_permanent_non_message_failure_releases_the_account_lane(incoming):
    api, db, account, http, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.operations = incoming_operations.Operations(service.db, http)
        original = service.operations.prepare

        async def prepare(account, payload):
            if payload["type"] == "stream":
                raise ValueError("Invalid catalog event")
            return await original(account, payload)

        service.operations.prepare = prepare
        try:
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    {"queue_id": "non-message-error"},
                    [
                        {"id": 1, "type": "stream", "op": "invalid"},
                        {
                            "id": 2,
                            "type": "subscription",
                            "op": "add",
                            "subscriptions": [
                                {"stream_id": 42, "name": "Valid catalog event"}
                            ],
                        },
                    ],
                )
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
            assert [row["event_id"] for row in ready] == [1]
            await service.process(ready[0])
            ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 10))
            assert [row["event_id"] for row in ready] == [2]
            await service.process(ready[0])
            assert await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE event_id=1 AND processed_at IS NOT NULL AND error IS NOT NULL"
            )
            assert await service.db.sql(
                "SELECT 1 FROM zulip_incoming_events WHERE event_id=2 AND processed_at IS NOT NULL AND error IS NULL"
            )
        finally:
            await service.db.close()

    asyncio.run(run())
    assert api.get("/v1/messenger/streams/").json()[0]["name"] == "Valid catalog event"
