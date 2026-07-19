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
import typing
import uuid as sys_uuid

import psycopg
import websockets

from gcl_iam import engines as iam_engines
from gcl_iam import tokens as iam_tokens

from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api import websocket_protocol
from workspace.services.messenger_workers import agents as messenger_worker_agents


LOG = logging.getLogger(__name__)


class WebsocketAuthError(Exception):
    pass


class MessengerEventsAuthenticator:
    def __init__(self, iam_engine_driver: typing.Any) -> None:
        self._iam_engine_driver = iam_engine_driver

    def authenticate(
        self,
        auth_token: str | None,
    ) -> tuple[sys_uuid.UUID, sys_uuid.UUID]:
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
    def __init__(
        self,
        websocket: typing.Any,
        project_id: sys_uuid.UUID,
        user_uuid: sys_uuid.UUID,
        last_epoch_version: int,
        epoch_generation: str | None = None,
    ) -> None:
        self.websocket = websocket
        self.project_id = project_id
        self.user_uuid = user_uuid
        self.last_epoch_version = last_epoch_version
        self.epoch_generation = epoch_generation
        self.ready = False
        self.send_lock = asyncio.Lock()


def _json_dumps(payload: dict[str, typing.Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _call_with_database_session(
    callback: typing.Callable[..., typing.Any],
    **kwargs: typing.Any,
) -> typing.Any:
    """Own one transaction at the standalone websocket worker boundary."""
    with messenger_worker_agents.database_session_context():
        return callback(**kwargs)


class MessengerEventsWebsocketServer:
    def __init__(
        self,
        *,
        db_url: str,
        iam_engine_driver: typing.Any,
        heartbeat_interval: float,
        client_timeout: float,
        catchup_limit: int,
        send_queue_limit: int,
        poll_interval: float = 3,
    ) -> None:
        self._db_url = db_url
        self._authenticator = MessengerEventsAuthenticator(iam_engine_driver)
        self._heartbeat_interval = heartbeat_interval
        self._client_timeout = client_timeout
        self._catchup_limit = catchup_limit
        self._send_queue_limit = send_queue_limit
        self._poll_interval = poll_interval
        self._connections: dict[tuple[str, str], set[ClientConnection]] = {}
        self._stop_event: asyncio.Event | None = None

    async def serve(self, host: str, port: int) -> None:
        self._stop_event = asyncio.Event()
        async with websockets.serve(
            self._handle,
            host,
            port,
            subprotocols=typing.cast(
                typing.Any,
                [websocket_protocol.WORKSPACE_EVENTS_PROTOCOL],
            ),
            select_subprotocol=typing.cast(
                typing.Any,
                websocket_protocol.select_subprotocol,
            ),
            ping_interval=self._heartbeat_interval,
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

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def _handle(self, websocket: typing.Any, path: str) -> None:
        token = websocket_protocol.bearer_token_from_subprotocols(
            websocket.request_headers.get("Sec-WebSocket-Protocol")
        )
        try:
            user_uuid, project_id = await asyncio.to_thread(
                self._authenticator.authenticate,
                token,
            )
            last_epoch_version = websocket_protocol.parse_last_epoch_version(path)
            epoch_generation = websocket_protocol.parse_epoch_generation(path)
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
            epoch_generation=epoch_generation,
        )
        self._add_connection(connection)
        try:
            if not await self._catch_up_until_current(connection):
                return
            await self._send_ready(connection)
            await self._consume_client_frames(connection)
        finally:
            self._remove_connection(connection)

    def _connection_key(self, connection: ClientConnection) -> tuple[str, str]:
        return str(connection.project_id), str(connection.user_uuid)

    def _add_connection(self, connection: ClientConnection) -> None:
        key = self._connection_key(connection)
        self._connections.setdefault(key, set()).add(connection)

    def _remove_connection(self, connection: ClientConnection) -> None:
        key = self._connection_key(connection)
        bucket = self._connections.get(key)
        if not bucket:
            return
        bucket.discard(connection)
        if not bucket:
            self._connections.pop(key, None)

    async def _send_json(
        self,
        connection: ClientConnection,
        payload: dict[str, typing.Any],
    ) -> None:
        async with connection.send_lock:
            await asyncio.wait_for(
                connection.websocket.send(_json_dumps(payload)),
                timeout=self._client_timeout,
            )

    async def _send_event(
        self,
        connection: ClientConnection,
        event: dict[str, typing.Any],
    ) -> None:
        await self._send_json(connection, event)
        connection.last_epoch_version = max(
            connection.last_epoch_version,
            event["epoch_version"],
        )

    async def _catch_up(self, connection: ClientConnection) -> int | bool:
        try:
            events = await asyncio.to_thread(
                _call_with_database_session,
                messenger_events.get_events_after,
                project_id=connection.project_id,
                user_uuid=connection.user_uuid,
                after_epoch_version=connection.last_epoch_version,
                limit=self._catchup_limit,
                epoch_generation=connection.epoch_generation,
            )
        except messenger_exceptions.EventsCursorExpiredError as error:
            await self._send_json(connection, error.as_dict())
            await connection.websocket.close(code=4410, reason="epoch_pruned")
            return False
        for event in events:
            await self._send_event(connection, event)
        return len(events)

    async def _catch_up_until_current(
        self,
        connection: ClientConnection,
    ) -> bool:
        if connection.last_epoch_version == 0 and connection.epoch_generation is None:
            cursor = await asyncio.to_thread(
                _call_with_database_session,
                messenger_events.get_event_cursor,
                project_id=connection.project_id,
                user_uuid=connection.user_uuid,
            )
            connection.epoch_generation = cursor["epoch_generation"]
        while True:
            count = await self._catch_up(connection)
            if count is False:
                return False
            if count < self._catchup_limit:
                return True

    async def _send_ready(self, connection: ClientConnection) -> bool:
        cursor = await asyncio.to_thread(
            _call_with_database_session,
            messenger_events.get_event_cursor,
            project_id=connection.project_id,
            user_uuid=connection.user_uuid,
        )
        if cursor["epoch_generation"] != connection.epoch_generation:
            error = messenger_exceptions.EventsCursorExpiredError(
                reason="epoch_generation_changed",
                epoch_generation=cursor["epoch_generation"],
                current_epoch_version=cursor["current_epoch_version"],
                minimum_epoch_version=cursor["minimum_epoch_version"],
            )
            await self._send_json(connection, error.as_dict())
            await connection.websocket.close(code=4410, reason="epoch_pruned")
            return False
        payload = {
            "type": "ready",
            "epoch_generation": cursor["epoch_generation"],
            "epoch_version": connection.last_epoch_version,
        }
        async with connection.send_lock:
            await asyncio.wait_for(
                connection.websocket.send(_json_dumps(payload)),
                timeout=self._client_timeout,
            )
            connection.epoch_generation = cursor["epoch_generation"]
            connection.ready = True
        return True

    async def _broadcast_epoch(self, epoch_version: int) -> None:
        del epoch_version
        # PostgreSQL epochs only wake the service; each connection catches up
        # from its own per-user event cursor.
        tasks = []
        for bucket in list(self._connections.values()):
            for connection in list(bucket):
                if not connection.ready:
                    continue
                tasks.append(self._catch_up(connection))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _consume_client_frames(self, connection: ClientConnection) -> None:
        async for _raw_frame in connection.websocket:
            continue

    async def _poll_connections(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            tasks = [
                self._catch_up(connection)
                for bucket in list(self._connections.values())
                for connection in list(bucket)
                if connection.ready
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _connect_listener(self) -> typing.Any:
        conn = psycopg.connect(self._db_url, autocommit=True)
        conn.execute(f"LISTEN {messenger_events.EVENTS_CHANNEL}")
        return conn

    @staticmethod
    def _wait_for_notify(conn: typing.Any) -> str | None:
        for notify in conn.notifies(timeout=1, stop_after=1):
            return notify.payload
        return None

    async def _listen_notifications(self) -> None:
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
