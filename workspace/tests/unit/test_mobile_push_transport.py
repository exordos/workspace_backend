# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
"""Real HTTP regressions for device-address privacy at the producer boundary."""

import http.server
import json
import threading
import uuid

import pytest

from workspace.messenger_api import push_outbox


@pytest.fixture
def notification_http(tmp_path):
    received = []
    redirect = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/sink":
                received.append(body)
            if redirect and self.path != "/sink":
                self.send_response(307)
                self.send_header("Location", redirect[0])
                self.end_headers()
                return
            if self.path.endswith("/actions/get_token/invoke"):
                body = json.dumps(
                    {
                        "access_token": "synthetic-issued-access",
                        "refresh_token": "synthetic-issued-refresh",
                    }
                ).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"base_url": endpoint, "access_token": "synthetic-core-token"})
    )
    auth.chmod(0o600)
    client = push_outbox.notification_client(endpoint, str(auth))
    try:
        yield endpoint, client, redirect, received, auth
    finally:
        push_outbox.notification_client.cache_clear()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_notification_client_redacts_request_and_echoed_response(
    notification_http, caplog, level
):
    _, client, _, _, _ = notification_http
    body = {
        "uuid": str(uuid.uuid4()),
        "devices": [{"registration_token": "synthetic-device-token"}],
        "content": "synthetic-encrypted-snapshot",
    }
    with caplog.at_level(level):
        # Exercise the configured transport directly: the SDK collection adapter
        # uses http.HTTPMethod, which is unavailable on the Python 3.10 CI job.
        response = client._http_client.post(
            client._base_url + "/v1/events/",
            json=body,
            headers=client._auth.get_auth_header(),
        )
        assert response.json() == body
    assert "HTTP(s) request:" in caplog.text
    for secret in [
        "synthetic-device-token",
        "synthetic-encrypted-snapshot",
        "synthetic-core-token",
    ]:
        assert secret not in caplog.text


def test_notification_client_never_redirects_a_post_with_device_addresses(
    notification_http,
):
    endpoint, client, redirect, received, _ = notification_http
    redirect.append(endpoint + "/sink")
    try:
        client._http_client.post(
            endpoint + "/v1/events/",
            json={"registration_token": "synthetic-device-token"},
            headers=client._auth.get_auth_header(),
        )
    except ValueError as error:
        assert str(error) == "notification_redirect"
    assert not received, "The producer forwarded device addresses to a redirect target"


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_notification_iam_login_and_refresh_never_log_credentials(
    notification_http, caplog, level
):
    endpoint, _, _, _, auth_file = notification_http
    auth_file.write_text(
        json.dumps(
            {
                "base_url": endpoint,
                "username": "synthetic-user",
                "password": "synthetic-password",
            }
        )
    )
    push_outbox.notification_client.cache_clear()
    client = push_outbox.notification_client(endpoint, str(auth_file))
    with caplog.at_level(level):
        client._auth.authenticate()
        client._auth.authenticate()  # Uses the newly issued refresh token.
    assert client._auth.get_auth_header() == {
        "Authorization": "Bearer synthetic-issued-access"
    }
    for secret in [
        "synthetic-password",
        "synthetic-issued-access",
        "synthetic-issued-refresh",
    ]:
        assert secret not in caplog.text
