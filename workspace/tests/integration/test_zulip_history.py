# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import contextlib
import copy
import datetime
import uuid
from unittest import mock

import pytest

from workspace.bridges.zulip import history, incoming_operations, outgoing_operations
from workspace.messenger_api import file_storage
from workspace.services import zulip_history
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming


def message(identifier=101, content="Hello", **changes):
    return {
        **incoming_tests.event(content, identifier)["message"],
        "timestamp": 1700000000,
        "reactions": [],
        **changes,
    }


def request(db, account, registration=None, cutoff_id=1000000, cutoff_at=None):
    db.execute(
        history.REQUEST_SQL,
        (
            account["uuid"],
            account["project_id"],
            account["provider_uuid"],
            cutoff_at or datetime.datetime.now(datetime.timezone.utc),
            cutoff_id,
            cutoff_id + 1,
            incoming_operations.json_value(registration or {}),
            [row["stream_id"] for row in registration["subscriptions"]]
            if registration and "subscriptions" in registration
            else None,
        ),
    )


@contextlib.asynccontextmanager
async def worker(incoming, messages, **options):
    _, _, account, _, url = incoming
    service = zulip_history.ZulipHistoryService(
        url, batch_size=options.pop("batch_size", 3), **options
    )
    await service.db.open()
    calls = []

    async def http(_account, method, path, query=None, **kwargs):
        calls.append((method, path, query))
        if path.startswith("/streams/"):
            remote = int(path.rsplit("/", 1)[1])
            return {
                "stream": {
                    "stream_id": remote,
                    "name": "Channel" if remote == 42 else "Other",
                    "description": "",
                }
            }
        if path.startswith("/users/"):
            return {
                "user": {
                    "user_id": int(path.rsplit("/", 1)[1]),
                    "full_name": "Sender",
                    "email": "user@example.com",
                }
            }
        assert path == "/messages", path
        ordered = sorted(messages)
        selected = [value for value in ordered if value < query["anchor"]][
            -query["num_before"] :
        ]
        return {
            "messages": [copy.deepcopy(messages[value]) for value in selected],
            "found_oldest": not selected or not ordered or selected[0] == ordered[0],
            "found_newest": not ordered or query["anchor"] > ordered[-1],
        }

    service.http = mock.Mock(request=mock.AsyncMock(side_effect=http))
    service.operations = history.Operations(service.db, service.http)
    try:
        yield service, calls
    finally:
        await service.db.close()


async def job(service, account):
    jobs = await service.db.sql(zulip_history.SELECT_JOBS_SQL, ([], 100))
    return next((row for row in jobs if row["uuid"] == account["uuid"]), None)


