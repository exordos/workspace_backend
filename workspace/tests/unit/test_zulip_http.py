# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import datetime
import socket
from unittest import mock

import pytest
from aiohttp import web

from workspace.bridges.zulip import outgoing_operations
from workspace.services import zulip_outgoing


@pytest.mark.parametrize("status", [200, 302, 403, 404])
def test_zulip_upload_download_authorizes_before_using_signed_storage(status):
    async def run():
        path = "/user_uploads/1/key/sticker.png"
        signed_path = "/bucket/1/key/sticker.png?X-Amz-Signature=signature&key=%2f%2B"
        seen = []

        async def handle(request):
            seen.append((request.raw_path, request.headers.get("Authorization")))
            if request.path == path:
                if status == 302:
                    raise web.HTTPFound(storage_url)
                return web.Response(
                    status=status, body=b"image", content_type="image/png"
                )
            if request.path == f"/api/v1{path}":
                return web.json_response({"result": "success", "url": storage_url})
            return web.Response(body=b"image", content_type="image/png")

        app = web.Application()
        app.router.add_get("/{path:.*}", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        zulip = web.TCPSite(runner, "127.0.0.1", 0)
        storage = web.TCPSite(runner, "127.0.0.1", 0)
        await zulip.start()
        await storage.start()
        zulip_port, storage_port = (address[1] for address in runner.addresses)
        storage_url = f"http://127.0.0.1:{storage_port}{signed_path}"
        http = zulip_outgoing.Http()
        account = {
            "uuid": "account",
            "settings": {
                "server_url": f"http://127.0.0.1:{zulip_port}",
                "email": "user@example.com",
                "api_key": "test-key",
            },
        }
        try:
            if status in (403, 404):
                with pytest.raises(outgoing_operations.ApiError) as error:
                    await http.download(account, path)
                assert error.value.status == status
            else:
                assert await http.download(account, path) == (b"image", "image/png")
            authorization = "Basic dXNlckBleGFtcGxlLmNvbTp0ZXN0LWtleQ=="
            expected = [(path, authorization)]
            if status == 302:
                expected.extend(
                    [(f"/api/v1{path}", authorization), (signed_path, None)]
                )
            assert seen == expected
        finally:
            await http.close()
            await runner.cleanup()

    asyncio.run(run())


def test_http_connection_keeps_idle_tcp_path_alive_and_remains_reusable():
    async def run():
        body_ready = asyncio.Event()
        transports = []

        async def handle(request):
            transports.append(request.transport)
            response = web.StreamResponse()
            await response.prepare(request)
            await body_ready.wait()
            await response.write(b"ok")
            return response

        app = web.Application()
        app.router.add_get("/", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        http = zulip_outgoing.Http()
        account = {
            "uuid": "account",
            "settings": {"email": "user@example.com", "api_key": "test-key"},
        }
        try:
            session = await http.session(account)
            url = f"http://127.0.0.1:{port}/"
            async with session.get(url) as response:
                sock = response.connection.transport.get_extra_info("socket")
                assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 10
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 5
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
                body_ready.set()
                assert await response.read() == b"ok"
            async with session.get(url) as response:
                assert await response.read() == b"ok"
            assert transports[0] is transports[1]
        finally:
            body_ready.set()
            await http.close()
            await runner.cleanup()

    asyncio.run(run())


def test_send_window_includes_waiting_for_a_pooled_connection():
    async def run():
        held = asyncio.Event()
        release = asyncio.Event()
        posts = []

        async def handle(request):
            if request.path.endswith("/hold"):
                held.set()
                await release.wait()
            else:
                posts.append(request.path)
            return web.json_response({"result": "success", "id": 123})

        app = web.Application()
        app.router.add_route("*", "/api/v1/{path:.*}", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        http = zulip_outgoing.Http(1)
        account = {
            "uuid": "account",
            "settings": {
                "server_url": f"http://127.0.0.1:{port}",
                "email": "user@example.com",
                "api_key": "test-key",
            },
        }
        blocker = asyncio.create_task(http.request(account, "GET", "/hold"))
        unblocker = None
        try:
            await asyncio.wait_for(held.wait(), 2)
            now = datetime.datetime.now(datetime.timezone.utc)

            class Clock(datetime.datetime):
                @classmethod
                def now(cls, tz=None):
                    return now

            created = (
                now - datetime.timedelta(hours=1) + datetime.timedelta(seconds=0.1)
            )
            intent = {"data": {"created_at": created.isoformat()}}
            prepared = outgoing_operations.request(
                "send", "POST", "/messages", {"content": "late marker"}
            )

            async def unblock():
                await asyncio.sleep(0.3)
                release.set()

            unblocker = asyncio.create_task(unblock())
            operations = outgoing_operations.Operations(mock.Mock(), http)
            with mock.patch.object(outgoing_operations.datetime, "datetime", Clock):
                with pytest.raises(asyncio.TimeoutError):
                    await operations.execute(intent, account, prepared, False)
            release.set()
            await blocker
            assert not posts
        finally:
            if unblocker is not None:
                unblocker.cancel()
                await asyncio.gather(unblocker, return_exceptions=True)
            release.set()
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)
            await http.close()
            await runner.cleanup()

    asyncio.run(run())
