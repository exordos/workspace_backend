# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
"""Real HTTP regressions for device-address privacy at the producer boundary."""

import http.server
import json
import threading
import uuid
from unittest import mock

import pytest
from gcl_sdk.events.services import senders

from workspace.messenger_api import push_events
from workspace.messenger_api.dm import push_events as event_payloads


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
            received.append((self.path, self.headers.get("Authorization"), body))
            if redirect and self.path != "/sink":
                self.send_response(307)
                self.send_header("Location", redirect[0])
                self.end_headers()
                return
            self.send_response(json.loads(body).get("_test_error_status", 201))
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
    auth.write_text(json.dumps({"access_token": "synthetic-core-token"}))
    auth.chmod(0o600)
    client = push_events.PushEventHttpClient(endpoint, str(auth))
    try:
        yield endpoint, client, redirect, received, auth
    finally:
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
        client.send_event_body(body)
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
    with pytest.raises(ValueError, match="notification_redirect"):
        client.send_event_body({"registration_token": "synthetic-device-token"})
    assert all(path != "/sink" for path, _, _ in received)


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_platform_token_rotation_is_loaded_without_login_or_service_restart(
    notification_http, caplog, level
):
    endpoint, first, _, received, auth_file = notification_http
    with caplog.at_level(level):
        first.send_event_body({"uuid": str(uuid.uuid4())})
        auth_file.write_text(json.dumps({"access_token": "synthetic-renewed-token"}))
        first.send_event_body({"uuid": str(uuid.uuid4())})
    assert [(path, auth) for path, auth, _ in received] == [
        ("/v1/events/", "Bearer synthetic-core-token"),
        ("/v1/events/", "Bearer synthetic-renewed-token"),
    ]
    assert "synthetic-core-token" not in caplog.text
    assert "synthetic-renewed-token" not in caplog.text


def test_messenger_worker_does_not_own_push_services(monkeypatch):
    from workspace.cmd import messenger_worker

    monkeypatch.setattr(push_events.mobile_push_opts, "enabled", lambda: True)
    monkeypatch.setattr(push_events, "PushEventHttpClient", lambda *_: None)
    assert all(
        not isinstance(service, senders.EventSenderService)
        for service in messenger_worker.build_worker_services()
    )


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_shared_sdk_sender_does_not_log_an_echoed_provider_error(
    notification_http, caplog, level
):
    _, client, _, received, _ = notification_http
    body = {
        "uuid": str(uuid.uuid4()),
        "_test_error_status": 500,
        "devices": [{"registration_token": "synthetic-device-token"}],
        "content": "synthetic-encrypted-snapshot",
    }
    event = mock.Mock(event_data=event_payloads.WorkspacePushEventPayload(body=body))
    sender = senders.EventSenderService(client)
    sender._fetch_new_events = mock.Mock(return_value=[event])
    with caplog.at_level(level):
        sender._send_new_events()
    assert received
    assert "Notification request failed" in caplog.text
    for secret in [
        "synthetic-device-token",
        "synthetic-encrypted-snapshot",
        "synthetic-core-token",
    ]:
        assert secret not in caplog.text
    event.save.assert_called_once_with()


def test_token_manifest_renews_without_restart_or_password_delivery():
    import pathlib
    import jinja2
    import yaml

    template = (
        pathlib.Path(__file__).resolve().parents[3]
        / "exordos/manifests/mobile_push.yaml.j2"
    )
    manifest = yaml.safe_load(jinja2.Template(template.read_text()).render())
    resources = manifest["resources"]
    token = resources["$core.iam.tokens"]["push_sender"]
    assert token["auto_renew"] is True
    assert token["scope"] == ""
    config = resources["$core.config.configs"]["authentication"]
    assert config["mode"] == "0600"
    assert config["on_change"] == {"kind": "no_action"}
    assert "$core.iam.tokens.$push_sender:access_token" in config["body"]["content"]
    assert "password" not in config["body"]["content"]


def test_push_preparation_and_delivery_have_independent_managed_services():
    import pathlib
    import jinja2
    import yaml

    root = pathlib.Path(__file__).resolve().parents[3]
    manifest = yaml.safe_load(
        jinja2.Template(
            (root / "exordos/manifests/mobile_push.yaml.j2").read_text()
        ).render()
    )
    services = manifest["resources"]["$core.em.services"]
    assert set(services) == {"push_handler", "push_sender"}
    for kind in ("handler", "sender"):
        service = services[f"push_{kind}"]
        assert service["name"] == f"workspace-push-{kind}"
        assert service["service_type"] == {"kind": "simple", "count": 1}
        assert "--config-dir /etc/workspace/workspace.conf.d" in service["path"]
        assert (
            f"workspace-push-{kind}"
            in (root / "exordos/images/backend-install.sh").read_text()
        )
        assert (
            f"workspace-push-{kind}"
            in (root / "exordos/images/workspace-restart-services.sh").read_text()
        )
    assert (
        push_events.WorkspacePushSender._send_new_events
        is senders.EventSenderService._send_new_events
    )
    assert (
        push_events.WorkspacePushSender._process_status_events
        is senders.EventSenderService._process_status_events
    )
