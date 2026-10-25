# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""One asyncio sender process; SQL intents are the durable source of work."""

import asyncio
import contextlib
import json
import logging
import signal
import typing

import aiohttp
import psycopg
from psycopg import rows
from psycopg.types import json as pg_json

from workspace.bridges.zulip import outgoing_operations
from workspace.common import database


LOG = logging.getLogger(__name__)

SELECT_READY_INTENTS_SQL = """
    SELECT intent.*
    FROM zulip_outgoing_intents intent
    WHERE status IN ('new', 'checking')
      AND next_attempt_at <= now()
      AND NOT (id = ANY(%s::bigint[]))
      AND NOT EXISTS (
          SELECT 1
          FROM zulip_outgoing_intents earlier
          WHERE (
              earlier.ordering_key = intent.ordering_key
              OR (
                  earlier.entity_type = intent.entity_type
                  AND earlier.entity_uuid = intent.entity_uuid
              )
          )
            AND earlier.id < intent.id
            AND earlier.status <> 'done'
      )
    ORDER BY next_attempt_at, id
    LIMIT %s
"""

SELECT_ACCOUNT_SQL = """
    SELECT account.*, provider.hostname
    FROM m_external_accounts_v2 account
    JOIN messenger_providers provider ON provider.uuid = account.provider_uuid
    WHERE account.uuid = %s
      AND account.project_id = %s
      AND account.provider = 'zulip'
      AND NOT account.disabled
      AND account.status <> 'new'
"""

INCREMENT_ATTEMPTS_SQL = """
    UPDATE zulip_outgoing_intents
    SET attempts = attempts + 1
    WHERE id = %s
"""

SAVE_REQUEST_SQL = """
    UPDATE zulip_outgoing_intents
    SET request = %s, status = 'checking'
    WHERE id = %s
"""

CLEAR_REQUEST_SQL = """
    UPDATE zulip_outgoing_intents
    SET request = NULL
    WHERE id = %s
"""

RETRY_INTENT_SQL = """
    UPDATE zulip_outgoing_intents
    SET status = CASE WHEN request IS NULL THEN 'new' ELSE 'checking' END,
        error = %s,
        next_attempt_at = now() + %s * interval '1 second'
    WHERE id = %s
"""

FAIL_INTENT_SQL = """
    UPDATE zulip_outgoing_intents
    SET status = 'error', error = %s
    WHERE id = %s
"""

FAIL_DELIVERY_SQL = """
    UPDATE bridge_zulip_v5_deliveries delivery
    SET sending = false
    FROM zulip_outgoing_intents intent
    WHERE intent.id = %s
      AND intent.request->>'kind' = 'send'
      AND delivery.uuid = (intent.request->>'delivery_uuid')::uuid
      AND delivery.zulip_message_id IS NULL
"""


class Database:
    def __init__(
        self,
        url: str,
        size: int = 4,
        *,
        application_name: str = "workspace-zulip-outgoing",
    ) -> None:
        self.url = database.connection_url_for_process(url, application_name)
        self.application_name = application_name
        self.size = size
        self.connections: asyncio.Queue = asyncio.Queue()

    async def open(self) -> None:
        for _ in range(self.size):
            await self.connections.put(await self.connect())

    async def connect(self) -> psycopg.AsyncConnection[dict[str, typing.Any]]:
        return await psycopg.AsyncConnection.connect(
            self.url,
            autocommit=True,
            row_factory=rows.dict_row,
            application_name=self.application_name,
        )

    async def close(self) -> None:
        while not self.connections.empty():
            await (await self.connections.get()).close()

    async def sql(self, query: str, parameters: tuple = ()) -> list[dict]:
        connection = await self.connections.get()
        try:
            if connection.closed:
                connection = await self.connect()
            cursor = await connection.execute(query, parameters)
            return await cursor.fetchall() if cursor.description else []
        finally:
            self.connections.put_nowait(connection)

    @contextlib.asynccontextmanager
    async def transaction(self) -> typing.AsyncIterator[typing.Any]:
        connection = await self.connections.get()
        try:
            if connection.closed:
                connection = await self.connect()
            async with connection.transaction():
                yield connection
        finally:
            self.connections.put_nowait(connection)


