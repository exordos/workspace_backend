# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import datetime
import uuid
from unittest import mock

from psycopg import rows
import pytest
from restalchemy.common import contexts

from workspace.bridges.zulip import incoming_operations
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
                prepared = await operations.prepare(account, value)
                async with database.transaction() as connection:
                    await connection.execute(zulip_incoming.INCOMING_TRANSACTION_SQL)
                    await operations.apply(connection, account, prepared)
        finally:
            await database.close()

    asyncio.run(run())


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
        prepared = await operations.prepare(account, event())
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
def test_user_topic_and_message_echo_complete_pending_topic_identity(
    incoming, missing_binding
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
