# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Receive Zulip events and apply their durable inbox using plain SQL."""

import asyncio
import contextlib
import logging
import signal
import typing

import aiohttp
import psycopg
from psycopg.types import json as pg_json

from workspace.bridges.zulip import incoming_operations
from workspace.bridges.zulip import outgoing_operations
from workspace.services import zulip_outgoing


LOG = logging.getLogger(__name__)
SELECT_ACCOUNTS_SQL = """
    SELECT account.*, provider.hostname
    FROM m_external_accounts_v2 account
    JOIN messenger_providers provider ON provider.uuid = account.provider_uuid
    WHERE account.provider = 'zulip' AND NOT account.disabled
      AND COALESCE(account.settings->>'api_key', '') <> ''
"""
SELECT_ACCOUNT_SQL = SELECT_ACCOUNTS_SQL + " AND account.uuid = %s"

SELECT_SUBSCRIPTION_SQL = """
    SELECT * FROM zulip_incoming_subscriptions
    WHERE external_account_uuid = %s AND provider_uuid = %s
      AND owner_user_uuid = %s AND account_identity = %s AND server_url = %s
"""
SAVE_SUBSCRIPTION_SQL = """
    INSERT INTO zulip_incoming_subscriptions (
        external_account_uuid, project_id, provider_uuid, owner_user_uuid,
        account_identity, server_url, queue_id, last_event_id, longpoll_timeout
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (external_account_uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        provider_uuid = EXCLUDED.provider_uuid,
        owner_user_uuid = EXCLUDED.owner_user_uuid,
        account_identity = EXCLUDED.account_identity,
        server_url = EXCLUDED.server_url,
        queue_id = EXCLUDED.queue_id,
        last_event_id = EXCLUDED.last_event_id,
        longpoll_timeout = EXCLUDED.longpoll_timeout
"""
INSERT_EVENT_SQL = """
    INSERT INTO zulip_incoming_events (
        project_id, external_account_uuid, provider_uuid, owner_user_uuid,
        account_identity, queue_id, event_id, payload, ordering_keys
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (external_account_uuid, queue_id, event_id) DO NOTHING
"""
ADVANCE_CURSOR_SQL = """
    UPDATE zulip_incoming_subscriptions
    SET last_event_id = GREATEST(last_event_id, %s)
    WHERE external_account_uuid = %s AND queue_id = %s
"""
SELECT_EVENT_SQL = """
    SELECT event.* FROM zulip_incoming_events event
    WHERE event.processed_at IS NULL AND event.next_retry_at <= now()
      AND NOT (event.id = ANY(%s::bigint[]))
      AND NOT EXISTS (
          SELECT 1 FROM zulip_incoming_events earlier
          WHERE earlier.project_id = event.project_id
            AND earlier.provider_uuid = event.provider_uuid
            AND earlier.id < event.id AND earlier.processed_at IS NULL
            AND (earlier.ordering_keys && event.ordering_keys
                 OR earlier.ordering_keys = ARRAY['*']::text[]
                 OR event.ordering_keys = ARRAY['*']::text[])
      )
    ORDER BY event.id LIMIT %s
"""
COMPLETE_EVENT_SQL = """
    UPDATE zulip_incoming_events
    SET processed_at = now(), error = NULL
    WHERE id = %s
"""
RETRY_EVENT_SQL = """
    UPDATE zulip_incoming_events
    SET attempts = attempts + 1, error = %s,
        next_retry_at = now() + %s * interval '1 second'
    WHERE id = %s
"""
FAIL_EVENT_SQL = """
    UPDATE zulip_incoming_events
    SET processed_at=now(), error=%s, attempts=attempts+1
    WHERE id=%s
"""
CURRENT_ACCOUNT_SQL = """
    SELECT 1 FROM m_external_accounts_v2
    WHERE uuid = %s AND project_id = %s AND provider_uuid = %s
      AND owner_user_uuid = %s AND settings = %s AND NOT disabled
"""
INCOMING_TRANSACTION_SQL = "SELECT set_config('workspace.zulip_incoming', 'on', true)"


