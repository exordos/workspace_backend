# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import logging
import threading
import time
import typing
import uuid

import psycopg


LOG = logging.getLogger(__name__)
CHANNEL_PREFIX = "workspace_provider_ops_"
FALLBACK_POLL_INTERVAL_SECONDS = 0.5
MAX_LISTENER_CONNECTIONS = 32
LISTENER_IDLE_TIMEOUT_SECONDS = 60.0


def channel_name(wait_key: str) -> str:
    """Return the dedicated PostgreSQL notification channel for one bridge."""
    return f"{CHANNEL_PREFIX}{uuid.UUID(wait_key).hex}"


class _ConnectAttempt:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.connection: typing.Any | None = None
        self.error: Exception | None = None


class ProviderOperationWakeup:
    """Retain one LISTEN connection for a bridge across long-poll requests."""

    def __init__(self, db_url: str, wait_key: str) -> None:
        self.wait_key = str(uuid.UUID(wait_key))
        self._db_url = db_url
        self._channel = channel_name(self.wait_key)
        self._connection: typing.Any | None = None
        self._connect_attempt: _ConnectAttempt | None = None
        self._state_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._closed = False
        self._last_released_at = time.monotonic()

    @property
    def last_released_at(self) -> float:
        return self._last_released_at

    def acquire(self, timeout: float) -> "ProviderOperationWaiter | None":
        if timeout <= 0:
            acquired = self._request_lock.acquire(blocking=False)
        else:
            acquired = self._request_lock.acquire(timeout=timeout)
        if not acquired:
            return None
        if self._closed:
            self._request_lock.release()
            return None
        return ProviderOperationWaiter(self)

    def _connect(self) -> typing.Any:
        connection = psycopg.connect(self._db_url, autocommit=True)
        try:
            connection.execute(f"LISTEN {self._channel}")
        except Exception:
            with contextlib.suppress(Exception):
                connection.close()
            raise
        return connection

    def _connect_in_background(self, attempt: _ConnectAttempt) -> None:
        try:
            connection = self._connect()
        except Exception as error:
            with self._state_lock:
                attempt.error = error
                attempt.done.set()
            return
        close_connection = False
        with self._state_lock:
            if self._closed or self._connect_attempt is not attempt:
                close_connection = True
            else:
                attempt.connection = connection
            attempt.done.set()
        if close_connection:
            with contextlib.suppress(Exception):
                connection.close()

    def subscribe(self, timeout: float) -> bool:
        """Start LISTEN before the request rechecks the queue."""
        with self._state_lock:
            if self._closed:
                return False
            if self._connection is not None:
                return True
            if timeout <= 0:
                return False
            attempt = self._connect_attempt
            if attempt is None:
                attempt = _ConnectAttempt()
                self._connect_attempt = attempt
                threading.Thread(
                    target=self._connect_in_background,
                    args=(attempt,),
                    name="workspace-provider-listener-connect",
                    daemon=True,
                ).start()
        if not attempt.done.wait(timeout):
            return False
        error = None
        with self._state_lock:
            if self._closed or self._connect_attempt is not attempt:
                return False
            self._connect_attempt = None
            error = attempt.error
            if attempt.connection is not None:
                self._connection = attempt.connection
                attempt.connection = None
                return True
        if error is not None:
            LOG.error(
                "Failed to subscribe to provider operation queue wakeups",
                exc_info=(type(error), error, error.__traceback__),
            )
        return False

    def wait(self, timeout: float) -> bool:
        if self._connection is None:
            time.sleep(min(timeout, FALLBACK_POLL_INTERVAL_SECONDS))
            return False
        deadline = time.monotonic() + timeout
        try:
            for _notification in self._connection.notifies(
                timeout=timeout,
                stop_after=1,
            ):
                return True
            return False
        except Exception:
            LOG.exception("Failed to wait for provider operation queue wakeup")
            self._close_connection()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, FALLBACK_POLL_INTERVAL_SECONDS))
            return False

    def _close_connection(self) -> None:
        with self._state_lock:
            connection = self._connection
            self._connection = None
        if connection is None:
            return
        with contextlib.suppress(Exception):
            connection.close()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            connection = self._connection
            self._connection = None
            attempt = self._connect_attempt
            self._connect_attempt = None
            attempt_connection = None
            if attempt is not None and attempt.done.is_set():
                attempt_connection = attempt.connection
                attempt.connection = None
        for item in (connection, attempt_connection):
            if item is not None:
                with contextlib.suppress(Exception):
                    item.close()

    def close_if_idle(self, *, idle_before: float | None = None) -> bool:
        if not self._request_lock.acquire(blocking=False):
            return False
        try:
            with self._state_lock:
                if (
                    self._connect_attempt is not None
                    and not self._connect_attempt.done.is_set()
                ):
                    return False
            if idle_before is not None and self._last_released_at > idle_before:
                return False
            self.close()
            return True
        finally:
            self._request_lock.release()


