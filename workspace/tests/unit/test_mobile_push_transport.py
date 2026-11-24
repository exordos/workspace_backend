# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
"""Real HTTP regressions for device-address privacy at the producer boundary."""

import http.server
import json
import logging
import os
import pathlib
import shlex
import ssl
import subprocess
import threading
from unittest import mock
import uuid

from gcl_sdk.events.dm import models as sdk_models
from gcl_sdk.events.services import senders
import jinja2
from oslo_config import cfg
import pytest
import requests
import yaml

from workspace.common import config
from workspace.common import mobile_push_opts
from workspace.messenger_api import push_events
from workspace.messenger_api import push_notifications
from workspace.messenger_api.dm import push_events as event_payloads


ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture
def notification_http(request, tmp_path):
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
    scheme = "http"
    if getattr(request, "param", None) == "tls":
        ca = tmp_path / "notification-ca.pem"
        key = tmp_path / "notification-key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-days", "1", "-subj", "/CN=localhost",
                "-addext", "subjectAltName=IP:127.0.0.1",
                "-out", ca, "-keyout", key,
            ],
            check=True,
            capture_output=True,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ca, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"{scheme}://127.0.0.1:{server.server_port}"
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


@pytest.mark.parametrize("notification_http", ("tls",), indirect=True)
def test_sender_uses_the_provisioned_notification_trust_bundle(
    notification_http, monkeypatch, tmp_path
):
    endpoint, default_client, _, received = notification_http
    event_id = str(uuid.uuid4())
    with pytest.raises(requests.exceptions.SSLError):
        default_client.send_event_body({"uuid": event_id})
    assert not received
    system_ca = tmp_path / "other-ca.pem"
    system_ca.write_text("Unrelated system trust")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(system_ca))

    conf = cfg.ConfigOpts()
    conf.register_opts(mobile_push_opts.OPTS, mobile_push_opts.DOMAIN)
    for name, value in {
        "organization_uuid": str(uuid.uuid4()),
        "notification_endpoint": endpoint,
        "notification_token": "synthetic-core-token",
        "apns_notification_token": "synthetic-apns-token",
        "notification_ca_file": str(tmp_path / "notification-ca.pem"),
    }.items():
        conf.set_override(name, value, group=mobile_push_opts.DOMAIN)
    monkeypatch.setattr(cfg, "CONF", conf)
    client = push_events.WorkspacePushSender()._notification_client
    client.send_event_body({"uuid": event_id})
    assert client.get_apns_event(event_id)["uuid"] == event_id
    assert [header for _, header, _ in received] == [
        "Bearer synthetic-core-token",
        "Bearer synthetic-apns-token",
    ]


def test_apns_event_uses_own_token_for_submission_and_status(notification_http):
    endpoint, _, _, received = notification_http
    client = push_events.PushEventHttpClient(
        endpoint, "synthetic-fcm", "synthetic-apns"
    )
    event_id = uuid.uuid4()
    snapshot = '{"content":{"ciphertext":"synthetic"}}'
    event = sdk_models.Event(
        uuid=event_id,
        event_type=sdk_models.UserEvent(uuid=uuid.uuid4(), user_id=uuid.uuid4()),
        event_data=event_payloads.WorkspacePushEventPayload(
            body={
                "uuid": str(event_id),
                "event_params": {
                    "apns_environment": "sandbox",
                    "payload": snapshot,
                    "priority": "high",
                },
                "_registration_updated_at": "internal",
            }
        ),
    )
    with pytest.raises(push_events.PushDeliveryError, match="APNs IAM token"):
        push_events.PushEventHttpClient(endpoint, "synthetic-fcm").send_event(event)
    assert not received
    client.send_event(event)
    assert client.get_apns_event(str(event_id))["uuid"] == str(event_id)
    assert [header for _, header, _ in received] == ["Bearer synthetic-apns"] * 2
    posted = json.loads(received[0][2])
    assert "_registration_updated_at" not in posted
    assert json.loads(posted["event_params"]["payload"]) == {
        "aps": {
            "content-available": 1,
            "alert": {"loc-key": "WORKSPACE_PUSH_NEW_MESSAGE"},
            "sound": "default",
        },
        "workspace": json.loads(snapshot),
    }
    assert event.event_data.body["event_params"]["payload"] == snapshot


