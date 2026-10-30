# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""One bounded history process; normalized pages merge independently of realtime."""

import asyncio
import contextlib
import logging
import signal
import time

from psycopg.types import json as pg_json

from workspace.bridges.zulip import history, incoming_operations
from workspace.services import zulip_outgoing

LOG = logging.getLogger(__name__)
SELECT_JOBS_SQL = """
    WITH candidates AS (
        SELECT job.*,account.owner_user_uuid,account.external_account_id,account.settings,
               provider.hostname,account.uuid,
               row_number() OVER (
                   PARTITION BY job.project_id,job.provider_uuid
                   ORDER BY (job.registration IS NOT NULL) DESC,job.before_id DESC,job.last_run_at
               ) AS priority
        FROM zulip_history_accounts job
        JOIN m_external_accounts_v2 account ON account.uuid=job.external_account_uuid
        JOIN messenger_providers provider ON provider.uuid=job.provider_uuid
        WHERE NOT account.disabled AND account.status<>'new'
          AND COALESCE(account.settings->>'api_key','')<>''
          AND (NOT job.finished OR job.registration IS NOT NULL)
    )
    SELECT * FROM candidates WHERE priority=1 AND next_attempt_at<=now()
      AND NOT((project_id::text||':'||provider_uuid::text)=ANY(%s::text[]))
    ORDER BY last_run_at LIMIT %s
"""
BUSY_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM zulip_incoming_events WHERE project_id=%s
          AND processed_at IS NULL AND next_retry_at<=now()
          AND received_at<now()-%s*interval '1 second'
    ) OR (SELECT count(*) FROM (
        SELECT 1 FROM messenger_publication_outbox WHERE project_id=%s LIMIT %s
    ) pending)>=%s AS busy
