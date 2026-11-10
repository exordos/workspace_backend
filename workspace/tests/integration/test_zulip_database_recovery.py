# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import os
import socket
import urllib.parse

import psycopg
import pytest

from workspace.bridges.zulip import incoming_operations
from workspace.services import zulip_incoming, zulip_outgoing
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming


class Blackhole:
    """A real PostgreSQL transport whose existing streams can stop forwarding."""

    def __init__(self, url):
        self.url = urllib.parse.urlsplit(url)
        self.links = []
        self.tasks = set()

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        try:
            parameters = dict(urllib.parse.parse_qsl(self.url.query))
            host = parameters.get("host", self.url.hostname or "localhost")
            port = int(parameters.get("port", self.url.port or 5432))
            if host.startswith("/"):
                remote, upstream = await asyncio.open_unix_connection(
                    f"{host}/.s.PGSQL.{port}"
                )
            else:
                remote, upstream = await asyncio.open_connection(host, port)
            link = {"blocked": False, "seen": asyncio.Event()}
            self.links.append(link)

            async def forward(source, destination, response=False):
                while data := await source.read(65536):
                    if link["blocked"] and (response or not link.get("reply_only")):
                        link["seen"].set()
                    else:
                        destination.write(data)
                        await destination.drain()

            pumps = [
                asyncio.create_task(forward(reader, upstream)),
                asyncio.create_task(forward(remote, writer, response=True)),
            ]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
        finally:
            writer.close()
            if upstream:
                upstream.close()
            self.tasks.discard(task)

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        query = dict(urllib.parse.parse_qsl(self.url.query))
        query.pop("host", None)
        query.pop("port", None)
        query["sslmode"] = "disable"
        authority = (
            self.url.netloc.rsplit("@", 1)[0] + "@" if "@" in self.url.netloc else ""
        )
        self.proxy_url = urllib.parse.urlunsplit(
            self.url._replace(
                netloc=authority + f"127.0.0.1:{port}",
                query=urllib.parse.urlencode(query),
            )
        )
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("transaction", [False, True])
def test_every_stale_pool_slot_expires_and_reconnects(_test_database_url, transaction):
    async def run():
        async with Blackhole(_test_database_url) as proxy:
            db = zulip_outgoing.Database(proxy.proxy_url, size=2)
            db.io_timeout = 0.15
            await db.open()
            connections = list(db.connections._queue)
            for link in proxy.links:
                link["blocked"] = True

            async def query():
                if transaction:
                    async with db.transaction() as connection:
                        await connection.execute("SELECT 1")
                else:
                    await db.sql("SELECT 1")

            tasks = [asyncio.create_task(query()) for _ in range(2)]
            try:
                done, pending = await asyncio.wait(tasks, timeout=1.5)
                # Close before cancelling even on the old implementation, so
                # the regression itself never waits on a lost cancel reply.
                assert not pending, "Stale sockets retained every pool slot"
                assert all(
                    isinstance(task.exception(), psycopg.OperationalError)
                    for task in done
                )
                assert all(connection.closed for connection in connections)
                assert db.connections.qsize() == 2
                assert await db.sql("SELECT 42 AS value") == [{"value": 42}]
                assert len(proxy.links) > 2
            finally:
                for connection in connections:
                    await connection.close()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await db.close()
                assert db.connections.empty()

    asyncio.run(run())


def test_healthy_queries_and_transaction_rollback_keep_pool_usable(_test_database_url):
    async def run():
        db = zulip_outgoing.Database(_test_database_url, size=1)
        db.io_timeout = 1
        await db.open()
        try:
            await db.sql("SELECT pg_sleep(0.2)")
            with pytest.raises(RuntimeError):
                async with db.transaction() as connection:
                    await connection.execute(
                        "CREATE TEMP TABLE rollback_probe(value int)"
                    )
                    raise RuntimeError("rollback")
            assert await db.sql("SELECT to_regclass('rollback_probe') AS value") == [
                {"value": None}
            ]
        finally:
            await db.close()

    asyncio.run(run())


