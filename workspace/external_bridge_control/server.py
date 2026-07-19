# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import http.server
import re
import socketserver
import ssl
import urllib.parse
from collections.abc import Callable
from typing import Any, cast

from workspace.external_bridge_control import pki
from workspace.external_bridge_control import provider_data
from workspace.external_bridge_control import provider_service
from workspace.external_bridge_control import service


MAX_REQUEST_TARGET = 512
MAX_BODY = 52 * 1024 * 1024
MAX_ENROLLMENT_BODY = 1024 * 1024
_CANONICAL_CONTENT_LENGTH = re.compile(r"(?:0|[1-9][0-9]*)")


class _RollbackResponse(RuntimeError):
    def __init__(self, response: service.Response) -> None:
        super().__init__(response.status)
        self.response = response


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def get_request(self) -> tuple[Any, Any]:
        request, address = super().get_request()
        request.settimeout(30)
        return request, address


class BootstrapHandler(http.server.BaseHTTPRequestHandler):
    server_version = "WorkspaceBridgeControlBootstrap/1"

    def do_GET(self) -> None:
        if len(self.path) > MAX_REQUEST_TARGET:
            self.send_error(414)
            return
        try:
            target = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(
                target.query, keep_blank_values=True, strict_parsing=True
            )
        except ValueError:
            self.send_error(400)
            return
        if (
            target.path != "/ca.crt"
            or set(query)
            != {
                "nonce",
                "hostname",
                "bridge_instance_uuid",
                "enrollment_generation",
            }
            or any(len(values) != 1 for values in query.values())
        ):
            self.send_error(404)
            return
        try:
            server = cast(BootstrapServer, self.server)
            content, signature = server.control_pki.ca_bootstrap(
                query["nonce"][0],
                query["hostname"][0],
                query["bridge_instance_uuid"][0],
                int(query["enrollment_generation"][0]),
            )
        except pki.EnrollmentValidationError:
            self.send_error(400)
            return
        except pki.EnrollmentConflictError:
            self.send_error(409)
            return
        except pki.EnrollmentNotFoundError:
            self.send_error(404)
            return
        except pki.PersistentStoreError:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-pem-file")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Workspace-CA-HMAC-SHA256", signature)
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


class BootstrapServer(_ThreadingServer):
    def __init__(
        self,
        address: tuple[str, int],
        control_pki: pki.PersistentControlPki,
    ) -> None:
        super().__init__(address, BootstrapHandler)
        self.control_pki = control_pki


class PrivateHandler(http.server.BaseHTTPRequestHandler):
    server_version = "WorkspaceExternalBridgeAPI/1"

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        if len(self.path) > 8192:
            self.send_error(414)
            return
        certificate_der = self.connection.getpeercert(binary_form=True)
        enrollment = (
            self.command == "POST"
            and urllib.parse.urlsplit(self.path).path == "/v1/enrollments"
        )
        if certificate_der is None and not enrollment:
            self.send_error(401)
            self.close_connection = True
            return
        content_lengths = self.headers.get_all("Content-Length", [])
        transfer_encodings = self.headers.get_all("Transfer-Encoding", [])
        if (
            transfer_encodings
            or len(content_lengths) != 1
            or _CANONICAL_CONTENT_LENGTH.fullmatch(content_lengths[0]) is None
        ):
            self.send_error(400)
            self.close_connection = True
            return
        length = int(content_lengths[0])
        limit = MAX_ENROLLMENT_BODY if enrollment else MAX_BODY
        if length > limit:
            self.send_error(413)
            self.close_connection = True
            return
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self.send_error(408)
            self.close_connection = True
            return
        if len(body) != length:
            self.send_error(400)
            self.close_connection = True
            return
        try:
            server = cast(PrivateServer, self.server)
            if server.request_session_factory is None:
                raise provider_service.ProviderIngressUnavailableError(
                    "Private API request transaction is not configured"
                )
            try:
                with server.request_session_factory() as request_session:
                    response = server.private_service.handle(
                        self.command,
                        self.path,
                        dict(self.headers.items()),
                        body,
                        certificate_der,
                        request_session=request_session,
                    )
                    if response.status >= 400:
                        raise _RollbackResponse(response)
            except _RollbackResponse as rollback:
                response = rollback.response
        except provider_data.ProviderDataError as error:
            response = service.Response.json(
                error.status,
                {
                    "type": "ProviderProblem",
                    "status": error.status,
                    "error": error.error,
                    "message": str(error),
                },
            )
        self.send_response(response.status)
        if response.content_type:
            self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    def log_message(self, format: str, *args: object) -> None:
        return


class PrivateServer(_ThreadingServer):
    def __init__(
        self,
        address: tuple[str, int],
        private_service: service.PrivateBridgeService,
        ssl_context: ssl.SSLContext,
        request_session_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(address, PrivateHandler)
        self.private_service = private_service
        self.request_session_factory = request_session_factory
        self.socket = ssl_context.wrap_socket(self.socket, server_side=True)