async def drain(service, account):
    for _ in range(100):
        current = await job(service, account)
        if current is None:
            return
        await service.step(current)
        state = await service.db.sql(
            "SELECT error FROM zulip_history_accounts WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        assert not state[0]["error"], state
    pytest.fail("History did not finish")


def test_bulk_import_is_newest_first_idempotent_and_silent(incoming):
    api, db, account, _, _ = incoming
    incoming_tests.drain()
    before = incoming_tests.client_events(incoming)
    messages = {
        i: message(i, f"History {i}", flags=["read"] if i % 2 else ["starred"])
        for i in range(101, 111)
    }
    request(db, account)

    async def run():
        async with worker(incoming, messages) as (service, calls):
            await drain(service, account)
            assert [c[2]["anchor"] for c in calls if c[1] == "/messages"] == [
                1000001,
                108,
                105,
                102,
            ]
            assert not await service.db.sql("SELECT 1 FROM zulip_history_batches")

    asyncio.run(run())
    incoming_tests.drain()
    assert len(api.get("/v1/messenger/messages/").json()) == 10
    assert incoming_tests.client_events(incoming) == before
    roots = db.execute(
        "SELECT uuid,content_uuid,content_revision FROM messenger_messages ORDER BY uuid"
    ).fetchall()
    request(db, account)
    asyncio.run(run())
    incoming_tests.drain()
    assert (
        roots
        == db.execute(
            "SELECT uuid,content_uuid,content_revision FROM messenger_messages ORDER BY uuid"
        ).fetchall()
    )
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_unreads WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (5,)
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_stars WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (5,)
    assert incoming_tests.client_events(incoming) == before


def test_realtime_flag_lookup_stays_bounded_before_history_is_analyzed(incoming):
    api, db, account, *_ = incoming
    incoming_tests.apply(incoming, incoming_tests.event())
    tables = (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
        "messenger_stream_bindings",
        "messenger_user_message_unreads",
        "messenger_user_message_stars",
    )
    plans = []
    original_rows = incoming_operations.rows

    async def explain(connection, query, parameters=()):
        if query == incoming_operations.SELECT_STATES_SQL:
            result = await original_rows(
                connection,
                "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + query,
                parameters,
            )
            plans.append(result[0]["QUERY PLAN"][0]["Plan"])
        return await original_rows(connection, query, parameters)

    try:
        # A just-imported database still has statistics from its initial rows.
        for table in tables:
            db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
            db.execute(f"ANALYZE {table}")
        messages = {i: message(i, flags=["starred"]) for i in range(200, 4296)}
        request(db, account)

        async def run():
            async with worker(incoming, messages, batch_size=1000) as (service, _):
                await drain(service, account)

        asyncio.run(run())
        with mock.patch.object(incoming_operations, "rows", side_effect=explain):
            incoming_tests.apply(
                incoming,
                {
                    "type": "update_message_flags",
                    "op": "add",
                    "flag": "read",
                    "messages": [4295],
                },
            )
        assert len(plans) == 1
        # Count buffer work, not wall time: one ID must not rescan all history
        # once for every personal binding when cardinality estimates are stale.
        assert plans[0]["Shared Hit Blocks"] < 10000, plans[0]
        placement = db.execute(
            "SELECT placement.uuid FROM messenger_messages message "
            "JOIN messenger_message_placements placement ON placement.message_uuid=message.uuid "
            "AND placement.project_id=message.project_id "
            "WHERE message.project_id=%s AND message.provider_uuid=%s AND message.provider_identity='4295'",
            (account["project_id"], account["provider_uuid"]),
        ).fetchone()[0]
        visible = api.get(f"/v1/messenger/messages/{placement}").json()
        assert visible["read"] and visible["starred"]
    finally:
        for table in tables:
            db.execute(f"ALTER TABLE {table} RESET (autovacuum_enabled)")
            db.execute(f"ANALYZE {table}")


@pytest.mark.parametrize("field", ["cross_realm_bots", "realm_non_active_users"])
def test_registration_keeps_historical_users_outside_the_active_realm(incoming, field):
    api, db, account, _, url = incoming
    person = {
        "user_id": 6,
        "full_name": "Historical sender",
        "email": "sender@example.com",
        "is_active": field != "realm_non_active_users",
    }

    async def run():
        receiver = incoming_tests.zulip_incoming.ZulipIncomingService(url)
        await receiver.db.open()
        receiver.http = mock.Mock(
            request=mock.AsyncMock(
                return_value={
                    "queue_id": "historical-user-catalog",
                    "last_event_id": 0,
                    "max_message_id": 101,
                    "subscriptions": [{"stream_id": 42, "name": "Channel"}],
                    "realm_users": [],
                    field: [person],
                    "user_topics": [],
                }
            )
        )
        receiver.poll_http = mock.Mock(
            request=mock.AsyncMock(side_effect=asyncio.CancelledError)
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await receiver.receive(account)
        finally:
            await receiver.db.close()
        async with worker(incoming, {101: message(sender_id=6)}) as (service, calls):
            original = service.http.request.side_effect

            async def http(account, method, path, query=None, **kwargs):
                if path == "/users/6":
                    raise RuntimeError("Zulip HTTP 400: No such user")
                return await original(account, method, path, query, **kwargs)

            service.http.request.side_effect = http
            service.busy = mock.AsyncMock(return_value=False)
            await drain(service, account)
            assert not any(call[1] == "/users/6" for call in calls)

    asyncio.run(run())
    incoming_tests.drain()
    assert len(api.get("/v1/messenger/messages/").json()) == 1
    assert db.execute(
        "SELECT users.first_name,users.email,users.disabled FROM m_workspace_users users "
        "JOIN m_external_accounts_v2 account ON account.owner_user_uuid=users.uuid "
        "WHERE account.project_id=%s AND account.provider_uuid=%s "
        "AND account.external_account_id='6'",
        (account["project_id"], account["provider_uuid"]),
    ).fetchone() == ("Historical sender", "sender@example.com", not person["is_active"])


def test_bulk_snapshot_replaces_flags_and_reactions_in_its_page(incoming):
    api, db, account, _, _ = incoming
    reaction = {
        "user_id": 7,
        "emoji_name": "smile",
        "emoji_code": "1f604",
        "reaction_type": "unicode_emoji",
    }
    messages = {101: message(flags=["starred"], reactions=[reaction])}
    request(db, account)

    async def run():
        async with worker(incoming, messages) as (service, _):
            await drain(service, account)

    asyncio.run(run())
    incoming_tests.drain()
    row = api.get("/v1/messenger/messages/").json()[0]
    assert not row["read"] and row["starred"] and row["reactions"]
    messages[101] = message(content="Edited", flags=["read"])
    request(db, account)
    asyncio.run(run())
    incoming_tests.drain()
    row = api.get("/v1/messenger/messages/").json()[0]
    assert row["read"] and not row["starred"] and row["reactions"] == {}
    assert row["payload"]["content"] == "Edited"
    assert db.execute("SELECT count(*) FROM messenger_counter_updates").fetchone() == (
        0,
    )


def test_history_upper_boundary_excludes_new_realtime_messages(incoming):
    api, db, account, _, _ = incoming
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    messages = {
        101: message(),
        102: message(102, timestamp=int(cutoff.timestamp()) + 1),
        103: message(103),
    }
    request(db, account, cutoff_id=102, cutoff_at=cutoff)

    async def run():
        async with worker(incoming, messages) as (service, _):
            await drain(service, account)

    asyncio.run(run())
    assert [
        r["provider_identity"] for r in api.get("/v1/messenger/messages/").json()
    ] == ["101"]


def test_failed_merge_keeps_durable_batch_and_cursor_for_retry(incoming):
    api, db, account, _, _ = incoming
    request(db, account)

    async def run():
        async with worker(incoming, {101: message()}) as (service, calls):
            await service.step(await job(service, account))  # catalog
            merge_batch = service.operations.merge_batch

            async def fail(connection, selected, batch):
                await merge_batch(connection, selected, batch)
                raise RuntimeError("interrupted before commit")

            service.operations.merge_batch = fail
            await service.step(await job(service, account))
            assert not api.get("/v1/messenger/messages/").json()
            assert (
                await service.db.sql("SELECT before_id FROM zulip_history_accounts")
            )[0]["before_id"] == 1000001
            assert (
                len(
                    await service.db.sql(
                        "SELECT 1 FROM zulip_history_batches WHERE messages IS NOT NULL"
                    )
                )
                == 1
            )
            service.operations.merge_batch = merge_batch
            await service.db.sql(
                "UPDATE zulip_history_accounts SET next_attempt_at=now()"
            )
            await drain(service, account)
            assert len([c for c in calls if c[1] == "/messages"]) == 1

    asyncio.run(run())
    assert len(api.get("/v1/messenger/messages/").json()) == 1


def test_disabling_account_during_preparation_prevents_merge_and_can_resume(incoming):
    api, db, account, _, _ = incoming
    request(db, account)

    async def run():
        async with worker(incoming, {101: message()}) as (service, _):
            await service.step(await job(service, account))
            prepare = service.operations.prepare_batch

            async def disable(selected, messages):
                value = await prepare(selected, messages)
                db.execute(
                    "UPDATE m_external_accounts_v2 SET disabled=true WHERE uuid=%s",
                    (account["uuid"],),
                )
                return value

            service.operations.prepare_batch = disable
            await service.step(await job(service, account))
            assert not api.get("/v1/messenger/messages/").json()
            assert await job(service, account) is None
            assert (
                await service.db.sql("SELECT before_id FROM zulip_history_accounts")
            )[0]["before_id"] == 1000001
            service.operations.prepare_batch = prepare
            db.execute(
                "UPDATE m_external_accounts_v2 SET disabled=false WHERE uuid=%s",
                (account["uuid"],),
            )
            await drain(service, account)

    asyncio.run(run())
    assert len(api.get("/v1/messenger/messages/").json()) == 1


@pytest.mark.parametrize("field", ["api_key", "email", "server_url"])
def test_settings_changed_during_preparation_prevent_merge(incoming, field):
    api, db, account, *_ = incoming
    request(db, account)

    async def run():
        async with worker(incoming, {101: message()}) as (service, _):
            await service.step(await job(service, account))
            prepare = service.operations.prepare_batch

            async def change_settings(selected, messages):
                batch = await prepare(selected, messages)
                settings = {
                    **account["settings"],
                    field: {
                        "api_key": "replacement-test-key",
                        "email": "replacement@example.com",
                        "server_url": "https://another.example.com",
                    }[field],
                }
                response = api.put(
                    f"/v1/messenger/external_accounts/{account['uuid']}",
                    json={"settings": settings},
                    permissions=("workspace.external_account.update",),
                )
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "new"
                return batch

            service.operations.prepare_batch = change_settings
            await service.step(await job(service, account))
            assert not db.execute(
                "SELECT 1 FROM messenger_messages WHERE project_id=%s",
                (account["project_id"],),
            ).fetchall()
            assert db.execute(
                "SELECT before_id,error FROM zulip_history_accounts WHERE external_account_uuid=%s",
                (account["uuid"],),
            ).fetchone() == (1000001, None)

    asyncio.run(run())


@pytest.mark.parametrize("disconnect", ["disable", "unlink"])
@pytest.mark.parametrize("kind,identity", [("user", "8"), ("stream", "999")])
def test_disconnect_during_dependency_lookup_prevents_catalog_writes(
    incoming, disconnect, kind, identity
):
    api, db, account, *_ = incoming
    request(db, account)
    changes = {"sender_id": 8} if kind == "user" else {"stream_id": 999}

    async def run():
        async with worker(incoming, {101: message(**changes)}) as (service, _):
            await service.step(await job(service, account))
            prepare = service.operations.prepare_dependency
            changed = False

            async def disconnect_account(selected, spec):
                nonlocal changed
                prepared = await prepare(selected, spec)
                if (
                    not changed
                    and spec["kind"] == kind
                    and spec["identity"] == identity
                ):
                    path = f"/v1/messenger/external_accounts/{account['uuid']}"
                    if disconnect == "disable":
                        response = api.put(
                            path,
                            json={"disabled": True},
                            permissions=("workspace.external_account.update",),
                        )
                        assert response.status_code == 200, response.text
                    else:
                        response = api.delete(
                            path, permissions=("workspace.external_account.delete",)
                        )
                        assert response.status_code == 204, response.text
                    changed = True
                return prepared

            service.operations.prepare_dependency = disconnect_account
            with contextlib.suppress(asyncio.CancelledError):
                await service.step(await job(service, account))
            assert changed
            if kind == "user":
                query = "SELECT 1 FROM m_external_accounts_v2 WHERE project_id=%s AND provider_uuid=%s AND external_account_id=%s"
            else:
                query = "SELECT 1 FROM messenger_streams WHERE project_id=%s AND provider_uuid=%s AND provider_identity=%s"
            assert not db.execute(
                query, (account["project_id"], account["provider_uuid"], identity)
            ).fetchall()
            assert not api.get("/v1/messenger/messages/").json()
            assert db.execute(
                "SELECT before_id,error FROM zulip_history_accounts WHERE external_account_uuid=%s",
                (account["uuid"],),
            ).fetchone() == (1000001, None)
            if disconnect == "disable":
                service.operations.prepare_dependency = prepare
                response = api.put(
                    f"/v1/messenger/external_accounts/{account['uuid']}",
                    json={"disabled": False},
                    permissions=("workspace.external_account.update",),
                )
                assert response.status_code == 200, response.text
                await drain(service, account)
                assert len(api.get("/v1/messenger/messages/").json()) == 1

    asyncio.run(run())


def test_scheduler_catches_up_new_account_before_advancing_older_accounts(incoming):
    _, db, account, _, _ = incoming
    peer, other_context = incoming_tests.peer_account(incoming, 8)
    other = other_context[2]
    try:
        request(db, account, cutoff_id=900)
        request(db, other, cutoff_id=1200)
        db.execute("UPDATE zulip_history_accounts SET registration=NULL")

        async def run():
            async with worker(incoming, {}) as (service, _):
                selected = await service.db.sql(zulip_history.SELECT_JOBS_SQL, ([], 10))
                assert [r["uuid"] for r in selected] == [other["uuid"]]
                await service.db.sql(
                    "UPDATE zulip_history_accounts SET before_id=800 WHERE external_account_uuid=%s",
                    (other["uuid"],),
                )
                selected = await service.db.sql(zulip_history.SELECT_JOBS_SQL, ([], 10))
                assert [r["uuid"] for r in selected] == [account["uuid"]]
                db.execute(
                    "UPDATE m_external_accounts_v2 SET disabled=true WHERE uuid=%s",
                    (account["uuid"],),
                )
                selected = await service.db.sql(zulip_history.SELECT_JOBS_SQL, ([], 10))
                assert [r["uuid"] for r in selected] == [other["uuid"]]

        asyncio.run(run())
    finally:
        peer.delete()


def test_running_scope_does_not_block_another_project_with_the_same_provider(incoming):
    _, db, account, _, _ = incoming
    peer, other_context = incoming_tests.peer_account(incoming, 8)
    other = {**other_context[2], "project_id": uuid.uuid4()}
    try:
        db.execute(
            "UPDATE m_external_accounts_v2 SET project_id=%s WHERE uuid=%s",
            (other["project_id"], other["uuid"]),
        )
        request(db, account)
        request(db, other)

        async def run():
            async with worker(incoming, {}) as (service, _):
                selected = await service.db.sql(zulip_history.SELECT_JOBS_SQL, ([], 10))
                assert {row["uuid"] for row in selected} == {
                    account["uuid"],
                    other["uuid"],
                }
                running = [f"{account['project_id']}:{account['provider_uuid']}"]
                selected = await service.db.sql(
                    zulip_history.SELECT_JOBS_SQL, (running, 10)
                )
                assert [row["uuid"] for row in selected] == [other["uuid"]]

        asyncio.run(run())
    finally:
        peer.delete()


def test_history_yields_before_provider_io_when_realtime_is_delayed(incoming):
    _, db, account, _, _ = incoming
    request(db, account)

    async def run():
        async with worker(incoming, {101: message()}) as (service, calls):
            service.busy = mock.AsyncMock(return_value=True)
            await service.step(await job(service, account))
            assert calls == []
            assert not await service.db.sql("SELECT 1 FROM zulip_history_batches")

    asyncio.run(run())


@pytest.mark.parametrize("prepare_before_delete", [False, True])
def test_history_cannot_restore_a_physically_deleted_message(
    incoming, prepare_before_delete
):
    api, db, account, _, _ = incoming
    incoming_tests.apply(incoming, incoming_tests.event(identifier=101))
    request(db, account)

    async def run():
        async with worker(incoming, {}) as (service, _):
            selected = await job(service, account)
            page = [message(101, flags=["starred"]), message(102, flags=[])]
            if prepare_before_delete:
                batch = await service.operations.prepare_batch(selected, page)
            await asyncio.to_thread(
                incoming_tests.apply,
                incoming,
                {"type": "delete_message", "message_id": 101},
            )
            if not prepare_before_delete:
                batch = await service.operations.prepare_batch(selected, page)
            async with service.db.transaction() as connection:
                await service.operations.merge_batch(connection, selected, batch)

    asyncio.run(run())
    incoming_tests.drain()
    assert db.execute(
        "SELECT provider_identity FROM messenger_messages WHERE project_id=%s ORDER BY provider_identity",
        (api.project_id,),
    ).fetchall() == [("102",)]
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_stars WHERE project_id=%s",
        (api.project_id,),
    ).fetchone() == (0,)


def test_bulk_merge_uses_a_bounded_number_of_statements(incoming):
    _, db, account, _, _ = incoming
    request(db, account)

    async def run():
        async with worker(incoming, {}, batch_size=1000) as (service, _):
            batch = await service.operations.prepare_batch(
                await job(service, account),
                [message(i) for i in range(1, 1001)],
            )
            async with service.db.transaction() as connection:
                execute = connection.execute
                queries = []

                async def record(query, *args, **kwargs):
                    queries.append(query)
                    return await execute(query, *args, **kwargs)

                with mock.patch.object(connection, "execute", side_effect=record):
                    await service.operations.merge_batch(connection, account, batch)
                assert len(queries) < 55

    asyncio.run(run())
    assert db.execute(
        "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (1000,)


@pytest.mark.isolated_schema
@pytest.mark.parametrize("age_days", [0, 4])
def test_history_process_is_silent_for_an_actual_websocket_client(
    incoming, tmp_path, age_days
):
    import json
    import signal
    import socket
    import sys

    import websockets
    from aiohttp import web

    from workspace.messenger_api import websocket_service
    from workspace.messenger_api.dm import external_models

    _, db, original, _, url = incoming

    async def run():
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        payload = message(
            content="From the history process", timestamp=now - age_days * 86400
        )
        transports = []

        async def provider(request):
            transports.append(request.transport)
            assert not db.execute(
                "SELECT 1 FROM pg_stat_activity WHERE application_name='workspace-zulip-history' AND state='idle in transaction'"
            ).fetchone()
            if request.path.endswith("/messages"):
                return web.json_response(
                    {"messages": [payload], "found_oldest": True, "found_newest": True}
                )
            return web.json_response(
                {"stream": {"stream_id": 42, "name": "Channel", "description": ""}}
            )

        app = web.Application()
        app.router.add_get("/api/v1/messages", provider)
        app.router.add_get("/api/v1/streams/42", provider)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        provider_row = external_models.Provider.register("127.0.0.1")
        account = {
            **original,
            "provider_uuid": provider_row.uuid,
            "hostname": "127.0.0.1",
            "settings": {
                **original["settings"],
                "server_url": f"http://127.0.0.1:{port}",
            },
        }
        db.execute(
            "UPDATE m_external_accounts_v2 SET provider_uuid=%s,settings=%s WHERE uuid=%s",
            (
                provider_row.uuid,
                incoming_operations.json_value(account["settings"]),
                account["uuid"],
            ),
        )
        request(db, account)
        config = tmp_path / "history.conf"
        config.write_text(
            f"[db]\nconnection_url = {url}\n[zulip_history]\npoll_interval = 0.02\n"
        )
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        websocket_port = probe.getsockname()[1]
        probe.close()
        server = websocket_service.MessengerEventsWebsocketServer(
            db_url=url,
            iam_engine_driver=mock.Mock(),
            heartbeat_interval=25,
            client_timeout=20,
            catchup_limit=500,
            send_queue_limit=32,
            poll_interval=0.02,
        )
        server._authenticator.authenticate = lambda token: (
            account["owner_user_uuid"],
            account["project_id"],
        )
        websocket_task = asyncio.create_task(server.serve("127.0.0.1", websocket_port))
        child = None
        try:
            async with asyncio.timeout(20):
                while server._stop_event is None:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.02)
                async with websockets.connect(
                    f"ws://127.0.0.1:{websocket_port}/v1/events/ws",
                    subprotocols=["workspace.events.v1", "bearer.test"],
                ) as websocket:
                    while json.loads(await websocket.recv()).get("type") != "ready":
                        pass
                    child = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "workspace.cmd.zulip_history",
                        "--config-file",
                        str(config),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    while not db.execute(
                        "SELECT 1 FROM messenger_messages WHERE project_id=%s",
                        (account["project_id"],),
                    ).fetchone():
                        assert child.returncode is None
                        await asyncio.sleep(0.02)
                    await asyncio.to_thread(incoming_tests.drain)
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(websocket.recv(), 0.2)
                    payload["content"] = "Edited through recovery"
                    payload["last_edit_timestamp"] = now + 5
                    request(db, account)
                    while (
                        db.execute(
                            "SELECT payload->>'content' FROM messenger_messages_with_content_v1 WHERE project_id=%s",
                            (account["project_id"],),
                        ).fetchone()[0]
                        != payload["content"]
                    ):
                        await asyncio.sleep(0.02)
                    await asyncio.to_thread(incoming_tests.drain)
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(websocket.recv(), 0.2)
                    # A new realtime message must still reach this same connection.
                    realtime = (incoming[0], db, account, incoming[3], url)
                    await asyncio.to_thread(
                        incoming_tests.apply,
                        realtime,
                        incoming_tests.event("Realtime stays visible", 102),
                    )
                    await asyncio.to_thread(incoming_tests.drain)
                    while True:
                        frame = json.loads(await websocket.recv())
                        if frame.get("object_type") == "message":
                            assert frame["action"] == "created"
                            assert (
                                frame["payload"]["payload"]["content"]
                                == "Realtime stays visible"
                            )
                            break
                    assert len(set(transports)) < len(transports)
        finally:
            if child is not None and child.returncode is None:
                child.send_signal(signal.SIGINT)
                _, stderr = await asyncio.wait_for(child.communicate(), 10)
                assert child.returncode == 0, stderr.decode()
            server.stop()
            await websocket_task
            await runner.cleanup()

    asyncio.run(run())


@pytest.mark.parametrize("known", [False, True])
def test_history_does_not_subscribe_an_account_to_old_channels(incoming, known):
    _, db, account, _, _ = incoming
    if known:
        incoming_tests.apply(incoming, {"type": "message", "message": message()})
        incoming_tests.apply(
            incoming,
            {
                "type": "subscription",
                "op": "remove",
                "subscriptions": [{"stream_id": 42}],
            },
        )
    request(db, account, {"subscriptions": []})

    async def run():
        async with worker(incoming, {101: message()}) as (service, _):
            await drain(service, account)
            assert not await service.db.sql(
                "SELECT 1 FROM messenger_stream_bindings WHERE project_id=%s AND active",
                (account["project_id"],),
            )
            assert not await service.db.sql("SELECT 1 FROM zulip_history_batches")

    asyncio.run(run())


@pytest.mark.parametrize("known", [False, True])
def test_history_respects_inactive_direct_message_memberships(incoming, known):
    api, db, account, _, _ = incoming
    linked, _ = incoming_tests.peer_account(incoming)
    remote = message(
        102,
        type="private",
        recipient_id=1,
        display_recipient=[
            {"id": 7, "full_name": "Sender"},
            {"id": 8, "full_name": "Peer"},
        ],
    )
    remote.pop("stream_id")
    remote.pop("subject")
    try:
        if known:
            incoming_tests.apply(
                incoming, {"type": "message", "message": {**remote, "id": 101}}
            )
            db.execute(
                "UPDATE messenger_stream_bindings SET active=false WHERE project_id=%s AND user_uuid=%s",
                (account["project_id"], account["owner_user_uuid"]),
            )
        request(db, account, {"subscriptions": []})

        async def run():
            async with worker(incoming, {102: remote}) as (service, _):
                await drain(service, account)
                assert not await service.db.sql("SELECT 1 FROM zulip_history_batches")
                active = await service.db.sql(
                    "SELECT active FROM messenger_stream_bindings WHERE project_id=%s AND user_uuid=%s",
                    (account["project_id"], account["owner_user_uuid"]),
                )
                assert active == [{"active": not known}]
                if known:
                    assert not await service.db.sql(
                        "SELECT 1 FROM messenger_messages WHERE project_id=%s AND provider_identity=%s",
                        (account["project_id"], "102"),
                    )
                else:
                    assert len(api.get("/v1/messenger/messages/").json()) == 1

        asyncio.run(run())
    finally:
        linked.delete()


@pytest.mark.parametrize("members", [(7, 8), (7, 8, 9)])
def test_readding_account_restores_its_direct_history_without_reopening_peer(
    incoming, members
):
    api, db, account, http, url = incoming
    linked, _ = incoming_tests.peer_account(incoming)
    group_peer = incoming_tests.peer_account(incoming, 9)[0] if 9 in members else None
    remote = message(
        101,
        type="private",
        recipient_id=1,
        display_recipient=[
            {"id": member, "full_name": str(member)} for member in members
        ],
    )
    remote.pop("stream_id")
    remote.pop("subject")
    permissions = tuple(
        f"workspace.external_account.{action}"
        for action in ("read", "create", "delete")
    )
    try:
        incoming_tests.apply(incoming, {"type": "message", "message": remote})
        db.execute(
            "UPDATE messenger_stream_bindings SET active=false WHERE project_id=%s AND user_uuid=%s",
            (account["project_id"], linked.owner_user_uuid),
        )
        path = f"/v1/messenger/external_accounts/{account['uuid']}"
        assert api.delete(path, permissions=permissions).status_code == 204
        assert api.get(path, permissions=permissions).status_code == 404
        created = api.post(
            "/v1/messenger/external_accounts/",
            json={"provider": "zulip", "settings": account["settings"]},
            permissions=permissions,
        )
        assert created.status_code == 201, created.text

        async def run():
            receiver = incoming_tests.zulip_incoming.ZulipIncomingService(url)
            await receiver.db.open()
            try:
                selected = (
                    await receiver.db.sql(
                        incoming_tests.zulip_incoming.SELECT_ACCOUNT_SQL,
                        (created.json()["uuid"],),
                    )
                )[0]
                operations = incoming_operations.Operations(receiver.db, http)
                async with receiver.db.transaction() as connection:
                    await connection.execute(
                        incoming_tests.zulip_incoming.INCOMING_TRANSACTION_SQL
                    )
                    restored = await operations.initialize(
                        connection, selected, {"user_id": 7}
                    )
                assert restored["uuid"] == account["uuid"]
            finally:
                await receiver.db.close()
            request(db, account, {"subscriptions": []})
            async with worker(incoming, {101: remote, 102: {**remote, "id": 102}}) as (
                service,
                _,
            ):
                await drain(service, account)

        asyncio.run(run())
        incoming_tests.drain()
        assert len(api.get("/v1/messenger/messages/").json()) == 2
        assert not db.execute(
            "SELECT active FROM messenger_stream_bindings WHERE project_id=%s AND user_uuid=%s",
            (account["project_id"], linked.owner_user_uuid),
        ).fetchone()[0]
    finally:
        linked.delete()
        if group_peer is not None:
            group_peer.delete()


def test_history_joins_an_existing_dm_when_this_account_has_no_membership(incoming):
    api, db, account, _, _ = incoming
    linked, _ = incoming_tests.peer_account(incoming)
    remote = message(
        101,
        type="private",
        recipient_id=1,
        display_recipient=[
            {"id": 7, "full_name": "Sender"},
            {"id": 8, "full_name": "Peer"},
        ],
    )
    remote.pop("stream_id")
    remote.pop("subject")
    try:
        incoming_tests.apply(incoming, {"type": "message", "message": remote})
        db.execute(
            "DELETE FROM messenger_stream_bindings WHERE project_id=%s AND user_uuid=%s",
            (account["project_id"], account["owner_user_uuid"]),
        )
        request(db, account, {"subscriptions": []})

        async def run():
            async with worker(incoming, {101: remote, 102: {**remote, "id": 102}}) as (
                service,
                _,
            ):
                await drain(service, account)

        asyncio.run(run())
        incoming_tests.drain()
        assert len(api.get("/v1/messenger/messages/").json()) == 2
    finally:
        linked.delete()


def test_history_imports_dm_without_reactivating_an_inactive_peer(incoming):
    api, db, account, _, _ = incoming
    linked, _ = incoming_tests.peer_account(incoming)
    remote = message(
        101,
        type="private",
        recipient_id=1,
        display_recipient=[
            {"id": 7, "full_name": "Sender"},
            {"id": 8, "full_name": "Peer"},
        ],
    )
    remote.pop("stream_id")
    remote.pop("subject")
    try:
        incoming_tests.apply(incoming, {"type": "message", "message": remote})
        db.execute(
            "UPDATE messenger_stream_bindings SET active=false WHERE project_id=%s AND user_uuid=%s",
            (account["project_id"], linked.owner_user_uuid),
        )
        request(db, account, {"subscriptions": []})

        async def run():
            async with worker(incoming, {101: remote, 102: {**remote, "id": 102}}) as (
                service,
                _,
            ):
                await drain(service, account)
                assert not await service.db.sql("SELECT 1 FROM zulip_history_batches")
                assert (
                    await service.db.sql(
                        "SELECT active FROM messenger_stream_bindings WHERE project_id=%s AND user_uuid=%s",
                        (account["project_id"], linked.owner_user_uuid),
                    )
                ) == [{"active": False}]

        asyncio.run(run())
        assert len(api.get("/v1/messenger/messages/").json()) == 2
        assert not api.get(
            "/v1/messenger/messages/", user=linked.owner_user_uuid
        ).json()
    finally:
        linked.delete()


def test_history_snapshot_preserves_native_reaction_facts_and_aggregates(incoming):
    import uuid

    api, db, account, _, _ = incoming
    incoming_tests.apply(incoming, {"type": "message", "message": message()})
    row = api.get("/v1/messenger/messages/").json()[0]
    root = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (row["uuid"],),
    ).fetchone()[0]
    db.execute(
        """
        INSERT INTO messenger_message_reaction_facts(uuid,project_id,canonical_message_uuid,placement_uuid,user_uuid,emoji_name)
        VALUES(%s,%s,%s,%s,%s,'heart')
    """,
        (
            uuid.uuid4(),
            account["project_id"],
            root,
            row["uuid"],
            account["owner_user_uuid"],
        ),
    )
    db.execute(
        "UPDATE messenger_messages SET reactions=%s WHERE project_id=%s AND uuid=%s",
        (incoming_operations.json_value({"heart": 1}), account["project_id"], root),
    )
    request(db, account)

    async def run():
        async with worker(incoming, {101: message()}) as (service, _):
            await drain(service, account)

    asyncio.run(run())
    result = api.get("/v1/messenger/messages/").json()[0]
    assert result["reactions"] == {"heart": 1}


