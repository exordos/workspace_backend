# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import email.message
import io
import threading
import time
import types
import uuid
from unittest import mock

import pytest

from workspace.external_bridge_control import server


def _handler(
    headers, body=b"", certificate=b"certificate", method="POST", path="/v1/x"
):
    handler = object.__new__(server.PrivateHandler)
    message = email.message.Message()
    for name, value in headers:
        message.add_header(name, value)
    handler.headers = message
    handler.command = method
    handler.path = path
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.connection = mock.Mock()
    handler.connection.getpeercert.return_value = certificate
    handler.transaction_events = []

    @contextlib.contextmanager
    def request_session():
        session = object()
        handler.transaction_events.append(("begin", session))
        try:
            yield session
        except Exception:
            handler.transaction_events.append(("rollback", session))
            raise
        else:
            handler.transaction_events.append(("commit", session))

    handler.server = types.SimpleNamespace(
        private_service=mock.Mock(),
        request_session_factory=request_session,
    )
    handler.server.private_service.handle.return_value = types.SimpleNamespace(
        status=204,
        content_type=None,
        body=b"",
        headers={},
    )
    handler.send_error = mock.Mock()
    handler.send_response = mock.Mock()
    handler.send_header = mock.Mock()
    handler.end_headers = mock.Mock()
    handler.close_connection = False
    return handler


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Content-Length", "-1")],
        [("Content-Length", "+1")],
        [("Content-Length", "01")],
        [("Content-Length", "1"), ("Content-Length", "1")],
        [("Content-Length", "1, 1")],
        [("Content-Length", "1"), ("Transfer-Encoding", "chunked")],
    ],
)
def test_private_handler_rejects_ambiguous_or_noncanonical_framing(headers):
    handler = _handler(headers, body=b"x")

    handler._dispatch()

    handler.send_error.assert_called_once_with(400)
    handler.server.private_service.handle.assert_not_called()
    assert handler.close_connection is True


def test_private_handler_rejects_unauthenticated_peer_before_body_read():
    handler = _handler(
        [("Content-Length", str(server.MAX_BODY))],
        certificate=None,
    )
    handler.rfile = mock.Mock()

    handler._dispatch()

    handler.send_error.assert_called_once_with(401)
    handler.rfile.read.assert_not_called()
    handler.server.private_service.handle.assert_not_called()


def test_private_handler_allows_bounded_initial_enrollment_without_certificate():
    handler = _handler(
        [("Content-Length", "2")],
        body=b"{}",
        certificate=None,
        path="/v1/enrollments",
    )

    handler._dispatch()

    handler.send_error.assert_not_called()
    handler.server.private_service.handle.assert_called_once()
    assert handler.server.private_service.handle.call_args.args[-1] is None
    request_session = handler.server.private_service.handle.call_args.kwargs[
        "request_session"
    ]
    assert handler.transaction_events == [
        ("begin", request_session),
        ("commit", request_session),
    ]


def test_private_handler_rolls_back_error_response():
    handler = _handler([("Content-Length", "0")], method="GET")
    handler.server.private_service.handle.return_value = types.SimpleNamespace(
        status=409,
        content_type="application/problem+json",
        body=b"{}",
        headers={},
    )

    handler._dispatch()

    request_session = handler.server.private_service.handle.call_args.kwargs[
        "request_session"
    ]
    assert handler.transaction_events == [
        ("begin", request_session),
        ("rollback", request_session),
    ]


def test_provider_event_commit_duration_is_logged_separately(monkeypatch):
    handler = _handler(
        [("Content-Length", "0")],
        path="/api/workspace-provider/v1/events",
    )
    clock = iter((10.0, 10.125))
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    infos = []
    monkeypatch.setattr(
        server.LOG,
        "info",
        lambda message, *args, **kwargs: infos.append((message, args, kwargs)),
    )

    handler._dispatch()

    assert infos == [
        (
            "Committed provider event batch: duration_seconds=%.3f",
            (0.125,),
            {
                "extra": {
                    "provider_batch_commit_duration_seconds": 0.125,
                }
            },
        )
    ]


def test_provider_request_retries_wrapped_deadlock_transaction(monkeypatch):
    class WrappedDeadlock(Exception):
        code = "40P01"

    handler = _handler(
        [("Content-Length", "0")],
        path="/api/workspace-provider/v2/commands",
    )
    response = types.SimpleNamespace(
        status=200,
        content_type="application/json",
        body=b"{}",
        headers={},
    )
    handler.server.private_service.handle.side_effect = [
        WrappedDeadlock("deadlock"),
        response,
    ]
    delays = []
    monkeypatch.setattr(server.random, "uniform", lambda _start, _end: 1.0)
    monkeypatch.setattr(server.time, "sleep", delays.append)

    handler._dispatch()

    first_session = handler.server.private_service.handle.call_args_list[0].kwargs[
        "request_session"
    ]
    second_session = handler.server.private_service.handle.call_args_list[1].kwargs[
        "request_session"
    ]
    assert first_session is not second_session
    assert handler.transaction_events == [
        ("begin", first_session),
        ("rollback", first_session),
        ("begin", second_session),
        ("commit", second_session),
    ]
    assert delays == [server.PROVIDER_DEADLOCK_BASE_DELAY_SECONDS]
    handler.send_response.assert_called_once_with(200)


