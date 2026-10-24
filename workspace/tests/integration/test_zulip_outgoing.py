# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import asyncio
import contextlib
import datetime
import importlib.util
from unittest import mock

import aiohttp
from aiohttp import web
from psycopg.types import json as pg_json

from workspace.services import zulip_outgoing
from workspace.bridges.zulip import outgoing_operations
from workspace.bridges.zulip import message_conversion
import uuid
import types

import pytest
import psycopg
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import v2_models
from workspace.tests.integration import conftest


@pytest.fixture
def outgoing(api, db, monkeypatch):
    api.project_id = uuid.UUID(api.project_id)
    api.user_uuid = uuid.UUID(api.user_uuid)
    stream_uuid = uuid.UUID(
        conftest.seed_user_stream(db, api.project_id, api.user_uuid, "Channel")
    )
    topic = v2_models.WorkspaceStreamTopic(
        project_id=api.project_id, stream_uuid=stream_uuid, name="General"
    )
    topic.insert()
    v2_models.WorkspaceUserTopicBinding(
        project_id=api.project_id, user_uuid=api.user_uuid, topic_uuid=topic.uuid
    ).insert()
    db.execute(
        "UPDATE messenger_streams SET default_topic_uuid=%s WHERE project_id=%s AND uuid=%s",
        (topic.uuid, api.project_id, stream_uuid),
    )
    account = external_models.ExternalAccount(
        project_id=api.project_id,
        owner_user_uuid=api.user_uuid,
        provider="zulip",
        external_account_id="7",
        status="active",
        settings={
            "server_url": "https://zulip.example.com",
            "email": "sender@example.com",
            "api_key": "sender-key",
        },
    )
    account.insert()
    db.execute(
        "UPDATE messenger_streams SET source_name='zulip',provider_uuid=%s,provider_identity='42' WHERE project_id=%s AND uuid=%s",
        (account.provider_uuid, api.project_id, stream_uuid),
    )
    for table in ("messenger_topics", "messenger_stream_bindings"):
        db.execute(
            f"UPDATE {table} SET source_name='zulip',provider_uuid=%s,provider_identity='42:General' WHERE project_id=%s AND stream_uuid=%s",
            (account.provider_uuid, api.project_id, stream_uuid),
        )
    db.execute(
        "UPDATE messenger_user_topic_bindings SET source_name='zulip',provider_uuid=%s,provider_identity='42:General:7' WHERE project_id=%s AND topic_uuid=%s",
        (account.provider_uuid, api.project_id, topic.uuid),
    )
    channel = types.SimpleNamespace(
        uuid=uuid.uuid4(),
        date_created=None,
        creator_id=None,
        message_retention_days=None,
        external_account_uuid=account.uuid,
        stream_uuid=stream_uuid,
        stream_id=42,
        name="Channel",
        description="",
        rendered_description="",
        invite_only=False,
        is_web_public=False,
        history_public_to_subscribers=True,
        is_archived=False,
    )
    conftest.seed_legacy_zulip_channel(db, channel)
    if db.execute("SELECT to_regclass('zulip_incoming_subscriptions')").fetchone()[0]:
        db.execute(
            """INSERT INTO zulip_incoming_subscriptions
            (external_account_uuid,project_id,provider_uuid,owner_user_uuid,account_identity,
             server_url,queue_id,last_event_id,longpoll_timeout)
            VALUES (%s,%s,%s,%s,'7',%s,'sender-queue',-1,90)""",
            (
                account.uuid,
                account.project_id,
                account.provider_uuid,
                account.owner_user_uuid,
                account.settings["server_url"],
            ),
        )
    else:
        db.execute(
            """INSERT INTO bridge_zulip_v5_subscriptions
            (uuid,project_id,external_account_uuid,generation,queue_id,last_event_id,account_revision,longpoll_timeout)
            VALUES (%s,%s,%s,%s,'sender-queue',-1,'published-schema-test',90)""",
            (uuid.uuid4(), account.project_id, account.uuid, uuid.uuid4()),
        )
    remote = {
        "id": 12345,
        "sender_id": 7,
        "flags": [],
        "is_me_message": False,
        "timestamp": int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 10,
        "content": "Raw **message**",
        "type": "stream",
        "stream_id": 42,
        "subject": "General",
    }
    zulip = mock.Mock()
    zulip.send_message.return_value = remote["id"]
    zulip.get_messages.side_effect = lambda first, last: [dict(remote)]
    constructor = mock.Mock(return_value=zulip)
    yield api, account, channel, remote, zulip, constructor
    for message in v2_models.WorkspaceMessage.objects.get_all(
        filters={"project_id": filters.EQ(api.project_id)}
    ):
        message.delete()
    account.delete()
    for stream in v2_models.WorkspaceStream.objects.get_all(
        filters={"project_id": filters.EQ(api.project_id)}
    ):
        stream.delete()
    for table in (
        "bridge_zulip_v5_topic_changes",
        "bridge_zulip_v5_message_changes",
        "bridge_zulip_v5_message_counter_changes",
        "messenger_message_changes",
        "messenger_user_message_counter_changes",
    ):
        if db.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]:
            db.execute(f"DELETE FROM {table} WHERE project_id=%s", (api.project_id,))


def create(outgoing, canonical_uuid=None):
    api, _account, channel, remote, _zulip, _constructor = outgoing
    values = {
        "stream_uuid": str(channel.stream_uuid),
        "payload": {"kind": "markdown", "content": remote["content"]},
    }
    if canonical_uuid is not None:
        values["uuid"] = str(canonical_uuid)
    response = api.post(
        "/v1/messenger/messages/",
        json=values,
    )
    assert response.status_code == 201, response.text
    return response.json()