class Http:
    """A shared connector grows on demand and closes idle connections."""

    def __init__(self, connections: int = 128, keepalive: float = 15) -> None:
        self.connector = aiohttp.TCPConnector(
            limit=connections,
            limit_per_host=connections,
            keepalive_timeout=keepalive,
        )
        self.sessions: dict[str, tuple[dict, aiohttp.ClientSession]] = {}

    async def close(self) -> None:
        for _, session in self.sessions.values():
            await session.close()
        await self.connector.close()

    async def session(self, account: dict) -> aiohttp.ClientSession:
        key = str(account["uuid"])
        settings = account["settings"]
        cached = self.sessions.get(key)
        if cached is None or cached[0] != settings:
            if cached is not None:
                await cached[1].close()
            session = aiohttp.ClientSession(
                connector=self.connector,
                connector_owner=False,
                auth=aiohttp.BasicAuth(settings["email"], settings["api_key"]),
                timeout=aiohttp.ClientTimeout(total=30, connect=5),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
            self.sessions[key] = (settings, session)
        return self.sessions[key][1]

    async def request(
        self,
        account: dict,
        method: str,
        path: str,
        data: dict | None = None,
        *,
        file: tuple | None = None,
        timeout: float = 30,
    ) -> dict:
        session = await self.session(account)
        settings = account["settings"]
        values = {
            key: json.dumps(value) if not isinstance(value, str) else value
            for key, value in (data or {}).items()
        }
        kwargs: dict[str, typing.Any] = {
            "params" if method == "GET" else "data": values
        }
        if file is not None:
            form = aiohttp.FormData()
            form.add_field("file", file[1], filename=file[0], content_type=file[2])
            kwargs = {"data": form}
        async with session.request(
            method,
            settings["server_url"].rstrip("/") + "/api/v1" + path,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=timeout, connect=5),
            **kwargs,
        ) as response:
            try:
                body = await response.json(content_type=None)
            except (ValueError, UnicodeError):
                if response.status >= 300:
                    raise outgoing_operations.ApiError(
                        response.status, {"msg": response.reason}, method
                    )
                raise aiohttp.ClientPayloadError("Invalid Zulip response")
            if response.status >= 300 or body.get("result") == "error":
                raise outgoing_operations.ApiError(response.status, body, method)
            return body

    async def download(self, account: dict, path: str) -> tuple[bytes, str]:
        url = account["settings"]["server_url"].rstrip("/") + path
        session = await self.session(account)
        async with session.get(url, allow_redirects=False) as response:
            if response.status >= 300:
                raise outgoing_operations.ApiError(
                    response.status, {"msg": response.reason}, "GET"
                )
            return await response.read(), response.content_type


