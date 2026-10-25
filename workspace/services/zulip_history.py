# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""One bounded, resumable SQL/asyncio history process per database."""

import asyncio
import contextlib
import datetime
import logging
import signal
import typing

import psycopg
from psycopg.types import json as pg_json

from workspace.bridges.zulip import history
from workspace.bridges.zulip import incoming_operations
from workspace.bridges.zulip import outgoing_operations
from workspace.services import zulip_incoming
from workspace.services import zulip_outgoing


LOG = logging.getLogger(__name__)
SELECT_JOBS_SQL = """
    SELECT job.*,account.settings,provider.hostname
    FROM zulip_history_jobs job
    JOIN m_external_accounts_v2 account ON account.uuid=job.external_account_uuid
    JOIN messenger_providers provider ON provider.uuid=job.provider_uuid
    WHERE NOT account.disabled AND account.provider='zulip' AND account.status<>'new'
      AND COALESCE(account.settings->>'api_key','')<>''
      AND account.project_id=job.project_id AND account.provider_uuid=job.provider_uuid
      AND account.owner_user_uuid=job.owner_user_uuid
      AND account.external_account_id=job.account_identity
      AND account.settings->>'server_url'=job.server_url
      AND job.next_attempt_at<=now()
      AND NOT(job.external_account_uuid=ANY(%s::uuid[]))
      AND NOT(job.provider_uuid=ANY(%s::uuid[]))
      AND (job.registration IS NOT NULL OR job.requested_at>job.completed_at
           OR NOT job.backfill_done OR job.repair_after<=now()
           OR EXISTS (SELECT 1 FROM zulip_history_intents intent
                      WHERE intent.external_account_uuid=job.external_account_uuid
                        AND NOT intent.failed AND intent.next_attempt_at<=now()))
    ORDER BY job.last_run_at,job.external_account_uuid LIMIT %s
"""
SELECT_INTENT_SQL = """
    SELECT * FROM zulip_history_intents
    WHERE external_account_uuid=%s AND NOT failed AND next_attempt_at<=now()
    ORDER BY id LIMIT 1
"""
SAVE_INTENT_SQL = """
    INSERT INTO zulip_history_intents (
        external_account_uuid,entity_key,payload,expected,fetched_at,identity
    ) VALUES (%s,%s,%s,%s,%s,%s)
    ON CONFLICT (external_account_uuid,entity_key) DO UPDATE SET
        payload=EXCLUDED.payload,expected=EXCLUDED.expected,
        fetched_at=EXCLUDED.fetched_at,identity=EXCLUDED.identity,
        next_attempt_at=now(),failed=false,error=NULL
    WHERE zulip_history_intents.fetched_at<EXCLUDED.fetched_at
"""
BUSY_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM zulip_incoming_events
        WHERE project_id=%s AND processed_at IS NULL AND next_retry_at<=now()
          AND received_at<now()-%s*interval '1 second'
    ) OR (SELECT count(*) FROM (
        SELECT 1 FROM messenger_publication_outbox WHERE project_id=%s LIMIT %s
    ) publication)>= %s AS busy
"""
ACTIVITY_SQL = """
    WITH matching AS MATERIALIZED (
        SELECT processed_at FROM zulip_incoming_events
        WHERE project_id=%s AND provider_uuid=%s
          AND (processed_at IS NULL OR received_at>=%s)
          AND (ordering_keys && %s::text[] OR ordering_keys=ARRAY['*']::text[])
    )
    SELECT EXISTS (SELECT 1 FROM matching WHERE processed_at IS NULL) OR EXISTS (
        SELECT 1 FROM zulip_outgoing_intents
        WHERE project_id=%s AND status<>'done'
          AND (entity_uuid::text=ANY(%s::text[])
               OR data->>'message_uuid'=ANY(%s::text[])
               OR ordering_key=ANY(%s::text[]))
    ) AS pending,
    EXISTS (SELECT 1 FROM matching WHERE processed_at IS NOT NULL) AS changed
