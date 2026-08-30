#    Copyright 2025 Genesis Corporation.
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

import datetime
import contextlib
import logging
import random
import time
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from gcl_looper.services import basic

from workspace.common import topic_summary_opts
from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import read_state
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.api import controllers as messenger_controllers
from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api import topic_summarization
from workspace.messenger_api import events as messenger_events
from workspace.external_bridge_control import sql_state
from workspace.services.messenger_workers import v2_projection

LOG = logging.getLogger(__name__)
EVENT_PRUNE_INTERVAL_SECONDS = 5 * 60
HEARTBEAT_RETENTION = datetime.timedelta(hours=24)
PROJECTION_REPAIR_LIMIT = 5
CAPABILITY_REFRESH_LIMIT = 100
CAPABILITY_PROJECTION_REFRESH_LIMIT = 100
CAPABILITY_PROJECTION_BATCH_SIZE = 16
DATABASE_DEADLOCK_MAX_ATTEMPTS = 3
DATABASE_DEADLOCK_RETRY_BASE_SECONDS = 0.05
READ_STATE_FAILURE_BACKOFF_BASE_SECONDS = 5.0
READ_STATE_FAILURE_BACKOFF_MAX_SECONDS = 5 * 60.0


@contextlib.contextmanager
def database_session_context() -> typing.Iterator[typing.Any]:
    """Own one transaction at a worker or operator-command boundary."""
    ctx = contexts.Context()
    with ctx.session_manager() as session:
        read_state.lock_read_state_schema_shared(session)
        yield session


def _is_database_deadlock(error: BaseException) -> bool:
    current: BaseException | None = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == "40P01":
            return True
        if getattr(current, "code", None) == "40P01":
            return True
        current = current.__cause__ or current.__context__
    return False


def _read_state_failure_backoff_seconds(failure_count: int) -> float:
    exponent = min(max(failure_count - 1, 0), 16)
    return min(
        READ_STATE_FAILURE_BACKOFF_BASE_SECONDS * (2**exponent),
        READ_STATE_FAILURE_BACKOFF_MAX_SECONDS,
    )