def test_bulk_page_resolves_quotes_before_either_message_is_persisted(incoming):
    api, db, account, _, _ = incoming
    messages = {
        101: message(101, "Source text"),
        102: message(
            102,
            "@_**Sender|7** [said](https://zulip.example.com/#narrow/near/101):\n"
            "```quote\nSource text\n```",
        ),
    }
    request(db, account)

    async def run():
        async with worker(incoming, messages) as (service, _):
            await drain(service, account)

    asyncio.run(run())
    rows = api.get("/v1/messenger/messages/").json()
    by_remote = {row["provider_identity"]: row for row in rows}
    assert (
        f"urn:quote:{by_remote['101']['uuid']}"
        in by_remote["102"]["payload"]["content"]
    )
    assert "urn:forward:" not in by_remote["102"]["payload"]["content"]


@pytest.mark.parametrize("separate_streams", [False, True])
def test_history_reuses_file_bytes_by_hash_across_pages_and_keeps_acl(
    incoming, tmp_path, monkeypatch, separate_streams
):
    api, db, account, _, _ = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))

    class Storage(file_storage.LocalWorkspaceFileStorage):
        storage_type = "s3"
        storage_id = "history-test-bucket"

    storage = Storage()
    monkeypatch.setattr(
        file_storage, "get_workspace_file_storage", lambda **kw: storage
    )
    saves = mock.Mock(wraps=file_storage.save_workspace_file)
    monkeypatch.setattr(file_storage, "save_workspace_file", saves)
    messages = {
        i: message(i, f"[file](/user_uploads/1/key/{i}.txt)") for i in (101, 102)
    }
    if separate_streams:
        messages[102]["stream_id"] = 43
    request(db, account)

    async def run():
        async with worker(incoming, messages) as (service, _):
            service.http.download = mock.AsyncMock(
                return_value=(b"same bytes", "text/plain")
            )
            await drain(service, account)

    asyncio.run(run())
    assert saves.call_count == 1
    messages[103] = message(103, "[file](/user_uploads/1/key/103.txt)")
    request(db, account)
    asyncio.run(run())
    assert saves.call_count == 1
    files = api.get("/v1/messenger/files/").json()
    assert len(files) == 3
    assert db.execute(
        "SELECT count(DISTINCT storage_object_id) FROM m_workspace_files WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (1,)
    for item in files:
        assert (
            api.get(f"/v1/messenger/files/{item['uuid']}/actions/download").content
            == b"same bytes"
        )
        assert (
            api.get(
                f"/v1/messenger/files/{item['uuid']}/actions/download",
                user="00000000-0000-0000-0000-000000000999",
            ).status_code
            == 404
        )

    if separate_streams:
        allowed_stream = db.execute(
            "SELECT uuid FROM messenger_streams WHERE project_id=%s AND provider_identity='42'",
            (account["project_id"],),
        ).fetchone()[0]
        peer = uuid.uuid4()
        conftest.seed_user_stream_binding(
            db, account["project_id"], allowed_stream, peer
        )
        assert not db.execute(
            "SELECT 1 FROM m_external_accounts_v2 WHERE owner_user_uuid=%s", (peer,)
        ).fetchall()
        for item in files:
            response = api.get(
                f"/v1/messenger/files/{item['uuid']}/actions/download", user=peer
            )
            assert response.status_code == (
                200 if item["stream_uuid"] == str(allowed_stream) else 404
            )
            if response.status_code == 200:
                assert response.content == b"same bytes"

    assert api.delete(f"/v1/messenger/files/{files[0]['uuid']}").status_code == 204

    async def finish_delete():
        async with worker(incoming, {}) as (service, _):
            intents = await service.db.sql(
                "SELECT * FROM zulip_outgoing_intents WHERE project_id=%s AND entity_type='file' AND operation='delete'",
                (account["project_id"],),
            )
            assert len(intents) == 1

            await outgoing_operations.Operations(service.db, service.http).finish(
                intents[0], account, {"kind": "delete"}, {}
            )

    asyncio.run(finish_delete())
    assert (
        api.get(f"/v1/messenger/files/{files[1]['uuid']}/actions/download").content
        == b"same bytes"
    )


def test_reconnection_replaces_an_inflight_page_without_advancing_the_new_cursor(
    incoming,
):
    api, db, account, _, _ = incoming
    request(db, account, cutoff_id=101)

    async def run():
        async with worker(incoming, {101: message()}) as (service, _):
            await service.step(await job(service, account))
            prepare = service.operations.prepare_batch

            async def reconnect(selected, messages):
                batch = await prepare(selected, messages)
                db.execute(
                    "DELETE FROM zulip_history_batches WHERE external_account_uuid=%s",
                    (account["uuid"],),
                )
                request(db, account, cutoff_id=200)
                return batch

            service.operations.prepare_batch = reconnect
            await service.step(await job(service, account))
            current = await job(service, account)
            assert current["before_id"] == 201
            assert not current["finished"]
            assert not api.get("/v1/messenger/messages/").json()

    asyncio.run(run())


@pytest.mark.parametrize("provider_content", [None, "Previous provider spelling"])
def test_history_repairs_provider_content_without_changing_workspace_payload(
    incoming, provider_content
):
    _, db, account, _, _ = incoming
    original = message()
    incoming_tests.apply(incoming, {"type": "message", "message": original})
    with db.transaction():
        db.execute("SELECT set_config('workspace.zulip_incoming','on',true)")
        db.execute(
            "UPDATE messenger_messages message SET content_uuid="
            "messenger_resolve_message_content(contents.workspace_content,%s) "
            "FROM messenger_message_contents contents "
            "WHERE contents.uuid=message.content_uuid AND message.project_id=%s",
            (provider_content, account["project_id"]),
        )
    incoming_tests.publish()
    request(db, account)

    async def run():
        async with worker(incoming, {101: original}) as (service, _):
            await drain(service, account)
            current = (
                await service.db.sql(
                    "SELECT message.content_revision,contents.workspace_content,"
                    "contents.provider_content FROM messenger_messages message "
                    "JOIN messenger_message_contents contents ON contents.uuid=message.content_uuid "
                    "WHERE message.project_id=%s AND message.provider_identity='101'",
                    (account["project_id"],),
                )
            )[0]
            assert current == {
                "content_revision": 3,
                "workspace_content": {"kind": "markdown", "content": "Hello"},
                "provider_content": "Hello",
            }
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_outgoing_intents WHERE project_id=%s",
                (account["project_id"],),
            )

    asyncio.run(run())


