# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""One bounded history process; normalized pages merge independently of realtime."""

import asyncio
import contextlib
import logging
import math
import signal
import time

import psycopg
from psycopg.types import json as pg_json

from workspace.bridges.zulip import history, history_blocks, incoming_operations
from workspace.services import zulip_outgoing

LOG = logging.getLogger(__name__)
SELECT_JOBS_SQL = """
    WITH work AS (
        SELECT job.*,false AS _recovery FROM zulip_history_accounts job
        UNION ALL
        SELECT job.*,true AS _recovery FROM zulip_history_recovery_accounts job
    ), candidates AS (
        SELECT job.*,account.owner_user_uuid,account.external_account_id,account.settings,
               provider.hostname,account.uuid,
               row_number() OVER (
                   PARTITION BY job.project_id,job.provider_uuid
                   ORDER BY job.last_run_at,(job.registration IS NOT NULL) DESC,
                            job.before_id DESC
               ) AS priority
        FROM work job
        JOIN m_external_accounts_v2 account ON account.uuid=job.external_account_uuid
        JOIN messenger_providers provider ON provider.uuid=job.provider_uuid
        WHERE NOT account.disabled AND account.status<>'new'
          AND COALESCE(account.settings->>'api_key','')<>''
          AND (NOT job.finished OR job.registration IS NOT NULL)
          AND job.next_attempt_at<=now()
    )
    SELECT * FROM candidates WHERE priority=1
      AND NOT((project_id::text||':'||provider_uuid::text)=ANY(%s::text[]))
    ORDER BY last_run_at LIMIT %s
"""
BUSY_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM zulip_incoming_events WHERE project_id=%s
          AND processed_at IS NULL AND next_retry_at<=now()
          AND COALESCE(payload->>'type','') NOT IN ('heartbeat','typing','typing_edit_message')
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
        database_io_timeout: float = 300,
        connections: int = 4,
        concurrency: int = 4,
        reaction_user_limit: int = 5,
        batch_size: int = 1000,
        publication_limit: int = 256,
        realtime_delay: float = 0.5,
        poll_interval: float = 1,
        statement_timeout: float = 60,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("History pages must contain 1..1000 messages")
        if not math.isfinite(statement_timeout) or statement_timeout <= 0:
            raise ValueError("History statement timeout must be positive")
        self.db = zulip_outgoing.Database(
            db_url,
            size=concurrency,
            application_name="workspace-zulip-history",
            statement_timeout=statement_timeout,
            io_timeout=database_io_timeout,
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
                try:
                    jobs = await self.db.sql(
                        SELECT_JOBS_SQL,
                        (list(self.tasks), self.concurrency - len(self.tasks)),
                    )
                except psycopg.Error:
                    LOG.warning(
                        "History job selection unavailable; retrying", exc_info=True
                    )
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.stopping.wait(), self.poll_interval)
                    continue
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
                f"UPDATE {history.account_table(account)} SET last_run_at=now() WHERE external_account_uuid=%s AND cutoff_at=%s",
                (account["uuid"], account["cutoff_at"]),
            )
            if await self.busy(account["project_id"]):
                await self.defer(account, self.poll_interval)
                return
            if account["registration"] is not None:
                await self.catalog(account)
                return
            async with self.db.transaction() as connection:
                if not await self.operations.current(connection, account):
                    return
                cursor = await connection.execute(
                    f"SELECT 1 FROM {history.account_table(account)} WHERE external_account_uuid=%s AND cutoff_at=%s AND registration IS NULL FOR UPDATE",
                    (account["uuid"], account["cutoff_at"]),
                )
                if await cursor.fetchone() is None:
                    return
                # A deliberate full resync can replace the historical boundary;
                # an old selected generation must never recreate its batch.
                await connection.execute(
                    f"DELETE FROM {history.batch_table(account)} WHERE external_account_uuid=%s AND before_id<>%s",
                    (account["uuid"], account["before_id"]),
                )
                cursor = await connection.execute(
                    f"""
                    INSERT INTO {history.batch_table(account)}(external_account_uuid,before_id)
                    VALUES (%s,%s) ON CONFLICT(external_account_uuid) DO UPDATE
                        SET external_account_uuid=EXCLUDED.external_account_uuid RETURNING *
                    """,
                    (account["uuid"], account["before_id"]),
                )
                batch = await cursor.fetchone()
            receipts = await self.db.sql(
                """SELECT * FROM zulip_history_block_receipts
                   WHERE external_account_uuid=%s AND upper_id<=%s
                   ORDER BY upper_id DESC LIMIT 1""",
                (account["uuid"], batch["before_id"]),
            )
            receipt = receipts[0] if receipts else None
            if batch["messages"] is None:
                await self.fetch_block(account, batch, receipt)
            elif batch["lower_id"] is None:
                # A preceding worker can persist its fetched page after migration.
                batch["lower_id"] = 0 if batch["finished"] else batch["next_before_id"]
            data_hash, state_hash = history_blocks.fingerprints(batch["messages"])
            mode, context_hash = "full", None
            if (
                receipt
                and receipt["upper_id"] == batch["before_id"]
                and receipt["data_hash"] == data_hash
            ):
                async with self.db.transaction() as connection:
                    context_hash = await history_blocks.context(
                        connection, account, batch["messages"], self.reaction_user_limit
                    )
                if context_hash == receipt["context_hash"]:
                    mode = "skip" if state_hash == receipt["state_hash"] else "flags"
            fetched = time.monotonic()
            normalized: dict = {}
            if mode == "flags":
                flags = await self.operations.prepare_flags(account, batch["messages"])
                if flags is None:
                    mode = "full"
                else:
                    normalized = flags
            if mode != "skip":
                # Only pages which still need work enter the durable JSON queue.
                await self.db.sql(
                    f"""UPDATE {history.batch_table(account)} SET messages=%s,next_before_id=%s,lower_id=%s,finished=%s WHERE id=%s""",
                    (
                        pg_json.Jsonb(batch["messages"]),
                        batch["next_before_id"],
                        batch["lower_id"],
                        batch["finished"],
                        batch["id"],
                    ),
                )
            if mode == "full":
                normalized = await self.operations.prepare_batch(
                    account, batch["messages"]
                )
            prepared = time.monotonic()
            # Recheck admission after HTTP/S3, before taking any working-row locks.
            if await self.busy(account["project_id"]):
                await self.defer(account, self.poll_interval)
                return
            async with self.db.transaction() as connection:
                if not await self.operations.current(connection, account):
                    return
                if mode != "full" and context_hash != await history_blocks.context(
                    connection, account, batch["messages"], self.reaction_user_limit
                ):
                    return
                if mode == "full":
                    await self.operations.merge_batch(connection, account, normalized)
                elif mode == "flags":
                    await self.operations.merge_personal_flags(connection, normalized)
                # A temporary missing attachment must be retried on the next scan.
                cacheable = mode != "full" or history_blocks.cacheable(
                    batch["messages"], normalized
                )
                if mode != "skip":
                    if cacheable:
                        context_hash = await history_blocks.context(
                            connection,
                            account,
                            batch["messages"],
                            self.reaction_user_limit,
                            normalized["_context"] if mode == "full" else None,
                        )
                        await connection.execute(
                            """INSERT INTO zulip_history_block_receipts(external_account_uuid,upper_id,lower_id,data_hash,state_hash,context_hash)
                               VALUES (%s,%s,%s,%s,%s,%s)
                               ON CONFLICT(external_account_uuid,upper_id) DO UPDATE SET
                                 lower_id=EXCLUDED.lower_id,data_hash=EXCLUDED.data_hash,
                                 state_hash=EXCLUDED.state_hash,context_hash=EXCLUDED.context_hash""",
                            (
                                account["uuid"],
                                batch["before_id"],
                                batch["lower_id"],
                                data_hash,
                                state_hash,
                                context_hash,
                            ),
                        )
                    else:
                        await connection.execute(
                            "DELETE FROM zulip_history_block_receipts WHERE external_account_uuid=%s AND upper_id=%s",
                            (account["uuid"], batch["before_id"]),
                        )
                await connection.execute(
                    f"""
                    UPDATE {history.account_table(account)} SET before_id=%s,finished=%s,error=NULL,next_attempt_at=now()
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
                    f"DELETE FROM {history.batch_table(account)} WHERE id=%s",
                    (batch["id"],),
                )
                if account.get("_recovery") and batch["finished"]:
                    await connection.execute(
                        "DELETE FROM zulip_history_recovery_accounts WHERE external_account_uuid=%s AND cutoff_at=%s",
                        (account["uuid"], account["cutoff_at"]),
                    )
                merged = time.monotonic()
            finished = time.monotonic()
            LOG.info(
                "History batch account=%s messages=%s before=%s mode=%s fetch_ms=%.1f prepare_ms=%.1f merge_ms=%.1f commit_ms=%.1f total_ms=%.1f range=%s",
                account["uuid"],
                len(batch["messages"]),
                batch["next_before_id"],
                mode,
                1000 * (fetched - started),
                1000 * (prepared - fetched),
                1000 * (merged - prepared),
                1000 * (finished - merged),
                1000 * (finished - started),
                "recovery" if account.get("_recovery") else "initial",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.exception("Cannot import history batch for %s", account["uuid"])
            await self.db.sql(
                f"""
                UPDATE {history.account_table(account)} SET error=%s,next_attempt_at=now()+interval '5 seconds'
                WHERE external_account_uuid=%s AND cutoff_at=%s
            """,
                (str(error), account["uuid"], account["cutoff_at"]),
            )

    async def fetch_block(
        self, account: dict, batch: dict, receipt: dict | None
    ) -> None:
        fixed = receipt is not None and receipt["upper_id"] == batch["before_id"]
        lower = (
            0
            if receipt is None
            else receipt["lower_id"]
            if fixed
            else receipt["upper_id"]
        )
        anchor = batch["before_id"]
        messages: list[dict] = []
        while True:
            page = await self.http.request(
                account,
                "GET",
                "/messages",
                {
                    "anchor": anchor,
                    "include_anchor": False,
                    "num_before": self.batch_size,
                    "num_after": 0,
                    "apply_markdown": False,
                },
            )
            raw = page["messages"]
            if any(item["id"] >= anchor for item in raw):
                raise ValueError("Zulip history page did not advance below its anchor")
            oldest = min((item["id"] for item in raw), default=0)
            messages.extend(
                item
                for item in raw
                if item["id"] >= lower
                and item["id"] <= account["cutoff_id"]
                and item["timestamp"] < account["cutoff_at"].timestamp()
                and (
                    account["subscribed_streams"] is None
                    or item["type"] != "stream"
                    or item["stream_id"] in account["subscribed_streams"]
                )
            )
            if not fixed:
                lower = max(lower, 0 if page["found_oldest"] else oldest)
                break
            if page["found_oldest"] or not raw or oldest <= lower:
                break
            # Access expansion can add more than one page inside a saved range.
            # A receipt is never compared against a truncated range.
            anchor = oldest
        batch.update(
            messages=sorted(messages, key=lambda item: item["id"]),
            lower_id=lower,
            next_before_id=lower,
            finished=lower == 0 and (page["found_oldest"] or not raw),
        )

    async def defer(self, account: dict, delay: float) -> None:
        await self.db.sql(
            f"""
            UPDATE {history.account_table(account)} SET next_attempt_at=now()+%s*interval '1 second'
            WHERE external_account_uuid=%s AND cutoff_at=%s
        """,
            (delay, account["uuid"], account["cutoff_at"]),
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
                            f"UPDATE {history.account_table(account)} SET catalog_position=%s WHERE external_account_uuid=%s AND cutoff_at=%s",
                            (position, account["uuid"], account["cutoff_at"]),
                        )
                    break
                except incoming_operations.MissingDependency as missing:
                    await self.operations.ensure(account, missing.spec)
        if position == len(events):
            await self.db.sql(
                f"UPDATE {history.account_table(account)} SET registration=NULL WHERE external_account_uuid=%s AND cutoff_at=%s",
                (account["uuid"], account["cutoff_at"]),
            )
