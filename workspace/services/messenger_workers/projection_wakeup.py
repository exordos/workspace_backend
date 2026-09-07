# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import logging
import time
import typing

import psycopg


LOG = logging.getLogger(__name__)
CHANNEL = "workspace_projection_tasks"


class ProjectionQueueWakeup:
    """Wake a projection worker while retaining timeout-based retry polling."""

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._connection: typing.Any | None = None

    def _connect(self) -> typing.Any:
        connection = psycopg.connect(self._db_url, autocommit=True)
        connection.execute(f"LISTEN {CHANNEL}")
        return connection

    def wait(self, timeout: float) -> bool:
        try:
            if self._connection is None:
                self._connection = self._connect()
            notified = False
            for _notification in self._connection.notifies(
                timeout=timeout,
                stop_after=1,
            ):
                notified = True
            return notified
        except Exception:
            LOG.exception("Failed to wait for Messenger projection queue wakeup")
            self.close()
            time.sleep(timeout)
            return False

    def close(self) -> None:
        if self._connection is None:
            return
        with contextlib.suppress(Exception):
            self._connection.close()
        self._connection = None