class ZulipIncomingService:
    """One process, one receiver and ordered inbox consumer per account."""

    def __init__(
        self,
        db_url: str,
        *,
        connections: int = 128,
        concurrency: int = 128,
        reaction_user_limit: int = 5,
        poll_interval: float = 1,
        dependency_delay: float = 0.01,
    ) -> None:
        self.db = zulip_outgoing.Database(
            db_url, application_name="workspace-zulip-incoming"
        )
        self.connections = connections
        self.concurrency = concurrency
        self.reaction_user_limit = reaction_user_limit
        self.tasks: dict[int, asyncio.Task] = {}
        self.poll_interval = poll_interval
        self.dependency_delay = dependency_delay
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        self.accounts: dict[object, tuple[tuple, asyncio.Task]] = {}

    async def run(self) -> None:
        await self.db.open()
        self.http = zulip_outgoing.Http(self.connections)
        # Each account keeps a long poll open. They must not exhaust the pool
        # used for registration, object lookups or attachment downloads.
        self.poll_http = zulip_outgoing.Http(0)
        self.operations = incoming_operations.Operations(
            self.db, self.http, self.reaction_user_limit
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        listener = asyncio.create_task(self.listen())
        try:
            while not self.stopping.is_set():
                self.ready.clear()
                try:
                    await self.reconcile_accounts()
                    await self.dispatch()
                except Exception:
                    LOG.exception("Cannot supervise Zulip incoming accounts")
                await self.wait()
        finally:
            tasks = [
                listener,
                *self.tasks.values(),
                *(entry[1] for entry in self.accounts.values()),
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.poll_http.close()
            await self.http.close()
            await self.db.close()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    async def wait(self) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.ready.wait(), self.poll_interval)

    async def listen(self) -> None:
        while True:
            try:
                async with await self.db.connect() as connection:
                    await connection.execute("LISTEN workspace_zulip_incoming")
                    async for _ in connection.notifies():
                        self.ready.set()
            except psycopg.Error:
                LOG.exception("Incoming notifications unavailable; polling continues")
                await asyncio.sleep(self.poll_interval)

    async def reconcile_accounts(self) -> None:
        accounts = {row["uuid"]: row for row in await self.db.sql(SELECT_ACCOUNTS_SQL)}
        for key, (settings, task) in list(self.accounts.items()):
            account = accounts.get(key)
            if account is None or self.settings(account) != settings or task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                del self.accounts[key]
        for key, account in accounts.items():
            if key not in self.accounts:
                self.accounts[key] = (
                    self.settings(account),
                    asyncio.create_task(self.account(account), name=f"zulip-in:{key}"),
                )

    @staticmethod
    def settings(account: dict) -> tuple:
        return (
            account["project_id"],
            account["provider_uuid"],
            account["owner_user_uuid"],
            account["settings"],
        )

    async def current(self, connection: typing.Any, account: dict) -> bool:
        cursor = await connection.execute(
            CURRENT_ACCOUNT_SQL,
            (
                account["uuid"],
                account["project_id"],
                account["provider_uuid"],
                account["owner_user_uuid"],
                pg_json.Jsonb(account["settings"]),
            ),
        )
        return await cursor.fetchone() is not None

    async def account(self, account: dict) -> None:
        delay = 1
        try:
            while True:
                try:
                    person = await self.http.request(account, "GET", "/users/me")
                    async with self.db.transaction() as connection:
                        if not await self.current(connection, account):
                            return
                        await connection.execute(INCOMING_TRANSACTION_SQL)
                        account = await self.operations.initialize(
                            connection, account, person
                        )
                    await self.receive(account)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    LOG.exception("Zulip incoming account %s failed", account["uuid"])
                    await self.operations.account_error(account, str(error))
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
        finally:
            self.ready.set()

    async def receive(self, account: dict) -> None:
        subscriptions = await self.db.sql(
            SELECT_SUBSCRIPTION_SQL,
            (
                account["uuid"],
                account["provider_uuid"],
                account["owner_user_uuid"],
                account["external_account_id"],
                account["settings"]["server_url"],
            ),
        )
        subscription = subscriptions[0] if subscriptions else None
        delay = 1
        while True:
            try:
                if subscription is None:
                    registration = await self.http.request(
                        account,
                        "POST",
                        "/register",
                        {
                            "fetch_event_types": [
                                "realm_user",
                                "subscription",
                                "realm",
                                "attachment",
                            ],
                            "apply_markdown": False,
                            "client_capabilities": {
                                "empty_topic_name": True,
                                "notification_settings_null": True,
                                "user_settings_object": True,
                                "archived_channels": True,
                            },
                        },
                    )
                    subscription = {
                        "queue_id": registration["queue_id"],
                        "last_event_id": registration["last_event_id"],
                        "longpoll_timeout": registration.get(
                            "event_queue_longpoll_timeout_seconds", 90
                        ),
                    }
                    async with self.db.transaction() as connection:
                        if not await self.current(connection, account):
                            return
                        await connection.execute(
                            SAVE_SUBSCRIPTION_SQL,
                            (
                                account["uuid"],
                                account["project_id"],
                                account["provider_uuid"],
                                account["owner_user_uuid"],
                                account["external_account_id"],
                                account["settings"]["server_url"],
                                subscription["queue_id"],
                                subscription["last_event_id"],
                                subscription["longpoll_timeout"],
                            ),
                        )
                        initial = [
                            {"type": "realm_user", "op": "add", "person": person}
                            for person in registration.get("realm_users", [])
                        ]
                        initial.extend(
                            {
                                "type": "subscription",
                                "op": "add",
                                "subscriptions": [channel],
                            }
                            for channel in registration.get("subscriptions", [])
                        )
                        initial.extend(
                            {"type": "attachment", "op": "add", "attachment": item}
                            for item in registration.get("attachments", [])
                        )
                        initial.append({"type": "account_ready"})
                        await self.append(
                            connection,
                            account,
                            subscription,
                            [
                                {"id": index - len(initial), **item}
                                for index, item in enumerate(initial)
                            ],
                        )
                    self.ready.set()
                data = await self.poll_http.request(
                    account,
                    "GET",
                    "/events",
                    {
                        "queue_id": subscription["queue_id"],
                        "last_event_id": subscription["last_event_id"],
                        "dont_block": False,
                    },
                    timeout=subscription["longpoll_timeout"] + 10,
                )
                async with self.db.transaction() as connection:
                    if not await self.current(connection, account):
                        return
                    await self.append(connection, account, subscription, data["events"])
                if data["events"]:
                    subscription["last_event_id"] = max(
                        subscription["last_event_id"],
                        max(event["id"] for event in data["events"]),
                    )
                    self.ready.set()
                delay = 1
            except outgoing_operations.ApiError as error:
                if error.data.get("code") == "BAD_EVENT_QUEUE_ID":
                    subscription = None
                    continue
                if error.status in (401, 403):
                    raise
                await asyncio.sleep(max(delay, float(error.data.get("retry-after", 0))))
                delay = min(delay * 2, 60)
            except (aiohttp.ClientError, asyncio.TimeoutError, psycopg.Error):
                LOG.exception("Cannot receive events for %s", account["uuid"])
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def append(
        self, connection: typing.Any, account: dict, subscription: dict, events: list
    ) -> None:
        for event in sorted(events, key=lambda item: item["id"]):
            await connection.execute(
                INSERT_EVENT_SQL,
                (
                    account["project_id"],
                    account["uuid"],
                    account["provider_uuid"],
                    account["owner_user_uuid"],
                    account["external_account_id"],
                    subscription["queue_id"],
                    event["id"],
                    pg_json.Jsonb(event),
                    self.ordering_keys(account, event),
                ),
            )
        if events:
            await connection.execute(
                ADVANCE_CURSOR_SQL,
                (
                    max(event["id"] for event in events),
                    account["uuid"],
                    subscription["queue_id"],
                ),
            )

    @staticmethod
    def ordering_keys(account: dict, event: dict) -> list[str]:
        message_event = event["type"] in {
            "message",
            "update_message",
            "delete_message",
            "reaction",
            "update_message_flags",
        }
        if event["type"] == "update_message_flags" and event.get("all"):
            return ["*"]
        keys = set() if message_event else {f"account:{account['uuid']}"}
        message = event.get("message", {})
        identifiers = event.get("message_ids", event.get("messages", []))
        identifiers = [*identifiers, event.get("message_id"), message.get("id")]
        keys.update(f"message:{value}" for value in identifiers if value is not None)
        if message_event:
            return sorted(keys)
        streams = event.get("stream_ids", [])
        streams = [
            *streams,
            event.get("stream_id"),
            event.get("orig_stream_id"),
            event.get("new_stream_id"),
            message.get("stream_id"),
        ]
        streams.extend(
            item["stream_id"]
            for item in event.get("streams", event.get("subscriptions", []))
        )
        keys.update(f"stream:{value}" for value in streams if value is not None)
        people = [
            event.get("user_id"),
            message.get("sender_id"),
            event.get("person", {}).get("user_id"),
        ]
        people.extend(event.get("user_ids", []))
        people.extend(item["user_id"] for item in event.get("users", []))
        keys.update(f"user:{value}" for value in people if value is not None)
        return sorted(keys)

    async def dispatch(self) -> None:
        room = self.concurrency - len(self.tasks)
        if room <= 0:
            return
        for event in await self.db.sql(SELECT_EVENT_SQL, (list(self.tasks), room)):
            task = asyncio.create_task(
                self.process(event), name=f"zulip-event:{event['id']}"
            )
            self.tasks[event["id"]] = task
            task.add_done_callback(self.completed)

    def completed(self, task: asyncio.Task) -> None:
        key = next(key for key, value in self.tasks.items() if value is task)
        del self.tasks[key]
        if not task.cancelled() and task.exception():
            LOG.error("Incoming event %s failed", key, exc_info=task.exception())
        self.ready.set()

    async def process(self, event: dict) -> None:
        account = None
        try:
            accounts = await self.db.sql(
                SELECT_ACCOUNT_SQL, (event["external_account_uuid"],)
            )
            account = accounts[0] if accounts else None
            valid = account is not None and (
                event["provider_uuid"] == account["provider_uuid"]
                and event["owner_user_uuid"] == account["owner_user_uuid"]
                and event["account_identity"] == account["external_account_id"]
            )
            if not valid:
                await self.db.sql(COMPLETE_EVENT_SQL, (event["id"],))
                return
            account = typing.cast(dict, account)
            prepared = await self.operations.prepare(account, event["payload"])
            async with self.db.transaction() as connection:
                if not await self.current(connection, account):
                    return
                await connection.execute(INCOMING_TRANSACTION_SQL)
                await self.operations.apply(connection, account, prepared)
                await connection.execute(COMPLETE_EVENT_SQL, (event["id"],))
        except outgoing_operations.Pending as error:
            await self.db.sql(
                RETRY_EVENT_SQL, (str(error), self.dependency_delay, event["id"])
            )
            await asyncio.sleep(self.dependency_delay)
        except Exception as error:
            LOG.exception("Cannot apply Zulip event %s", event["id"])
            permanent = isinstance(error, (ValueError, KeyError, TypeError)) or (
                isinstance(error, outgoing_operations.ApiError)
                and 400 <= error.status < 500
                and error.status not in (408, 429)
            )
            if permanent:
                if account is not None:
                    await self.operations.account_error(
                        account, f"Incoming event {event['id']}: {error}"
                    )
                await self.db.sql(FAIL_EVENT_SQL, (str(error), event["id"]))
            else:
                delay = min(2 ** min(event["attempts"], 6), 60)
                await self.db.sql(RETRY_EVENT_SQL, (str(error), delay, event["id"]))