def test_lost_commit_reply_can_be_replayed_without_duplicate_message(incoming):
    api, db, account, http, url = incoming

    async def run():
        async with Blackhole(url) as proxy:
            service = zulip_incoming.ZulipIncomingService(
                proxy.proxy_url, database_io_timeout=0.15
            )
            service.db.size = 1
            await service.db.open()
            service.operations = incoming_operations.Operations(service.db, http)
            subscription = {"queue_id": "lost-commit"}
            event = {
                "id": 1,
                **incoming_tests.event("Recovered after lost commit", 902),
            }
            try:
                with pytest.raises(psycopg.OperationalError, match="deadline"):
                    async with service.admission() as (connection, stage):
                        await service.append(
                            connection, account, subscription, [event], stage
                        )
                        proxy.links[0].update(blocked=True, reply_only=True)
                # The server committed; the client cannot assume failure. The
                # native inbox uniqueness key makes replay safe on a fresh slot.
                async with service.admission() as (connection, stage):
                    await service.append(
                        connection, account, subscription, [event], stage
                    )
                for _ in range(30):
                    await service.dispatch()
                    await asyncio.gather(*list(service.tasks.values()))
                    if not await service.db.sql(
                        "SELECT 1 FROM zulip_incoming_events WHERE queue_id='lost-commit' AND processed_at IS NULL"
                    ):
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("Recovered inbox did not drain")
            finally:
                await service.db.close()

    asyncio.run(run())
    incoming_tests.publish()
    assert db.execute(
        "SELECT count(*) FROM zulip_incoming_events WHERE queue_id='lost-commit'"
    ).fetchone() == (1,)
    assert [
        m["payload"]["content"] for m in api.get("/v1/messenger/messages/").json()
    ] == ["Recovered after lost commit"]


def test_idle_listener_probes_dead_connection_and_receives_after_reconnect(
    incoming, monkeypatch
):
    _, _, _, _, url = incoming
    monkeypatch.setattr(zulip_incoming, "LISTENER_PROBE_INTERVAL", 0.05)

    async def run():
        async with Blackhole(url) as proxy:
            service = zulip_incoming.ZulipIncomingService(
                proxy.proxy_url, database_io_timeout=0.1, poll_interval=0.02
            )
            listener = asyncio.create_task(service.listen())
            try:
                for _ in range(100):
                    if proxy.links:
                        break
                    await asyncio.sleep(0.01)
                assert proxy.links
                proxy.links[0]["blocked"] = True
                for _ in range(100):
                    if len(proxy.links) > 1:
                        break
                    await asyncio.sleep(0.01)
                assert len(proxy.links) > 1, "Dead LISTEN socket was never replaced"
                sender = await psycopg.AsyncConnection.connect(url, autocommit=True)
                try:
                    for _ in range(10):
                        await sender.execute("NOTIFY workspace_zulip_incoming")
                        await asyncio.sleep(0.02)
                        if service.ready.is_set():
                            break
                    assert service.ready.is_set()
                finally:
                    await sender.close()
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
                await service.db.close()

    asyncio.run(run())


@pytest.mark.parametrize("idle", [None, 2])
def test_database_socket_has_bounded_detection_and_honors_dsn(_test_database_url, idle):
    async def run():
        parts = urllib.parse.urlsplit(_test_database_url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if idle is not None:
            query["keepalives_idle"] = str(idle)
        url = urllib.parse.urlunsplit(
            parts._replace(query=urllib.parse.urlencode(query))
        )
        db = zulip_outgoing.Database(url, size=1)
        await db.open()
        try:
            async with db.transaction() as connection:
                with socket.socket(fileno=os.dup(connection.fileno())) as tcp:
                    assert tcp.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
                    assert tcp.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == (
                        idle or 10
                    )
                    assert tcp.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 5
                    assert tcp.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
                    assert (
                        tcp.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT)
                        == 30000
                    )
        finally:
            await db.close()

    asyncio.run(run())


def test_cancellation_during_lost_reply_releases_pool_slot(_test_database_url):
    async def run():
        async with Blackhole(_test_database_url) as proxy:
            db = zulip_outgoing.Database(proxy.proxy_url, size=1)
            await db.open()
            connection = db.connections._queue[0]
            proxy.links[0]["blocked"] = True
            task = asyncio.create_task(db.sql("SELECT 1"))
            await asyncio.wait_for(proxy.links[0]["seen"].wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert connection.closed
            assert db.connections.qsize() == 1
            assert await db.sql("SELECT 7 AS value") == [{"value": 7}]
            await db.close()

    asyncio.run(run())