class Server:
    def __init__(self, db_url):
        self.db_url = db_url
        self.calls = []
        self.messages = {}
        self.attachments = []
        self.events = []
        self.next_id = 100
        self.lose_response = False
        self.gate = None
        self.active = 0
        self.maximum = 0
        self.transports = set()

    async def handle(self, http):
        data = dict(http.query) if http.method == "GET" else dict(await http.post())
        self.calls.append((http.method, http.path, data))
        self.transports.add(http.transport)
        assert (
            http.headers["Authorization"]
            == aiohttp.BasicAuth("sender@example.com", "sender-key").encode()
        )
        with psycopg.connect(self.db_url) as connection:
            assert not connection.execute(
                "SELECT 1 FROM pg_stat_activity WHERE application_name='workspace-zulip-outgoing' AND state='idle in transaction'"
            ).fetchall()
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            path = http.path.removeprefix("/api/v1")
            if path == "/attachments":
                return web.json_response(
                    {"result": "success", "attachments": self.attachments}
                )
            if path.startswith("/attachments/"):
                self.attachments = []
                return web.json_response({"result": "success"})
            if path == "/user_uploads":
                upload = data["file"]
                attachment = {
                    "id": len(self.attachments) + 1,
                    "name": upload.filename,
                    "path_id": f"1/abc/{upload.filename}",
                }
                self.attachments.append(attachment)
                return web.json_response(
                    {
                        "result": "success",
                        "uri": "/user_uploads/" + attachment["path_id"],
                    }
                )
            if path == "/messages" and http.method == "GET":
                return web.json_response(
                    {"result": "success", "messages": list(self.messages.values())}
                )
            if path == "/messages/flags":
                return web.json_response({"result": "success"})
            if path == "/events":
                return web.json_response({"result": "success", "events": self.events})
            if path == "/messages" and http.method == "POST":
                self.next_id += 1
                remote = self.next_id
                self.messages[remote] = {
                    "id": remote,
                    "content": data["content"],
                    "flags": [],
                    "reactions": [],
                }
                self.events.append(
                    {
                        "id": len(self.events),
                        "type": "message",
                        "local_message_id": data["local_id"],
                        "message": self.messages[remote],
                    }
                )
                if self.lose_response:
                    http.transport.close()
                    return web.Response()
                return web.json_response({"result": "success", "id": remote})
            if path.startswith("/messages/"):
                remote = int(path.split("/")[2])
                if remote not in self.messages:
                    return web.json_response(
                        {"result": "error", "msg": "Not found"}, status=404
                    )
                if path.endswith("/reactions"):
                    reaction = {**data, "user_id": 7}
                    if http.method == "POST":
                        self.messages[remote]["reactions"].append(reaction)
                    else:
                        self.messages[remote]["reactions"] = []
                elif http.method == "DELETE":
                    del self.messages[remote]
                elif http.method == "PATCH":
                    self.messages[remote].update(data)
                return web.json_response(
                    {"result": "success", "message": self.messages.get(remote)}
                )
            if path == "/messages/flags":
                return web.json_response({"result": "success"})
            return web.json_response({"result": "success"})
        finally:
            self.active -= 1