def test_provider_lease_long_poll_rechecks_after_listener_registration():
    handler = _handler(
        [("Content-Length", "2")],
        body=b"{}",
        path="/api/workspace-provider/v2/operations/actions/lease",
    )
    empty = types.SimpleNamespace(
        status=200,
        content_type="application/json",
        body=b'{"operations":[]}',
        headers={server.provider_service.LEASE_WAIT_HEADER: "5.0"},
    )
    empty_after_subscribe = types.SimpleNamespace(
        status=200,
        content_type="application/json",
        body=b'{"operations":[]}',
        headers={server.provider_service.LEASE_WAIT_HEADER: "0.25"},
    )
    ready = types.SimpleNamespace(
        status=200,
        content_type="application/json",
        body=b'{"operations":[{}]}',
        headers={},
    )
    handler.server.private_service.handle.side_effect = [
        empty,
        empty_after_subscribe,
        ready,
    ]
    waits = []
    closed = []
    subscribed = []
    wakeup = types.SimpleNamespace(
        subscribe=lambda timeout: subscribed.append(timeout) or True,
        wait=lambda timeout, wait_key: waits.append((timeout, wait_key)) or True,
        close=lambda: closed.append(True),
    )
    wait_key = str(uuid.uuid4())
    empty.provider_wait_key = wait_key
    empty_after_subscribe.provider_wait_key = wait_key
    requested_wait_keys = []
    handler.server.provider_operation_wakeup_factory = (
        lambda requested_wait_key, acquire_timeout: (
            requested_wait_keys.append((requested_wait_key, acquire_timeout)) or wakeup
        )
    )

    handler._dispatch()

    assert handler.server.private_service.handle.call_count == 3
    assert len(handler.transaction_events) == 6
    assert all(event[0] in {"begin", "commit"} for event in handler.transaction_events)
    assert len(waits) == 1
    assert 0 < waits[0][0] <= 0.25
    assert waits[0][1] == wait_key
    assert len(requested_wait_keys) == 1
    assert requested_wait_keys[0][0] == wait_key
    assert 0 < requested_wait_keys[0][1] <= 5.0
    assert len(subscribed) == 1
    assert 0 < subscribed[0] <= 5.0
    assert closed == [True]
    handler.send_response.assert_called_once_with(200)


def test_provider_operation_wakeup_reuses_listener_connection(monkeypatch):
    executed = []
    closed = []

    class Connection:
        def execute(self, query):
            executed.append(query)

        def notifies(self, *, timeout, stop_after):
            assert 0 < timeout <= 2.0
            assert stop_after == 1
            yield types.SimpleNamespace(payload="")

        def close(self):
            closed.append(True)

    connection = Connection()
    connections = []

    def connect(db_url, *, autocommit):
        connections.append((db_url, autocommit))
        return connection

    monkeypatch.setattr(server.provider_wakeup.psycopg, "connect", connect)
    wait_key = str(uuid.uuid4())
    pool = server.provider_wakeup.ProviderOperationWakeupPool("postgresql://test")
    first = pool(wait_key, 2.0)
    assert first.subscribe(2.0) is True
    assert first.wait(2.0, wait_key) is True
    first.close()
    second = pool(wait_key, 2.0)
    assert second.subscribe(2.0) is True
    second.close()
    pool.close()

    assert connections == [("postgresql://test", True)]
    assert executed == [f"LISTEN {server.provider_wakeup.channel_name(wait_key)}"]
    assert closed == [True]


def test_provider_operation_wakeup_uses_short_polling_after_listener_failure(
    monkeypatch,
):
    sleeps = []
    monkeypatch.setattr(
        server.provider_wakeup.psycopg,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )
    monkeypatch.setattr(server.provider_wakeup.time, "sleep", sleeps.append)
    wait_key = str(uuid.uuid4())
    pool = server.provider_wakeup.ProviderOperationWakeupPool("postgresql://test")
    waiter = pool(wait_key, 2.0)

    assert waiter.subscribe(2.0) is False
    assert waiter.wait(20.0, wait_key) is False
    waiter.close()
    pool.close()

    assert sleeps == [server.provider_wakeup.FALLBACK_POLL_INTERVAL_SECONDS]


