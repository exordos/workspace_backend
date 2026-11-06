# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Run only inside a private network namespace; emulate an idle TCP path."""

import asyncio
import json
import subprocess
from unittest import mock

from aiohttp import web

from workspace.services import zulip_outgoing


async def run():
    async def events(request):
        assert request.query["dont_block"] == "false"
        await asyncio.sleep(4)
        return web.json_response({"events": [{"id": 8, "type": "heartbeat"}]})

    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    app = web.Application()
    app.router.add_get("/api/v1/events", events)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    # Any client packet, including a TCP keepalive probe, refreshes the path.
    # Once idle state expires, return packets disappear without a TCP reset.
    rules = f"""
table inet cassi_idle_path {{
    set clients {{ type inet_service; flags dynamic,timeout; timeout 3s; }}
    chain output {{
        type filter hook output priority 0; policy accept;
        tcp dport {port} update @clients {{ tcp sport timeout 3s }} accept
        tcp sport {port} tcp dport @clients accept
        tcp sport {port} drop
    }}
}}
"""
    subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)
    http = zulip_outgoing.Http()
    account = {
        "uuid": "account",
        "settings": {
            "server_url": f"http://127.0.0.1:{port}",
            "email": "user@example.com",
            "api_key": "test-key",
        },
    }
    try:
        # Accelerate the kernel timers without changing whether keepalive is
        # enabled. The original client ignores these settings and still fails.
        with (
            mock.patch.object(zulip_outgoing, "TCP_KEEPALIVE_IDLE", 1, create=True),
            mock.patch.object(zulip_outgoing, "TCP_KEEPALIVE_INTERVAL", 1, create=True),
        ):
            result = await http.request(
                account, "GET", "/events", {"dont_block": False}, timeout=7
            )
        assert result == {"events": [{"id": 8, "type": "heartbeat"}]}
        print(json.dumps({"delivered": True}))
    finally:
        subprocess.run(
            ["nft", "delete", "table", "inet", "cassi_idle_path"], check=True
        )
        await http.close()
        await runner.cleanup()


asyncio.run(run())
