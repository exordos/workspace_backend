# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Runnable daemon service for Workspace v3 projections."""

import logging
import time
import typing
import uuid as sys_uuid

from gcl_looper.services import basic
from restalchemy.common import contexts

from workspace.workspace_v3 import projections

LOG = logging.getLogger(__name__)


class WorkspaceV3ProjectionAgent(basic.BasicService):
    def __init__(
        self,
        *,
        batch_size: int = projections.DEFAULT_BATCH_SIZE,
        lease_seconds: int = projections.DEFAULT_LEASE_SECONDS,
        max_attempts: int = projections.DEFAULT_MAX_ATTEMPTS,
        reaction_user_limit: int = projections.DEFAULT_REACTION_USER_LIMIT,
        idle_sleep_seconds: float = 0.1,
        metrics_log_interval_seconds: int = 30,
        event_prune_interval_seconds: int = 60,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(**kwargs)
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._reaction_user_limit = reaction_user_limit
        self._idle_sleep_seconds = idle_sleep_seconds
        self._metrics_log_interval_seconds = metrics_log_interval_seconds
        self._event_prune_interval_seconds = event_prune_interval_seconds
        self._event_pruned_at = 0.0
        self._worker_id = f"workspace-v3:{sys_uuid.uuid4()}"
        self._metrics_started_at = time.monotonic()
        self._metrics = {
            "claimed": 0.0,
            "completed": 0.0,
            "failed": 0.0,
            "operations": 0.0,
            "projections": 0.0,
            "events": 0.0,
            "pruned_events": 0.0,
            "elapsed_seconds": 0.0,
        }

    def _iteration(self) -> None:
        ctx = contexts.Context()
        now = time.monotonic()
        pruned_events = 0
        if now - self._event_pruned_at >= self._event_prune_interval_seconds:
            with ctx.session_manager() as session:
                pruned_events = projections.prune_event_journal(session)
            self._event_pruned_at = now
        with ctx.session_manager() as session:
            tasks = projections.claim_projection_tasks(
                session,
                self._worker_id,
                batch_size=self._batch_size,
                lease_seconds=self._lease_seconds,
                max_attempts=self._max_attempts,
            )
        if tasks:
            with ctx.session_manager() as session:
                metrics = projections.process_claimed_projection_tasks(
                    session,
                    self._worker_id,
                    tasks,
                    max_attempts=self._max_attempts,
                    reaction_user_limit=self._reaction_user_limit,
                )
        else:
            metrics = {
                "claimed": 0.0,
                "completed": 0.0,
                "failed": 0.0,
                "operations": 0.0,
                "projections": 0.0,
                "events": 0.0,
                "pruned_events": 0.0,
                "elapsed_seconds": 0.0,
            }
        for name, value in metrics.items():
            self._metrics[name] += value
        self._metrics["pruned_events"] += pruned_events
        now = time.monotonic()
        if now - self._metrics_started_at >= self._metrics_log_interval_seconds:
            elapsed = max(now - self._metrics_started_at, 0.001)
            LOG.info(
                "Workspace v3 projection metrics",
                extra={
                    **self._metrics,
                    "tasks_per_second": self._metrics["completed"] / elapsed,
                    "worker_id": self._worker_id,
                },
            )
            self._metrics_started_at = now
            self._metrics = {name: 0.0 for name in self._metrics}
        if metrics["claimed"] == 0:
            time.sleep(self._idle_sleep_seconds)