@pytest.mark.parametrize("provider_identity", [None, "101:7:smile"])
@pytest.mark.parametrize("retained", [False, True])
def test_history_reconciles_reactions_using_persisted_fact_keys(
    incoming, provider_identity, retained
):
    _, db, account, _, _ = incoming
    reaction = {
        "user_id": 7,
        "emoji_name": "smile",
        "emoji_code": "1f604",
        "reaction_type": "unicode_emoji",
    }
    incoming_tests.apply(
        incoming, {"type": "message", "message": message(reactions=[reaction])}
    )
    canonical_name = db.execute(
        "SELECT emoji_name FROM messenger_message_reaction_facts WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone()[0]
    db.execute(
        "UPDATE messenger_message_reaction_facts SET provider_identity=%s WHERE project_id=%s",
        (provider_identity, account["project_id"]),
    )
    request(db, account)

    async def run():
        async with worker(
            incoming, {101: message(reactions=[reaction] if retained else [])}
        ) as (service, _):
            await drain(service, account)
            request(db, account)
            await drain(service, account)
            facts = await service.db.sql(
                "SELECT emoji_name FROM messenger_message_reaction_facts WHERE project_id=%s",
                (account["project_id"],),
            )
            assert facts == ([{"emoji_name": canonical_name}] if retained else [])
            root = (
                await service.db.sql(
                    "SELECT reactions FROM messenger_messages WHERE project_id=%s",
                    (account["project_id"],),
                )
            )[0]
            assert root["reactions"] == ({canonical_name: 1} if retained else {})
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_outgoing_intents WHERE project_id=%s",
                (account["project_id"],),
            )

    asyncio.run(run())


