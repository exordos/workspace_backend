# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import webob

from workspace.messenger_api.api import middlewares


class _DeadlockDetected(Exception):
    sqlstate = "40P01"


def test_idempotent_read_state_request_retries_whole_transaction(monkeypatch):
    bodies = []
    operation_uuids = []

    @webob.dec.wsgify
    def downstream(req):
        bodies.append(req.body)
        if len(bodies) < 3:
            raise _DeadlockDetected("deadlock")
        operation_uuids.append("committed-once")
        return webob.Response(status=200, json={"result": "success"})

    monkeypatch.setattr(middlewares.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(middlewares.random, "uniform", lambda _start, _end: 1.0)
    app = middlewares.DatabaseDeadlockRetryMiddleware(downstream)
    request = webob.Request.blank(
        "/v1/messages/11111111-1111-1111-1111-111111111111/actions/read_up_to/invoke",
        method="POST",
        body=b'{"read":true}',
        content_type="application/json",
    )

    response = request.get_response(app)

    assert response.status_code == 200
    assert bodies == [b'{"read":true}'] * 3
    assert operation_uuids == ["committed-once"]


def test_non_idempotent_request_does_not_retry(monkeypatch):
    attempts = []

    @webob.dec.wsgify
    def downstream(_req):
        attempts.append(1)
        raise _DeadlockDetected("deadlock")

    monkeypatch.setattr(
        middlewares.time,
        "sleep",
        lambda _delay: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )
    app = middlewares.DatabaseDeadlockRetryMiddleware(downstream)
    request = webob.Request.blank(
        "/v1/messages/",
        method="POST",
        body=b"{}",
        content_type="application/json",
    )

    try:
        request.get_response(app)
    except _DeadlockDetected:
        pass
    else:
        raise AssertionError("deadlock must escape without retry")

    assert attempts == [1]


def test_idempotent_read_state_request_returns_safe_retryable_error(monkeypatch):
    attempts = []

    @webob.dec.wsgify
    def downstream(_req):
        attempts.append(1)
        raise _DeadlockDetected(
            "deadlock detected in table m_internal "
            "DETAIL: Process 123 waits for ShareLock"
        )

    monkeypatch.setattr(middlewares.time, "sleep", lambda _delay: None)
    app = middlewares.ErrorsHandlerMiddleware(
        middlewares.DatabaseDeadlockRetryMiddleware(downstream)
    )
    request = webob.Request.blank(
        "/v1/stream_topics/11111111-1111-1111-1111-111111111111/actions/read/invoke",
        method="POST",
    )

    response = request.get_response(app)

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Retry-After"] == "1"
    assert response.json == {
        "type": "DatabaseDeadlockRetryExhaustedError",
        "code": 503,
        "error": "concurrent_update_retry_exhausted",
        "message": (
            "The read-state update could not be completed due to concurrent activity"
        ),
        "retryable": True,
    }
    assert "m_internal" not in response.text
    assert "Process 123" not in response.text
    assert "DETAIL" not in response.text
    assert len(attempts) == middlewares.DATABASE_DEADLOCK_MAX_ATTEMPTS