class MessengerWorkerAgent(basic.BasicService):
    def __init__(
        self,
        event_retention: datetime.timedelta = (sql_canonical_store.EVENT_RETENTION),
        event_prune_interval_seconds: int = EVENT_PRUNE_INTERVAL_SECONDS,
        event_prune_batch_size: int = (sql_canonical_store.EVENT_PRUNE_BATCH_SIZE),
        heartbeat_retention: datetime.timedelta = HEARTBEAT_RETENTION,
        read_state_compaction_enabled: bool = False,
        read_state_cleanup_enabled: bool = False,
        read_state_batch_size: int = read_state.COMPACTION_BATCH_SIZE,
        read_state_max_batches_per_iteration: int = 1,
        v2_projection_enabled: bool = False,
        v2_projection_max_tasks_per_iteration: int = 100,
        v2_fanout_batch_size: int = v2_projection.DEFAULT_FANOUT_BATCH_SIZE,
        summary_secret_key: str | None = None,
        summary_connect_timeout_seconds: int = (
            topic_summary_opts.DEFAULT_CONNECT_TIMEOUT_SECONDS
        ),
        summary_request_timeout_seconds: int = (
            topic_summary_opts.DEFAULT_REQUEST_TIMEOUT_SECONDS
        ),
        summary_topic_claim_seconds: int = (
            topic_summary_opts.DEFAULT_TOPIC_CLAIM_SECONDS
        ),
        summary_endpoint_claim_seconds: int = (
            topic_summary_opts.DEFAULT_ENDPOINT_CLAIM_SECONDS
        ),
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(**kwargs)
        self._event_retention = event_retention
        self._event_prune_interval_seconds = event_prune_interval_seconds
        self._event_prune_batch_size = event_prune_batch_size
        self._heartbeat_retention = heartbeat_retention
        self._read_state_compaction_enabled = read_state_compaction_enabled
        self._read_state_cleanup_enabled = read_state_cleanup_enabled
        self._read_state_batch_size = read_state_batch_size
        self._read_state_max_batches_per_iteration = (
            read_state_max_batches_per_iteration
        )
        self._read_state_failures: dict[object, tuple[int, float]] = {}
        self._v2_projection_enabled = v2_projection_enabled
        self._v2_projection_max_tasks_per_iteration = (
            v2_projection_max_tasks_per_iteration
        )
        self._v2_fanout_batch_size = v2_fanout_batch_size
        self._v2_worker_id = f"workspace-messenger:{sys_uuid.uuid4()}"
        self._summary_secret_key = summary_secret_key
        self._summary_connect_timeout_seconds = summary_connect_timeout_seconds
        self._summary_request_timeout_seconds = summary_request_timeout_seconds
        self._summary_topic_claim_seconds = max(
            summary_topic_claim_seconds,
            topic_summarization.MAX_PROVIDER_ATTEMPTS
            * (
                summary_request_timeout_seconds + topic_summary_opts.CLAIM_GRACE_SECONDS
            ),
        )
        self._summary_endpoint_claim_seconds = max(
            summary_endpoint_claim_seconds,
            summary_request_timeout_seconds + topic_summary_opts.CLAIM_GRACE_SECONDS,
        )
        self._last_event_prune: float | None = None
        self._capability_refresh_cursor: object | None = None
        self._capability_projection_refresh_cursor: object | None = None

    def _prune_expired_events(
        self,
        session: typing.Any,
        now: datetime.datetime,
    ) -> int:
        return sql_canonical_store.prune_expired_events(
            session,
            now,
            retention=self._event_retention,
            batch_size=self._event_prune_batch_size,
        )

    def _iteration(self) -> None:
        if self._v2_projection_enabled:
            self._run_v2_projection_tasks()
        now = datetime.datetime.now(datetime.timezone.utc)
        monotonic_now = time.monotonic()
        prune_due = (
            self._last_event_prune is None
            or monotonic_now - self._last_event_prune
            >= self._event_prune_interval_seconds
        )
        if prune_due:
            # Pruning takes per-project advisory locks. Commit them before
            # capability refresh locks external-account rows so concurrent
            # message/provider writes cannot form a reverse lock-order cycle.
            try:
                with database_session_context() as session:
                    pruned = self._prune_expired_events(session, now)
            except Exception:
                LOG.exception("Failed to prune expired Workspace event rows")
            else:
                self._last_event_prune = monotonic_now
                if pruned:
                    LOG.info("Pruned %d expired Workspace event rows", pruned)
        try:
            with database_session_context() as session:
                messenger_dm_helpers.mark_stale_workspace_users_offline(
                    session=session,
                )
                degraded = sql_state.degrade_stale_bridge_instances(
                    session,
                    now=now,
                )
        except Exception:
            LOG.exception("Failed to refresh stale Workspace presence and bridge rows")
        else:
            if degraded:
                LOG.info("Degraded %d stale external bridge instances", degraded)

        if self._read_state_compaction_enabled:
            for _batch in range(self._read_state_max_batches_per_iteration):
                retry_now = time.monotonic()
                excluded_projects = {
                    project_id
                    for project_id, (_failure_count, retry_at) in (
                        self._read_state_failures.items()
                    )
                    if retry_at > retry_now
                }
                try:
                    with database_session_context() as session:
                        read_state_maintenance = read_state.maintain_next_project(
                            session,
                            batch_size=self._read_state_batch_size,
                            cleanup_enabled=self._read_state_cleanup_enabled,
                            excluded_project_ids=excluded_projects,
                        )
                except read_state.MaintenanceProjectError as error:
                    failure_now = time.monotonic()
                    failure_count = (
                        self._read_state_failures.get(
                            error.project_id,
                            (0, 0.0),
                        )[0]
                        + 1
                    )
                    backoff = _read_state_failure_backoff_seconds(
                        failure_count,
                    )
                    self._read_state_failures[error.project_id] = (
                        failure_count,
                        failure_now + backoff,
                    )
                    LOG.exception(
                        "Failed to maintain compact Workspace read state",
                        extra={
                            "project_id": str(error.project_id),
                            "retry_in_seconds": backoff,
                        },
                    )
                    continue
                except Exception:
                    LOG.exception("Failed to maintain compact Workspace read state")
                    break
                if read_state_maintenance is None:
                    break
                action, project_id, processed = read_state_maintenance
                self._read_state_failures.pop(project_id, None)
                LOG.info(
                    "Completed bounded Workspace read-state maintenance",
                    extra={
                        "action": action,
                        "project_id": str(project_id),
                        "processed": processed,
                    },
                )

        self._refresh_capabilities(now)
        self._refresh_capability_projections()

        if prune_due:
            try:
                with database_session_context() as session:
                    pruned_heartbeats = sql_state.prune_expired_heartbeats(
                        session,
                        now,
                        retention=self._heartbeat_retention,
                        batch_size=self._event_prune_batch_size,
                    )
            except Exception:
                LOG.exception("Failed to prune expired bridge heartbeat rows")
            else:
                if pruned_heartbeats:
                    LOG.info(
                        "Pruned %d expired bridge heartbeat rows",
                        pruned_heartbeats,
                    )

        try:
            with database_session_context() as session:
                self._repair_external_projection_transitions(session)
        except Exception:
            LOG.exception("Failed to repair external projection transitions")

        self._summarize_one_topic()

    def _run_v2_projection_tasks(self) -> None:
        for _task in range(self._v2_projection_max_tasks_per_iteration):
            try:
                with database_session_context() as session:
                    cleaned = v2_projection.process_one_provider_file_cleanup_task(
                        session,
                        self._v2_worker_id,
                    )
                    v2_projection.derive_projection_tasks(session)
                    processed = v2_projection.process_one_projection_task(
                        session,
                        self._v2_worker_id,
                        fanout_batch_size=self._v2_fanout_batch_size,
                    )
            except Exception:
                LOG.exception("Failed to run the Messenger v2 projection queue")
                return
            if not cleaned and not processed:
                return

    def _summarize_one_topic(self) -> bool:
        if self._summary_secret_key is None:
            return False
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            with database_session_context() as session:
                work = topic_summarization.claim_summary_work(
                    session,
                    now=now,
                    key_material=self._summary_secret_key,
                    topic_claim_seconds=self._summary_topic_claim_seconds,
                    endpoint_claim_seconds=self._summary_endpoint_claim_seconds,
                )
        except Exception:
            LOG.exception("Failed to claim bounded topic summary work")
            return False
        if work is None:
            return False

        while True:
            try:
                summary = topic_summarization.call_openai_compatible_endpoint(
                    work,
                    connect_timeout_seconds=(self._summary_connect_timeout_seconds),
                    timeout_seconds=self._summary_request_timeout_seconds,
                )
            except topic_summarization.ProviderCallError as error:
                try:
                    with database_session_context() as session:
                        work = topic_summarization.fail_summary_work(
                            session,
                            work,
                            error,
                            now=datetime.datetime.now(datetime.timezone.utc),
                            key_material=self._summary_secret_key,
                            endpoint_claim_seconds=(
                                self._summary_endpoint_claim_seconds
                            ),
                        )
                except Exception:
                    LOG.exception("Failed to persist topic summary provider failure")
                    return True
                if work is None:
                    return True
                continue
            except Exception:
                LOG.exception("Failed to construct topic summary provider request")
                failure = topic_summarization.ProviderCallError(
                    "worker_error",
                    retryable=False,
                )
                try:
                    with database_session_context() as session:
                        topic_summarization.fail_summary_work(
                            session,
                            work,
                            failure,
                            now=datetime.datetime.now(datetime.timezone.utc),
                            key_material=self._summary_secret_key,
                            endpoint_claim_seconds=(
                                self._summary_endpoint_claim_seconds
                            ),
                        )
                except Exception:
                    LOG.exception("Failed to persist topic summary worker failure")
                return True

            try:
                with database_session_context() as session:
                    topic_summarization.complete_summary_work(
                        session,
                        work,
                        summary,
                        now=datetime.datetime.now(datetime.timezone.utc),
                    )
            except Exception:
                LOG.exception(
                    "Failed to persist topic summary result",
                    extra={"topic_uuid": str(work.topic_uuid)},
                )
            else:
                LOG.info(
                    "Completed bounded topic summary",
                    extra={
                        "topic_uuid": str(work.topic_uuid),
                        "endpoint_uuid": str(work.endpoint.uuid),
                        "attempt": work.attempt,
                    },
                )
            return True

    def _refresh_capabilities(self, now: datetime.datetime) -> None:
        """Refresh each account in its own bounded, retryable transaction."""
        started_at = time.monotonic()
        after_uuid = self._capability_refresh_cursor
        batch_size = 0
        failure_count = 0
        lock_wait_seconds = 0.0
        deadlock_retries = 0
        for _batch_index in range(CAPABILITY_REFRESH_LIMIT):
            claimed_uuid = None
            succeeded = False
            for attempt in range(1, DATABASE_DEADLOCK_MAX_ATTEMPTS + 1):
                try:
                    with database_session_context() as session:
                        claim_started_at = time.monotonic()
                        claimed_uuid = sql_state.claim_capability_refresh_account(
                            session,
                            after_uuid=after_uuid,
                        )
                        lock_wait_seconds += time.monotonic() - claim_started_at
                        if claimed_uuid is None:
                            if after_uuid is not None:
                                self._capability_refresh_cursor = None
                            break
                        sql_state.refresh_effective_capabilities(
                            session,
                            account_uuid=claimed_uuid,
                            now=now,
                        )
                except Exception as error:
                    if not _is_database_deadlock(error):
                        LOG.exception(
                            "Failed to refresh external account capabilities",
                            extra={
                                "external_account_uuid": str(claimed_uuid),
                            },
                        )
                        break
                    deadlock_retries += 1
                    if attempt == DATABASE_DEADLOCK_MAX_ATTEMPTS:
                        LOG.exception(
                            "Capability refresh exhausted PostgreSQL deadlock retries",
                            extra={
                                "deadlock_retry_attempt": attempt,
                                "external_account_uuid": str(claimed_uuid),
                            },
                        )
                        break
                    delay = DATABASE_DEADLOCK_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                    delay *= random.uniform(0.75, 1.25)
                    LOG.warning(
                        "Retrying capability refresh after PostgreSQL deadlock",
                        extra={
                            "deadlock_retry_attempt": attempt,
                            "deadlock_retry_delay_seconds": delay,
                            "external_account_uuid": str(claimed_uuid),
                        },
                    )
                    time.sleep(delay)
                    continue
                else:
                    succeeded = True
                    break
            if claimed_uuid is None:
                break
            after_uuid = claimed_uuid
            self._capability_refresh_cursor = claimed_uuid
            if succeeded:
                batch_size += 1
            else:
                failure_count += 1
        LOG.info(
            "Completed bounded external capability refresh batch",
            extra={
                "capability_refresh_batch_size": batch_size,
                "capability_refresh_failure_count": failure_count,
                "capability_refresh_duration_seconds": (time.monotonic() - started_at),
                "capability_refresh_lock_wait_seconds": lock_wait_seconds,
                "capability_refresh_deadlock_retries": deadlock_retries,
            },
        )

    def _refresh_capability_projections(self) -> None:
        """Drain stale projection metadata in separately committed batches."""
        started_at = time.monotonic()
        after_uuid = self._capability_projection_refresh_cursor
        batch_count = 0
        entity_count = 0
        recipient_count = 0
        event_count = 0
        failure_count = 0
        lock_contention_count = 0
        max_batch_duration_seconds = 0.0
        for _batch_index in range(CAPABILITY_PROJECTION_REFRESH_LIMIT):
            claimed_uuid = None
            batch_started_at = time.monotonic()
            try:
                with database_session_context() as session:
                    claimed_uuid = (
                        sql_state.claim_capability_projection_refresh_account(
                            session,
                            after_uuid=after_uuid,
                        )
                    )
                    if claimed_uuid is None:
                        if after_uuid is not None:
                            after_uuid = None
                            self._capability_projection_refresh_cursor = None
                            continue
                        break
                    entities, recipients, events = (
                        sql_state.refresh_projected_capabilities_batch(
                            session,
                            account_uuid=claimed_uuid,
                            batch_size=CAPABILITY_PROJECTION_BATCH_SIZE,
                        )
                    )
            except messenger_events.ProjectEventLockUnavailableError as error:
                contention_duration_seconds = time.monotonic() - batch_started_at
                lock_contention_count += 1
                # Resume after the contended account so it cannot starve later
                # accounts while the cursor still wraps back for a future retry.
                self._capability_projection_refresh_cursor = claimed_uuid
                max_batch_duration_seconds = max(
                    max_batch_duration_seconds,
                    contention_duration_seconds,
                )
                LOG.info(
                    "Deferred external capability projection refresh because "
                    "the project event lock is busy",
                    extra={
                        "external_account_uuid": str(claimed_uuid),
                        "project_id": str(error),
                        "capability_projection_lock_contention_duration_seconds": (
                            contention_duration_seconds
                        ),
                    },
                )
                break
            except Exception:
                failure_count += 1
                LOG.exception(
                    "Failed to refresh external capability projections",
                    extra={"external_account_uuid": str(claimed_uuid)},
                )
                if claimed_uuid is None:
                    break
            else:
                batch_count += 1
                entity_count += entities
                recipient_count += recipients
                event_count += events
                max_batch_duration_seconds = max(
                    max_batch_duration_seconds,
                    time.monotonic() - batch_started_at,
                )
            after_uuid = claimed_uuid
            self._capability_projection_refresh_cursor = claimed_uuid
        LOG.info(
            "Completed bounded external capability projection refresh",
            extra={
                "capability_projection_batch_count": batch_count,
                "capability_projection_entity_count": entity_count,
                "capability_projection_recipient_count": recipient_count,
                "capability_projection_event_count": event_count,
                "capability_projection_failure_count": failure_count,
                "capability_projection_lock_contention_count": (lock_contention_count),
                "capability_projection_max_batch_duration_seconds": (
                    max_batch_duration_seconds
                ),
                "capability_projection_duration_seconds": (
                    time.monotonic() - started_at
                ),
            },
        )

    def _repair_external_projection_transitions(
        self,
        session: typing.Any,
    ) -> None:
        """Repair a bounded batch in the worker-owned transaction."""
        rows = session.execute(
            """
            SELECT uuid, external_chat_uuid
            FROM m_external_projection_transitions_v1
            WHERE phase NOT IN ('completed', 'failed')
              AND next_repair_at <= NOW()
            ORDER BY next_repair_at, created_at
            LIMIT %s
            """,
            (PROJECTION_REPAIR_LIMIT,),
        ).fetchall()
        for row in rows:
            chat = external_models.ExternalChat.objects.get_one(
                filters={"uuid": dm_filters.EQ(row["external_chat_uuid"])},
                session=session,
            )
            messenger_controllers.ExternalChatController._resume_transition(
                row["uuid"], chat, session
            )
