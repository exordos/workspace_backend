# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import concurrent.futures
import datetime
import logging
import time
import uuid

from gcl_looper.services import basic
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models


LOG = logging.getLogger(__name__)


class HistoryImportService(basic.BasicService):
    def __init__(self, *args, queue="all", workers=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = queue
        self.workers = workers
        self._executor = None
        self._futures = set()

    def stop(self):
        super().stop()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def _ensure_reconciliation_task(self) -> None:
        session = contexts.Context().get_session()
        existing = {
            row["kind"]
            for row in session.execute(
                """SELECT DISTINCT content->>'kind' AS kind FROM bridge_zulip_v5_tasks
                   WHERE status IN ('new','waiting','retry')
                     AND content->>'kind' IN ('reconciliation','refresh_message_counters')"""
            ).fetchall()
        }
        for content, priority in (
            (models.ReconciliationContent(), models.TASK_PRIORITY_RECONCILIATION),
            (
                models.RefreshMessageCountersContent(),
                models.TASK_PRIORITY_REFRESH_MESSAGE_COUNTERS,
            ),
        ):
            if content.kind not in existing:
                models.Task(
                    priority=priority,
                    not_before_at=(
                        datetime.datetime.now(datetime.timezone.utc)
                        + datetime.timedelta(seconds=content.interval)
                        if isinstance(content, models.RefreshMessageCountersContent)
                        else None
                    ),
                    content=content,
                ).insert()

    def _get_tasks(self) -> list[models.Task]:
        now = datetime.datetime.now(datetime.timezone.utc)
        return models.Task.objects.get_all(
            filters=filters.AND(
                {
                    "status": filters.In(
                        [
                            models.TaskStatus.NEW.value,
                            models.TaskStatus.WAITING.value,
                            models.TaskStatus.RETRY.value,
                        ]
                    ),
                    **(
                        {"queue": filters.EQ(self.queue)} if self.queue != "all" else {}
                    ),
                },
                filters.OR(
                    {"not_before_at": filters.Is(None)},
                    {"not_before_at": filters.LE(now)},
                ),
                filters.OR(
                    {"next_retry_at": filters.Is(None)},
                    {"next_retry_at": filters.LE(now)},
                ),
                filters.OR(
                    {"lease_expires_at": filters.Is(None)},
                    {"lease_expires_at": filters.LE(now)},
                ),
            ),
            order_by={
                "priority": "desc",
                "message_created_at": "desc",
                "message_sequence": "desc",
                "created_at": "asc",
                "uuid": "asc",
            },
            limit=1,
        )

    def _execute_task(self, task: models.Task) -> None:
        task.execute()
        LOG.info(
            "History import task %s finished with status %s", task.uuid, task.status
        )

    def _iteration(self) -> None:
        if self.queue != "all" or self.workers != 1:
            self._pool_iteration()
            return
        prepared = []
        with contexts.Context().session_manager():
            self._ensure_reconciliation_task()
            tasks = self._get_tasks()
            for task in tasks:
                if isinstance(task.content, models.OutboundMessageContent):
                    with task.track_execution():
                        try:
                            if task.content.prepare(task):
                                prepared.append(task)
                        except Exception as error:
                            task.on_error(error)
                else:
                    self._execute_task(task)
        # Commit the send attempt before HTTP: a crash must not replay POST.
        for task in prepared:
            with contexts.Context().session_manager():
                self._execute_task(task)
        if tasks:
            self.boost(iterations=2)

    def _claim_tasks(self, limit):
        session = contexts.Context().get_session()
        queues = ("background", "realtime") if self.queue == "all" else (self.queue,)
        for queue in queues:
            if not session.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
                (f"zulip-task-dispatch:{queue}",),
            ).fetchone()["locked"]:
                return []
        # Separate statements keep queue statistics visible in generic plans.
        queue_filter = {
            "all": "",
            "background": "AND task.queue='background'",
            "realtime": "AND task.queue='realtime'",
        }[self.queue]
        claimed = []
        for _index in range(limit):
            owner = uuid.uuid4()
            row = session.execute(
                f"""WITH candidate AS (
                    SELECT task.uuid FROM bridge_zulip_v5_tasks AS task
                    WHERE task.status IN ('new','waiting','retry')
                      {queue_filter}
                      AND (task.not_before_at IS NULL OR task.not_before_at<=now() AT TIME ZONE 'UTC')
                      AND (task.next_retry_at IS NULL OR task.next_retry_at<=now() AT TIME ZONE 'UTC')
                      AND (task.lease_expires_at IS NULL OR task.lease_expires_at<=now() AT TIME ZONE 'UTC')
                      AND NOT EXISTS (
                          SELECT 1 FROM bridge_zulip_v5_tasks AS active
                          WHERE active.queue=task.queue AND active.concurrency_key=task.concurrency_key
                            AND active.status IN ('new','waiting','retry') AND active.lease_owner IS NOT NULL
                            AND active.lease_expires_at>now() AT TIME ZONE 'UTC'
                      )
                    ORDER BY task.priority DESC,task.message_created_at DESC,task.message_sequence DESC,
                             task.created_at,task.uuid
                    LIMIT 1 FOR UPDATE OF task SKIP LOCKED
                )
                UPDATE bridge_zulip_v5_tasks AS task
                SET queue_wait_time=COALESCE(queue_wait_time,0)+GREATEST(0,EXTRACT(EPOCH FROM (
                        (now() AT TIME ZONE 'UTC')-GREATEST(
                            CASE WHEN execution_time IS NULL THEN created_at ELSE updated_at END,
                            not_before_at,next_retry_at,lease_expires_at)))),
                    lease_owner=%s,lease_expires_at=(now() AT TIME ZONE 'UTC')+interval '2 minutes'
                FROM candidate WHERE task.uuid=candidate.uuid RETURNING task.uuid""",
                (owner,),
            ).fetchone()
            if row is None:
                break
            claimed.append((row["uuid"], owner))
        return claimed

    @staticmethod
    def _locked_task(task_uuid, owner):
        session = contexts.Context().get_session()
        task = models.Task.objects.get_one(filters={"uuid": filters.EQ(task_uuid)})
        # Keep account-before-task locking consistent with rebind and send paths.
        if task.external_account_uuid is not None:
            account = session.execute(
                "SELECT uuid,status,settings ? 'api_key' AS has_credentials "
                "FROM m_external_accounts_v2 WHERE project_id=%s AND uuid=%s FOR UPDATE SKIP LOCKED",
                (task.project_id, task.external_account_uuid),
            ).fetchone()
            if account is None:
                session.execute(
                    "UPDATE bridge_zulip_v5_tasks SET next_retry_at=(now() AT TIME ZONE 'UTC')+interval '0.1 seconds' "
                    "WHERE uuid=%s AND lease_owner=%s AND status IN ('new','waiting','retry')",
                    (task_uuid, owner),
                )
                return None
            if (
                account["status"] == external_models.ExternalAccountStatus.NEW.value
                and account["has_credentials"]
                and not isinstance(task.content, models.InitializeConnectionContent)
            ):
                session.execute(
                    "UPDATE bridge_zulip_v5_tasks SET next_retry_at=(now() AT TIME ZONE 'UTC')+interval '3 seconds' "
                    "WHERE uuid=%s AND lease_owner=%s AND status IN ('new','waiting','retry')",
                    (task_uuid, owner),
                )
                return None
        row = session.execute(
            """SELECT uuid FROM bridge_zulip_v5_tasks WHERE uuid=%s AND lease_owner=%s
               AND status IN ('new','waiting','retry') FOR UPDATE SKIP LOCKED""",
            (task_uuid, owner),
        ).fetchone()
        if row is None:
            return None
        task = models.Task.objects.get_one(filters={"uuid": filters.EQ(task_uuid)})
        key = (
            f"zulip-task:{task.concurrency_key}"
            if task.concurrency_key.endswith(":catalog")
            else f"zulip-task:{task.queue}:{task.concurrency_key}"
        )
        locked = session.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
            (key,),
        ).fetchone()["locked"]
        if not locked:
            if task.concurrency_key.endswith(":catalog"):
                session.execute(
                    "UPDATE bridge_zulip_v5_tasks SET next_retry_at=(now() AT TIME ZONE 'UTC')+interval '0.1 seconds' WHERE uuid=%s AND lease_owner=%s",
                    (task_uuid, owner),
                )
            return None
        task._worker_owner = owner
        return task

    def _run_task(self, task_uuid, owner):
        unsubmitted_delivery = None
        try:
            imported, prepare_error, prepare_seconds = self._fetch_import(
                task_uuid, owner
            )
            prepared = None
            with contexts.Context().session_manager():
                task = self._locked_task(task_uuid, owner)
                if task is None:
                    return
                if imported is not None or prepare_error is not None:
                    task.content._prepared_import = imported
                    task.content._prepared_error = prepare_error
                    task.execution_time = (task.execution_time or 0.0) + prepare_seconds
                if isinstance(task.content, models.OutboundMessageContent):
                    with task.track_execution():
                        try:
                            if task.content.prepare(task):
                                prepared = task
                        except Exception as error:
                            task.on_error(error)
                else:
                    self._execute_task(task)
            # Preserve the committed attempt marker across a lost HTTP response.
            if prepared is not None:
                with contexts.Context().session_manager():
                    task = self._locked_task(task_uuid, owner)
                    if task is not None:
                        task.content._submit = getattr(
                            prepared.content, "_submit", False
                        )
                        self._execute_task(task)
                    elif isinstance(
                        prepared.content, models.SendMessageContent
                    ) and getattr(prepared.content, "_submit", False):
                        # The second lock was skipped before execute/HTTP began.
                        unsubmitted_delivery = prepared.content.delivery_uuid
        finally:
            with contexts.Context().session_manager() as session:
                released = session.execute(
                    "UPDATE bridge_zulip_v5_tasks SET lease_owner=NULL,lease_expires_at=NULL "
                    "WHERE uuid=%s AND lease_owner=%s RETURNING project_id",
                    (task_uuid, owner),
                ).fetchone()
                if released is not None and unsubmitted_delivery is not None:
                    # Hold the owned task row until reset commits: a reclaimed
                    # lease must never clear another worker's send attempt.
                    session.execute(
                        "UPDATE bridge_zulip_v5_deliveries SET sending=FALSE "
                        "WHERE project_id=%s AND uuid=%s AND zulip_message_id IS NULL",
                        (released["project_id"], unsubmitted_delivery),
                    )

    @staticmethod
    def _fetch_import(task_uuid, owner):
        """Fetch history before holding account/task rows; fence writes afterwards."""
        started = time.monotonic()
        try:
            with contexts.Context().session_manager():
                task = models.Task.objects.get_one(
                    filters={"uuid": filters.EQ(task_uuid)}
                )
                if task.lease_owner != owner or not isinstance(
                    task.content, models.ImportHistoryContent
                ):
                    return None, None, 0.0
                account = external_models.ExternalAccount.objects.get_one(
                    filters={
                        "project_id": filters.EQ(task.project_id),
                        "uuid": filters.EQ(task.external_account_uuid),
                    }
                )
                if account.external_account_id is None or (
                    account.status == external_models.ExternalAccountStatus.NEW.value
                    and account.settings.get("api_key")
                ):
                    return None, None, 0.0
                revision = account.realtime_revision()
            observed_at = datetime.datetime.now(datetime.timezone.utc)
            zulip = client.ZulipClient(account)
            try:
                messages = zulip.get_messages(
                    task.content.first_message_id, task.content.last_message_id
                )
            finally:
                zulip.close()
            return (revision, observed_at, messages), None, time.monotonic() - started
        except Exception as error:
            return None, error, time.monotonic() - started

    def _pool_iteration(self):
        for future in list(self._futures):
            if future.done():
                self._futures.remove(future)
                try:
                    future.result()
                except Exception:
                    LOG.exception("Zulip task worker failed")
        capacity = self.workers - len(self._futures)
        if not capacity:
            return
        with contexts.Context().session_manager() as session:
            if (
                self.queue != "realtime"
                and session.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('zulip-recurring-tasks',0)) AS locked"
                ).fetchone()["locked"]
            ):
                self._ensure_reconciliation_task()
            claimed = self._claim_tasks(capacity)
        if claimed:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.workers, thread_name_prefix=f"zulip-{self.queue}"
                )
            for task_uuid, owner in claimed:
                future = self._executor.submit(self._run_task, task_uuid, owner)
                self._futures.add(future)
                # Pacing survives a completion during _iteration(), even when
                # BasicService clears the wake event before its next wait.
                future.add_done_callback(
                    lambda _future: self.boost(iterations=2, force=True)
                )
            self.boost(iterations=2)
