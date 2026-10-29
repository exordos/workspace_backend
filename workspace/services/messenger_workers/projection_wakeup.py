# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import logging
import threading
import typing

import psycopg


LOG = logging.getLogger(__name__)
CHANNEL = "workspace_projection_tasks"


class ProjectionQueueWakeup:
    """Own a LISTEN connection only while its worker is waiting for work.

    Notifications are hints: work committed between an empty queue check and
    LISTEN is recovered by timeout-based polling. Never leave an unread listener
    behind while the worker processes its durable queues.
    """

    def __init__(self, db_url: str, *, mutations: bool = False) -> None:
        self._db_url = db_url
        self._mutations = mutations
        self._closed = threading.Event()
        self._wait_lock = threading.Lock()

    def _connect(self) -> typing.Any:
        connection = psycopg.connect(self._db_url, autocommit=True, connect_timeout=5)
        try:
            connection.execute(f"LISTEN {CHANNEL}")
            if self._mutations:
                connection.execute("LISTEN workspace_mutation_commands")
        except BaseException:
            connection.close()
            raise
        return connection

    def wait(self, timeout: float) -> bool:
        with self._wait_lock:
            if self._closed.is_set() or timeout <= 0:
                return False
            try:
                with contextlib.closing(self._connect()) as connection:
                    if self._closed.is_set():
                        return False
                    with contextlib.closing(
                        connection.notifies(timeout=timeout, stop_after=1)
                    ) as notifications:
                        return next(notifications, None) is not None
            except Exception:
                LOG.exception("Failed to wait for Messenger projection queue wakeup")
                self._closed.wait(timeout)
                return False

    def close(self) -> None:
        self._closed.set()
        # The wait caller alone owns and closes its connection. Shutdown waits
        # for that bounded idle wait rather than using psycopg from two threads.
        with self._wait_lock:
            pass