class ProviderOperationWaiter:
    """Own exclusive use of one bridge listener during a request."""

    def __init__(self, wakeup: ProviderOperationWakeup) -> None:
        self._wakeup = wakeup
        self._closed = False

    def subscribe(self, timeout: float) -> bool:
        return self._wakeup.subscribe(timeout)

    def wait(self, timeout: float, wait_key: str) -> bool:
        if str(uuid.UUID(wait_key)) != self._wakeup.wait_key:
            raise ValueError("Provider operation wait key changed during request")
        return self._wakeup.wait(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wakeup._last_released_at = time.monotonic()
        self._wakeup._request_lock.release()


class ProviderOperationPollingWaiter:
    """Use bounded polling when no listener can be acquired safely."""

    def __init__(self, wait_key: str) -> None:
        self._wait_key = str(uuid.UUID(wait_key))

    def subscribe(self, timeout: float) -> bool:
        return False

    def wait(self, timeout: float, wait_key: str) -> bool:
        if str(uuid.UUID(wait_key)) != self._wait_key:
            raise ValueError("Provider operation wait key changed during request")
        time.sleep(min(timeout, FALLBACK_POLL_INTERVAL_SECONDS))
        return False

    def close(self) -> None:
        return


ProviderOperationWaitHandle = ProviderOperationWaiter | ProviderOperationPollingWaiter


class ProviderOperationWakeupPool:
    """Share persistent listeners across requests without cross-bridge wakeups."""

    def __init__(
        self,
        db_url: str,
        *,
        max_listeners: int = MAX_LISTENER_CONNECTIONS,
        idle_timeout: float = LISTENER_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        if max_listeners < 1:
            raise ValueError("max_listeners must be at least one")
        if idle_timeout < 0:
            raise ValueError("idle_timeout must be non-negative")
        self._db_url = db_url
        self._max_listeners = max_listeners
        self._idle_timeout = idle_timeout
        self._lock = threading.Lock()
        self._wakeups: dict[str, ProviderOperationWakeup] = {}
        self._closed = False

    def __call__(
        self,
        wait_key: str,
        acquire_timeout: float,
    ) -> ProviderOperationWaitHandle:
        wait_key = str(uuid.UUID(wait_key))
        with self._lock:
            if self._closed:
                raise RuntimeError("Provider operation wakeup pool is closed")
            self._evict_idle_locked(time.monotonic())
            wakeup = self._wakeups.get(wait_key)
            if wakeup is None:
                self._evict_for_capacity_locked()
                if len(self._wakeups) >= self._max_listeners:
                    return ProviderOperationPollingWaiter(wait_key)
                wakeup = ProviderOperationWakeup(self._db_url, wait_key)
                self._wakeups[wait_key] = wakeup
                waiter = wakeup.acquire(0.0)
                assert waiter is not None
                return waiter
        waiter = wakeup.acquire(acquire_timeout)
        if waiter is None:
            return ProviderOperationPollingWaiter(wait_key)
        return waiter

    def _evict_idle_locked(self, now: float) -> None:
        idle_before = now - self._idle_timeout
        for wait_key, wakeup in list(self._wakeups.items()):
            if wakeup.close_if_idle(idle_before=idle_before):
                del self._wakeups[wait_key]

    def _evict_for_capacity_locked(self) -> None:
        if len(self._wakeups) < self._max_listeners:
            return
        candidates = sorted(
            self._wakeups.items(),
            key=lambda item: item[1].last_released_at,
        )
        for wait_key, wakeup in candidates:
            if wakeup.close_if_idle():
                del self._wakeups[wait_key]
                return

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            wakeups = list(self._wakeups.values())
            self._wakeups.clear()
        for wakeup in wakeups:
            wakeup.close()