@contextlib.asynccontextmanager
async def sender(account, db_url, **options):
    server = Server(db_url)
    app = web.Application()
    app.router.add_route("*", "/api/v1/{path:.*}", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    with psycopg.connect(db_url, autocommit=True) as connection:
        connection.execute(
            "UPDATE m_external_accounts_v2 SET settings=jsonb_set(settings,'{server_url}',%s) WHERE uuid=%s",
            (pg_json.Jsonb(f"http://127.0.0.1:{port}"), account.uuid),
        )
    service = zulip_outgoing.ZulipOutgoingService(db_url, **options)
    await service.db.open()
    service.http = zulip_outgoing.Http(options.get("connections", 128), keepalive=0.05)
    service.operations = outgoing_operations.Operations(service.db, service.http)
    try:
        yield service, server
    finally:
        if server.gate is not None:
            server.gate.set()
        for task in list(service.tasks.values()):
            task.cancel()
        await asyncio.gather(*service.tasks.values(), return_exceptions=True)
        await service.http.close()
        await service.db.close()
        await runner.cleanup()


async def drain(service):
    for _ in range(100):
        await service.dispatch()
        if service.tasks:
            await asyncio.wait_for(asyncio.gather(*list(service.tasks.values())), 2)
            await asyncio.sleep(0)
        rows = await service.db.sql(
            "SELECT status,error FROM zulip_outgoing_intents WHERE status<>'done'"
        )
        if not rows:
            return
        assert not any(row["status"] == "error" for row in rows), rows
        await service.db.sql(
            "UPDATE zulip_outgoing_intents SET next_attempt_at=now() WHERE next_attempt_at>now()"
        )
    pytest.fail(f"Outgoing intents did not drain: {rows}")


def intents(db, account):
    return db.execute(
        "SELECT entity_type,operation,status,data FROM zulip_outgoing_intents WHERE external_account_uuid=%s ORDER BY id",
        (account.uuid,),
    ).fetchall()


def test_api_intent_is_atomic_and_receipt_updates_canonical_identity(
    outgoing, db, _test_database_url
):
    api, account, channel, *_ = outgoing
    public = create(outgoing)
    rows = intents(db, account)
    assert [(row[0], row[1]) for row in rows] == [("message", "insert")]
    assert rows[0][3]["topic_name"] == "General"

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            await drain(service)
            assert (
                len(
                    [
                        call
                        for call in server.calls
                        if call[:2] == ("POST", "/api/v1/messages")
                    ]
                )
                == 1
            )

    asyncio.run(run())
    canonical = (
        db.execute(
            "SELECT source_name,provider_identity FROM messenger_messages WHERE uuid=%s",
            (public["canonical_message_uuid"],),
        ).fetchone()
        if "canonical_message_uuid" in public
        else db.execute(
            "SELECT message.source_name,message.provider_identity FROM messenger_messages message JOIN messenger_message_placements placement ON placement.message_uuid=message.uuid WHERE placement.uuid=%s",
            (public["uuid"],),
        ).fetchone()
    )
    assert canonical == ("zulip", "101")
    assert not db.execute(
        "SELECT 1 FROM zulip_entity_links WHERE entity_type='message'"
    ).fetchone()


def test_every_edit_is_sent_in_order(outgoing, db, _test_database_url):
    api, account, *_ = outgoing
    public = create(outgoing)
    for content in ("second", "third"):
        response = api.put(
            f"/v1/messenger/messages/{public['uuid']}",
            json={"payload": {"kind": "markdown", "content": content}},
        )
        assert response.status_code == 200, response.text

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            await drain(service)
            assert [
                data["content"]
                for method, path, data in server.calls
                if method in ("POST", "PATCH")
            ] == ["Raw **message**", "second", "third"]

    asyncio.run(run())


def test_reaction_waits_for_message_id(outgoing, db, _test_database_url):
    api, account, *_ = outgoing
    public = create(outgoing)
    response = api.post(
        "/v1/messenger/message_reactions/",
        json={"message_uuid": public["uuid"], "emoji_name": "thumbs_up"},
    )
    assert response.status_code == 201, response.text

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            server.gate = asyncio.Event()
            await service.dispatch()
            await asyncio.sleep(0.04)
            assert service.tasks
            assert not any(path.endswith("/reactions") for _, path, _ in server.calls)
            server.gate.set()
            await drain(service)
            assert server.calls[-1][1].endswith("/101/reactions")

    asyncio.run(run())


def test_independent_messages_run_concurrently_and_reuse_connections(
    outgoing, db, _test_database_url
):
    _api, account, *_ = outgoing
    for _ in range(4):
        create(outgoing)

    async def run():
        async with sender(account, _test_database_url, connections=2) as (
            service,
            server,
        ):
            server.gate = asyncio.Event()
            await service.dispatch()
            for _ in range(100):
                if server.active == 2:
                    break
                await asyncio.sleep(0.01)
            assert server.active == 2
            server.gate.set()
            await drain(service)
            assert server.maximum == 2
            assert len(server.transports) == 2
            await asyncio.sleep(0.15)
            assert not service.http.connector._conns

    asyncio.run(run())


def test_lost_non_idempotent_response_is_verified_by_get(
    outgoing, db, _test_database_url
):
    _api, account, *_ = outgoing
    create(outgoing)

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            server.lose_response = True
            await drain(service)
            methods = [
                method for method, path, _ in server.calls if path == "/api/v1/messages"
            ]
            assert methods == ["POST"]
            assert any(
                method == "GET" and path.endswith("/events")
                for method, path, _ in server.calls
            )
            assert any(
                method == "GET" and path.endswith("/messages/101")
                for method, path, _ in server.calls
            )

    asyncio.run(run())


def test_delete_404_uses_snapshot_after_local_root_is_removed(
    outgoing, db, _test_database_url
):
    api, account, *_ = outgoing
    public = create(outgoing)

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            await drain(service)
            assert (
                api.delete(f"/v1/messenger/messages/{public['uuid']}").status_code
                == 204
            )
            with psycopg.connect(_test_database_url, autocommit=True) as connection:
                connection.execute(
                    "DELETE FROM messenger_messages WHERE project_id=%s",
                    (account.project_id,),
                )
            server.messages.clear()
            await drain(service)
            assert server.calls[-1][:2] == ("DELETE", "/api/v1/messages/101")

    asyncio.run(run())


def test_import_writes_and_rolled_back_api_writes_do_not_enqueue(outgoing, db):
    _api, account, channel, *_ = outgoing
    with contexts.Context().session_manager() as session:
        session.execute(
            "UPDATE messenger_streams SET name='Imported rename' WHERE uuid=%s",
            (channel.stream_uuid,),
        )
    assert intents(db, account) == []
    with pytest.raises(RuntimeError):
        with contexts.Context().session_manager() as session:
            session.execute(
                "SELECT set_config('workspace.outgoing_actor',%s,true)",
                (str(account.owner_user_uuid),),
            )
            session.execute(
                "UPDATE messenger_streams SET name='Local rename' WHERE uuid=%s",
                (channel.stream_uuid,),
            )
            raise RuntimeError("rollback")
    assert intents(db, account) == []


def test_other_api_entities_capture_only_provider_operations(
    outgoing, db, _test_database_url
):
    api, account, channel, *_ = outgoing
    public = create(outgoing)
    topics = api.get("/v1/messenger/stream_topics/").json()
    topic = next(
        row for row in topics if row["stream_uuid"] == str(channel.stream_uuid)
    )
    requests = [
        ("put", f"/v1/messenger/streams/{channel.stream_uuid}", {"name": "Renamed"}),
        (
            "put",
            f"/v1/messenger/stream_topics/{topic['uuid']}",
            {"name": "Renamed topic"},
        ),
        (
            "post",
            f"/v1/messenger/streams/{channel.stream_uuid}/actions/notifications/invoke",
            {"notification_mode": "muted"},
        ),
        (
            "post",
            f"/v1/messenger/stream_topics/{topic['uuid']}/actions/notifications/invoke",
            {"notification_mode": "follow"},
        ),
        ("post", f"/v1/messenger/messages/{public['uuid']}/actions/star/invoke", {}),
    ]
    for method, path, data in requests:
        response = getattr(api, method)(path, json=data)
        assert response.status_code in (200, 201, 204), response.text
    kinds = [row[0] for row in intents(db, account)]
    assert kinds == [
        "message",
        "stream",
        "topic",
        "stream_binding",
        "topic_binding",
        "message_state",
    ]
    count = len(kinds)
    response = api.put(
        f"/v1/messenger/streams/{channel.stream_uuid}", json={"color": 123}
    )
    assert response.status_code == 200, response.text
    assert len(intents(db, account)) == count
    assert db.execute(
        "SELECT notification_mode FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
        (account.project_id, channel.stream_uuid, account.owner_user_uuid),
    ).fetchone() == ("muted",)

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            await drain(service)
            sent = [
                (method, path, data)
                for method, path, data in server.calls
                if method != "GET"
            ]
            assert any(
                method == "PATCH" and path == "/api/v1/streams/42"
                for method, path, _ in sent
            )
            assert any(
                path == "/api/v1/user_topics" and data["visibility_policy"] == "3"
                for _, path, data in sent
            )
            assert any(
                path == "/api/v1/messages/flags" and data["flag"] == "starred"
                for _, path, data in sent
            )

    asyncio.run(run())


def test_pending_file_upload_survives_api_deletion(
    outgoing, db, _test_database_url, tmp_path, monkeypatch
):
    api, account, channel, *_ = outgoing
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    response = api.post(
        "/v1/messenger/files/",
        data={"stream_uuid": str(channel.stream_uuid)},
        files={"file": ("example.txt", b"content", "text/plain")},
    )
    assert response.status_code == 201, response.text
    file = response.json()
    assert api.delete(f"/v1/messenger/files/{file['uuid']}").status_code == 204
    rows = [row for row in intents(db, account) if row[0] == "file"]
    assert [row[1] for row in rows] == ["insert", "delete"]
    from workspace.messenger_api import file_storage

    assert (
        file_storage.read_workspace_file(
            uuid.UUID(file["uuid"]),
            storage_type=rows[0][3]["storage_type"],
            storage_object_id=rows[0][3]["storage_object_id"],
        )
        == b"content"
    )

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            await drain(service)
            assert [
                method
                for method, path, _ in server.calls
                if path == "/api/v1/user_uploads"
            ] == ["POST"]
            assert server.attachments == []

    asyncio.run(run())
    with pytest.raises(FileNotFoundError):
        file_storage.read_workspace_file(
            uuid.UUID(file["uuid"]),
            storage_type=rows[0][3]["storage_type"],
            storage_object_id=rows[0][3]["storage_object_id"],
        )


def test_restart_verifies_prepared_request_and_keeps_following_changes_blocked(
    outgoing, db, _test_database_url
):
    api, account, *_ = outgoing
    public = create(outgoing)
    assert (
        api.put(
            f"/v1/messenger/messages/{public['uuid']}",
            json={"payload": {"kind": "markdown", "content": "later"}},
        ).status_code
        == 200
    )

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            rows = await service.db.sql(
                "SELECT * FROM zulip_outgoing_intents ORDER BY id"
            )
            account_row = (
                await service.db.sql(
                    "SELECT account.*,provider.hostname FROM m_external_accounts_v2 account JOIN messenger_providers provider ON provider.uuid=account.provider_uuid WHERE account.uuid=%s",
                    (account.uuid,),
                )
            )[0]
            prepared = await service.operations.prepare(rows[0], account_row)
            await service.db.sql(
                "UPDATE zulip_outgoing_intents SET request=%s,status='checking' WHERE id=%s",
                (pg_json.Jsonb(prepared), rows[0]["id"]),
            )
            await service.dispatch()
            await asyncio.gather(*list(service.tasks.values()))
            assert not any(method == "POST" for method, _, _ in server.calls)
            assert (
                await service.db.sql(
                    "SELECT status FROM zulip_outgoing_intents ORDER BY id"
                )
            ) == [{"status": "checking"}, {"status": "new"}]

    asyncio.run(run())


def test_attachment_dependency_added_after_message_does_not_starve(
    outgoing, db, _test_database_url, tmp_path, monkeypatch
):
    api, account, channel, *_ = outgoing
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    response = api.post(
        "/v1/messenger/files/",
        data={"stream_uuid": str(channel.stream_uuid)},
        files={"file": ("example.txt", b"content", "text/plain")},
    )
    assert response.status_code == 201, response.text
    file = response.json()
    assert [row[0] for row in intents(db, account)] == ["file"]
    for _ in range(2):
        response = api.post(
            "/v1/messenger/messages/",
            json={
                "stream_uuid": str(channel.stream_uuid),
                "payload": {
                    "kind": "markdown",
                    "content": f"[file](urn:file:{file['uuid']})",
                },
            },
        )
        assert response.status_code == 201, response.text

    async def run():
        async with sender(account, _test_database_url, concurrency=1) as (
            service,
            server,
        ):
            await drain(service)
            assert (
                len(
                    [
                        call
                        for call in server.calls
                        if call[:2] == ("POST", "/api/v1/user_uploads")
                    ]
                )
                == 1
            )
            contents = [
                data["content"]
                for method, path, data in server.calls
                if method == "POST" and path == "/api/v1/messages"
            ]
            assert len(contents) == 2 and all(
                "/user_uploads/" in text and "urn:file:" not in text
                for text in contents
            )

    asyncio.run(run())


def test_create_then_delete_executes_from_snapshots_after_root_cleanup(
    outgoing, db, _test_database_url
):
    api, account, *_ = outgoing
    public = create(outgoing)
    assert api.delete(f"/v1/messenger/messages/{public['uuid']}").status_code == 204
    db.execute(
        "DELETE FROM messenger_messages WHERE project_id=%s", (account.project_id,)
    )

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            await drain(service)
            assert [(method, path) for method, path, _ in server.calls] == [
                ("POST", "/api/v1/messages"),
                ("DELETE", "/api/v1/messages/101"),
            ]

    asyncio.run(run())


def seed_delivery(account, placement, message):
    """Persist a known copy when testing inbound observations without HTTP."""
    topic = v2_models.WorkspaceStreamTopic.objects.get_one(
        filters={
            "project_id": filters.EQ(account.project_id),
            "uuid": filters.EQ(placement.topic_uuid),
        }
    )
    session = contexts.Context().get_session()
    row = session.execute(
        outgoing_operations.UPSERT_SENDING_MESSAGE_SQL,
        (
            uuid.uuid4(),
            account.project_id,
            message.uuid,
            placement.stream_uuid,
            account.uuid,
            account.get_host(),
            account.external_account_id,
            pg_json.Jsonb({"to": 42}),
            topic.name,
        ),
    ).fetchone()
    delivery = types.SimpleNamespace(uuid=row["uuid"])
    return delivery


def export(account, payload, stream_uuid, zulip):
    """Exercise SQL conversion inside a test's existing fixture transaction."""
    session = contexts.Context().get_session()

    class Database:
        async def sql(self, query, parameters=()):
            cursor = session.execute(query, parameters)
            return cursor.fetchall() if cursor.description else []

    class Http:
        async def request(self, _account, method, path, data=None, *, file=None):
            if path == "/attachments":
                return {"attachments": []}
            if path == "/user_uploads":
                return {"uri": zulip.upload_file(*file)}
            remote = int(path.rsplit("/", 1)[1])
            messages = zulip.get_messages(remote, remote)
            return {"message": next(row for row in messages if row["id"] == remote)}

    async def run():
        db = Database()
        account_data = (
            await db.sql(
                "SELECT account.*,provider.hostname FROM m_external_accounts_v2 account JOIN messenger_providers provider ON provider.uuid=account.provider_uuid WHERE account.uuid=%s",
                (account.uuid,),
            )
        )[0]
        operations = outgoing_operations.Operations(db, Http())
        intent = {
            "id": 0,
            "uuid": uuid.uuid4(),
            "entity_uuid": uuid.uuid4(),
            "entity_type": "message",
            "project_id": account.project_id,
            "data": {"payload": payload, "stream_uuid": stream_uuid},
        }
        for _ in range(10):
            try:
                return await operations.convert(intent, account_data)
            except outgoing_operations.Pending:
                files = await db.sql(
                    "SELECT * FROM zulip_outgoing_intents WHERE entity_type='file' AND status='new' AND entity_uuid=ANY(%s)",
                    (
                        [
                            uuid.UUID(urn.split(":", 2)[2].partition("?")[0])
                            for urn in message_conversion.get_workspace_files(
                                payload, include_quotes=True
                            )
                        ],
                    ),
                )
                if not files:
                    raise
                for file_intent in files:
                    prepared = await operations.prepare(file_intent, account_data)
                    result = await operations.execute(
                        file_intent, account_data, prepared, False
                    )
                    await operations.finish(file_intent, account_data, prepared, result)
        pytest.fail("Export dependencies did not converge")

    return asyncio.run(run())


@pytest.mark.pre_outgoing_schema
def test_cutover_preserves_queued_and_unscheduled_changes(outgoing, db):
    api, account, channel, *_ = outgoing
    seeded = []
    for _ in range(2):
        message_uuid, placement_uuid = uuid.uuid4(), uuid.uuid4()
        topic_uuid = db.execute(
            "SELECT default_topic_uuid FROM messenger_streams WHERE uuid=%s",
            (channel.stream_uuid,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload) VALUES(%s,%s,%s,%s)",
            (
                message_uuid,
                account.project_id,
                api.user_uuid,
                pg_json.Jsonb({"kind": "markdown", "content": "Before cutover"}),
            ),
        )
        db.execute(
            "INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid) VALUES(%s,%s,%s,%s,%s)",
            (
                placement_uuid,
                account.project_id,
                message_uuid,
                channel.stream_uuid,
                topic_uuid,
            ),
        )
        db.execute(
            "INSERT INTO messenger_user_message_bindings(uuid,project_id,placement_uuid,user_uuid,membership_generation,relation_role,visibility,permissions) VALUES(%s,%s,%s,%s,1,'author','visible',jsonb_build_object('read',true,'react',true,'star',true,'pin',true))",
            (
                uuid.uuid5(placement_uuid, str(api.user_uuid)),
                account.project_id,
                placement_uuid,
                api.user_uuid,
            ),
        )
        db.execute(
            "INSERT INTO messenger_user_message_states(uuid,project_id,placement_uuid,user_uuid,starred,membership_generation) VALUES(%s,%s,%s,%s,false,1)",
            (
                uuid.uuid5(placement_uuid, str(api.user_uuid)),
                account.project_id,
                placement_uuid,
                api.user_uuid,
            ),
        )
        seeded.append({"uuid": str(placement_uuid)})
    first, second = seeded
    with contexts.Context().session_manager():
        copies = []
        for public in (first, second):
            placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
                filters={
                    "uuid": filters.EQ(uuid.UUID(public["uuid"])),
                    "project_id": filters.EQ(account.project_id),
                }
            )
            message = v2_models.WorkspaceMessage.objects.get_one(
                filters={
                    "uuid": filters.EQ(placement.message_uuid),
                    "project_id": filters.EQ(account.project_id),
                }
            )
            copies.append(seed_delivery(account, placement, message))
        contexts.Context().get_session().execute(
            "UPDATE bridge_zulip_v5_deliveries SET sending=true WHERE uuid=%s",
            (copies[0].uuid,),
        )
        contexts.Context().get_session().execute(
            "UPDATE bridge_zulip_v5_deliveries SET zulip_message_id=123 WHERE uuid=%s",
            (copies[1].uuid,),
        )
    assert (
        api.post(
            f"/v1/messenger/messages/{second['uuid']}/actions/star/invoke"
        ).status_code
        == 200
    )
    reaction = api.post(
        "/v1/messenger/message_reactions/",
        json={"message_uuid": second["uuid"], "emoji_name": "thumbs_up"},
    )
    assert reaction.status_code == 201, reaction.text
    old_uuid = uuid.uuid4()
    for task_uuid, content in (
        (
            old_uuid,
            {
                "kind": "send_message",
                "delivery_uuid": str(copies[0].uuid),
                "content": "before cutover",
            },
        ),
        (
            uuid.uuid4(),
            {
                "kind": "delete_message",
                "message_id": 999,
                "hostname": account.get_host(),
                "external_account_id": account.external_account_id,
                "author_uuid": str(account.owner_user_uuid),
            },
        ),
    ):
        db.execute(
            "INSERT INTO bridge_zulip_v5_tasks(uuid,project_id,external_account_uuid,priority,status,content) VALUES(%s,%s,%s,100,'new',%s)",
            (task_uuid, account.project_id, account.uuid, pg_json.Jsonb(content)),
        )
    path = next(conftest.MIGRATIONS_DIR.glob("0265-*.py"))
    spec = importlib.util.spec_from_file_location("outgoing_cutover", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with db.transaction():
        db.execute(module.CAPTURE)
        db.execute(
            "CREATE TEMP TABLE zulip_choice_codes(name text,code text) ON COMMIT DROP"
        )
        db.execute(module.CUTOVER)
    assert db.execute(
        "SELECT status,request->>'delivery_uuid',request->'data'->>'queue_id' FROM zulip_outgoing_intents WHERE uuid=%s",
        (old_uuid,),
    ).fetchone() == ("checking", str(copies[0].uuid), "sender-queue")
    assert db.execute(
        "SELECT 1 FROM zulip_outgoing_intents WHERE entity_type='message_state' AND data->>'starred'='true'"
    ).fetchone()
    assert db.execute(
        "SELECT 1 FROM zulip_outgoing_intents WHERE entity_type='reaction' AND operation='insert'"
    ).fetchone()
    assert db.execute(
        "SELECT 1 FROM zulip_outgoing_intents WHERE operation='delete' AND data->>'remote_identity'='999'"
    ).fetchone()
    assert db.execute(
        "SELECT to_regclass('bridge_zulip_v5_workspace_events'),to_regclass('bridge_zulip_v5_reaction_intents')"
    ).fetchone() == (None, None)
    assert (
        db.execute(
            "SELECT 1 FROM bridge_zulip_v5_tasks WHERE content->>'kind' IN ('send_message','delete_message','update_message_flags','update_reaction')"
        ).fetchone()
        is None
    )


def test_copy_identity_stays_with_the_original_provider(
    outgoing, db, _test_database_url
):
    _api, account, *_ = outgoing
    public = create(outgoing)

    async def run():
        async with sender(account, _test_database_url) as (service, _server):
            await drain(service)
            provider_uuid = uuid.uuid4()
            with psycopg.connect(_test_database_url, autocommit=True) as connection:
                connection.execute(
                    "INSERT INTO messenger_providers(uuid,hostname) VALUES(%s,%s)",
                    (provider_uuid, f"{provider_uuid}.example.com"),
                )
                connection.execute(
                    "UPDATE m_external_accounts_v2 SET provider_uuid=%s WHERE uuid=%s",
                    (provider_uuid, account.uuid),
                )
            account_row = (
                await service.db.sql(
                    "SELECT * FROM m_external_accounts_v2 WHERE uuid=%s",
                    (account.uuid,),
                )
            )[0]
            message = (
                await service.db.sql(
                    "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
                    (public["uuid"],),
                )
            )[0]["message_uuid"]
            with pytest.raises(outgoing_operations.Pending):
                await service.operations.identity(
                    {"project_id": account.project_id}, account_row, "message", message
                )

    asyncio.run(run())


def test_zulip_children_inherit_provider_before_receipt(
    outgoing, db, monkeypatch, tmp_path
):
    api, account, channel, *_ = outgoing
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    response = api.post(
        "/v1/messenger/stream_topics/",
        json={
            "stream_uuid": str(channel.stream_uuid),
            "name": "Pending topic",
        },
    )
    assert response.status_code == 201, response.text
    topic = response.json()
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": str(channel.stream_uuid),
            "topic_uuid": topic["uuid"],
            "payload": {"kind": "markdown", "content": "Pending message"},
        },
    )
    assert response.status_code == 201, response.text
    message = response.json()
    reaction = api.post(
        "/v1/messenger/message_reactions/",
        json={
            "message_uuid": message["uuid"],
            "emoji_name": "thumbs_up",
        },
    )
    assert reaction.status_code == 201, reaction.text
    file = api.post(
        "/v1/messenger/files/",
        data={"stream_uuid": str(channel.stream_uuid)},
        files={"file": ("pending.txt", b"pending", "text/plain")},
    )
    assert file.status_code == 201, file.text
    for entity in (topic, message, reaction.json(), file.json()):
        assert entity["source_name"] == "zulip"
        assert entity["provider_uuid"] == str(account.provider_uuid)
        assert entity["provider_identity"] is None
        assert entity["status"] == "new"
    for table, predicate, identifier in (
        ("messenger_user_topic_bindings", "topic_uuid", topic["uuid"]),
        ("messenger_user_message_states", "placement_uuid", message["uuid"]),
    ):
        assert db.execute(
            f"SELECT source_name,provider_uuid FROM {table} WHERE {predicate}=%s",
            (uuid.UUID(identifier),),
        ).fetchone() == ("zulip", account.provider_uuid)
    assert all(row[3]["source_name"] == "zulip" for row in intents(db, account))


