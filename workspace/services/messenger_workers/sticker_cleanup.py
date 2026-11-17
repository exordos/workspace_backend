# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Durable post-commit cleanup for deleted sticker media objects."""

import collections.abc
import contextlib
import logging
import typing

from workspace.messenger_api import sticker_storage


LOG = logging.getLogger(__name__)
DEFAULT_LEASE_SECONDS = 30
MAX_RETRY_DELAY_SECONDS = 1200


def process_one_sticker_cleanup_task(
    session_context: collections.abc.Callable[
        [], contextlib.AbstractContextManager[typing.Any]
    ],
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Delete one unreferenced sticker object with durable retry state."""

    with session_context() as session:
        task = session.execute(
            """
            WITH candidate AS (
                SELECT sticker_uuid
                FROM messenger_sticker_cleanup_tasks
                WHERE status IN ('pending', 'running', 'failed')
                  AND next_retry_at <= NOW()
                  AND (lease_expires_at IS NULL OR lease_expires_at <= NOW())
                ORDER BY created_at, sticker_uuid
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE messenger_sticker_cleanup_tasks AS task
            SET status = 'running', attempts = attempts + 1,
                lease_owner = %s,
                lease_expires_at = NOW() + make_interval(secs => %s),
                updated_at = NOW()
            FROM candidate
            WHERE task.sticker_uuid = candidate.sticker_uuid
            RETURNING task.sticker_uuid, task.storage_object_id, task.attempts
            """,
            (worker_id, lease_seconds),
        ).fetchone()
    if task is None:
        return False
    status, safe_error, delay_seconds = "completed", None, 0
    try:
        sticker_storage.get_sticker_storage().delete(task["storage_object_id"])
    except Exception as error:
        status, safe_error = "failed", type(error).__name__[:128]
        delay_seconds = min(
            5 * (2 ** min(int(task["attempts"]) - 1, 8)),
            MAX_RETRY_DELAY_SECONDS,
        )
        LOG.exception(
            "Failed to delete sticker storage object",
            extra={"sticker_uuid": str(task["sticker_uuid"])},
        )

    # A later claim can expire this lease while storage deletion is in flight.
    # The attempt number fences stale results even when the worker ID is reused.
    with session_context() as session:
        session.execute(
            """
            UPDATE messenger_sticker_cleanup_tasks
            SET status = %s, safe_error = %s,
                lease_owner = NULL, lease_expires_at = NULL,
                next_retry_at = NOW() + make_interval(secs => %s),
                updated_at = NOW()
            WHERE sticker_uuid = %s AND lease_owner = %s AND attempts = %s
            """,
            (
                status,
                safe_error,
                delay_seconds,
                task["sticker_uuid"],
                worker_id,
                task["attempts"],
            ),
        )
    return True