@pytest.mark.parametrize("mode", ("direct", "mesh"))
def test_apns_companion_uses_a_separate_event_and_existing_sender(mode):
    connector, endpoint = {
        "direct": ("workspace_apns_push", "workspace_apns_receiver"),
        "mesh": ("workspace_mesh_apns_push", "workspace_mesh_apns_push"),
    }[mode]
    directory = ROOT / "exordos/manifests"
    source = directory / f"{endpoint}.yaml.j2"
    delivery = yaml.safe_load(jinja2.Template(source.read_text()).render())["resources"]
    event_name = push_notifications.APNS_TOPIC_UPDATE
    event = delivery["$exordos_notification.types.email.event_types"][event_name]
    event_id = str(push_notifications.EVENT_TYPES[event_name])
    assert event["uuid"] == event_id
    template = delivery["$exordos_notification.types.email.templates"][event_name]
    assert template["content"] == {"kind": "apns"}
    providers = delivery["$exordos_notification.types.email.providers"]
    if mode == "direct":
        assert providers["apns"]["protocol"]["kind"] == "apns"
        assert template["provider"] == "$exordos_notification.types.email.providers.$apns:uuid"
    else:
        assert providers["apns_relay"]["protocol"] == {
            "kind": "notification_relay",
            "transport": "apns",
        }
        assert template["provider"] == (
            "$exordos_notification.types.email.providers.$apns_relay:uuid"
        )
        assert "apns_credentials" not in delivery["$core.secret.secrets"]
    permissions = delivery["$core.iam.permissions"]
    assert {item["name"] for item in permissions.values()} == {
        f"exordos_notification.event_type_{event_id}.{action}"
        for action in ("create", "read")
    }
    source = directory / f"{connector}.yaml.j2"
    resources = yaml.safe_load(jinja2.Template(source.read_text()).render())["resources"]
    assert (
        "apns_notification_token"
        in resources["$core.config.configs"]["authentication"]["body"]["content"]
    )
    assert resources["$core.config.configs"]["authentication"]["on_change"][
        "command"
    ].endswith(" --push-sender")
    assert (
        "apns_enabled = true"
        in resources["$core.config.configs"]["enable_apns"]["body"]["content"]
    )
    assert "$core.em.services" not in resources


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
        "event_params": {},
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


@pytest.mark.parametrize("mode", ("direct", "mesh"))
def test_managed_token_config_renews_and_restarts_only_the_push_sender(tmp_path, mode):
    template = ROOT / f"exordos/manifests/workspace_{mode}_firebase_push.yaml.j2"
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
    template = ROOT / "exordos/manifests/workspace_direct_firebase_push.yaml.j2"
    manifest = yaml.safe_load(jinja2.Template(template.read_text()).render())
    assert set(manifest["requirements"]) == {
        "core",
        "workspace",
        "exordos_notification",
        "notification_api_https",
    }
    resources = manifest["resources"]
    event_type = resources["$exordos_notification.types.email.event_types"][
        "notification_topic_update"
    ]
    template = resources["$exordos_notification.types.email.templates"][
        "notification_topic_update"
    ]
    assert event_type["uuid"] == "aba6e448-3b72-51ef-b0c6-d2e3be17efe7"
    provider = resources["$exordos_notification.types.email.providers"]["firebase"]
    assert provider["protocol"] == {"kind": "firebase", "ttl_seconds": 3600}
    assert template["provider"] == "$exordos_notification.types.email.providers.$firebase:uuid"
    assert template["content"] == {
        "kind": "firebase_android",
        "device_uuid": "{{ device_uuid }}",
        "registration_token": "{{ registration_token }}",
        "data": {"payload": "{{ payload }}"},
        "priority": "{{ priority | default('normal') }}",
    }
    assert "public_notification_gateway" not in resources["$core.config.configs"]


@pytest.mark.parametrize("mode", ("direct", "mesh"))
def test_manifest_imports_the_local_notification_https_route(mode):
    element = f"workspace_{mode}_firebase_push"
    template = ROOT / f"exordos/manifests/{element}.yaml.j2"
    source = template.read_text()
    # The CLI selects --element by loading the source before rendering Jinja.
    assert yaml.safe_load(source)["name"] == element
    manifest = yaml.safe_load(jinja2.Template(source).render())
    assert "notification_api_https" in manifest["requirements"]
    for key in ("notification_api_domain", "notification_api_trust_pem"):
        assert manifest["imports"][key] == {
            "element": "$notification_api_https",
            "kind": "resource",
            "link": f"$core.vs.variables.${key}",
        }
    resources = manifest["resources"]
    assert "notification_host" not in resources["$core.vs.variables"]
    configs = resources["$core.config.configs"]
    backend = {"kind": "node", "node": f"${element}.imports.$backend_node:uuid"}
    assert configs["notification_endpoint"]["target"] == backend
    assert configs["notification_api_trust"]["target"] == backend
    assert (
        f"notification_endpoint = https://{{${element}.imports."
        "$notification_api_domain:value}/api/notification"
        in configs["notification_endpoint"]["body"]["content"]
    )
    assert configs["notification_api_trust"]["body"]["content"] == (
        f"${element}.imports.$notification_api_trust_pem:value"
    )