def test_message_created_during_queue_registration_is_not_lost(incoming):
    api, db, account, _, url = incoming
    captured = {}

    async def run():
        receiver = incoming_tests.zulip_incoming.ZulipIncomingService(url)
        await receiver.db.open()

        async def register(*args, **kwargs):
            # The queue snapshot includes a message created while /register was
            # in flight. It will not arrive again through the new event queue.
            await asyncio.sleep(1.05)
            captured[101] = message(
                timestamp=int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            )
            return {
                "queue_id": "history-registration-window",
                "last_event_id": 0,
                "max_message_id": 101,
                "subscriptions": [{"stream_id": 42, "name": "Channel"}],
                "realm_users": [],
                "user_topics": [],
            }

        receiver.http = mock.Mock(request=mock.AsyncMock(side_effect=register))
        receiver.poll_http = mock.Mock(
            request=mock.AsyncMock(side_effect=asyncio.CancelledError)
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await receiver.receive(account)
        finally:
            await receiver.db.close()
        async with worker(incoming, captured) as (service, _):
            # The fixture already initialized this account; only the synthetic
            # account_ready notification remains in the receiver inbox.
            service.busy = mock.AsyncMock(return_value=False)
            await drain(service, account)

    asyncio.run(run())
    messages = api.get("/v1/messenger/messages/").json()
    assert [row["provider_identity"] for row in messages] == ["101"]


def test_history_reuses_a_locally_created_topic_before_its_provider_receipt(incoming):
    api, db, account, _, _ = incoming
    incoming_tests.apply(incoming, incoming_tests.event("Existing message"))
    stream = api.get("/v1/messenger/streams/").json()[0]
    response = api.post(
        "/v1/messenger/stream_topics/",
        json={"stream_uuid": stream["uuid"], "name": "Local pending topic"},
    )
    assert response.status_code == 201, response.text
    topic = response.json()
    assert topic["provider_identity"] is None
    incoming_tests.drain()
    request(db, account)

    async def run():
        async with worker(
            incoming, {102: message(102, subject="Local pending topic")}
        ) as (service, _):
            await drain(service, account)

    asyncio.run(run())
    rows = api.get("/v1/messenger/messages/").json()
    imported = next(row for row in rows if row["provider_identity"] == "102")
    assert imported["topic_uuid"] == topic["uuid"]
    topics = api.get("/v1/messenger/stream_topics/").json()
    assert [row["uuid"] for row in topics if row["name"] == topic["name"]] == [
        topic["uuid"]
    ]


def test_history_reuses_a_delivered_local_message_before_its_realtime_echo(incoming):
    api, db, account, _, _ = incoming
    incoming_tests.apply(incoming, incoming_tests.event("Seed"))
    seed = api.get("/v1/messenger/messages/").json()[0]
    payload = {"kind": "markdown", "content": "**Original**"}
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": seed["stream_uuid"],
            "topic_uuid": seed["topic_uuid"],
            "payload": payload,
        },
    )
    assert response.status_code == 201, response.text
    local = response.json()
    assert local["provider_identity"] is None
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (local["uuid"],),
    ).fetchone()[0]
    with db.transaction():
        db.execute(history.TRANSACTION_SQL)
        delivery = db.execute(
            outgoing_operations.UPSERT_DELIVERED_MESSAGE_SQL,
            (
                uuid.uuid4(),
                account["project_id"],
                canonical,
                seed["stream_uuid"],
                account["uuid"],
                account["hostname"],
                account["external_account_id"],
                incoming_operations.json_value({"to": 42}),
                "General",
                102,
            ),
        ).fetchone()[0]
        content = db.execute(
            "SELECT messenger_resolve_message_content(%s::jsonb,%s)",
            (incoming_operations.json_value(payload), "*Original*"),
        ).fetchone()[0]
        db.execute(
            outgoing_operations.INSERT_CONTENT_SNAPSHOT_SQL,
            (uuid.uuid4(), delivery, content, 1),
        )
        db.execute(
            "UPDATE zulip_outgoing_intents SET status='done' WHERE project_id=%s",
            (account["project_id"],),
        )
    incoming_tests.drain()
    before = incoming_tests.client_events(incoming)
    request(db, account)

    async def run():
        async with worker(
            incoming, {102: message(102, "*Original*", flags=["read"])}
        ) as (service, _):
            await drain(service, account)

    asyncio.run(run())
    incoming_tests.drain()
    rows = api.get("/v1/messenger/messages/").json()
    assert len(rows) == 2
    imported = next(row for row in rows if row["provider_identity"] == "102")
    assert imported["uuid"] == local["uuid"]
    assert imported["payload"] == payload
    assert incoming_tests.client_events(incoming) == before