"""
SELECT_MISSING_SQL = """
    SELECT DISTINCT message.provider_identity::bigint AS remote_id
    FROM messenger_messages message
    JOIN messenger_message_placements placement
      ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid
    JOIN messenger_user_message_bindings access
      ON access.project_id=placement.project_id AND access.placement_uuid=placement.uuid
     AND access.user_uuid=%s AND access.visibility='visible'
    WHERE message.project_id=%s AND message.provider_uuid=%s AND message.source_name='zulip'
      AND message.deleted_at IS NULL AND message.provider_identity ~ '^[0-9]+$'
      AND message.provider_identity::bigint>=%s AND message.provider_identity::bigint<=%s
      AND NOT(message.provider_identity::bigint=ANY(%s::bigint[]))
    ORDER BY remote_id DESC LIMIT %s
"""
SELECT_LATEST_KNOWN_SQL = """
    SELECT COALESCE(max(message.provider_identity::bigint),0) AS remote_id
    FROM messenger_messages message
    JOIN messenger_message_placements placement
      ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid
    JOIN messenger_user_message_bindings access
      ON access.project_id=placement.project_id AND access.placement_uuid=placement.uuid
     AND access.user_uuid=%s AND access.visibility='visible'
    WHERE message.project_id=%s AND message.provider_uuid=%s
      AND message.source_name='zulip' AND message.provider_identity ~ '^[0-9]+$'
      AND message.deleted_at IS NULL
"""
HISTORY_TRANSACTION_SQL = """
    SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
    SET LOCAL statement_timeout='3s';
    SET LOCAL lock_timeout='50ms';
    SELECT set_config('workspace.zulip_incoming','on',true),
           set_config('workspace.zulip_history','on',true)
"""
DEFER_INTENT_SQL = """
    UPDATE zulip_history_intents SET next_attempt_at=now()+interval '1 second'
    WHERE id=%s
"""
DISCARD_CATALOG_SQL = """
    WITH discarded AS (
        DELETE FROM zulip_history_intents WHERE id=%s
        RETURNING external_account_uuid,entity_key,fetched_at
    )
    UPDATE zulip_history_jobs job SET registration=jsonb_set(registration,'{_ignored}',
        COALESCE(registration->'_ignored','[]'::jsonb)||to_jsonb(discarded.entity_key))
    FROM discarded WHERE job.external_account_uuid=discarded.external_account_uuid
      AND job.registration_started_at=discarded.fetched_at
"""
CATALOG_PROGRESS_SQL = """
    UPDATE zulip_history_jobs SET catalog_position=%s,
        registration=CASE WHEN %s THEN NULL ELSE registration END,flag_after_id=%s
    WHERE external_account_uuid=%s AND registration_queue=%s
"""
CURRENT_ACCOUNT_SQL = """
    SELECT 1 FROM m_external_accounts_v2
    WHERE uuid=%s AND project_id=%s AND provider_uuid=%s AND owner_user_uuid=%s
      AND settings=%s AND NOT disabled AND external_account_id=%s
"""

TOUCH_JOB_SQL = """
    UPDATE zulip_history_jobs SET last_run_at=now()
    WHERE external_account_uuid=%s
"""
COMPLETE_STEP_SQL = """
    UPDATE zulip_history_jobs SET attempts=0,error=NULL
    WHERE external_account_uuid=%s
"""
DEFER_JOB_SQL = """
    UPDATE zulip_history_jobs SET next_attempt_at=now()+%s*interval '1 second'
    WHERE external_account_uuid=%s
"""

RETRY_JOB_SQL = """
    UPDATE zulip_history_jobs SET attempts=attempts+1,error=%s,
        next_attempt_at=now()+%s*interval '1 second'
    WHERE external_account_uuid=%s
"""

SAVE_RECENT_PROGRESS_SQL = """
    UPDATE zulip_history_jobs SET newest_id=%s,target_id=%s,
        scanning_at=%s,completed_at=CASE WHEN %s THEN %s ELSE completed_at END,
        before_id=CASE WHEN %s THEN %s ELSE before_id END,
        backfill_done=CASE WHEN %s THEN %s ELSE backfill_done END,
        repair_after=LEAST(repair_after,now())
    WHERE external_account_uuid=%s