def test_native_entities_never_enter_outbox_with_an_old_stream_link(
    outgoing, db, tmp_path, monkeypatch
):
    api, account, channel, *_ = outgoing
    db.execute(
        "UPDATE messenger_streams SET source_name='native',provider_uuid=NULL,provider_identity=NULL WHERE uuid=%s",
        (channel.stream_uuid,),
    )
    db.execute(
        """INSERT INTO zulip_entity_links(project_id,entity_type,entity_uuid,provider_uuid,external_account_uuid,provider_identity)
        VALUES(%s,%s,%s,%s,%s,%s)""",
        (
            account.project_id,
            "stream",
            channel.stream_uuid,
            account.provider_uuid,
            account.uuid,
            "42",
        ),
    )
    for table, where in (
        ("messenger_topics", "stream_uuid"),
        ("messenger_stream_bindings", "stream_uuid"),
    ):
        db.execute(
            f"UPDATE {table} SET source_name='native',provider_uuid=NULL,provider_identity=NULL,status='active' WHERE {where}=%s",
            (channel.stream_uuid,),
        )
    db.execute(
        "UPDATE messenger_user_topic_bindings SET source_name='native',provider_uuid=NULL,provider_identity=NULL,status='active' WHERE project_id=%s",
        (account.project_id,),
    )
    public = create(outgoing)
    assert public["source_name"] == "native"
    reaction = api.post(
        "/v1/messenger/message_reactions/",
        json={"message_uuid": public["uuid"], "emoji_name": "thumbs_up"},
    )
    assert reaction.status_code == 201, reaction.text
    assert reaction.json()["source_name"] == "native"
    assert (
        api.post(
            f"/v1/messenger/messages/{public['uuid']}/actions/star/invoke"
        ).status_code
        == 200
    )
    topic = api.post(
        "/v1/messenger/stream_topics/",
        json={"stream_uuid": str(channel.stream_uuid), "name": "Local topic"},
    )
    assert topic.status_code == 201, topic.text
    assert topic.json()["source_name"] == "native"
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    file = api.post(
        "/v1/messenger/files/",
        data={"stream_uuid": str(channel.stream_uuid)},
        files={"file": ("local.txt", b"local", "text/plain")},
    )
    assert file.status_code == 201, file.text
    assert file.json()["source_name"] == "native"
    assert (
        api.put(
            f"/v1/messenger/streams/{channel.stream_uuid}",
            json={"description": "Local only"},
        ).status_code
        == 200
    )
    assert intents(db, account) == []