def test_mobile_push_manifest_delivers_core_secret_only_to_notification():
    template = ROOT / "exordos/manifests/workspace_direct_firebase_push.yaml.j2"
    manifest = yaml.safe_load(jinja2.Template(template.read_text()).render())
    resources = manifest["resources"]
    assert manifest["imports"]["notification_node"] == {
        "element": "$exordos_notification",
        "kind": "resource",
        "link": "$core.compute.nodes.$notification_cp",
    }

    secret = resources["$core.secret.secrets"]["firebase_service_account"]
    assert secret["name"] == "notification-firebase-service-account"
    assert secret["constructor"] == {"kind": "plain"}
    assert "value" not in secret
    assert "default_value" not in secret

    configs = resources["$core.config.configs"]
    credential = configs["firebase_service_account_file"]
    assert credential["project_id"] == secret["project_id"]
    assert credential["target"] == {
        "kind": "node",
        "node": "$workspace_direct_firebase_push.imports.$notification_node:uuid",
    }
    assert (
        credential["path"] == "/etc/exordos_notification/firebase-service-account.json"
    )
    assert (credential["owner"], credential["group"], credential["mode"]) == (
        "root",
        "root",
        "0600",
    )
    link = "$core.secret.secrets.$firebase_service_account:value"
    assert credential["body"] == {"kind": "text", "content": link}
    assert yaml.safe_dump(manifest).count(link) == 1
    assert credential["on_change"] == {
        "kind": "shell",
        "command": "systemctl try-restart exordos-notification-mail-agent.service",
    }


@pytest.mark.parametrize("mode", ("direct", "mesh"))
def test_push_preparation_and_delivery_have_independent_managed_services(mode):
    manifest = yaml.safe_load(
        jinja2.Template(
            (
                ROOT / f"exordos/manifests/workspace_{mode}_firebase_push.yaml.j2"
            ).read_text()
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
    assert "manifests/workspace_direct_firebase_push.yaml.j2" in manifests
    assert "manifests/workspace_mesh_firebase_push.yaml.j2" in manifests


def test_mesh_configures_local_relay_with_upstream_secret_only_on_notification():
    template = ROOT / "exordos/manifests/workspace_mesh_firebase_push.yaml.j2"
    manifest = yaml.safe_load(jinja2.Template(template.read_text()).render())
    assert set(manifest["requirements"]) == {
        "core",
        "workspace",
        "exordos_notification",
        "notification_api_https",
    }
    resources = manifest["resources"]
    assert resources["$exordos_notification.types.email.providers"][
        "notification_relay"
    ]["protocol"] == {"kind": "notification_relay"}
    event_type = resources["$exordos_notification.types.email.event_types"][
        "notification_topic_update"
    ]
    assert event_type["uuid"] == "aba6e448-3b72-51ef-b0c6-d2e3be17efe7"
    template = resources["$exordos_notification.types.email.templates"][
        "notification_topic_update"
    ]
    assert template["content"] == {"kind": "notification_relay"}
    assert (
        template["event_type"]
        == "$exordos_notification.types.email.event_types.$notification_topic_update:uuid"
    )
    assert template["is_default"] is True
    secret = resources["$core.secret.secrets"]["mesh_upstream"]
    assert secret["name"] == "workspace-mesh-upstream"
    assert secret["constructor"] == {"kind": "plain"}
    assert "value" not in secret
    config = resources["$core.config.configs"]["mesh_upstream_file"]
    assert config["path"] == "/etc/exordos_notification/workspace-mesh-upstream.json"
    assert (config["owner"], config["group"], config["mode"]) == (
        "root",
        "root",
        "0600",
    )
    assert (
        config["target"]["node"]
        == "$workspace_mesh_firebase_push.imports.$notification_node:uuid"
    )
    link = "$core.secret.secrets.$mesh_upstream:value"
    assert config["body"] == {"kind": "text", "content": link}
    assert yaml.safe_dump(manifest).count(link) == 1
    assert config["on_change"] == {
        "kind": "shell",
        "command": "systemctl try-restart exordos-notification-mail-agent.service",
    }