"""

RETRY_INTENT_SQL = """
    UPDATE zulip_history_intents SET attempts=attempts+1,error=%s,failed=%s,
        next_attempt_at=now()+%s*interval '1 second' WHERE id=%s
"""

SAVE_FORWARD_PROGRESS_SQL = """
    UPDATE zulip_history_jobs SET newest_id=%s,
        target_id=CASE WHEN %s THEN NULL ELSE target_id END,
        completed_at=CASE WHEN %s THEN scanning_at ELSE completed_at END
    WHERE external_account_uuid=%s
"""

COUNT_PENDING_SQL = """
    SELECT count(*) AS count
    FROM (SELECT 1 FROM zulip_history_intents
    WHERE NOT failed
    LIMIT %s) pending
"""
SCHEDULE_NEXT_SQL = """
    UPDATE zulip_history_jobs job SET next_attempt_at=CASE
        WHEN registration IS NOT NULL OR requested_at>completed_at
          OR NOT backfill_done OR target_id IS NOT NULL THEN now()
        ELSE LEAST(repair_after,COALESCE((
            SELECT min(next_attempt_at) FROM zulip_history_intents intent
            WHERE intent.external_account_uuid=job.external_account_uuid AND NOT failed
        ),repair_after)) END
    WHERE external_account_uuid=%s
"""

SAVE_BACKFILL_PROGRESS_SQL = """
    UPDATE zulip_history_jobs SET before_id=%s,backfill_done=%s
    WHERE external_account_uuid=%s
"""

SAVE_REPAIR_PROGRESS_SQL = """
    UPDATE zulip_history_jobs SET repair_before_id=%s,
        repair_after=CASE WHEN %s THEN now()+%s*interval '1 second' ELSE now() END
    WHERE external_account_uuid=%s