def test_provider_operation_wakeup_bounds_listener_setup(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class Connection:
        def __init__(self):
            self.closed = False

        def execute(self, _query):
            return None

        def close(self):
            self.closed = True

    connection = Connection()

    def connect(_db_url, *, autocommit):
        assert autocommit is True
        started.set()
        release.wait(2.0)
        return connection

    monkeypatch.setattr(server.provider_wakeup.psycopg, "connect", connect)
    wait_key = str(uuid.uuid4())
    pool = server.provider_wakeup.ProviderOperationWakeupPool(
        "postgresql://test",
        max_listeners=1,
    )
    first = pool(wait_key, 1.0)

    started_at = time.monotonic()
    assert first.subscribe(0.02) is False
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    assert started.wait(0.5)
    first.close()

    fallback = pool(str(uuid.uuid4()), 1.0)
    assert isinstance(fallback, server.provider_wakeup.ProviderOperationPollingWaiter)
    fallback.close()

    release.set()

    second = pool(wait_key, 1.0)
    assert second.subscribe(1.0) is True
    second.close()
    pool.close()

    assert connection.closed is True


def test_provider_operation_wakeup_bounds_same_bridge_acquisition():
    wait_key = str(uuid.uuid4())
    pool = server.provider_wakeup.ProviderOperationWakeupPool("postgresql://test")
    first = pool(wait_key, 1.0)

    started_at = server.provider_wakeup.time.monotonic()
    second = pool(wait_key, 0.02)
    elapsed = server.provider_wakeup.time.monotonic() - started_at

    assert elapsed < 0.1
    assert second.subscribe(0.02) is False
    second.close()
    first.close()
    pool.close()


def test_provider_operation_wakeup_pool_evicts_idle_listener(monkeypatch):
    connections = []

    class Connection:
        def __init__(self):
            self.closed = False

        def execute(self, _query):
            return None

        def close(self):
            self.closed = True

    def connect(_db_url, *, autocommit):
        assert autocommit is True
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(server.provider_wakeup.psycopg, "connect", connect)
    pool = server.provider_wakeup.ProviderOperationWakeupPool(
        "postgresql://test",
        max_listeners=1,
    )
    first = pool(str(uuid.uuid4()), 1.0)
    assert first.subscribe(1.0) is True
    first.close()

    second = pool(str(uuid.uuid4()), 1.0)
    assert connections[0].closed is True
    assert second.subscribe(1.0) is True
    second.close()
    pool.close()

    assert len(connections) == 2
    assert connections[1].closed is True


def test_provider_operation_wakeup_pool_polls_when_all_listeners_are_busy(
    monkeypatch,
):
    sleeps = []
    monkeypatch.setattr(server.provider_wakeup.time, "sleep", sleeps.append)
    pool = server.provider_wakeup.ProviderOperationWakeupPool(
        "postgresql://test",
        max_listeners=1,
    )
    first = pool(str(uuid.uuid4()), 1.0)
    second_wait_key = str(uuid.uuid4())
    second = pool(second_wait_key, 1.0)

    assert second.subscribe(1.0) is False
    assert second.wait(20.0, second_wait_key) is False
    second.close()
    first.close()
    pool.close()

    assert sleeps == [server.provider_wakeup.FALLBACK_POLL_INTERVAL_SECONDS]


def test_non_provider_request_does_not_retry_database_deadlock():
    class DeadlockDetected(Exception):
        sqlstate = "40P01"

    handler = _handler([("Content-Length", "0")], path="/v1/desired-state/changes")
    handler.server.private_service.handle.side_effect = DeadlockDetected("deadlock")

    with pytest.raises(DeadlockDetected):
        handler._dispatch()

    assert handler.server.private_service.handle.call_count == 1


def test_provider_deadlock_retry_exhaustion_returns_safe_retryable_response(
    monkeypatch,
):
    class DeadlockDetected(Exception):
        sqlstate = "40P01"

    handler = _handler(
        [("Content-Length", "0")],
        path="/api/workspace-provider/v1/events",
    )
    handler.server.private_service.handle.side_effect = DeadlockDetected("deadlock")
    monkeypatch.setattr(server.time, "sleep", lambda _delay: None)

    handler._dispatch()

    assert handler.server.private_service.handle.call_count == (
        server.PROVIDER_DEADLOCK_MAX_ATTEMPTS
    )
    handler.send_response.assert_called_once_with(503)
    assert b"provider_database_contention" in handler.wfile.getvalue()
    assert b"deadlock" not in handler.wfile.getvalue()


def test_private_handler_rejects_incomplete_body_and_closes_connection():
    handler = _handler([("Content-Length", "2")], body=b"x")

    handler._dispatch()

    handler.send_error.assert_called_once_with(400)
    handler.server.private_service.handle.assert_not_called()
    assert handler.close_connection is True


@pytest.mark.parametrize(
    ("path", "certificate", "length"),
    [
        ("/v1/x", b"certificate", server.MAX_BODY + 1),
        ("/v1/enrollments", None, server.MAX_ENROLLMENT_BODY + 1),
    ],
)
def test_private_handler_rejects_overlong_body(path, certificate, length):
    handler = _handler(
        [("Content-Length", str(length))],
        certificate=certificate,
        path=path,
    )
    handler.rfile = mock.Mock()

    handler._dispatch()

    handler.send_error.assert_called_once_with(413)
    handler.rfile.read.assert_not_called()
    handler.server.private_service.handle.assert_not_called()