"""


class ZulipHistoryService:
    def __init__(
        self,
        db_url: str,
        *,
        connections: int = 4,
        concurrency: int = 4,
        reaction_user_limit: int = 5,
        batch_size: int = 1000,
        publication_limit: int = 256,
        realtime_delay: float = 0.5,
        poll_interval: float = 1,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("History pages must contain 1..1000 messages")
        self.db = zulip_outgoing.Database(
            db_url, size=concurrency, application_name="workspace-zulip-history"
        )
        self.connections, self.concurrency = connections, concurrency
        self.reaction_user_limit, self.batch_size = reaction_user_limit, batch_size
        self.publication_limit, self.realtime_delay = publication_limit, realtime_delay
        self.poll_interval = poll_interval
        self.tasks: dict[str, asyncio.Task] = {}
        self.stopping, self.ready = asyncio.Event(), asyncio.Event()

    async def run(self) -> None:
        await self.db.open()
        self.http = zulip_outgoing.Http(self.connections)
        self.operations = history.Operations(
            self.db, self.http, self.reaction_user_limit
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        try:
            while not self.stopping.is_set():
                self.ready.clear()
                jobs = await self.db.sql(
                    SELECT_JOBS_SQL,
                    (list(self.tasks), self.concurrency - len(self.tasks)),
                )
                for job in jobs:
                    scope = f"{job['project_id']}:{job['provider_uuid']}"
                    self.tasks[scope] = asyncio.create_task(self.step(job))
                    self.tasks[scope].add_done_callback(self.completed)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.ready.wait(), self.poll_interval)
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            await self.http.close()
            await self.db.close()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    def completed(self, task: asyncio.Task) -> None:
        provider = next(key for key, value in self.tasks.items() if value is task)
        del self.tasks[provider]
        if not task.cancelled() and task.exception():
            LOG.error("History batch failed", exc_info=task.exception())
        self.ready.set()

    async def busy(self, project: object) -> bool:
        return (
            await self.db.sql(
                BUSY_SQL,
                (
                    project,
                    self.realtime_delay,
                    project,
                    self.publication_limit,
                    self.publication_limit,
                ),
            )
        )[0]["busy"]

    async def step(self, account: dict) -> None:
        started = time.monotonic()
        try:
            await self.db.sql(
                "UPDATE zulip_history_accounts SET last_run_at=now() WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
            if await self.busy(account["project_id"]):
                await self.defer(account["uuid"], self.poll_interval)
                return
            if account["registration"] is not None:
                await self.catalog(account)
                return
            tasks = await self.db.sql(
                """
                INSERT INTO zulip_history_batches(external_account_uuid,before_id)
                VALUES (%s,%s) ON CONFLICT(external_account_uuid) DO UPDATE
                    SET external_account_uuid=EXCLUDED.external_account_uuid RETURNING *
            """,
                (account["uuid"], account["before_id"]),
            )
            batch = tasks[0]
            if batch["messages"] is None:
                page = await self.http.request(
                    account,
                    "GET",
                    "/messages",
                    {
                        "anchor": batch["before_id"],
                        "include_anchor": False,
                        "num_before": self.batch_size,
                        "num_after": 0,
                        "apply_markdown": False,
                    },
                )
                messages = [
                    item
                    for item in page["messages"]
                    if item["id"] <= account["cutoff_id"]
                    and item["timestamp"] < account["cutoff_at"].timestamp()
                ]
                batch["messages"] = messages
                batch["next_before_id"] = min(
                    (m["id"] for m in page["messages"]), default=0
                )
                batch["finished"] = page["found_oldest"] or not page["messages"]
                await self.db.sql(
                    """
                    UPDATE zulip_history_batches SET messages=%s,next_before_id=%s,finished=%s WHERE id=%s
                """,
                    (
                        pg_json.Jsonb(messages),
                        batch["next_before_id"],
                        batch["finished"],
                        batch["id"],
                    ),
                )
            fetched = time.monotonic()
            normalized = await self.operations.prepare_batch(account, batch["messages"])
            prepared = time.monotonic()
            # Recheck admission after HTTP/S3, before taking any working-row locks.
            if await self.busy(account["project_id"]):
                await self.defer(account["uuid"], self.poll_interval)
                return
            async with self.db.transaction() as connection:
                if not await self.operations.current(connection, account):
                    return
                await self.operations.merge_batch(connection, account, normalized)
                await connection.execute(
                    """
                    UPDATE zulip_history_accounts SET before_id=%s,finished=%s,error=NULL,next_attempt_at=now()
                    WHERE external_account_uuid=%s AND cutoff_at=%s
                """,
                    (
                        batch["next_before_id"],
                        batch["finished"],
                        account["uuid"],
                        account["cutoff_at"],
                    ),
                )
                await connection.execute(
                    "DELETE FROM zulip_history_batches WHERE id=%s", (batch["id"],)
                )
                merged = time.monotonic()
            finished = time.monotonic()
            LOG.info(
                "History batch account=%s messages=%s before=%s fetch_ms=%.1f prepare_ms=%.1f merge_ms=%.1f commit_ms=%.1f total_ms=%.1f",
                account["uuid"],
                len(batch["messages"]),
                batch["next_before_id"],
                1000 * (fetched - started),
                1000 * (prepared - fetched),
                1000 * (merged - prepared),
                1000 * (finished - merged),
                1000 * (finished - started),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.exception("Cannot import history batch for %s", account["uuid"])
            await self.db.sql(
                """
                UPDATE zulip_history_accounts SET error=%s,next_attempt_at=now()+interval '5 seconds'
                WHERE external_account_uuid=%s AND cutoff_at=%s
            """,
                (str(error), account["uuid"], account["cutoff_at"]),
            )

    async def defer(self, account_uuid: object, delay: float) -> None:
        await self.db.sql(
            """
            UPDATE zulip_history_accounts SET next_attempt_at=now()+%s*interval '1 second'
            WHERE external_account_uuid=%s
        """,
            (delay, account_uuid),
        )

    async def catalog(self, account: dict) -> None:
        events = history.catalog_events(account["registration"])
        position = account["catalog_position"]
        for event in events[position : position + 100]:
            while True:
                try:
                    prepared = await self.operations.prepare(account, event)
                    async with self.db.transaction() as connection:
                        if not await self.operations.current(connection, account):
                            return
                        await connection.execute(history.TRANSACTION_SQL)
                        await self.operations.apply(connection, account, prepared)
                        position += 1
                        await connection.execute(
                            "UPDATE zulip_history_accounts SET catalog_position=%s WHERE external_account_uuid=%s AND cutoff_at=%s",
                            (position, account["uuid"], account["cutoff_at"]),
                        )
                    break
                except incoming_operations.MissingDependency as missing:
                    await self.operations.ensure(account, missing.spec)
        if position == len(events):
            await self.db.sql(
                "UPDATE zulip_history_accounts SET registration=NULL WHERE external_account_uuid=%s AND cutoff_at=%s",
                (account["uuid"], account["cutoff_at"]),
            )