def test_native_file_reference_never_schedules_an_upload(
    outgoing, db, tmp_path, monkeypatch
):
    api, account, channel, *_ = outgoing
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    file = api.post(
        "/v1/messenger/files/",
        data={"acl": '{"mode":"public"}'},
        files={"file": ("local.txt", b"local", "text/plain")},
    )
    assert file.status_code == 201, file.text
    assert file.json()["source_name"] == "native"
    with contexts.Context().session_manager():
        with pytest.raises(ValueError, match="different source"):
            export(
                account,
                {
                    "kind": "markdown",
                    "content": f"[file](urn:file:{file.json()['uuid']})",
                },
                channel.stream_uuid,
                outgoing[4],
            )
    assert not intents(db, account)


def test_provenance_migration_converts_legacy_children_and_retires_root_links(
    outgoing, db
):
    api, account, channel, *_ = outgoing
    public = create(outgoing)
    identifier = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (uuid.UUID(public["uuid"]),),
    ).fetchone()[0]
    db.execute(
        "UPDATE messenger_messages SET source_name='native',provider_uuid=NULL,provider_identity=NULL,status='active' WHERE uuid=%s",
        (identifier,),
    )
    db.execute(
        "INSERT INTO zulip_entity_links(project_id,entity_type,entity_uuid,provider_uuid,external_account_uuid,provider_identity) VALUES(%s,'message',%s,%s,%s,'987')",
        (account.project_id, identifier, account.provider_uuid, account.uuid),
    )
    db.execute(
        'UPDATE zulip_outgoing_intents SET data=data||\'{"source_name":"native"}\'::jsonb WHERE entity_uuid=%s',
        (identifier,),
    )
    filename = next(conftest.MIGRATIONS_DIR.glob("0267-*.py")).name
    spec = importlib.util.spec_from_file_location(
        "provenance_cutover", conftest.MIGRATIONS_DIR / filename
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    topic_uuid = db.execute(
        "SELECT default_topic_uuid FROM messenger_streams WHERE uuid=%s",
        (channel.stream_uuid,),
    ).fetchone()[0]
    with contexts.Context().session_manager() as session:
        session.execute("SET CONSTRAINTS ALL DEFERRED")
        session.execute(
            "UPDATE messenger_streams SET default_topic_uuid=NULL WHERE uuid=%s",
            (channel.stream_uuid,),
        )
        session.execute(
            "UPDATE messenger_streams SET default_topic_uuid=%s WHERE uuid=%s",
            (topic_uuid, channel.stream_uuid),
        )
        migration.migration_step.upgrade(session)
    assert db.execute(
        "SELECT source_name,provider_uuid,provider_identity FROM messenger_messages WHERE uuid=%s",
        (identifier,),
    ).fetchone() == ("zulip", account.provider_uuid, "987")
    assert db.execute(
        "SELECT data->>'source_name',data->>'provider_identity' FROM zulip_outgoing_intents WHERE entity_uuid=%s",
        (identifier,),
    ).fetchone() == ("zulip", "987")
    assert not db.execute(
        "SELECT 1 FROM zulip_entity_links WHERE entity_type IN('stream','topic','message')"
    ).fetchone()


def test_send_uses_workspace_entity_uuid_as_local_id(outgoing, db, _test_database_url):
    _api, account, *_ = outgoing
    public = create(outgoing)
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (uuid.UUID(public["uuid"]),),
    ).fetchone()[0]

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            await drain(service)
            sent = [
                data
                for method, path, data in server.calls
                if method == "POST" and path == "/api/v1/messages"
            ]
            assert len(sent) == 1
            assert sent[0]["local_id"] == str(canonical)
            assert sent[0]["queue_id"] == "sender-queue"

    asyncio.run(run())


