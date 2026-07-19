# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import email.message
import io
import types
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
