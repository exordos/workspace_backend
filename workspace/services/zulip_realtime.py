# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import asyncio
import concurrent.futures
import contextlib
import datetime
import logging
import random
import signal
import typing
import uuid as sys_uuid

import psycopg
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import flags as message_flags
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models


LOG = logging.getLogger(__name__)
EVENTS_CHANNEL = "workspace_zulip_realtime"
WORKSPACE_EVENTS_CHANNEL = "workspace_projection_tasks"


class ZulipRealtimeService:
    """Supervise account pollers and dispatch their durable event inbox."""

    def __init__(self, db_url: str, step_interval: float = 1) -> None:
        self._db_url = db_url
        self._step_interval = step_interval
        self._stopping = asyncio.Event()
        self._accounts_changed = asyncio.Event()
        self._events_ready = asyncio.Event()
        self._workspace_events_ready = asyncio.Event()
        self._pollers: dict[
            tuple[sys_uuid.UUID, sys_uuid.UUID], tuple[str, asyncio.Task[None]]
        ] = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    @staticmethod
    def _transaction(callback: typing.Callable, args: tuple) -> typing.Any:
        with contexts.Context().session_manager():
            return callback(*args)

    async def _database(
        self, callback: typing.Callable, *args: typing.Any
    ) -> typing.Any:
        future = asyncio.get_running_loop().run_in_executor(
            self._executor,
            self._transaction,
            callback,
            args,
        )
        # Finish an in-flight transaction before its cancelled poller exits.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await future
            raise

    def stop(self) -> None:
        self._stopping.set()

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        supervisors = [
            asyncio.create_task(self._watch_accounts()),
            asyncio.create_task(self._consume_events()),
            asyncio.create_task(self._consume_workspace_events()),
            asyncio.create_task(self._listen_notifications()),
        ]
        try:
            await self._stopping.wait()
        finally:
            tasks = supervisors + [entry[1] for entry in self._pollers.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._pollers.clear()
            self._executor.shutdown(wait=True)
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    async def _wait(self, event: asyncio.Event) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=self._step_interval)

    @staticmethod
    def _accounts() -> list[external_models.ExternalAccount]:
        return [
            account
            for account in external_models.ExternalAccount.objects.get_all(
                filters={
                    "provider": filters.EQ("zulip"),
                    "disabled": filters.EQ(False),
                    "status": filters.NE(
                        external_models.ExternalAccountStatus.NEW.value
                    ),
                },
            )
            if account.can_stream()
        ]

    async def _reconcile_accounts(self) -> None:
        accounts = await self._database(self._accounts)
        desired = {(account.project_id, account.uuid): account for account in accounts}
        for key, (revision, task) in list(self._pollers.items()):
            account = desired.get(key)
            if (
                account is None
                or revision != account.realtime_revision()
                or task.done()
            ):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                del self._pollers[key]
        for key, account in desired.items():
            if key not in self._pollers:
                self._pollers[key] = (
                    account.realtime_revision(),
                    asyncio.create_task(
                        self._poll_account(account),
                        name=f"zulip:{account.uuid}",
                    ),
                )

    async def _watch_accounts(self) -> None:
        while True:
            self._accounts_changed.clear()
            try:
                await self._reconcile_accounts()
            except Exception:
                LOG.exception("Failed to reconcile Zulip account subscriptions")
            await self._wait(self._accounts_changed)

    async def _consume_events(self) -> None:
        while True:
            self._events_ready.clear()
            try:
                for _index in range(100):
                    if not await self._database(models.RealtimeEvent.dispatch_one):
                        break
                else:
                    await asyncio.sleep(0)
                    continue
            except Exception:
                LOG.exception("Failed to consume the Zulip event inbox")
            await self._wait(self._events_ready)

    async def _consume_workspace_events(self) -> None:
        while True:
            self._workspace_events_ready.clear()
            try:
                for _index in range(100):
                    flags_changed = await self._database(
                        message_flags.FlagChange.reconcile_batch, 16
                    )
                    event_processed = await self._database(
                        models.WorkspaceEvent.dispatch_one
                    )
                    if not flags_changed and not event_processed:
                        break
                else:
                    await asyncio.sleep(0)
                    continue
            except Exception:
                LOG.exception("Failed to consume Workspace events for Zulip")
            await self._wait(self._workspace_events_ready)

    async def _listen_notifications(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._db_url,
                    autocommit=True,
                    application_name="workspace-zulip-realtime-listener",
                ) as connection:
                    await connection.execute(f"LISTEN {EVENTS_CHANNEL}")
                    await connection.execute(f"LISTEN {WORKSPACE_EVENTS_CHANNEL}")
                    async for notification in connection.notifies():
                        if notification.channel == WORKSPACE_EVENTS_CHANNEL:
                            self._workspace_events_ready.set()
                            continue
                        if notification.payload in ("accounts", ""):
                            self._accounts_changed.set()
                        if notification.payload in ("events", ""):
                            self._events_ready.set()
                        if notification.payload in ("flags", ""):
                            self._workspace_events_ready.set()
            except Exception:
                LOG.exception("Failed to listen for Zulip realtime notifications")
                await asyncio.sleep(self._step_interval)

    async def _poll_account(self, account: external_models.ExternalAccount) -> None:
        zulip = client.AsyncZulipClient(account)
        delay = 1.0
        try:
            subscription = await self._database(
                models.RealtimeSubscription.for_account, account
            )
            if (
                subscription is not None
                and subscription.account_revision != account.realtime_revision()
            ):
                subscription = None
            while True:
                try:
                    if subscription is None:
                        started_at = datetime.datetime.now(datetime.timezone.utc)
                        registration = await zulip.register_queue()
                        subscription = await self._database(
                            models.RealtimeSubscription.register,
                            account,
                            registration,
                            started_at,
                        )
                    events = await zulip.get_events(
                        subscription.queue_id,
                        subscription.last_event_id,
                        subscription.longpoll_timeout,
                    )
                    received_at = datetime.datetime.now(datetime.timezone.utc)
                    subscription.last_event_id = await self._database(
                        models.RealtimeSubscription.append_events,
                        account,
                        subscription.generation,
                        events,
                        received_at,
                    )
                    delay = 1.0
                except models.SubscriptionChangedError:
                    return
                except client.ZulipApiError as error:
                    if error.code == "BAD_EVENT_QUEUE_ID":
                        subscription = None
                        continue
                    LOG.warning(
                        "Zulip account %s API error: %s", account.uuid, error.code
                    )
                    await asyncio.sleep(delay + random.uniform(0, delay / 4))
                    delay = min(delay * 2, 60)
                except Exception:
                    LOG.exception("Zulip account %s polling failed", account.uuid)
                    await asyncio.sleep(delay + random.uniform(0, delay / 4))
                    delay = min(delay * 2, 60)
        finally:
            await zulip.close()