class ZulipOutgoingService:
    def __init__(
        self,
        db_url: str,
        *,
        concurrency: int = 128,
        connections: int = 128,
        dependency_delay: float = 0.01,
        poll_interval: float = 1,
    ) -> None:
        self.db = Database(db_url)
        self.concurrency = concurrency
        self.connections = connections
        self.dependency_delay = dependency_delay
        self.poll_interval = poll_interval
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        self.tasks: dict[int, asyncio.Task] = {}

    async def run(self) -> None:
        await self.db.open()
        self.http = Http(self.connections)
        self.operations = outgoing_operations.Operations(self.db, self.http)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        # Run one sender process per database; independent work uses coroutines.
        listener = asyncio.create_task(self.listen())
        try:
            while not self.stopping.is_set():
                self.ready.clear()
                try:
                    await self.dispatch()
                except psycopg.Error:
                    LOG.exception("Cannot read Zulip outgoing intents")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.ready.wait(), self.poll_interval)
        finally:
            listener.cancel()
            tasks = [listener, *self.tasks.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.http.close()
            await self.db.close()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    async def listen(self) -> None:
        while True:
            try:
                async with await self.db.connect() as connection:
                    await connection.execute("LISTEN workspace_zulip_outgoing")
                    async for _ in connection.notifies():
                        self.ready.set()
            except psycopg.Error:
                LOG.exception("Outgoing notifications unavailable; polling continues")
                await asyncio.sleep(self.poll_interval)

    async def dispatch(self) -> None:
        room = self.concurrency - len(self.tasks)
        if room <= 0:
            return
        intents = await self.db.sql(
            SELECT_READY_INTENTS_SQL,
            (list(self.tasks), room),
        )
        for intent in intents:
            task = asyncio.create_task(
                self.process(intent), name=f"zulip-outgoing:{intent['id']}"
            )
            self.tasks[intent["id"]] = task
            task.add_done_callback(self.task_completed)

    def task_completed(self, task: asyncio.Task) -> None:
        key = next(key for key, active in self.tasks.items() if active is task)
        self.completed(key)

    def completed(self, key: int) -> None:
        task = self.tasks.pop(key)
        if not task.cancelled() and task.exception():
            LOG.error("Outgoing intent %s failed", key, exc_info=task.exception())
        self.ready.set()

    async def process(self, intent: dict) -> None:
        try:
            await self._process(intent)
        except psycopg.Error:
            # A failed retry write cannot carry its durable deadline. Keep
            # this dispatcher slot briefly instead of hammering a recovering DB.
            await asyncio.sleep(max(self.poll_interval, 1))
            raise

    async def _process(self, intent: dict) -> None:
        checking = intent["request"] is not None
        try:
            accounts = await self.db.sql(
                SELECT_ACCOUNT_SQL,
                (intent["external_account_uuid"], intent["project_id"]),
            )
            if not accounts:
                raise outgoing_operations.Pending("Waiting for the linked account")
            account = accounts[0]
            data = intent["data"]
            if data.get("account_identity", account["external_account_id"]) != account[
                "external_account_id"
            ] or data.get(
                "account_provider_uuid", str(account["provider_uuid"])
            ) != str(account["provider_uuid"]):
                raise ValueError("The external account identity changed")
            request = intent["request"]
            if request is None:
                await self.db.sql(
                    INCREMENT_ATTEMPTS_SQL,
                    (intent["id"],),
                )
                try:
                    request = await self.operations.prepare(intent, account)
                except outgoing_operations.Pending as error:
                    # Release the task slot too: a newly queued parent can run.
                    await asyncio.sleep(self.dependency_delay)
                    await self.retry(intent, str(error), 0)
                    return
                await self.db.sql(
                    SAVE_REQUEST_SQL,
                    (pg_json.Jsonb(request), intent["id"]),
                )
                checking = False
            else:
                checking = True
            result = await self.operations.execute(intent, account, request, checking)
            await self.operations.finish(intent, account, request, result)
        except asyncio.CancelledError:
            raise
        except outgoing_operations.Pending as error:
            await self.retry(intent, str(error), 0.1)
        except aiohttp.ClientConnectorError as error:
            if not checking:
                await self.db.sql(
                    CLEAR_REQUEST_SQL,
                    (intent["id"],),
                )
            await self.retry(intent, type(error).__name__, 1)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            psycopg.OperationalError,
            psycopg.errors.ReadOnlySqlTransaction,
        ) as error:
            await self.retry(intent, type(error).__name__, 1)
        except outgoing_operations.ApiError as error:
            if error.status == 429 and error.method != "GET":
                await self.db.sql(
                    CLEAR_REQUEST_SQL,
                    (intent["id"],),
                )
            if error.status in (408, 429) or error.status >= 500:
                await self.retry(
                    intent, str(error), float(error.data.get("retry-after", 1))
                )
            else:
                await self.fail(intent, str(error))
        except Exception as error:
            LOG.exception("Cannot execute outgoing intent %s", intent["id"])
            await self.fail(intent, str(error))

    async def retry(self, intent: dict, error: str, delay: float) -> None:
        await self.db.sql(
            RETRY_INTENT_SQL,
            (error, delay, intent["id"]),
        )

    async def fail(self, intent: dict, error: str) -> None:
        await self.db.sql(
            FAIL_INTENT_SQL,
            (error, intent["id"]),
        )
        await self.db.sql(
            FAIL_DELIVERY_SQL,
            (intent["id"],),
        )
        await self.operations.status(intent, "error")
