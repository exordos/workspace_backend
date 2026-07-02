#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import asyncio
import contextlib
import json
import logging
import uuid as sys_uuid

import psycopg
import websockets

from gcl_iam import engines as iam_engines
from gcl_iam import tokens as iam_tokens

from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import websocket_protocol


LOG = logging.getLogger(__name__)


class WebsocketAuthError(Exception):
    pass


class MessengerEventsAuthenticator:
    def __init__(self, iam_engine_driver):
        self._iam_engine_driver = iam_engine_driver

    def authenticate(self, auth_token):
        if not auth_token:
            raise WebsocketAuthError("Missing bearer token")
        try:
            token_info = iam_tokens.UnverifiedToken(auth_token)
            algorithm = self._iam_engine_driver.get_algorithm(token_info)
            iam_context = iam_engines.IamEngine(
                auth_token=auth_token,
                algorithm=algorithm,
                driver=self._iam_engine_driver,
            )
            user_uuid = iam_context.token_info.user_uuid
            if not isinstance(user_uuid, sys_uuid.UUID):
                user_uuid = sys_uuid.UUID(user_uuid)
            return user_uuid, iam_context.get_introspection_info().project_id
        except Exception as exc:
            LOG.exception("Websocket auth failed")
            raise WebsocketAuthError(str(exc))


class ClientConnection:
    def __init__(self, websocket, project_id, user_uuid, last_epoch_version):
        self.websocket = websocket
        self.project_id = project_id
        self.user_uuid = user_uuid
        self.last_epoch_version = last_epoch_version
        self.send_lock = asyncio.Lock()


def _json_dumps(payload):
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


class MessengerEventsWebsocketServer:
    def __init__(
        self,
        *,
        db_url,
        iam_engine_driver,
        heartbeat_interval,
        client_timeout,
        catchup_limit,
        send_queue_limit,
        poll_interval=3,
    ):
        self._db_url = db_url
        self._authenticator = MessengerEventsAuthenticator(iam_engine_driver)
        self._heartbeat_interval = heartbeat_interval
        self._client_timeout = client_timeout
        self._catchup_limit = catchup_limit
        self._send_queue_limit = send_queue_limit
        self._poll_interval = poll_interval
        self._connections = {}
        self._stop_event = None

    async def serve(self, host, port):
        self._stop_event = asyncio.Event()
        async with websockets.serve(
            self._handle,
            host,
            port,
            subprotocols=[websocket_protocol.WORKSPACE_EVENTS_PROTOCOL],
            select_subprotocol=websocket_protocol.select_subprotocol,
            ping_interval=None,
            max_queue=self._send_queue_limit,
        ):
            LOG.info("Workspace events websocket listening on %s:%s", host, port)
            listener_task = asyncio.create_task(self._listen_notifications())
            poll_task = asyncio.create_task(self._poll_connections())
            try:
                await self._stop_event.wait()
            finally:
                listener_task.cancel()
                poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener_task
                with contextlib.suppress(asyncio.CancelledError):
                    await poll_task

    def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()

    async def _handle(self, websocket, path):
        token = websocket_protocol.bearer_token_from_subprotocols(
            websocket.request_headers.get("Sec-WebSocket-Protocol")
        )
        try:
            user_uuid, project_id = await asyncio.to_thread(
                self._authenticator.authenticate,
                token,
            )
            last_epoch_version = websocket_protocol.parse_last_epoch_version(path)
        except WebsocketAuthError:
            await websocket.close(code=4401, reason="Unauthorized")
            return
        except Exception:
            LOG.exception("Invalid websocket handshake")
            await websocket.close(code=4400, reason="Bad request")
            return

        connection = ClientConnection(
            websocket=websocket,
            project_id=project_id,
            user_uuid=user_uuid,
            last_epoch_version=last_epoch_version,
        )
        self._add_connection(connection)
        try:
            await self._catch_up(connection)
            await self._consume_client_frames(connection)
        finally:
            self._remove_connection(connection)

    def _connection_key(self, connection):
        return str(connection.project_id), str(connection.user_uuid)

    def _add_connection(self, connection):
        key = self._connection_key(connection)
        self._connections.setdefault(key, set()).add(connection)

    def _remove_connection(self, connection):
        key = self._connection_key(connection)
        bucket = self._connections.get(key)
        if not bucket:
            return
        bucket.discard(connection)
        if not bucket:
            self._connections.pop(key, None)

    async def _send_json(self, connection, payload):
        async with connection.send_lock:
            await asyncio.wait_for(
                connection.websocket.send(_json_dumps(payload)),
                timeout=self._client_timeout,
            )

    async def _send_event(self, connection, event):
        await self._send_json(connection, event)
        connection.last_epoch_version = max(
            connection.last_epoch_version,
            event["epoch_version"],
        )

    async def _catch_up(self, connection):
        events = await asyncio.to_thread(
            messenger_events.get_events_after,
            project_id=connection.project_id,
            user_uuid=connection.user_uuid,
            after_epoch_version=connection.last_epoch_version,
            limit=self._catchup_limit,
        )
        for event in events:
            await self._send_event(connection, event)

    async def _broadcast_epoch(self, epoch_version):
        tasks = []
        for bucket in list(self._connections.values()):
            for connection in list(bucket):
                if epoch_version <= connection.last_epoch_version:
                    continue
                tasks.append(self._send_epoch_if_visible(connection, epoch_version))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_epoch_if_visible(self, connection, epoch_version):
        try:
            event = await asyncio.to_thread(
                messenger_events.get_event_for_user,
                project_id=connection.project_id,
                user_uuid=connection.user_uuid,
                epoch_version=epoch_version,
            )
            if event is not None:
                await self._send_event(connection, event)
        except Exception:
            LOG.exception("Failed to send workspace event")
            await connection.websocket.close(code=1011, reason="Event delivery failed")

    async def _consume_client_frames(self, connection):
        async for _raw_frame in connection.websocket:
            continue

    async def _poll_connections(self):
        while True:
            await asyncio.sleep(self._poll_interval)
            tasks = [
                self._catch_up(connection)
                for bucket in list(self._connections.values())
                for connection in list(bucket)
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _connect_listener(self):
        conn = psycopg.connect(self._db_url, autocommit=True)
        conn.execute(f"LISTEN {messenger_events.EVENTS_CHANNEL}")
        return conn

    @staticmethod
    def _wait_for_notify(conn):
        for notify in conn.notifies(timeout=1, stop_after=1):
            return notify.payload
        return None

    async def _listen_notifications(self):
        conn = await asyncio.to_thread(self._connect_listener)
        try:
            while True:
                payload = await asyncio.to_thread(self._wait_for_notify, conn)
                if payload is None:
                    continue
                try:
                    epoch_version = int(payload)
                except (TypeError, ValueError):
                    LOG.warning("Ignore invalid workspace event payload %r", payload)
                    continue
                await self._broadcast_epoch(epoch_version)
        finally:
            conn.close()