def test_retained_local_receipt_resolves_lost_response_after_queue_expiry(
    outgoing, db, _test_database_url
):
    _api, account, *_ = outgoing
    public = create(outgoing)
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (uuid.UUID(public["uuid"]),),
    ).fetchone()[0]

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            server.lose_response = True
            original = service.http.request

            async def expired_queue(account, method, path, data=None, **kwargs):
                if method == "GET" and path == "/events":
                    raise outgoing_operations.ApiError(
                        400, {"code": "BAD_EVENT_QUEUE_ID"}, "GET"
                    )
                return await original(account, method, path, data, **kwargs)

            service.http.request = expired_queue
            await service.dispatch()
            await asyncio.gather(*list(service.tasks.values()))
            await asyncio.sleep(0)
            await service.db.sql(
                "UPDATE zulip_outgoing_intents SET next_attempt_at=now()"
            )
            await service.dispatch()
            await asyncio.gather(*list(service.tasks.values()))
            await asyncio.sleep(0)
            pending = await service.db.sql(
                "SELECT status,error FROM zulip_outgoing_intents WHERE entity_uuid=%s",
                (canonical,),
            )
            assert pending[0]["status"] == "checking"
            assert "queue expiry" in pending[0]["error"]
            # The original receiver had durably stored this event before its queue expired.
            receipt = {**server.events[0], "local_message_id": str(canonical)}
            await service.db.sql(
                "INSERT INTO zulip_incoming_events(external_account_uuid,project_id,provider_uuid,owner_user_uuid,account_identity,queue_id,event_id,payload) VALUES(%s,%s,%s,%s,%s,'sender-queue',1,%s)",
                (
                    account.uuid,
                    account.project_id,
                    account.provider_uuid,
                    account.owner_user_uuid,
                    account.external_account_id,
                    pg_json.Jsonb(receipt),
                ),
            )
            await drain(service)
            sent = [
                data
                for method, path, data in server.calls
                if method == "POST" and path == "/api/v1/messages"
            ]
            assert len(sent) == 1
            assert sent[0]["local_id"] == str(canonical)

    asyncio.run(run())
    assert db.execute(
        "SELECT provider_identity FROM messenger_messages WHERE uuid=%s", (canonical,)
    ).fetchone() == ("101",)


