# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import json
import socket
import threading
import time
import uuid
from unittest import mock

import websockets

from workspace.messenger_api import websocket_service
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native


POLICY = "allow_key_sharing_by_holders"


async def _receive(websocket, kind, stream_uuid):
    async with asyncio.timeout(5):
        while True:
            event = json.loads(await websocket.recv())
            if (
                event.get("payload", {}).get("kind") == kind
                and event["payload"]["uuid"] == stream_uuid
            ):
                assert event["schema_version"] == 1
                assert event["object_type"] == "stream"
                assert event["action"] == kind.split(".")[1]
                return event


async def _ready(websocket):
    async with asyncio.timeout(5):
        while True:
            frame = json.loads(await websocket.recv())
            if frame.get("type") == "ready":
                return frame


def test_key_sharing_websocket_snapshots_resume_and_legacy_events(
    api, db, _test_database_url, record_property
):
    response = api.post(native.STREAMS, json={"name": "Stream policy"})
    assert response.status_code == 201, response.text
    stream = response.json()
    peer = uuid.uuid4()
    conftest.seed_user_stream_binding(db, api.project_id, stream["uuid"], peer)
    native._drain()
    frames = []
    listener_ready = threading.Event()

    async def run():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = websocket_service.MessengerEventsWebsocketServer(
            db_url=_test_database_url,
            iam_engine_driver=mock.Mock(),
            heartbeat_interval=25,
            client_timeout=5,
            catchup_limit=500,
            send_queue_limit=32,
            # A live update must arrive through LISTEN/NOTIFY, not polling.
            poll_interval=3600,
        )
        users = {"owner": api.user_uuid, "member": peer}
        server._authenticator.authenticate = lambda token: (
            users[token],
            api.project_id,
        )
        connect_listener = server._connect_listener

        def listen():
            connection = connect_listener()
            listener_ready.set()
            return connection

        server._connect_listener = listen
        serving = asyncio.create_task(server.serve("127.0.0.1", port))
        owner = member = None
        url = f"ws://127.0.0.1:{port}/v1/events/ws"
        try:
            assert await asyncio.to_thread(listener_ready.wait, 5)
            owner = await websockets.connect(
                url, subprotocols=["workspace.events.v1", "bearer.owner"]
            )
            assert owner.subprotocol == "workspace.events.v1"
            created = await _receive(owner, "stream.created", stream["uuid"])
            assert created["payload"][POLICY] is False
            frames.append(created)
            await _ready(owner)
            member = await websockets.connect(
                url, subprotocols=["workspace.events.v1", "bearer.member"]
            )
            ready = await _ready(member)
            path = native.STREAMS + stream["uuid"]

            response = await asyncio.to_thread(api.put, path, json={POLICY: True})
            assert response.status_code == 200, response.text
            started = time.monotonic()
            await asyncio.to_thread(native._drain)
            for websocket, user in ((owner, api.user_uuid), (member, peer)):
                frame = await _receive(websocket, "stream.updated", stream["uuid"])
                assert frame["payload"][POLICY] is True
                assert frame["payload"]["user_uuid"] == str(user)
                frames.append(frame)
            record_property(
                "live_delivery_seconds", round(time.monotonic() - started, 4)
            )

            # An old client's metadata PUT omits the new field and keeps it.
            response = await asyncio.to_thread(
                api.put, path, user=peer, json={"description": "Legacy metadata update"}
            )
            assert response.status_code == 200, response.text
            assert response.json()[POLICY] is True
            await asyncio.to_thread(native._drain)
            frame = await _receive(member, "stream.updated", stream["uuid"])
            assert frame["payload"][POLICY] is True
            frames.append(frame)

            cursor = frame["epoch_version"]
            await member.close()
            response = await asyncio.to_thread(api.put, path, json={POLICY: False})
            assert response.status_code == 200, response.text
            await asyncio.to_thread(native._drain)
            member = await websockets.connect(
                url
                + f"?last_epoch_version={cursor}&epoch_generation={ready['epoch_generation']}",
                subprotocols=["workspace.events.v1", "bearer.member"],
            )
            resumed = await _receive(member, "stream.updated", stream["uuid"])
            assert resumed["payload"][POLICY] is False
            assert resumed["epoch_version"] > cursor
            frames.append(resumed)
            await _ready(member)

            # Reconstruct a saved pre-field snapshot and replay it on this socket.
            await member.close()
            db.execute(
                "UPDATE m_workspace_broadcast_message_events_v1 "
                "SET payload=payload-%s WHERE project_id=%s AND epoch_version=%s",
                (POLICY, api.project_id, resumed["epoch_version"]),
            )
            member = await websockets.connect(
                url
                + f"?last_epoch_version={cursor}&epoch_generation={ready['epoch_generation']}",
                subprotocols=["workspace.events.v1", "bearer.member"],
            )
            legacy = await _receive(member, "stream.updated", stream["uuid"])
            assert POLICY not in legacy["payload"]
            frames.append(legacy)
            await _ready(member)
            response = await asyncio.to_thread(api.put, path, json={POLICY: True})
            assert response.status_code == 200, response.text
            await asyncio.to_thread(native._drain)
            frame = await _receive(member, "stream.updated", stream["uuid"])
            assert frame["payload"][POLICY] is True
            frames.append(frame)

            rest = await asyncio.to_thread(api.get, "/v1/events/", user=peer)
            assert rest.status_code == 200, rest.text
            same = next(e for e in rest.json() if e["uuid"] == frame["uuid"])
            assert same == frame
        finally:
            for websocket in (owner, member):
                if websocket is not None:
                    await websocket.close()
            server.stop()
            await serving

    asyncio.run(run())
    record_property("stream_frames", json.dumps(frames))