"""


def account_from_job(job: dict) -> dict:
    return {
        **job,
        "uuid": job["external_account_uuid"],
        "external_account_id": job["account_identity"],
    }


def identity(account: dict) -> list[str]:
    return [
        str(account[key])
        for key in (
            "project_id",
            "provider_uuid",
            "owner_user_uuid",
            "external_account_id",
        )
    ] + [account["settings"]["server_url"]]


class ZulipHistoryService:
    def __init__(
        self,
        db_url: str,
        *,
        concurrency: int = 4,
        connections: int = 4,
        batch_size: int = 500,
        queue_limit: int = 1000,
        publication_limit: int = 256,
        realtime_delay: float = 0.5,
        repair_interval: float = 86400,
        poll_interval: float = 1,
        reaction_user_limit: int = 5,
    ) -> None:
        if not 1 <= batch_size <= 1000 or queue_limit < batch_size:
            raise ValueError(
                "Use a batch size of 1..1000 and a queue limit of at least one batch"
            )
        self.db = zulip_outgoing.Database(
            db_url, size=2, application_name="workspace-zulip-history"
        )
        self.concurrency, self.connections = concurrency, connections
        self.batch_size, self.queue_limit = batch_size, queue_limit
        self.publication_limit, self.realtime_delay = publication_limit, realtime_delay
        self.repair_interval, self.poll_interval = repair_interval, poll_interval
        self.reaction_user_limit = reaction_user_limit
        self.ready, self.stopping = asyncio.Event(), asyncio.Event()
        self.tasks: dict[object, tuple[object, asyncio.Task]] = {}
        self.scanning = False

    async def run(self) -> None:
        await self.db.open()
        self.http = zulip_outgoing.Http(self.connections)
        self.operations = history.Operations(
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
                    await self.dispatch()
                except psycopg.Error:
                    LOG.exception("Cannot dispatch Zulip history jobs")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.ready.wait(), self.poll_interval)
        finally:
            tasks = [listener, *(item[1] for item in self.tasks.values())]
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
                    await connection.execute("LISTEN workspace_zulip_history")
                    async for _ in connection.notifies():
                        self.ready.set()
            except psycopg.Error:
                LOG.exception("History notifications unavailable; polling continues")
                await asyncio.sleep(self.poll_interval)

    async def dispatch(self) -> None:
        room = self.concurrency - len(self.tasks)
        if room <= 0:
            return
        jobs = await self.db.sql(
            SELECT_JOBS_SQL,
            (
                list(self.tasks),
                [item[0] for item in self.tasks.values()],
                room,
            ),
        )
        providers = {item[0] for item in self.tasks.values()}
        for job in jobs:
            if job["provider_uuid"] in providers:
                continue
            providers.add(job["provider_uuid"])
            task = asyncio.create_task(
                self.step(job), name=f"zulip-history:{job['external_account_uuid']}"
            )
            self.tasks[job["external_account_uuid"]] = job["provider_uuid"], task
            task.add_done_callback(self.completed)

    def completed(self, task: asyncio.Task) -> None:
        key = next(key for key, value in self.tasks.items() if value[1] is task)
        del self.tasks[key]
        if not task.cancelled() and task.exception():
            LOG.error("History job %s failed", key, exc_info=task.exception())
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

    async def step(self, job: dict) -> None:
        try:
            await self._step(job)
        except psycopg.Error:
            await asyncio.sleep(max(self.poll_interval, 1))
            raise

    async def _step(self, job: dict) -> None:
        account = account_from_job(job)
        try:
            await self.db.sql(
                TOUCH_JOB_SQL,
                (account["uuid"],),
            )
            if await self.busy(account["project_id"]):
                await self.defer(account["uuid"], self.poll_interval)
                return
            intents = await self.db.sql(SELECT_INTENT_SQL, (account["uuid"],))
            if intents:
                await self.process(account, intents[0])
                await self.db.sql(SCHEDULE_NEXT_SQL, (account["uuid"],))
                return
            # One page admission at a time gives a hard bound without database locks.
            # Other tasks can prepare/apply already admitted intentions concurrently.
            if self.scanning:
                await self.defer(account["uuid"], self.poll_interval)
                return
            self.scanning = True
            try:
                queued = (
                    await self.db.sql(
                        COUNT_PENDING_SQL,
                        (self.queue_limit,),
                    )
                )[0]["count"]
                if queued + self.batch_size > self.queue_limit:
                    await self.defer(account["uuid"], self.poll_interval)
                    return
                if job["registration"] and (
                    job["requested_at"] > job["completed_at"]
                    or job["target_id"] is not None
                ):
                    await self.scan(account, job)
                elif job["registration"] is not None:
                    await self.catalog(account, job)
                else:
                    await self.scan(account, job)
            finally:
                self.scanning = False
            await self.db.sql(
                COMPLETE_STEP_SQL,
                (account["uuid"],),
            )
            await self.db.sql(SCHEDULE_NEXT_SQL, (account["uuid"],))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.exception("Cannot reconcile Zulip history for %s", account["uuid"])
            delay = min(2 ** min(job["attempts"], 8), 300)
            if isinstance(error, outgoing_operations.ApiError) and error.status == 429:
                delay = max(delay, float(error.data.get("retry-after", 1)))
            await self.db.sql(
                RETRY_JOB_SQL,
                (str(error), delay, account["uuid"]),
            )

    async def defer(self, account_uuid: object, delay: float) -> None:
        await self.db.sql(
            DEFER_JOB_SQL,
            (delay, account_uuid),
        )

    async def current(self, connection: typing.Any, account: dict) -> bool:
        return bool(
            await incoming_operations.rows(
                connection,
                CURRENT_ACCOUNT_SQL,
                (
                    account["uuid"],
                    account["project_id"],
                    account["provider_uuid"],
                    account["owner_user_uuid"],
                    pg_json.Jsonb(account["settings"]),
                    account["external_account_id"],
                ),
            )
        )

    async def enqueue(
        self,
        connection: typing.Any,
        account: dict,
        event: dict,
        expected: dict | None,
        fetched_at: datetime.datetime,
    ) -> None:
        await connection.execute(
            SAVE_INTENT_SQL,
            (
                account["uuid"],
                history.entity_key(event),
                pg_json.Jsonb(event),
                pg_json.Jsonb(expected),
                fetched_at,
                pg_json.Jsonb(identity(account)),
            ),
        )

    async def catalog(self, account: dict, job: dict) -> None:
        registration = job["registration"]
        ignored = set(registration.get("_ignored", []))
        events = history.catalog_events(registration)
        position = job["catalog_position"]
        page = events[position : position + self.batch_size]
        finished = False
        flag_after = job["flag_after_id"]
        if not page and "subscriptions" in registration:
            subscribed = {
                str(item["stream_id"]) for item in registration["subscriptions"]
            }
            streams = await self.db.sql(
                history.SELECT_CATALOG_STREAMS_SQL,
                (
                    account["owner_user_uuid"],
                    account["project_id"],
                    account["provider_uuid"],
                ),
            )
            page = [
                {
                    "type": "subscription",
                    "op": "remove",
                    "subscriptions": [{"stream_id": int(item["provider_identity"])}],
                }
                for item in streams
                if item["provider_identity"].isdigit()
                and item["membership"]
                and item["membership"]["active"]
                and item["provider_identity"] not in subscribed
                and f"subscription:{item['provider_identity']}" not in ignored
                and history.timestamp(item["membership"]["updated_at"])
                <= job["registration_started_at"]
                and item["updated_at"].replace(tzinfo=datetime.timezone.utc)
                <= job["registration_started_at"]
            ][: self.batch_size]
        if not page and "attachments" in registration:
            removed = await self.db.sql(
                history.SELECT_REMOVED_ATTACHMENTS_SQL,
                (
                    account["project_id"],
                    account["provider_uuid"],
                    account["external_account_id"],
                    [item["id"] for item in registration["attachments"]],
                    job["registration_started_at"],
                    job["registration_started_at"],
                    [
                        int(key.removeprefix("attachment:"))
                        for key in ignored
                        if key.startswith("attachment:")
                    ],
                    self.batch_size,
                ),
            )
            page = [
                {
                    "type": "attachment",
                    "op": "remove",
                    "attachment": {
                        "id": item["attachment_id"],
                        "name": item["name"],
                        "path_id": item["path"].removeprefix("/user_uploads/"),
                    },
                }
                for item in removed
            ]
        catalog_page = position < len(events)
        changes = (
            await self.operations.catalog_differences(account, page) if page else []
        )
        changes = [
            event for event in changes if history.entity_key(event) not in ignored
        ]
        if not page:
            rows = (
                await self.db.sql(
                    history.SELECT_FLAG_PAGE_SQL,
                    (
                        account["project_id"],
                        account["owner_user_uuid"],
                        account["provider_uuid"],
                        flag_after,
                        self.batch_size,
                    ),
                )
                if "unread_msgs" in registration or "starred_messages" in registration
                else []
            )
            unread = {
                value
                for kind in ("pms", "streams", "huddles")
                for group in registration.get("unread_msgs", {}).get(kind, [])
                for value in group["unread_message_ids"]
            }
            stars = set(registration.get("starred_messages", []))
            complete_read = (
                "unread_msgs" in registration
                and not registration["unread_msgs"]["old_unreads_missing"]
            )
            for row in rows:
                read = row["remote_id"] not in unread if complete_read else row["read"]
                if row["remote_id"] in unread:
                    read = False
                star = (
                    row["remote_id"] in stars
                    if "starred_messages" in registration
                    else row["starred"]
                )
                if read != row["read"] or star != row["starred"]:
                    changes.append(
                        {
                            "type": "history_flags",
                            "message_id": row["remote_id"],
                            "flags": (["read"] if read else [])
                            + (["starred"] if star else []),
                        }
                    )
            if rows:
                flag_after = rows[-1]["remote_id"]
            finished = len(rows) < self.batch_size
        flags = [
            item["message_id"] for item in changes if item["type"] == "history_flags"
        ]
        snapshots = await self.operations.snapshots(account, flags) if flags else {}
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                return
            for event in changes:
                await self.enqueue(
                    connection,
                    account,
                    event,
                    snapshots.get(str(event.get("message_id"))),
                    job["registration_started_at"],
                )
            await connection.execute(
                CATALOG_PROGRESS_SQL,
                (
                    position + len(page) if catalog_page else position,
                    finished,
                    flag_after,
                    account["uuid"],
                    job["registration_queue"],
                ),
            )

    async def scan(self, account: dict, job: dict) -> None:
        started = (await self.db.sql("SELECT clock_timestamp() AS value"))[0]["value"]
        initial = job["before_id"] is None and not job["backfill_done"]
        refresh = job["requested_at"] > job["completed_at"] and job["target_id"] is None
        if initial or refresh:
            mode = "recent"
            query = {
                "anchor": "newest",
                "num_before": self.batch_size - 1,
                "num_after": 0,
            }
        elif job["target_id"] is not None:
            mode = "forward"
            query = {
                "anchor": job["newest_id"],
                "num_before": 0,
                "num_after": self.batch_size,
                "include_anchor": False,
            }
        else:
            mode = "backfill" if not job["backfill_done"] else "repair"
            anchor = job["before_id"] if mode == "backfill" else job["repair_before_id"]
            query = {
                "anchor": anchor or job["newest_id"] + 1,
                "num_before": self.batch_size,
                "num_after": 0,
                "include_anchor": False,
            }
        data = await self.http.request(
            account, "GET", "/messages", {**query, "apply_markdown": False}
        )
        messages = data["messages"]
        if mode == "forward":
            messages = [item for item in messages if item["id"] <= job["target_id"]]
        for message in messages:
            message.setdefault("reactions", [])
        changes = await self.operations.differences(
            account, messages, job["subscribed_streams"]
        )
        low = (
            0
            if data["found_oldest"]
            else min((item["id"] for item in messages), default=job["newest_id"])
        )
        high = (
            int(query["anchor"])
            if isinstance(query["anchor"], int)
            else max(
                job["newest_id"], max((item["id"] for item in messages), default=0)
            )
        )
        if mode == "recent":
            latest = await self.db.sql(
                SELECT_LATEST_KNOWN_SQL,
                (
                    account["owner_user_uuid"],
                    account["project_id"],
                    account["provider_uuid"],
                ),
            )
            high = max(high, latest[0]["remote_id"])
        elif mode != "forward":
            high -= 1
        missing = (
            await self.db.sql(
                SELECT_MISSING_SQL,
                (
                    account["owner_user_uuid"],
                    account["project_id"],
                    account["provider_uuid"],
                    low,
                    high,
                    [item["id"] for item in messages],
                    self.batch_size + 1,
                ),
            )
            if mode != "forward"
            else []
        )
        # A large deletion range is walked in bounded portions as well.
        candidates = sorted(
            {item["id"] for item, _ in changes}
            | {item["remote_id"] for item in missing},
            reverse=True,
        )
        truncated = len(candidates) > self.batch_size
        if truncated:
            low = candidates[self.batch_size - 1]
            missing = [item for item in missing if item["remote_id"] >= low]
            changes = [(item, old) for item, old in changes if item["id"] >= low]
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                return
            for message, expected in changes:
                await self.enqueue(
                    connection,
                    account,
                    {"type": "message", "message": message, "history": True},
                    expected,
                    started,
                )
            for item in missing:
                await self.enqueue(
                    connection,
                    account,
                    {"type": "history_absent", "message_id": item["remote_id"]},
                    None,
                    started,
                )
            newest = max((item["id"] for item in messages), default=job["newest_id"])
            before = min((item["id"] for item in messages), default=0)
            if truncated:
                before = low
            if mode == "recent":
                caught_up = (
                    initial or data["found_oldest"] or before <= job["newest_id"]
                )
                await connection.execute(
                    SAVE_RECENT_PROGRESS_SQL,
                    (
                        newest if caught_up else job["newest_id"],
                        None if caught_up else newest,
                        job["requested_at"],
                        caught_up,
                        job["requested_at"],
                        initial,
                        before,
                        initial,
                        data["found_oldest"] and not truncated,
                        account["uuid"],
                    ),
                )
            elif mode == "forward":
                done = (
                    data["found_newest"] or not messages or newest >= job["target_id"]
                )
                await connection.execute(
                    SAVE_FORWARD_PROGRESS_SQL,
                    (job["target_id"] if done else newest, done, done, account["uuid"]),
                )
            elif mode == "backfill":
                await connection.execute(
                    SAVE_BACKFILL_PROGRESS_SQL,
                    (before, data["found_oldest"] and not truncated, account["uuid"]),
                )
            else:
                done = data["found_oldest"] and not truncated
                await connection.execute(
                    SAVE_REPAIR_PROGRESS_SQL,
                    (
                        None if done else before,
                        done,
                        self.repair_interval,
                        account["uuid"],
                    ),
                )

    async def prepare(self, account: dict, event: dict) -> dict:
        while True:
            try:
                return await self.operations.prepare(account, event)
            except incoming_operations.MissingDependency as error:
                prepared = await self.prepare(
                    account, {"type": "ensure_dependency", "dependency": error.spec}
                )
                async with self.db.transaction() as connection:
                    await connection.execute(HISTORY_TRANSACTION_SQL)
                    if not await self.current(connection, account):
                        raise outgoing_operations.Pending(
                            "Account changed while preparing history"
                        )
                    await self.operations.apply(connection, account, prepared)

    async def activity(
        self,
        connection: typing.Any,
        account: dict,
        event: dict,
        snapshot: dict | None,
        fetched_at: datetime.datetime,
    ) -> dict:
        remote = event.get("message_id")
        if event["type"] == "message":
            remote = event["message"]["id"]
        keys = (
            [f"message:{remote}"]
            if remote is not None
            else zulip_incoming.ZulipIncomingService.ordering_keys(account, event)
        )
        # Account-ready/catalog events must not invalidate every catalog entry.
        if remote is None and event["type"] != "attachment":
            keys = [key for key in keys if not key.startswith("account:")]
        identifiers = []
        catalog_objects = []
        if snapshot:
            identifiers = [snapshot["message"]["uuid"]]
            identifiers.extend(
                snapshot[key]["uuid"]
                for key in ("stream", "topic", "placement", "state")
                if snapshot[key]
            )
        elif remote is None:
            catalog_objects = await self.operations.catalog_objects(
                connection, account, event
            )
            identifiers = [str(item["uuid"]) for item in catalog_objects]
        result = await incoming_operations.rows(
            connection,
            ACTIVITY_SQL,
            (
                account["project_id"],
                account["provider_uuid"],
                fetched_at,
                keys,
                account["project_id"],
                identifiers,
                identifiers,
                keys,
            ),
        )
        activity = result[0]
        activity["changed"] = activity["changed"] or any(
            history.timestamp(item["updated_at"].isoformat()) > fetched_at
            for item in catalog_objects
        )
        return activity

    async def process(self, account: dict, intent: dict) -> None:
        event = intent["payload"]
        if intent["identity"] != identity(account):
            await self.db.sql(
                "DELETE FROM zulip_history_intents WHERE id=%s", (intent["id"],)
            )
            return
        try:
            async with self.db.transaction() as connection:
                if (
                    await self.activity(
                        connection,
                        account,
                        event,
                        intent["expected"],
                        intent["fetched_at"],
                    )
                )["pending"]:
                    await connection.execute(DEFER_INTENT_SQL, (intent["id"],))
                    return
            if event["type"] == "history_absent":
                await self.refresh(account, intent)
                return
            prepared = await self.prepare(account, event)
            prepared["received_at"] = intent["fetched_at"]
            refresh = False
            async with self.db.transaction() as connection:
                await connection.execute(HISTORY_TRANSACTION_SQL)
                if not await self.current(connection, account):
                    return
                remote = event.get("message_id") or (
                    event["message"]["id"] if event["type"] == "message" else None
                )
                states = (
                    await self.operations.snapshots(account, [remote], connection)
                    if remote
                    else {}
                )
                snapshot = states.get(str(remote))
                activity = await self.activity(
                    connection, account, event, snapshot, intent["fetched_at"]
                )
                if activity["pending"]:
                    await connection.execute(DEFER_INTENT_SQL, (intent["id"],))
                elif activity["changed"]:
                    refresh = remote is not None
                    if not refresh:
                        # A newer catalog event is authoritative; discard its old snapshot.
                        await connection.execute(DISCARD_CATALOG_SQL, (intent["id"],))
                elif remote and (
                    snapshot != intent["expected"]
                    or history.recently_changed(snapshot, intent["fetched_at"])
                ):
                    refresh = True
                else:
                    await self.operations.apply(connection, account, prepared)
                    await connection.execute(
                        "DELETE FROM zulip_history_intents WHERE id=%s", (intent["id"],)
                    )
            if refresh:
                await self.refresh(account, intent)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.warning("Cannot apply history intent %s: %s", intent["id"], error)
            permanent = isinstance(error, (KeyError, TypeError, ValueError)) or (
                isinstance(error, outgoing_operations.ApiError)
                and 400 <= error.status < 500
                and error.status not in (408, 429)
            )
            delay = min(2 ** min(intent["attempts"], 8), 300)
            if isinstance(error, outgoing_operations.ApiError) and error.status == 429:
                delay = max(delay, float(error.data.get("retry-after", 1)))
            await self.db.sql(
                RETRY_INTENT_SQL,
                (str(error), permanent, delay, intent["id"]),
            )

    async def refresh(self, account: dict, intent: dict) -> None:
        event = intent["payload"]
        remote = (
            event["message"]["id"]
            if event["type"] == "message"
            else event["message_id"]
        )
        started = (await self.db.sql("SELECT clock_timestamp() AS value"))[0]["value"]
        try:
            message = (
                await self.http.request(
                    account, "GET", f"/messages/{remote}", {"apply_markdown": False}
                )
            )["message"]
        except outgoing_operations.ApiError as error:
            if error.status != 403 and not outgoing_operations.Operations.absent(error):
                raise
            expected = (await self.operations.snapshots(account, [remote])).get(
                str(remote)
            )
            # Absence confirms lost access for this account, not global deletion.
            async with self.db.transaction() as connection:
                await connection.execute(HISTORY_TRANSACTION_SQL)
                if not await self.current(connection, account):
                    return
                latest = (
                    await self.operations.snapshots(account, [remote], connection)
                ).get(str(remote))
                if (
                    latest != expected
                    or history.recently_changed(latest, started)
                    or any(
                        (
                            await self.activity(
                                connection, account, event, latest, started
                            )
                        ).values()
                    )
                ):
                    await connection.execute(
                        DEFER_INTENT_SQL,
                        (intent["id"],),
                    )
                    return
                await self.operations.remove_access(connection, account, remote)
                await connection.execute(
                    "DELETE FROM zulip_history_intents WHERE id=%s", (intent["id"],)
                )
        else:
            message.setdefault("reactions", [])
            changes = await self.operations.differences(
                account, [message], account.get("subscribed_streams")
            )
            async with self.db.transaction() as connection:
                if not await self.current(connection, account):
                    return
                await connection.execute(
                    "DELETE FROM zulip_history_intents WHERE id=%s", (intent["id"],)
                )
                if changes:
                    await self.enqueue(
                        connection,
                        account,
                        {"type": "message", "message": message, "history": True},
                        changes[0][1],
                        started,
                    )