@pytest.mark.isolated_schema
def test_capture_and_send_do_not_depend_on_retired_channel_routes(
    outgoing, db, _test_database_url
):
    _api, account, *_ = outgoing
    db.execute("DROP TABLE bridge_zulip_v5_channels CASCADE")
    create(outgoing)

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            await drain(service)
            sent = [
                data
                for method, path, data in server.calls
                if method == "POST" and path == "/api/v1/messages"
            ]
            assert len(sent) == 1
            assert sent[0]["to"] == "42"

    asyncio.run(run())


def test_file_receipt_never_temporarily_revokes_the_uploaders_access(
    outgoing, db, _test_database_url, tmp_path, monkeypatch
):
    api, account, channel, *_ = outgoing
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    response = api.post(
        "/v1/messenger/files/",
        data={"stream_uuid": str(channel.stream_uuid)},
        files={"file": ("receipt.png", b"receipt", "image/png")},
    )
    assert response.status_code == 201, response.text
    identifier = uuid.UUID(response.json()["uuid"])

    async def run():
        database = zulip_outgoing.Database(_test_database_url)
        await database.open()
        try:
            intent = (
                await database.sql(
                    "SELECT * FROM zulip_outgoing_intents WHERE entity_type='file' AND entity_uuid=%s",
                    (identifier,),
                )
            )[0]
            account_data = (
                await database.sql(
                    zulip_outgoing.SELECT_ACCOUNT_SQL,
                    (
                        account.uuid,
                        account.project_id,
                    ),
                )
            )[0]
            original_sql = database.sql

            async def observe_committed_step(query, parameters=()):
                result = await original_sql(query, parameters)
                visible = db.execute(
                    "SELECT 1 FROM m_workspace_visible_files_v1 WHERE uuid=%s AND viewer_user_uuid=%s",
                    (identifier, account.owner_user_uuid),
                ).fetchall()
                assert visible, (
                    "A concurrent message converter loses file access after a receipt commit"
                )
                return result

            database.sql = observe_committed_step
            operations = outgoing_operations.Operations(database, mock.Mock())
            await operations.finish(
                intent,
                account_data,
                {"kind": "upload"},
                {"identity": "/user_uploads/2/example/receipt.png"},
            )
        finally:
            await database.close()

    asyncio.run(run())


