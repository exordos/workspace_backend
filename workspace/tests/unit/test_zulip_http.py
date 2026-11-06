# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import socket

from aiohttp import web

from workspace.services import zulip_outgoing


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
