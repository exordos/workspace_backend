# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
"""Real HTTP regressions for device-address privacy at the producer boundary."""

import http.server
import json
import logging
import os
import pathlib
import shlex
import subprocess
import threading
import uuid
from unittest import mock

import jinja2
import pytest
import yaml
from gcl_sdk.events.services import senders
from oslo_config import cfg

from workspace.common import config, mobile_push_opts
from workspace.messenger_api import push_events
from workspace.messenger_api.dm import push_events as event_payloads

ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture
def notification_http():
    received = []
    redirect = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization"), b""))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"uuid": self.path.split("/")[-1]}).encode())

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
    client = push_events.PushEventHttpClient(endpoint, "synthetic-core-token")
    try:
        yield endpoint, client, redirect, received
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_notification_client_redacts_request_and_echoed_response(
    notification_http, caplog, level
):
    _, client, _, _ = notification_http
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
    endpoint, client, redirect, received = notification_http
    redirect.append(endpoint + "/sink")
    with pytest.raises(ValueError, match="notification_redirect"):
        client.send_event_body({"registration_token": "synthetic-device-token"})
    assert all(path != "/sink" for path, _, _ in received)


def test_notification_client_keeps_ingress_prefix(notification_http):
    endpoint, _, _, received = notification_http
    client = push_events.PushEventHttpClient(
        endpoint + "/api/notification", "synthetic-core-token"
    )
    event_id = str(uuid.uuid4())
    client.send_event_body({"uuid": event_id})
    assert client.get_event(event_id)["uuid"] == event_id
    assert [path for path, _, _ in received] == [
        "/api/notification/v1/events/",
        f"/api/notification/v1/events/{event_id}",
    ]