def test_canonical_message_has_one_destination_and_copy_gets_new_uuid(
    outgoing, db, _test_database_url
):
    api, account, channel, *_ = outgoing
    second = api.post(
        "/v1/messenger/streams/",
        json={"name": "Second channel", "description": "", "source_name": "native"},
    )
    assert second.status_code == 201, second.text
    second_stream = second.json()
    with db.transaction():
        db.execute("SELECT set_config('workspace.zulip_incoming','on',true)")
        db.execute(
            "UPDATE messenger_streams SET source_name='zulip',provider_uuid=%s,provider_identity='43' WHERE uuid=%s",
            (account.provider_uuid, second_stream["uuid"]),
        )
        for table in ("messenger_topics", "messenger_stream_bindings"):
            db.execute(
                f"UPDATE {table} SET source_name='zulip',provider_uuid=%s,provider_identity='43:' WHERE stream_uuid=%s",
                (account.provider_uuid, second_stream["uuid"]),
            )
        db.execute(
            "UPDATE messenger_user_topic_bindings SET source_name='zulip',provider_uuid=%s,provider_identity='43::7' WHERE topic_uuid=%s",
            (account.provider_uuid, second_stream["default_topic_uuid"]),
        )
    canonical = uuid.uuid4()
    first = create(outgoing, canonical)
    original = {
        "uuid": str(canonical),
        "stream_uuid": str(channel.stream_uuid),
        "payload": first["payload"],
    }
    again = api.post("/v1/messenger/messages/", json=original)
    assert again.status_code == 201 and again.json()["uuid"] == first["uuid"]
    other = {**original, "stream_uuid": second_stream["uuid"]}
    rejected = api.post("/v1/messenger/messages/", json=other)
    assert rejected.status_code == 400, rejected.text
    copied = api.post(
        "/v1/messenger/messages/", json={**other, "uuid": str(uuid.uuid4())}
    )
    assert copied.status_code == 201, copied.text
    assert copied.json()["uuid"] != first["uuid"]
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_message_placements WHERE message_uuid=%s",
            (canonical,),
        ).fetchone()[0]
        == 1
    )

    async def run():
        async with sender(account, _test_database_url) as (service, server):
            await drain(service)
            sends = [
                call
                for call in server.calls
                if call[:2] == ("POST", "/api/v1/messages")
            ]
            assert len(sends) == 2
            assert {str(call[2]["to"]) for call in sends} == {"42", "43"}
            assert len(server.messages) == 2

    asyncio.run(run())