def test_sender_reads_managed_token_at_startup_and_redacts_it(
    notification_http, caplog, monkeypatch, tmp_path
):
    endpoint, _, _, received = notification_http
    base = tmp_path / "workspace.conf"
    base.touch()
    conf_dir = tmp_path / "workspace.conf.d"
    conf_dir.mkdir()
    (conf_dir / "90-mobile-push.conf").write_text(
        "[mobile_push]\n"
        f"organization_uuid = {uuid.uuid4()}\n"
        f"notification_endpoint = {endpoint}\n"
    )
    token_conf = conf_dir / "91-mobile-push-token.conf"

    def start_sender():
        conf = cfg.ConfigOpts()
        conf.register_opts(mobile_push_opts.OPTS, mobile_push_opts.DOMAIN)
        monkeypatch.setattr(cfg, "CONF", conf)
        config.parse(["--config-file", str(base), "--config-dir", str(conf_dir)])
        conf.log_opt_values(logging.getLogger(__name__), logging.DEBUG)
        return push_events.WorkspacePushSender()

    with caplog.at_level("DEBUG"):
        unconfigured = start_sender()
        assert mobile_push_opts.enabled()
        with mock.patch.object(unconfigured, "_fetch_new_events") as fetch:
            unconfigured._iteration()
        fetch.assert_not_called()

        token_conf.write_text(
            "[mobile_push]\nnotification_token = synthetic-core-token\n"
        )
        first = start_sender()._notification_client
        body = {"uuid": str(uuid.uuid4())}
        first.send_event_body(body)
        token_conf.write_text(
            "[mobile_push]\nnotification_token = synthetic-renewed-token\n"
        )
        first.send_event_body(body)
        start_sender()._notification_client.send_event_body(body)
    assert [(path, auth) for path, auth, _ in received] == [
        ("/v1/events/", "Bearer synthetic-core-token"),
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
    _, client, _, received = notification_http
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


def test_managed_token_config_renews_and_restarts_only_the_push_sender(tmp_path):
    template = ROOT / "exordos/manifests/workspace_mobile_push.yaml.j2"
    manifest = yaml.safe_load(jinja2.Template(template.read_text()).render())
    resources = manifest["resources"]
    token = resources["$core.iam.tokens"]["push_sender"]
    assert token["auto_renew"] is True
    assert token["scope"] == ""
    config = resources["$core.config.configs"]["authentication"]
    assert config["path"] == "/etc/workspace/workspace.conf.d/91-mobile-push-token.conf"
    assert config["mode"] == "0600"
    assert "[mobile_push]" in config["body"]["content"]
    assert (
        "notification_token = {$core.iam.tokens.$push_sender:access_token}"
        in config["body"]["content"]
    )
    assert config["on_change"]["kind"] == "shell"
    executable, *args = shlex.split(config["on_change"]["command"])
    assert executable == "/usr/local/bin/workspace-restart-services"
    pgrep = tmp_path / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit 0\n")
    pgrep.chmod(0o700)
    pkill = tmp_path / "pkill"
    pkill.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    pkill.chmod(0o700)
    result = subprocess.run(
        ["bash", ROOT / "exordos/images/workspace-restart-services.sh", *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert result.stdout.splitlines() == [
        "-TERM",
        "-f",
        "(^|[ /])workspace-push-sender([[:space:]]|$)",
    ]


def test_manifest_owns_direct_firebase_event_contract():
    template = ROOT / "exordos/manifests/workspace_mobile_push.yaml.j2"
    manifest = yaml.safe_load(jinja2.Template(template.read_text()).render())
    assert set(manifest["requirements"]) == {
        "workspace",
        "exordos_notification",
        "notification_firebase",
    }
    resources = manifest["resources"]
    event_type = resources["$exordos_notification.types.email.event_types"][
        "notification_topic_update"
    ]
    template = resources["$exordos_notification.types.email.templates"][
        "notification_topic_update"
    ]
    assert event_type["uuid"] == "aba6e448-3b72-51ef-b0c6-d2e3be17efe7"
    assert template["provider"] == (
        "$workspace_mobile_push.imports.$firebase_provider:uuid"
    )
    assert template["content"] == {
        "kind": "firebase_android",
        "device_uuid": "{{ device_uuid }}",
        "registration_token": "{{ registration_token }}",
        "data": {"payload": "{{ payload }}"},
        "priority": "{{ priority | default('normal') }}",
    }
    assert "public_notification_gateway" not in resources["$core.config.configs"]


def test_manifest_routes_to_notification_over_configurable_https():
    template = ROOT / "exordos/manifests/workspace_mobile_push.yaml.j2"
    source = template.read_text()
    # The CLI selects --element by loading the source before rendering Jinja.
    assert yaml.safe_load(source)["name"] == "workspace_mobile_push"
    manifest = yaml.safe_load(jinja2.Template(source).render())
    assert "notification_node" not in manifest["imports"]
    assert "installation_domain" not in manifest["imports"]
    assert "ecosystem_realm" not in manifest["requirements"]
    resources = manifest["resources"]
    host = resources["$core.vs.variables"]["notification_host"]
    assert host["name"] == "workspace_mobile_push_notification_host"
    assert host["setter"] == {
        "kind": "selector",
        "selector_strategy": "latest",
    }
    default_host = resources["$core.vs.values"]["notification_host"]
    assert default_host["variable"] == "$core.vs.variables.$notification_host:uuid"
    assert default_host["value"] == "exordos.com"
    assert default_host["read_only"] is True
    configs = resources["$core.config.configs"]
    assert (
        "notification_endpoint = https://{$core.vs.variables.$notification_host:value}"
        "/api/notification" in configs["notification_endpoint"]["body"]["content"]
    )


def test_push_preparation_and_delivery_have_independent_managed_services():
    manifest = yaml.safe_load(
        jinja2.Template(
            (ROOT / "exordos/manifests/workspace_mobile_push.yaml.j2").read_text()
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
            in (ROOT / "exordos/images/backend-install.sh").read_text()
        )
        assert (
            f"workspace-push-{kind}"
            in (ROOT / "exordos/images/workspace-restart-services.sh").read_text()
        )
    assert (
        push_events.WorkspacePushSender._send_new_events
        is senders.EventSenderService._send_new_events
    )
    assert (
        push_events.WorkspacePushSender._process_status_events
        is senders.EventSenderService._process_status_events
    )


def test_core_managed_workspace_api_and_counter_worker_load_push_config():
    template = ROOT / "exordos/manifests/workspace.yaml.j2"
    manifest = yaml.safe_load(
        jinja2.Template(template.read_text()).render(
            images={"workspace_backend": "test"}
        )
    )
    services = manifest["resources"]["$core.em.services"]
    for name in ("workspace_api", "workspace_messenger_worker"):
        args = shlex.split(services[name]["path"])
        assert "--config-dir" in args
        assert args[args.index("--config-dir") + 1] == "/etc/workspace/workspace.conf.d"


def test_mobile_push_element_is_available_to_the_standard_builder():
    config_path = ROOT / "exordos/exordos.yaml"
    config = yaml.safe_load(config_path.read_text())

    manifests = [element["manifest"] for element in config["build"]["elements"]]
    assert "manifests/workspace_mobile_push.yaml.j2" in manifests
