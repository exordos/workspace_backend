# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import configparser
import grp
import imaplib
import json
import os
import pathlib
import pwd
import re
import runpy
import socket
import smtplib
import ssl
import subprocess
import sys
import time

import yaml


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
STAGE1_LIVE_MESSENGER_MAIL_SECTION = """smtp_host = 127.0.0.1
smtp_port = 25
smtp_security = plain
imap_host = 127.0.0.1
imap_port = 1143
imap_security = plain
imap_master_username = workspace-service
imap_master_password = {$core.secret.passwords.$workspace_mail_master_password:value}
technical_domain = messenger.workspace.invalid
state_mailbox = Workspace/State
event_mailbox_prefix = Workspace/Events
external_bridge_outbox_target = zulip-bridge-producer@bridge.workspace.invalid
external_bridge_outbox_prefix = Workspace/Bridge/Zulip/V1/Accounts
external_bridge_ingress_target = zulip-bridge-ingress@messenger.workspace.invalid"""


def _read(relative_path):
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _messenger_mail_section(config_body):
    return (
        config_body.split("[messenger_mail]\n", 1)[1]
        .split(
            "\n\n\n[messenger_files]",
            1,
        )[0]
        .rstrip()
    )


def _render_workspace_manifest(
    *, stage1, cutover=False, retain_legacy_mail_resources=None
):
    variables = {
        "version": "test",
        "images": {
            "workspace_backend": "urn:images:built-backend",
            "workspace_mail_raw_zst": "urn:images:built-mail",
        },
    }
    if stage1:
        variables.update(
            {
                "mail_migration_stage1": True,
                "workspace_backend_image": "urn:images:current-live-backend",
            }
        )
    elif cutover:
        variables.update(
            {
                "messenger_storage_mode": "postgresql_canonical",
                "messenger_canonical_cutover_confirmed": True,
            }
        )
    if retain_legacy_mail_resources is not None:
        variables["retain_legacy_mail_resources"] = retain_legacy_mail_resources
    result = _render_workspace_manifest_with_jinja(variables)
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)


def _render_workspace_manifest_with_jinja(variables):
    script = """
import json
import pathlib
import sys

import jinja2

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
print(jinja2.Template(source).render(**json.loads(sys.argv[2])))
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(PROJECT_ROOT / "exordos/manifests/workspace.yaml.j2"),
            json.dumps(variables),
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def _configured_port(section):
    parser = configparser.ConfigParser()
    parser.read(PROJECT_ROOT / "etc/workspace/workspace.conf")
    return parser.getint(section, "bind_port")


def _manifest_port(manifest, section):
    match = re.search(
        rf"(?ms)^\s{{10}}\[{re.escape(section)}\]\n"
        rf".*?^\s{{10}}bind_port = (?P<port>\d+)$",
        manifest,
    )
    assert match is not None, f"missing [{section}] in deployment config"
    return int(match.group("port"))


def test_mail_image_installs_real_smtp_writer_gate_boundary():
    install_script = _read("exordos/images/mail-install.sh")

    assert "postgresql-client" in install_script
    assert "workspace-smtp-ingress-attester.py" in install_script
    assert "/usr/local/bin/workspace-smtp-ingress-attester" in install_script
    assert "workspace-mail-runtime-readiness.sh" in install_script
    assert "/usr/local/lib/workspace/mail-runtime-readiness.sh" in install_script
    assert "exim4.service.d/workspace-writer-gate.conf" in install_script
    assert (
        "ExecStartPre=/usr/local/bin/workspace-smtp-ingress-attester exim-prestart"
        in (install_script)
    )


def test_manifest_preserves_legacy_mail_resources_inertly_at_cutover():
    manifest = _render_workspace_manifest(stage1=False, cutover=True)
    resources = manifest["resources"]

    assert "workspace_mail" in resources["$core.compute.nodes"]
    services = resources["$core.em.services"]
    configs = resources["$core.config.configs"]
    records = resources.get("$workspace.imports.$core_local_domain.records", {})
    assert "workspace_smtp_ingress_attester" not in services
    assert "workspace_mail_bootstrap" not in services
    assert "workspace_mail_ca" not in services
    assert "workspace_mail_config" in configs
    assert "workspace_mail_pki_config" in configs
    assert "workspace_smtp_writer_gate_config" in configs
    assert "on_change" not in configs["workspace_smtp_writer_gate_config"]
    assert "workspace_smtp_writer_gate_enforced_marker_v1" in configs
    assert "workspace_backend_smtp_gate_role_config" not in configs
    assert records["workspace_mail"]["record"]["address"] == (
        "$core.compute.nodes.$workspace_mail:default_network:ipv4"
    )

    users = resources[
        "$dbaas.types.postgres.instances.$workspace_projection_cluster.users"
    ]
    assert users["workspace_mail_gate_user"]["name"] == "workspace_mail_gate"


def test_runtime_entry_points_keep_separate_messenger_and_workspace_apis():
    pyproject = _read("pyproject.toml")
    install_script = _read("exordos/images/backend-install.sh")
    restart_script = _read("exordos/images/workspace-restart-services.sh")
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert 'workspace-messenger-api = "workspace.cmd.messenger_api:main"' in pyproject
    assert 'workspace-api = "workspace.cmd.workspace_api:main"' in pyproject

    for service_name in ("workspace-messenger-api", "workspace-api"):
        assert service_name in install_script
        assert service_name in restart_script
        assert f"name: {service_name}" in manifest
    assert "workspace-external-bridge-api" in restart_script

    assert (
        "ExecStart=workspace-messenger-api --config-file /etc/workspace/workspace.conf"
        in _read("etc/systemd/workspace-messenger-api.service")
    )
    assert (
        "ExecStart=workspace-api --config-file /etc/workspace/workspace.conf"
        in _read("etc/systemd/workspace-api.service")
    )
    workspace_api_unit = _read("etc/systemd/workspace-api.service")
    assert "After=network-online.target" in workspace_api_unit
    assert "dovecot.service" not in workspace_api_unit
    assert "exim4.service" not in workspace_api_unit
    assert "workspace-user-api" not in pyproject
    assert "[user_api]" not in manifest
    assert not (PROJECT_ROOT / "etc/systemd/workspace-user-api.service").exists()


def test_api_ports_are_distinct_in_local_and_deployment_config():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert _configured_port("messenger_api") == 21081
    assert _configured_port("workspace_api") == 21084
    assert _manifest_port(manifest, "messenger_api") == 21081
    assert _manifest_port(manifest, "workspace_api") == 21084


def test_workspace_api_command_default_does_not_collide_with_messenger_api():
    command = _read("workspace/cmd/workspace_api.py")

    assert 'cfg.IntOpt("bind-port", default=21084)' in command


def test_public_messenger_namespace_maps_entirely_to_unchanged_messenger_api():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    messenger_locations = re.findall(
        r"(?m)^\s*location(?:\s+=)?\s+"
        r"(/api/workspace/v1/messenger[^\s{]*)\s*\{",
        manifest,
    )

    assert messenger_locations == ["/api/workspace/v1/messenger/"]
    assert "location /api/workspace/v1/messenger/ {" in manifest
    assert "proxy_pass http://127.0.0.1:21081/v1/;" in manifest


def test_workspace_api_and_common_websocket_routes_use_dedicated_ports():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert "location /api/workspace/ {" in manifest
    assert "proxy_pass http://127.0.0.1:21084/;" in manifest
    assert "location = /api/workspace/v1/events/ws {" in manifest
    assert "proxy_pass http://127.0.0.1:21082/v1/events/ws;" in manifest
    assert manifest.count("proxy_set_header X-Forwarded-Proto https;") == 4


def test_legacy_public_messenger_routes_are_absent():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert "/api/messenger/" not in manifest
    assert "/api/messenger/ws" not in manifest
    assert "/api/workspace/v1/messenger/events/ws" not in manifest


def test_backend_manifest_references_the_local_build_artifact():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    # Exordos CLI normalizes image names by replacing hyphens with underscores
    # before exposing them to the Jinja manifest context.
    assert (
        'image: "{{ workspace_backend_image | '
        "default(images['workspace_backend'], true) }}\"" in manifest
    )
    assert "images['workspace-backend']" not in manifest
    assert 'image: "workspace-backend"' not in manifest
    assert "{{ repository" not in manifest
    assert ".raw.zst" not in manifest


def test_backend_root_disk_and_control_disk_fit_the_built_image():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    build_config = _read("exordos/exordos.yaml")

    assert 'disk_size: "10G"' in build_config
    assert re.search(
        r'(?ms)^\s{8}kind: "disks"\n'
        r"\s{8}disks:\n"
        r"\s{10}- size: 10\n"
        r'\s{12}image: "\{\{ workspace_backend_image \| '
        r'default\(images\[\'workspace_backend\'\], true\) \}\}"\n'
        r"\s{12}label: root\n"
        r"\s{10}- size: 2\n"
        r"\s{12}label: external-bridge-control\n",
        manifest,
    )


def test_element_password_resources_include_the_required_project_scope():
    manifest = _render_workspace_manifest(stage1=False, cutover=True)
    passwords = manifest["resources"]["$core.secret.passwords"]

    assert set(passwords) == {
        "workspace_projection_db_password",
        "workspace_mail_gate_db_password",
        "workspace_mail_master_password",
        "workspace_mail_ca_bootstrap_secret",
        "workspace_zulip_bridge_enrollment_secret",
        "workspace_s3_access_key",
        "workspace_s3_secret_key",
    }
    for password in passwords.values():
        assert password["project_id"] == "12345678-c625-4fee-81d5-f691897b8142"


def test_mail_bootstrap_does_not_remount_an_active_persistent_disk():
    bootstrap = _read("exordos/images/mail-bootstrap.sh")
    wait_ready = _read("exordos/images/workspace-wait-ready.sh")

    assert 'mountpoint -q "$PERSISTENT_MOUNT"' in bootstrap
    assert bootstrap.index('mountpoint -q "$PERSISTENT_MOUNT"') < bootstrap.index(
        'prepare_persistent_disk "$PERSISTENT_DISK" "$PERSISTENT_MOUNT"'
    )
    assert 'mountpoint -q "$WORKSPACE_MAIL_DIR"' in bootstrap
    assert bootstrap.index('mountpoint -q "$WORKSPACE_MAIL_DIR"') < bootstrap.index(
        "migrate_to_persistent"
    )
    assert "/usr/local/bin/workspace-bootstrap" not in wait_ready


def test_canonical_backend_services_do_not_wait_for_mail_readiness():
    manifest = _render_workspace_manifest(stage1=False, cutover=True)
    services = manifest["resources"]["$core.em.services"]

    service_commands = {
        "workspace_api": "/usr/bin/workspace-api",
        "workspace_messenger_api": "/usr/bin/workspace-messenger-api",
        "workspace_messenger_worker": "/usr/bin/workspace-messenger-worker",
        "workspace_messenger_events": "/usr/bin/workspace-messenger-events",
    }
    for service_name, command in service_commands.items():
        assert service_name not in services
        service = services[f"{service_name}_postgresql_canonical_v1"]
        assert service["name"] == service_name.replace("_", "-")
        assert service["path"].startswith(f"{command} ")
        assert "workspace-wait-ready" not in service["path"]
        assert service["before"] == [
            {"kind": "shell", "command": "/usr/local/bin/workspace-bootstrap"}
        ]


def test_stage1_keeps_service_commands_compatible_with_the_pinned_image():
    manifest = _render_workspace_manifest(stage1=True)
    services = manifest["resources"]["$core.em.services"]

    service_commands = {
        "workspace_api": "/usr/bin/workspace-api",
        "workspace_messenger_api": "/usr/bin/workspace-messenger-api",
        "workspace_messenger_worker": "/usr/bin/workspace-messenger-worker",
        "workspace_messenger_events": "/usr/bin/workspace-messenger-events",
    }
    for service_name, command in service_commands.items():
        assert f"{service_name}_remote_mail_v1" not in services
        service = services[service_name]
        assert service["path"].startswith(f"{command} ")
        assert service["before"] == [
            {
                "kind": "shell",
                "command": "/usr/local/bin/workspace-wait-ready",
            }
        ]


def test_backend_bootstrap_disables_shell_trace_while_using_database_password():
    bootstrap = _read("exordos/images/backend-bootstrap.sh")

    disable_trace = bootstrap.index("TRACE_ENABLED=0")
    read_password = bootstrap.index('eval "$(')
    clear_password = bootstrap.index("unset WORKSPACE_PG_PASS")
    restore_trace = bootstrap.index('if [[ "$TRACE_ENABLED" -eq 1 ]]')

    assert disable_trace < read_password < clear_password < restore_trace


def test_retained_mail_bootstrap_requires_writer_gate_role_before_migrations():
    bootstrap = _read("exordos/images/backend-bootstrap.sh")

    storage_mode = bootstrap.index('MESSENGER_STORAGE_MODE="$(')
    role_config = bootstrap.index('config["smtp_writer_gate_role"]["name"]')
    role_name = bootstrap.index('if [ "$SMTP_GATE_ROLE" != "workspace_mail_gate" ]')
    role_ready = bootstrap.index(
        "SELECT 1 FROM pg_roles WHERE rolname = 'workspace_mail_gate';"
    )
    apply_migrations = bootstrap.index("ra-apply-migration")
    repair_role_acl = bootstrap.index(
        'GRANT CONNECT ON DATABASE %I TO "workspace_mail_gate"'
    )

    assert (
        storage_mode
        < role_config
        < role_name
        < role_ready
        < apply_migrations
        < repair_role_acl
    )
    assert 'if [ -n "$SMTP_GATE_ROLE" ]' in bootstrap
    assert "GRANT SELECT ON" in bootstrap
    assert "m_messenger_writer_gate_releases_v1" in bootstrap
    assert "GRANT INSERT, UPDATE ON" in bootstrap
    assert "DELETE" not in bootstrap[apply_migrations:repair_role_acl]
    assert "GRANT CREATE" not in bootstrap
    assert "GRANT TEMPORARY" not in bootstrap


def test_backend_bootstrap_defers_mail_readiness_to_config_on_change():
    bootstrap = _read("exordos/images/backend-bootstrap.sh")
    reload_config = _read("exordos/images/workspace-reload-config.sh")

    assert "workspace-mail-healthcheck" not in bootstrap
    assert "workspace-mail-healthcheck" in reload_config

    clear_ready = reload_config.rindex('rm -f "$READY_FILE"')
    check_mail = reload_config.index('until "$MAIL_HEALTHCHECK"')
    bootstrap_config = reload_config.rindex('"$WORKSPACE_BOOTSTRAP"')
    restart_services = reload_config.rindex('"$RESTART_SERVICES"')

    assert clear_ready < check_mail < bootstrap_config < restart_services


def test_backend_config_reload_defers_until_remote_mail_ca_is_delivered():
    reload_config = _read("exordos/images/workspace-reload-config.sh")

    require_workspace = reload_config.index('if [ ! -s "$WORKSPACE_CONFIG" ]')
    check_starttls = reload_config.index("smtp|imap)_security")
    check_pki = reload_config.index(
        '[ "$STARTTLS_REQUIRED" -eq 1 ] && [ ! -s "$PKI_CONFIG" ]'
    )
    deferred_exit = reload_config.index("exit 0", check_pki)
    optional_pki = reload_config.index('if [ -s "$PKI_CONFIG" ]')
    sync_ca = reload_config.index('"$CA_SYNC" "$PKI_CONFIG"')
    check_ca = reload_config.index('[ ! -s "$TLS_CA" ]')
    clear_ready = reload_config.rindex('rm -f "$READY_FILE"')

    assert (
        require_workspace
        < check_starttls
        < check_pki
        < deferred_exit
        < optional_pki
        < sync_ca
        < check_ca
        < clear_ready
    )


def test_backend_config_reload_fetches_ca_after_deferred_config_delivery(
    tmp_path,
):
    reload_config = PROJECT_ROOT / "exordos/images/workspace-reload-config.sh"
    workspace_config = tmp_path / "workspace.conf"
    pki_config = tmp_path / "mail-pki.conf"
    ca_file = tmp_path / "workspace-mail-ca.crt"
    ready_file = tmp_path / "bootstrap.ready"
    action_log = tmp_path / "actions.log"
    workspace_config.write_text(
        "[messenger_mail]\nsmtp_security = starttls\nimap_security = starttls\n",
        encoding="utf-8",
    )
    ready_file.write_text("ready\n", encoding="utf-8")

    def write_mock(name, body):
        path = tmp_path / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    ca_sync = write_mock(
        "ca-sync",
        'printf "sync\\n" >> "$MOCK_ACTION_LOG"\nprintf "ca\\n" > "$MOCK_CA_FILE"\n',
    )
    healthcheck = write_mock(
        "healthcheck",
        'printf "healthcheck\\n" >> "$MOCK_ACTION_LOG"\n',
    )
    bootstrap = write_mock(
        "bootstrap",
        'printf "bootstrap\\n" >> "$MOCK_ACTION_LOG"\n',
    )
    restart = write_mock(
        "restart",
        'printf "restart\\n" >> "$MOCK_ACTION_LOG"\n',
    )
    environment = {
        **os.environ,
        "WORKSPACE_CONFIG": str(workspace_config),
        "PKI_CONFIG": str(pki_config),
        "READY_FILE": str(ready_file),
        "WORKSPACE_MAIL_CA_FILE": str(ca_file),
        "WORKSPACE_MAIL_CA_SYNC": str(ca_sync),
        "WORKSPACE_MAIL_HEALTHCHECK": str(healthcheck),
        "WORKSPACE_BOOTSTRAP": str(bootstrap),
        "WORKSPACE_RESTART_SERVICES": str(restart),
        "MOCK_ACTION_LOG": str(action_log),
        "MOCK_CA_FILE": str(ca_file),
    }

    deferred = subprocess.run(
        ["bash", reload_config],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert deferred.returncode == 0, deferred.stderr
    assert ready_file.is_file()
    assert not action_log.exists()
    assert not ca_file.exists()

    pki_config.write_text("[mail_pki]\nhostname = workspace-mail.local\n")
    completed = subprocess.run(
        ["bash", reload_config],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert not ready_file.exists()
    assert ca_file.read_text(encoding="utf-8") == "ca\n"
    assert action_log.read_text(encoding="utf-8").splitlines() == [
        "sync",
        "healthcheck",
        "bootstrap",
        "restart",
    ]


def test_canonical_config_reload_never_touches_mail_runtime(tmp_path):
    reload_config = PROJECT_ROOT / "exordos/images/workspace-reload-config.sh"
    workspace_config = tmp_path / "workspace.conf"
    ready_file = tmp_path / "bootstrap.ready"
    action_log = tmp_path / "actions.log"
    workspace_config.write_text(
        "[messenger_storage]\n"
        "mode = postgresql_canonical\n"
        "canonical_cutover_confirmed = true\n",
        encoding="utf-8",
    )
    ready_file.write_text("ready\n", encoding="utf-8")

    def write_mock(name, body):
        path = tmp_path / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    forbidden = write_mock("forbidden", "exit 97\n")
    bootstrap = write_mock(
        "bootstrap",
        'printf "bootstrap\\n" >> "$MOCK_ACTION_LOG"\n'
        'printf "ready\\n" > "$READY_FILE"\n',
    )
    restart = write_mock(
        "restart",
        'printf "restart\\n" >> "$MOCK_ACTION_LOG"\n',
    )
    result = subprocess.run(
        ["bash", reload_config],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "WORKSPACE_CONFIG": str(workspace_config),
            "READY_FILE": str(ready_file),
            "WORKSPACE_MAIL_CA_SYNC": str(forbidden),
            "WORKSPACE_MAIL_HEALTHCHECK": str(forbidden),
            "WORKSPACE_BOOTSTRAP": str(bootstrap),
            "WORKSPACE_RESTART_SERVICES": str(restart),
            "MOCK_ACTION_LOG": str(action_log),
        },
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert action_log.read_text(encoding="utf-8").splitlines() == [
        "bootstrap",
        "restart",
    ]
    assert ready_file.read_text(encoding="utf-8") == "ready\n"


def test_backend_bootstrap_requires_smtp_role_only_before_canonical_cutover():
    bootstrap = _read("exordos/images/backend-bootstrap.sh")

    missing_config = bootstrap.index('if [ ! -s "$WORKSPACE_CONFIG" ]')
    deferred_exit = bootstrap.index("exit 0", missing_config)
    read_mode = bootstrap.index('config.get("messenger_storage", "mode"')
    require_role = bootstrap.index(
        '[ "$MESSENGER_STORAGE_MODE" != "postgresql_canonical" ]'
    )
    assert '&& \\\n        [ ! -s "$SMTP_GATE_ROLE_CONFIG" ]' in bootstrap
    read_config = bootstrap.index('python3 - "$WORKSPACE_CONFIG"')
    publish_ready = bootstrap.index('touch "$READY_FILE"')

    assert missing_config < deferred_exit < read_mode < require_role < publish_ready
    assert read_config < publish_ready


def test_backend_image_supports_platform_managed_ssh_keys():
    install_script = _read("exordos/images/backend-install.sh")

    assert "    openssh-server \\\n" in install_script
    assert "sudo systemctl enable ssh.service" in install_script


def test_manifest_defaults_to_safe_mail_retention_and_requires_explicit_cutover():
    default_render = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": {
                "workspace_backend": "backend-image",
                "workspace_mail_raw_zst": "mail-image",
            },
        }
    )
    assert default_render.returncode == 0, default_render.stderr
    assert "[messenger_storage]\n          mode = mail_projection" in (
        default_render.stdout
    )
    assert "canonical_cutover_confirmed = false" in default_render.stdout
    default_manifest = yaml.safe_load(default_render.stdout)
    assert "workspace_mail" in default_manifest["resources"]["$core.compute.nodes"]

    canonical_render = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": {
                "workspace_backend": "backend-image",
                "workspace_mail_raw_zst": "mail-image",
            },
            "messenger_storage_mode": "postgresql_canonical",
            "messenger_canonical_cutover_confirmed": True,
        }
    )
    assert canonical_render.returncode == 0, canonical_render.stderr
    assert "[messenger_storage]\n          mode = postgresql_canonical" in (
        canonical_render.stdout
    )
    assert "canonical_cutover_confirmed = true" in canonical_render.stdout
    canonical_manifest = yaml.safe_load(canonical_render.stdout)
    assert "workspace_mail" in canonical_manifest["resources"]["$core.compute.nodes"]

    incomplete_cutover = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": {
                "workspace_backend": "backend-image",
                "workspace_mail_raw_zst": "mail-image",
            },
            "messenger_storage_mode": "postgresql_canonical",
        }
    )
    assert incomplete_cutover.returncode != 0
    assert "incompatible_manifest_vars" in incomplete_cutover.stderr

    unsafe_removal = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": {
                "workspace_backend": "backend-image",
                "workspace_mail_raw_zst": "mail-image",
            },
            "retain_legacy_mail_resources": False,
        }
    )
    assert unsafe_removal.returncode != 0
    assert "incompatible_manifest_vars" in unsafe_removal.stderr


def test_external_provider_runtime_artifacts_are_absent():
    pyproject = _read("pyproject.toml")
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    build_config = _read("exordos/exordos.yaml")
    install_script = _read("exordos/images/backend-install.sh")
    restart_script = _read("exordos/images/workspace-restart-services.sh")

    for content in (
        pyproject,
        manifest,
        build_config,
        install_script,
        restart_script,
    ):
        assert "workspace-provider-api" not in content
        assert "workspace-mail-provider" not in content
        assert "workspace-calendar-provider" not in content
        assert "workspace-zulip-provider" not in content

    assert "/api/workspace-service/" not in manifest
    assert "listen 21085;" not in manifest
    assert not list((PROJECT_ROOT / "workspace_providers").rglob("*.py"))
    assert not list((PROJECT_ROOT / "workspace/provider_api").rglob("*.py"))
    assert not list((PROJECT_ROOT / "workspace/groupware").rglob("*.py"))
    assert not list(
        (PROJECT_ROOT / "exordos/manifests").glob("workspace-*-provider.yaml.j2")
    )


def test_workspace_keeps_mail_only_in_the_production_migration_build():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    backend_install = _read("exordos/images/backend-install.sh")
    backend_bootstrap = _read("exordos/images/backend-bootstrap.sh")
    mail_install = _read("exordos/images/mail-install.sh")
    mail_bootstrap = _read("exordos/images/mail-bootstrap.sh")
    build_config = _read("exordos/exordos.yaml")
    migration_build_config = _read("exordos/exordos-production-migration.yaml")

    assert "workspace_projection_cluster" in manifest
    assert "rebuildable PostgreSQL projection" in manifest
    assert "postgresql-client" in backend_install
    assert "ra-apply-migration" in backend_bootstrap
    assert "workspace_mail:" in manifest
    assert "name: workspace-mail" in manifest
    assert (
        'image: "{{ workspace_mail_image | '
        "default(images['workspace_mail_raw_zst'], true) }}\"" in manifest
    )
    assert "name: workspace-mail" not in build_config
    assert "script: images/mail-install.sh" not in build_config
    assert "name: workspace-mail" in migration_build_config
    assert "script: images/mail-install.sh" in migration_build_config
    assert "label: data" in manifest
    assert "[messenger_mail]" in manifest
    assert "'127.0.0.1' if mail_migration_stage1_enabled" in manifest
    assert "workspace-mail.{$workspace.imports.$core_local_domain:name}" in manifest
    assert "imap_host = {{ '127.0.0.1' if mail_migration_stage1_enabled" in (manifest)
    assert "imap_port = 1143" in manifest
    assert "dovecot-imapd" in mail_install
    assert "exim4-daemon-light" in mail_install
    assert "dovecot-imapd" not in backend_install
    assert "exim4-daemon-light" not in backend_install
    assert "prepare_persistent_disk" in mail_bootstrap
    assert "workspace-mail-configure" in mail_bootstrap
    assert "prepare_persistent_disk" not in backend_bootstrap


def test_external_bridge_control_uses_bridge_canonical_hostname():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    control_config = manifest.split("[external_bridge_control]", 1)[1].split('"', 1)[0]

    assert (
        "hostname = workspace-bridge-control."
        "{$workspace.imports.$core_local_domain:name}"
    ) in control_config
    assert "hostname = workspace-backend." not in control_config


def test_external_provider_iam_permissions_are_manifest_provisioned():
    manifest = yaml.safe_load(_read("exordos/manifests/workspace.yaml.j2"))
    permissions = manifest["resources"]["$core.iam.permissions"]
    product_contract = _read("docs/zulip_bridge_v1_product_and_api.md")
    administration_contract = product_contract.split(
        "## 6. Realm administration API proposal",
        1,
    )[1].split("## 7. Private control plane", 1)[0]
    expected = {
        "workspace_external_provider_policy_read": (
            "workspace.external_provider_policy.read"
        ),
        "workspace_external_provider_policy_update": (
            "workspace.external_provider_policy.update"
        ),
        "workspace_external_provider_policy_suspend": (
            "workspace.external_provider_policy.suspend"
        ),
        "workspace_external_provider_policy_resume": (
            "workspace.external_provider_policy.resume"
        ),
        "workspace_external_provider_health_read": (
            "workspace.external_provider_health.read"
        ),
        "workspace_external_bridge_instance_read": (
            "workspace.external_bridge_instance.read"
        ),
        "workspace_external_bridge_instance_suspend": (
            "workspace.external_bridge_instance.suspend"
        ),
        "workspace_external_bridge_instance_resume": (
            "workspace.external_bridge_instance.resume"
        ),
        "workspace_external_bridge_instance_revoke": (
            "workspace.external_bridge_instance.revoke"
        ),
    }

    assert set(permissions) == set(expected)
    assert {key: value["name"] for key, value in permissions.items()} == expected
    assert all(value["description"] for value in permissions.values())
    assert not any(value["name"].endswith(".*") for value in permissions.values())
    policy_permissions = {
        "workspace.external_provider_policy.read",
        "workspace.external_provider_policy.update",
        "workspace.external_provider_policy.suspend",
        "workspace.external_provider_policy.resume",
        "workspace.external_provider_health.read",
    }
    assert policy_permissions < set(expected.values())
    assert all(
        f"`{permission}`" in administration_contract
        for permission in policy_permissions
    )
    assert (
        "Do not grant `workspace.external_provider_policy.*`" in administration_contract
    )
    assert "the wildcard is a role grant" not in administration_contract


def test_external_bridge_enrollment_config_renders_as_json():
    manifest = yaml.safe_load(_read("exordos/manifests/workspace.yaml.j2"))
    content = manifest["resources"]["$core.config.configs"][
        "workspace_external_bridge_enrollment_config"
    ]["body"]["content"]
    rendered = content[2 : content.rfind('"')]
    rendered = rendered.replace(
        "{$core.secret.passwords.$workspace_zulip_bridge_enrollment_secret:uuid}",
        "11111111-2222-4333-8444-555555555555",
    ).replace(
        "{$core.secret.passwords.$workspace_zulip_bridge_enrollment_secret:value}",
        "test-enrollment-token",
    )

    assert json.loads(rendered) == {
        "schema_version": 1,
        "enrollments": [
            {
                "bridge_instance_uuid": "11111111-2222-4333-8444-555555555555",
                "provider_kind": "zulip",
                "enrollment_generation": 1,
                "enrollment_token": "test-enrollment-token",
            }
        ],
    }


def test_external_bridge_control_store_is_prepared_after_mount_on_every_start(
    tmp_path,
):
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    install_script = _read("exordos/images/backend-install.sh")
    prepare_script_path = (
        PROJECT_ROOT / "exordos/images/workspace-external-bridge-control-prepare.sh"
    )
    prepare_script = prepare_script_path.read_text(encoding="utf-8")
    service = manifest.split("    workspace_external_bridge_control:", 1)[1].split(
        "    workspace_messenger_events", 1
    )[0]

    assert "workspace-external-bridge-control-prepare.sh:" in install_script
    assert service.index("workspace-wait-ready") < service.index(
        "workspace-external-bridge-control-prepare"
    )
    assert (
        "workspace-external-bridge-control-prepare "
        "/var/lib/workspace/external-bridge-control root:root 2"
    ) in service
    assert 'STORE_OWNER="${2:-root:root}"' in prepare_script
    assert 'EXPECTED_SIZE_GIB="${3:-}"' in prepare_script
    assert 'FILESYSTEM_LABEL="ws-bridge-ctrl"' in prepare_script
    assert manifest.count("label: external-bridge-control") == 2
    assert "mount_point: /var/lib/workspace/external-bridge-control" not in manifest
    assert "Expected exactly one blank" in prepare_script
    assert 'wipefs -n "$device"' in prepare_script
    assert 'mkfs.ext4 -L "$FILESYSTEM_LABEL" "$device"' in prepare_script
    assert 'migrate_existing_store "$device"' in prepare_script
    assert 'cp -a "$STORE_PATH"/. "$migration_mount"/' in prepare_script
    assert "Both the root filesystem and control disk contain bridge state" in (
        prepare_script
    )
    assert "UUID=%s %s ext4 defaults,nofail 0 2" in prepare_script
    assert "chmod 0700" in prepare_script
    assert "require_dedicated_filesystem = true" in manifest

    store = tmp_path / "store"
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name

    first = subprocess.run(
        ["bash", str(prepare_script_path), str(store), f"{owner}:{group}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert store.stat().st_mode & 0o777 == 0o700
    marker = store / "preserved"
    marker.write_text("persistent", encoding="utf-8")
    second = subprocess.run(
        ["bash", str(prepare_script_path), str(store), f"{owner}:{group}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert marker.read_text(encoding="utf-8") == "persistent"
    assert store.stat().st_mode & 0o777 == 0o700


def test_external_bridge_control_store_initializes_only_expected_blank_disk(
    tmp_path,
):
    prepare_script_path = (
        PROJECT_ROOT / "exordos/images/workspace-external-bridge-control-prepare.sh"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_command = fake_bin / "fake-command"
    fake_command.write_text(
        """#!/usr/bin/env bash
set -eu
command_name="$(basename "$0")"
case "$command_name" in
    findmnt)
        if [[ -f "$FAKE_MOUNT_STATE" ]]; then
            printf '%s\n' "$FAKE_STORE_PATH"
        else
            printf '/\n'
        fi
        ;;
    lsblk)
        case "$*" in
            *NAME,LABEL*)
                if [[ -f "$FAKE_FS_STATE" ]]; then
                    printf '/dev/fake ws-bridge-ctrl\n'
                else
                    printf '/dev/fake\n'
                fi
                ;;
            *NAME,TYPE,SIZE*) printf '/dev/fake disk 2147483648\n' ;;
            *) printf '/dev/fake\n' ;;
        esac
        ;;
    blkid)
        if [[ "$*" == *"-p -s TYPE"* ]]; then
            [[ -f "$FAKE_FS_STATE" ]] || exit 2
            printf 'ext4\n'
        else
            [[ -f "$FAKE_FS_STATE" ]] || exit 2
            printf '11111111-2222-3333-4444-555555555555\n'
        fi
        ;;
    wipefs) ;;
    mkfs.ext4) touch "$FAKE_FS_STATE" ;;
    mount)
        destination="${@: -1}"
        if [[ "$destination" == "$FAKE_STORE_PATH" ]]; then
            touch "$FAKE_MOUNT_STATE"
        else
            cp -a "$FAKE_DEVICE_DIR"/. "$destination"/
            printf '%s\n' "$destination" > "$FAKE_STAGING_STATE"
        fi
        ;;
    mountpoint)
        [[ -f "$FAKE_STAGING_STATE" ]] || exit 1
        [[ "$(cat "$FAKE_STAGING_STATE")" == "${@: -1}" ]]
        ;;
    umount)
        destination="$1"
        find "$FAKE_DEVICE_DIR" -mindepth 1 -delete
        cp -a "$destination"/. "$FAKE_DEVICE_DIR"/
        find "$destination" -mindepth 1 -delete
        rm -f "$FAKE_STAGING_STATE"
        ;;
    sync) ;;
esac
""",
        encoding="utf-8",
    )
    fake_command.chmod(0o755)
    for command in (
        "findmnt",
        "lsblk",
        "blkid",
        "wipefs",
        "mkfs.ext4",
        "mount",
        "mountpoint",
        "umount",
        "sync",
    ):
        (fake_bin / command).symlink_to(fake_command)

    store = tmp_path / "store"
    store.mkdir()
    source_marker = store / "existing-pki-state"
    source_marker.write_text("must survive first disk attachment\n", encoding="utf-8")
    fake_device = tmp_path / "fake-device"
    fake_device.mkdir()
    fstab = tmp_path / "fstab"
    fstab.write_text("", encoding="utf-8")
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FSTAB_PATH": str(fstab),
        "FAKE_FS_STATE": str(tmp_path / "filesystem-created"),
        "FAKE_MOUNT_STATE": str(tmp_path / "filesystem-mounted"),
        "FAKE_STAGING_STATE": str(tmp_path / "filesystem-staging-mount"),
        "FAKE_DEVICE_DIR": str(fake_device),
        "FAKE_STORE_PATH": str(store),
    }

    result = subprocess.run(
        [
            "bash",
            str(prepare_script_path),
            str(store),
            f"{owner}:{group}",
            "2",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "filesystem-created").is_file()
    assert (tmp_path / "filesystem-mounted").is_file()
    assert (fake_device / source_marker.name).read_text(encoding="utf-8") == (
        "must survive first disk attachment\n"
    )
    assert fstab.read_text(encoding="utf-8") == (
        f"UUID=11111111-2222-3333-4444-555555555555 {store} ext4 defaults,nofail 0 2\n"
    )
    assert store.stat().st_mode & 0o777 == 0o700

    repeated = subprocess.run(
        [
            "bash",
            str(prepare_script_path),
            str(store),
            f"{owner}:{group}",
            "2",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert fstab.read_text(encoding="utf-8").count("UUID=") == 1


def test_mail_root_image_can_be_pinned_for_backend_only_releases():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert (
        'image: "{{ workspace_mail_image | '
        "default(images['workspace_mail_raw_zst'], true) }}\"" in manifest
    )


def test_mail_multidisk_root_uses_built_image_and_data_disk_is_image_less():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    mail_node = re.search(
        r"(?ms)^    workspace_mail:\n"
        r".*?^      disk_spec:\n"
        r'        kind: "disks"\n'
        r"        disks:\n"
        r"          - size: 10\n"
        r'            image: "(?P<root_image>[^\n]+)"\n'
        r"            label: root\n"
        r"          - size: 20\n"
        r"(?P<data_fields>(?:            [^\n]+\n)+?)"
        r"(?=^\n|^# \{% endif %\}|^  \$core\.)",
        manifest,
    )

    assert mail_node is not None
    assert mail_node.group("root_image") == (
        "{{ workspace_mail_image | default(images['workspace_mail_raw_zst'], true) }}"
    )
    assert "label: data" in mail_node.group("data_fields")
    assert "image:" not in mail_node.group("data_fields")
    assert "images['workspace_mail']" not in manifest


def test_raw_workspace_manifest_metadata_is_yaml_before_jinja_rendering():
    manifest = yaml.safe_load(_read("exordos/manifests/workspace.yaml.j2"))

    assert manifest["name"] == "workspace"
    assert manifest["description"] == "Workspace backend element"
    assert manifest["schema_version"] == 1
    assert manifest["version"] == "{{ version }}"


def test_actual_jinja_render_supports_mail_modes_and_rejects_invalid_flags():
    images = {
        "workspace_backend": "urn:images:built-backend",
        "workspace_backend_raw_zst": "urn:images:built-backend",
        "workspace_mail_raw_zst": "urn:images:built-mail",
    }
    final_result = _render_workspace_manifest_with_jinja(
        {"version": "test", "images": images}
    )
    assert final_result.returncode == 0, final_result.stderr
    final = yaml.safe_load(final_result.stdout)
    final_backend = final["resources"]["$core.compute.nodes"]["workspace_backend"]
    assert final_backend["ram"] == 4096
    assert final_backend["disk_spec"]["kind"] == "disks"

    pinned_final_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "workspace_backend_image": "urn:images:current-live-backend",
        }
    )
    assert pinned_final_result.returncode == 0, pinned_final_result.stderr
    pinned_final = yaml.safe_load(pinned_final_result.stdout)
    assert (
        pinned_final["resources"]["$core.compute.nodes"]["workspace_backend"][
            "disk_spec"
        ]["disks"][0]["image"]
        == "urn:images:current-live-backend"
    )

    stage1_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_stage1": True,
            "workspace_backend_image": "urn:images:current-live-backend",
        }
    )
    assert stage1_result.returncode == 0, stage1_result.stderr
    stage1 = yaml.safe_load(stage1_result.stdout)
    stage1_backend = stage1["resources"]["$core.compute.nodes"]["workspace_backend"]
    assert stage1_backend["disk_spec"]["kind"] == "disks"
    assert stage1_backend["disk_spec"]["disks"][0]["image"] == (
        "urn:images:current-live-backend"
    )
    stage1_config = stage1["resources"]["$core.config.configs"][
        "workspace_backend_config"
    ]["body"]["content"]
    assert (
        "workspace_backend_mail_pki_config"
        not in stage1["resources"]["$core.config.configs"]
    )
    assert _messenger_mail_section(stage1_config) == (
        STAGE1_LIVE_MESSENGER_MAIL_SECTION
    )
    assert "# " not in _messenger_mail_section(stage1_config)
    stage1_resources = stage1["resources"]
    assert stage1_resources["$core.compute.nodes"]["workspace_mail"]["disk_spec"][
        "disks"
    ] == [
        {"size": 10, "image": "urn:images:built-mail", "label": "root"},
        {"size": 20, "label": "data"},
    ]
    stage1_configs = stage1_resources["$core.config.configs"]
    stage1_services = stage1_resources["$core.em.services"]
    assert "workspace_smtp_writer_gate_config" not in stage1_configs
    assert "workspace_backend_smtp_gate_role_config" not in stage1_configs
    assert "workspace_smtp_ingress_attester" not in stage1_services
    assert "workspace_mail_bootstrap" in stage1_services

    compatibility_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_compatibility": True,
            "workspace_backend_image": "urn:images:current-live-backend",
        }
    )
    assert compatibility_result.returncode == 0, compatibility_result.stderr
    compatibility = yaml.safe_load(compatibility_result.stdout)
    compatibility_resources = compatibility["resources"]
    compatibility_configs = compatibility_resources["$core.config.configs"]
    compatibility_services = compatibility_resources["$core.em.services"]
    compatibility_backend = compatibility_resources["$core.compute.nodes"][
        "workspace_backend"
    ]
    assert compatibility_backend["disk_spec"]["disks"][0]["image"] == (
        "urn:images:current-live-backend"
    )
    compatibility_config = compatibility_configs[
        "workspace_backend_config_remote_mail_v1"
    ]["body"]["content"]
    assert "smtp_host = workspace-mail." in compatibility_config
    assert "smtp_security = starttls" in compatibility_config
    assert "workspace_api_remote_mail_v1" in compatibility_services
    assert (
        "workspace-wait-ready"
        in compatibility_services["workspace_api_remote_mail_v1"]["path"]
    )
    assert "workspace_smtp_writer_gate_compatibility_marker" in (compatibility_configs)
    assert compatibility_configs["workspace_smtp_writer_gate_compatibility_marker"][
        "on_change"
    ] == {
        "kind": "shell",
        "command": "/usr/local/bin/workspace-mail-reload",
    }
    assert "workspace_smtp_writer_gate_config" not in compatibility_configs
    assert "workspace_smtp_ingress_attester" not in compatibility_services

    cutover_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_cutover_keep_legacy_disk": True,
            "workspace_backend_image": "urn:images:current-live-backend",
            "messenger_storage_mode": "postgresql_canonical",
            "messenger_canonical_cutover_confirmed": True,
        }
    )
    assert cutover_result.returncode == 0, cutover_result.stderr
    cutover = yaml.safe_load(cutover_result.stdout)
    cutover_resources = cutover["resources"]
    assert cutover_resources["$core.compute.nodes"]["workspace_backend"]["disk_spec"][
        "disks"
    ] == [
        {
            "size": 10,
            "image": "urn:images:current-live-backend",
            "label": "root",
        },
        {"size": 20, "label": "data"},
        {
            "size": 2,
            "label": "external-bridge-control",
        },
    ]
    cutover_configs = cutover_resources["$core.config.configs"]
    assert "workspace_backend_mail_pki_config" not in cutover_configs
    assert "workspace_backend_config" not in cutover_configs
    cutover_config = cutover_configs[
        "workspace_backend_config_postgresql_canonical_v1"
    ]["body"]["content"]
    assert "[messenger_mail]" not in cutover_config
    assert "workspace-mail." not in cutover_config

    missing_pin_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_stage1": True,
        }
    )
    assert missing_pin_result.returncode != 0
    assert "missing_required_manifest_var" in missing_pin_result.stderr

    compatibility_missing_pin_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_compatibility": True,
        }
    )
    assert compatibility_missing_pin_result.returncode != 0
    assert "missing_required_manifest_var" in compatibility_missing_pin_result.stderr

    incompatible_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_stage1": True,
            "mail_migration_cutover_keep_legacy_disk": True,
            "workspace_backend_image": "urn:images:current-live-backend",
        }
    )
    assert incompatible_result.returncode != 0
    assert "incompatible_manifest_vars" in incompatible_result.stderr

    compatibility_incompatible_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_compatibility": True,
            "mail_migration_cutover_keep_legacy_disk": True,
            "workspace_backend_image": "urn:images:current-live-backend",
        }
    )
    assert compatibility_incompatible_result.returncode != 0
    assert "incompatible_manifest_vars" in compatibility_incompatible_result.stderr

    compatibility_without_retention_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_compatibility": True,
            "workspace_backend_image": "urn:images:current-live-backend",
            "retain_legacy_mail_resources": False,
        }
    )
    assert compatibility_without_retention_result.returncode != 0
    assert "incompatible_manifest_vars" in compatibility_without_retention_result.stderr


def test_stage1_mail_migration_preserves_backend_data_and_local_mail():
    template = _read("exordos/manifests/workspace.yaml.j2")
    assert "missing_required_manifest_var.workspace_backend_image" in template
    manifest = _render_workspace_manifest(stage1=True)
    resources = manifest["resources"]
    nodes = resources["$core.compute.nodes"]
    configs = resources["$core.config.configs"]
    services = resources["$core.em.services"]

    backend = nodes["workspace_backend"]
    assert backend["name"] == "workspace-backend"
    assert backend["disk_spec"] == {
        "kind": "disks",
        "disks": [
            {
                "size": 10,
                "image": "urn:images:current-live-backend",
                "label": "root",
            },
            {"size": 20, "label": "data"},
            {
                "size": 2,
                "label": "external-bridge-control",
            },
        ],
    }
    assert "image" not in backend["disk_spec"]["disks"][1]

    assert "workspace_backend_mail_config" not in configs
    assert "workspace_backend_mail_bootstrap" not in services
    assert "workspace_backend_config_remote_mail_v1" not in configs
    assert "workspace_backend_mail_pki_config" not in configs
    assert services["workspace_bootstrap"]["path"] == (
        "/usr/local/bin/workspace-bootstrap"
    )
    assert services["workspace_bootstrap"]["target"]["node"] == (
        "$core.compute.nodes.$workspace_backend:uuid"
    )

    backend_config = configs["workspace_backend_config"]["body"]["content"]
    assert _messenger_mail_section(backend_config) == (
        STAGE1_LIVE_MESSENGER_MAIL_SECTION
    )
    assert "# " not in _messenger_mail_section(backend_config)

    mail = nodes["workspace_mail"]
    assert mail["name"] == "workspace-mail"
    assert mail["disk_spec"]["disks"] == [
        {"size": 10, "image": "urn:images:built-mail", "label": "root"},
        {"size": 20, "label": "data"},
    ]
    assert configs["workspace_mail_config"]["target"]["node"] == (
        "$core.compute.nodes.$workspace_mail:uuid"
    )
    assert services["workspace_mail_bootstrap"]["target"]["node"] == (
        "$core.compute.nodes.$workspace_mail:uuid"
    )


def test_production_stage_preserves_image_less_backend_data_disk():
    result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": {
                "workspace_backend": "urn:images:built-backend",
                "workspace_mail_raw_zst": "urn:images:built-mail",
            },
            "mail_migration_cutover_keep_legacy_disk": True,
            "messenger_storage_mode": "mail_projection",
            "messenger_canonical_cutover_confirmed": False,
            "retain_legacy_mail_resources": True,
        }
    )

    assert result.returncode == 0, result.stderr
    manifest = yaml.safe_load(result.stdout)
    backend_disks = manifest["resources"]["$core.compute.nodes"]["workspace_backend"][
        "disk_spec"
    ]["disks"]
    assert backend_disks == [
        {
            "size": 10,
            "image": "urn:images:built-backend",
            "label": "root",
        },
        {"size": 20, "label": "data"},
        {"size": 2, "label": "external-bridge-control"},
    ]
    assert "image" not in backend_disks[1]
    resources = manifest["resources"]
    writer_gate_config = resources["$core.config.configs"][
        "workspace_smtp_writer_gate_config"
    ]
    assert "on_change" not in writer_gate_config
    assert (
        resources["$core.em.services"]["workspace_smtp_ingress_attester"]["path"]
        == "/usr/local/bin/workspace-smtp-ingress-attester run"
    )


def test_cutover_uses_new_backend_root_and_keeps_legacy_data_disk():
    manifest = _render_workspace_manifest(stage1=False, cutover=True)
    resources = manifest["resources"]
    nodes = resources["$core.compute.nodes"]
    configs = resources["$core.config.configs"]
    services = resources["$core.em.services"]

    assert nodes["workspace_backend"]["disk_spec"] == {
        "kind": "disks",
        "disks": [
            {
                "size": 10,
                "image": "urn:images:built-backend",
                "label": "root",
            },
            {"size": 20, "label": "data"},
            {
                "size": 2,
                "label": "external-bridge-control",
            },
        ],
    }
    assert "workspace_backend_mail_config" not in configs
    assert "workspace_backend_mail_bootstrap" not in services
    assert "workspace_backend_config" not in configs
    assert "workspace_backend_mail_pki_config" not in configs
    backend_config = configs["workspace_backend_config_postgresql_canonical_v1"][
        "body"
    ]["content"]
    assert "[messenger_mail]" not in backend_config
    assert "workspace-mail." not in backend_config
    assert "mode = postgresql_canonical" in backend_config
    assert "canonical_cutover_confirmed = true" in backend_config
    assert "workspace_mail" in nodes
    assert nodes["workspace_mail"]["disk_spec"]["disks"][1] == {
        "size": 20,
        "label": "data",
    }
    assert "workspace_mail_config" in configs
    assert "workspace_mail_pki_config" in configs
    assert "workspace_smtp_writer_gate_config" in configs
    assert "on_change" not in configs["workspace_smtp_writer_gate_config"]
    assert "workspace_smtp_writer_gate_enforced_marker_v1" in configs
    assert "workspace_mail_bootstrap" not in services
    assert "workspace_mail_ca" not in services
    assert "workspace_smtp_ingress_attester" not in services
    for service_name in (
        "workspace_api_postgresql_canonical_v1",
        "workspace_messenger_api_postgresql_canonical_v1",
        "workspace_messenger_worker_postgresql_canonical_v1",
        "workspace_messenger_events_postgresql_canonical_v1",
    ):
        service = services[service_name]
        assert "workspace-wait-ready" not in service["path"]
        assert service["before"] == [
            {"kind": "shell", "command": "/usr/local/bin/workspace-bootstrap"}
        ]
    bridge_before = services["workspace_external_bridge_control"]["before"]
    assert bridge_before == [
        {"kind": "shell", "command": "/usr/local/bin/workspace-bootstrap"},
        {
            "kind": "shell",
            "command": (
                "/usr/local/bin/workspace-external-bridge-control-prepare "
                "/var/lib/workspace/external-bridge-control root:root 2"
            ),
        },
    ]


def test_final_no_mail_render_contains_no_mail_resources_or_references():
    manifest = _render_workspace_manifest(
        stage1=False,
        cutover=True,
        retain_legacy_mail_resources=False,
    )
    resources = manifest["resources"]

    assert resources["$core.compute.nodes"]["workspace_backend"]["disk_spec"] == {
        "kind": "disks",
        "disks": [
            {
                "size": 10,
                "image": "urn:images:built-backend",
                "label": "root",
            },
            {"size": 2, "label": "external-bridge-control"},
        ],
    }
    assert set(resources["$core.secret.passwords"]) == {
        "workspace_projection_db_password",
        "workspace_zulip_bridge_enrollment_secret",
        "workspace_s3_access_key",
        "workspace_s3_secret_key",
    }
    assert set(manifest["exports"]) == {
        "backend_node",
        "zulip_bridge_enrollment_secret",
        "projection_db_instance",
    }
    rendered_contract = json.dumps(
        {"resources": resources, "exports": manifest["exports"]},
        sort_keys=True,
    )
    for legacy_token in (
        "workspace_mail",
        "workspace-mail",
        "messenger_mail",
        "smtp_",
        "mail-pki",
    ):
        assert legacy_token not in rendered_contract


def test_final_mail_migration_uses_root_and_control_disks_and_remote_mail():
    manifest = _render_workspace_manifest(stage1=False)
    resources = manifest["resources"]
    nodes = resources["$core.compute.nodes"]
    configs = resources["$core.config.configs"]
    services = resources["$core.em.services"]

    assert nodes["workspace_backend"]["disk_spec"] == {
        "kind": "disks",
        "disks": [
            {
                "size": 10,
                "image": "urn:images:built-backend",
                "label": "root",
            },
            {
                "size": 2,
                "label": "external-bridge-control",
            },
        ],
    }
    assert "workspace_backend_mail_config" not in configs
    assert "workspace_backend_mail_bootstrap" not in services
    assert "workspace_backend_config" not in configs
    assert "workspace_backend_mail_pki_config" in configs

    backend_config = configs["workspace_backend_config_remote_mail_v1"]["body"][
        "content"
    ]
    assert (
        "smtp_host = workspace-mail.{$workspace.imports.$core_local_domain:name}"
        in backend_config
    )
    assert (
        "imap_host = workspace-mail.{$workspace.imports.$core_local_domain:name}"
        in backend_config
    )
    assert "smtp_host = 127.0.0.1" not in backend_config
    assert "imap_host = 127.0.0.1" not in backend_config
    assert "smtp_username = workspace-service" in backend_config
    assert "smtp_security = starttls" in backend_config
    assert "smtp_ca_file = /etc/workspace/tls/workspace-mail-ca.crt" in (backend_config)
    assert "imap_security = starttls" in backend_config
    assert "imap_ca_file = /etc/workspace/tls/workspace-mail-ca.crt" in (backend_config)
    assert (
        "smtp_password = "
        "{$core.secret.passwords.$workspace_mail_master_password:value}"
        in backend_config
    )
    assert nodes["workspace_mail"]["disk_spec"]["disks"][1] == {
        "size": 20,
        "label": "data",
    }


def test_mail_node_is_discovered_through_the_core_local_domain():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert "$workspace.imports.$core_local_domain.records:" in manifest
    assert "name: workspace-mail" in manifest
    assert (
        "address: $core.compute.nodes.$workspace_mail:default_network:ipv4" in manifest
    )
    assert "workspace-mail.{$workspace.imports.$core_local_domain:name}" in manifest


def test_zulip_bridge_mail_hook_is_least_privileged_and_persistent():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    install_script = _read("exordos/images/mail-install.sh")
    configure_script = _read("exordos/images/workspace-zulip-bridge-mail-configure.sh")
    dovecot = _read("etc/dovecot/99-workspace-messenger.conf")
    exim_auth = _read("etc/exim4/workspace-messenger-auth.conf")
    exim_router = _read("etc/exim4/workspace-messenger-router.conf")
    exim_transport = _read("etc/exim4/workspace-messenger-transport.conf")

    assert (
        'backend_node:\n    link: "$core.compute.nodes.$workspace_backend"' in manifest
    )
    assert 'mail_node:\n    link: "$core.compute.nodes.$workspace_mail"' in manifest
    assert (
        "mail_ca_bootstrap_secret:\n"
        '    link: "$core.secret.passwords.$workspace_mail_ca_bootstrap_secret"\n'
        '    kind: "resource"'
    ) in manifest
    assert "default=21085" in _read("exordos/images/workspace-mail-ca-server.py")
    assert "realm_uuid = {$workspace.imports.$core_local_domain:uuid}" in manifest
    assert "/usr/local/bin/workspace-zulip-bridge-mail-configure" in install_script
    assert (
        "BRIDGE_HOME=/var/lib/workspace/messenger/mail/"
        "bridge.workspace.invalid/zulip-bridge"
    ) in configure_script
    assert "/var/lib/workspace/messenger/bridge/" not in configure_script
    assert "userdb_mail_readonly=yes" in configure_script
    assert "zulip-bridge-producer@bridge.workspace.invalid:{CRYPT}*" in (
        configure_script
    )
    assert "passdb workspace-zulip-bridge {" in dovecot
    assert "username_filter = zulip-bridge" in dovecot
    assert "userdb workspace-zulip-bridge {" in dovecot
    assert "/etc/dovecot/workspace-zulip-bridge.passwd" in dovecot
    assert "/etc/exim4/workspace-zulip-bridge-smtp.passwd" in exim_auth
    assert "workspace_zulip_bridge_ingress:" in exim_router
    assert "local_parts = zulip-bridge-ingress" in exim_router
    assert "eq{$authenticated_id}{zulip-bridge}" in exim_router
    assert (
        "eq{$sender_address}{zulip-bridge@messenger.workspace.invalid}" in exim_router
    )
    assert "workspace_zulip_bridge_ingress_lda:" in exim_transport
    assert "-d zulip-bridge-ingress@messenger.workspace.invalid" in exim_transport


def test_mail_services_use_authenticated_internal_network_and_disable_os_logins():
    dovecot = _read("etc/dovecot/99-workspace-messenger.conf")
    exim_router = _read("etc/exim4/workspace-messenger-router.conf")
    exim_transport = _read("etc/exim4/workspace-messenger-transport.conf")
    exim_local_parts = _read("etc/exim4/workspace-messenger-local-parts")
    exim_auth = _read("etc/exim4/workspace-messenger-auth.conf")
    exim_tls = _read("etc/exim4/workspace-messenger-tls.conf")
    install_script = _read("exordos/images/mail-install.sh")

    assert "listen = 0.0.0.0" in dovecot
    assert "ssl = required" in dovecot
    assert "ssl_server_cert_file = /etc/workspace/tls/workspace-mail.pem" in dovecot
    assert "ssl_server_key_file = /etc/workspace/tls/workspace-mail.pem" in dovecot
    assert "auth_allow_cleartext = no" in dovecot
    assert "auth_mechanisms = plain login" in dovecot
    assert "disable_plaintext_auth" not in dovecot
    assert "mail_driver = maildir" in dovecot
    assert "mail_path = ~/Maildir" in dovecot
    assert "mail_inbox_path = ~/Maildir" in dovecot
    assert (
        "mail_index_path = /run/workspace/dovecot-indexes/"
        "%{user | domain}/%{user | username}"
    ) in dovecot
    assert "mail_control_path" not in dovecot
    assert "separator = /" in dovecot
    assert 'mailbox "Workspace/State"' in dovecot
    assert 'mailbox "Workspace/Events"' in dovecot
    assert "Workspace.State" not in dovecot
    assert "Workspace.Events" not in dovecot
    assert "mail_location" not in dovecot
    assert "passdb workspace-master {" in dovecot
    assert "master = yes" in dovecot
    assert "result_success = continue" in dovecot
    assert "passwd_file_path = /etc/dovecot/workspace-master.passwd" in dovecot
    assert "passdb workspace-authorized {" in dovecot
    assert "static_password = {CRYPT}*" in dovecot
    assert "userdb workspace-static {" in dovecot
    assert "driver = static" in dovecot
    assert "disables OS-user IMAP authentication" in install_script
    assert "dc_local_interfaces='0.0.0.0'" in install_script
    assert "public_name = PLAIN" in exim_auth
    assert "server_advertise_condition = ${if eq{$tls_in_cipher}{}{}{*}}" in (exim_auth)
    assert "MAIN_TLS_ENABLE = true" in exim_tls
    assert "MAIN_TLS_CERTIFICATE = /etc/workspace/tls/workspace-mail.pem" in exim_tls
    assert "MAIN_TLS_PRIVATEKEY = /etc/workspace/tls/workspace-mail.pem" in exim_tls
    assert "workspace-messenger-tls.conf" in install_script
    assert "/etc/exim4/conf.d/main/01_workspace_messenger" in install_script
    assert "/etc/exim4/workspace-smtp.passwd" in exim_auth
    assert "$authenticated_id" in exim_router
    assert "domains = messenger.workspace.invalid" in exim_router
    assert (
        "local_parts = nwildlsearch,ret=key;/etc/exim4/workspace-messenger-local-parts"
    ) in exim_router
    assert "^(?-i)u-[0-9a-f]{32}$:" in exim_local_parts
    assert "$local_part_data@$domain_data" in exim_transport
    assert "$local_part@$domain" not in exim_transport
    assert "$sender_address" not in exim_transport
    assert "workspace-messenger-local-parts" in install_script


def test_mail_bootstrap_creates_tls_on_the_persistent_disk():
    bootstrap = _read("exordos/images/mail-bootstrap.sh")
    reload_script = _read("exordos/images/workspace-mail-reload.sh")

    assert "/etc/workspace/mail.conf" in bootstrap
    assert "/etc/workspace/mail.conf" in reload_script
    assert "/etc/workspace/smtp-writer-gate.conf" in bootstrap
    assert "/etc/workspace/smtp-writer-gate.conf" not in reload_script
    assert 'if [[ -z "$PERSISTENT_DISK" ]]' in bootstrap
    assert "workspace-mail-pki" in bootstrap
    assert "TLS_STORE_RELATIVE=${TLS_STORE_RELATIVE:-workspace-mail-pki}" in bootstrap
    assert "$PERSISTENT_MOUNT/$TLS_STORE_RELATIVE" in bootstrap
    deferred_exit = bootstrap.index("exit 0")
    mount_disk = bootstrap.index('mountpoint -q "$PERSISTENT_MOUNT"')
    create_tls = bootstrap.index('"$MAIL_PKI_BIN"', mount_disk)
    configure = bootstrap.index('"$MAIL_CONFIGURE_BIN"', create_tls)
    healthcheck = bootstrap.index('"$MAIL_HEALTHCHECK_BIN"', configure)
    assert deferred_exit < mount_disk < create_tls < configure < healthcheck
    assert "sleep 1" not in bootstrap


def test_mail_gate_readiness_covers_every_config_delivery_order(tmp_path):
    readiness = PROJECT_ROOT / "exordos/images/workspace-mail-runtime-readiness.sh"
    compatibility = tmp_path / "compatibility"
    enforced = tmp_path / "enforced"
    gate_config = tmp_path / "gate.conf"

    def state():
        result = subprocess.run(
            [
                "bash",
                "-c",
                (
                    'set -eu; source "$1"; '
                    'if workspace_mail_gate_is_provisioned "$2" "$3" "$4"; '
                    "then printf ready; else printf deferred; fi"
                ),
                "gate-readiness",
                str(readiness),
                str(compatibility),
                str(enforced),
                str(gate_config),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert state() == "deferred"
    compatibility.write_text("wrong\n", encoding="utf-8")
    assert state() == "deferred"
    compatibility.write_text("compatibility-prestart-v1\n", encoding="utf-8")
    assert state() == "ready"
    gate_config.write_text("[smtp_writer_gate]\n", encoding="utf-8")
    assert state() == "ready"
    enforced.write_text("wrong\n", encoding="utf-8")
    assert state() == "deferred"
    enforced.write_text("enforced-prestart-v1\n", encoding="utf-8")
    assert state() == "ready"
    gate_config.unlink()
    assert state() == "deferred"


def test_gate_markers_are_final_reload_points_after_their_inputs():
    images = {
        "workspace_backend": "urn:images:built-backend",
        "workspace_backend_raw_zst": "urn:images:built-backend",
        "workspace_mail_raw_zst": "urn:images:built-mail",
    }
    compatibility_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_compatibility": True,
            "workspace_backend_image": "urn:images:current-live-backend",
        }
    )
    stage_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_cutover_keep_legacy_disk": True,
            "messenger_storage_mode": "mail_projection",
            "messenger_canonical_cutover_confirmed": False,
            "retain_legacy_mail_resources": True,
        }
    )
    assert compatibility_result.returncode == 0, compatibility_result.stderr
    assert stage_result.returncode == 0, stage_result.stderr

    compatibility_configs = list(
        yaml.safe_load(compatibility_result.stdout)["resources"]["$core.config.configs"]
    )
    assert compatibility_configs.index("workspace_mail_pki_config") < (
        compatibility_configs.index("workspace_smtp_writer_gate_compatibility_marker")
    )
    assert compatibility_configs.index("workspace_mail_config") < (
        compatibility_configs.index("workspace_smtp_writer_gate_compatibility_marker")
    )

    stage_configs = yaml.safe_load(stage_result.stdout)["resources"][
        "$core.config.configs"
    ]
    stage_names = list(stage_configs)
    gate_config = stage_configs["workspace_smtp_writer_gate_config"]
    marker = stage_configs["workspace_smtp_writer_gate_enforced_marker_v1"]
    assert stage_names.index("workspace_mail_config") < stage_names.index(
        "workspace_smtp_writer_gate_config"
    )
    assert stage_names.index("workspace_smtp_writer_gate_config") < (
        stage_names.index("workspace_smtp_writer_gate_enforced_marker_v1")
    )
    assert "on_change" not in gate_config
    assert marker["on_change"] == {
        "kind": "shell",
        "command": "/usr/local/bin/workspace-mail-reload",
    }


def test_mail_bootstrap_defers_successfully_before_phase_marker(tmp_path):
    bootstrap = PROJECT_ROOT / "exordos/images/mail-bootstrap.sh"
    readiness = PROJECT_ROOT / "exordos/images/workspace-mail-runtime-readiness.sh"
    config = tmp_path / "mail.conf"
    pki_config = tmp_path / "mail-pki.conf"
    stub_library = tmp_path / "lib-bootstrap.sh"
    config.write_text("[messenger_mail]\n", encoding="utf-8")
    pki_config.write_text("[mail_pki]\n", encoding="utf-8")
    stub_library.write_text(
        "find_persistent_disk() { echo unexpected >&2; return 99; }\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(bootstrap)],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "EXORDOS_BOOTSTRAP_LIB": str(stub_library),
            "WORKSPACE_MAIL_READINESS_LIB": str(readiness),
            "WORKSPACE_CONFIG": str(config),
            "PKI_CONFIG": str(pki_config),
            "COMPATIBILITY_MARKER": str(tmp_path / "compatibility"),
            "ENFORCED_MARKER": str(tmp_path / "enforced"),
            "WRITER_GATE_CONFIG": str(tmp_path / "gate.conf"),
            "RUN_DIR": str(tmp_path / "run"),
        },
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "writer-gate state is incomplete; deferring bootstrap" in result.stdout
    assert "unexpected" not in result.stderr


def test_mail_bootstrap_succeeds_under_valid_hold_with_smtp_inactive_healthcheck(
    tmp_path,
):
    bootstrap = PROJECT_ROOT / "exordos/images/mail-bootstrap.sh"
    readiness = PROJECT_ROOT / "exordos/images/workspace-mail-runtime-readiness.sh"
    config = tmp_path / "mail.conf"
    pki_config = tmp_path / "mail-pki.conf"
    gate_config = tmp_path / "gate.conf"
    enforced = tmp_path / "enforced"
    hold = tmp_path / "smtp-ingress-hold.json"
    tls_dir = tmp_path / "tls"
    log = tmp_path / "commands.log"
    persistent_mount = tmp_path / "persistent"
    maildir = tmp_path / "maildir"
    run_dir = tmp_path / "run"
    stub_library = tmp_path / "lib-bootstrap.sh"
    stub_pki = tmp_path / "mail-pki"
    stub_configure = tmp_path / "mail-configure"
    stub_healthcheck = tmp_path / "mail-healthcheck"

    config.write_text("[messenger_mail]\n", encoding="utf-8")
    pki_config.write_text("[mail_pki]\n", encoding="utf-8")
    gate_config.write_text("[smtp_writer_gate]\n", encoding="utf-8")
    enforced.write_text("enforced-prestart-v1\n", encoding="utf-8")
    hold.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instance_id": "mail-test",
                "gate_ids": ["11111111-1111-4111-8111-111111111111"],
                "held_at": "2026-07-19T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    stub_library.write_text(
        "find_persistent_disk() { printf /dev/fake; }\n"
        'prepare_persistent_disk() { mkdir -p "$2"; }\n'
        'migrate_to_persistent() { mkdir -p "$2"; }\n'
        "persist_migrate_complete() { :; }\n",
        encoding="utf-8",
    )
    stub_pki.write_text(
        '#!/usr/bin/env bash\nset -eu\nmkdir -p "$3"\n'
        'printf bundle >"$3/workspace-mail.pem"\n'
        'printf ca >"$3/workspace-mail-ca.crt"\n',
        encoding="utf-8",
    )
    stub_configure.write_text(
        f"#!/usr/bin/env bash\nset -eu\nprintf '%s\\n' configure >>{log!s}\n",
        encoding="utf-8",
    )
    stub_healthcheck.write_text(
        f"#!/usr/bin/env bash\nset -eu\nprintf '%s\\n' \"$*\" >>{log!s}\n",
        encoding="utf-8",
    )
    for executable in (stub_pki, stub_configure, stub_healthcheck):
        executable.chmod(0o755)

    result = subprocess.run(
        ["bash", str(bootstrap)],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "EXORDOS_BOOTSTRAP_LIB": str(stub_library),
            "WORKSPACE_MAIL_READINESS_LIB": str(readiness),
            "WORKSPACE_CONFIG": str(config),
            "PKI_CONFIG": str(pki_config),
            "COMPATIBILITY_MARKER": str(tmp_path / "compatibility"),
            "ENFORCED_MARKER": str(enforced),
            "WRITER_GATE_CONFIG": str(gate_config),
            "SMTP_WRITER_GATE_HOLD": str(hold),
            "TLS_BUNDLE": str(tls_dir / "workspace-mail.pem"),
            "TLS_CA": str(tls_dir / "workspace-mail-ca.crt"),
            "TLS_OUTPUT_DIR": str(tls_dir),
            "RUN_DIR": str(run_dir),
            "WORKSPACE_MAIL_DIR": str(maildir),
            "MAIL_PKI_BIN": str(stub_pki),
            "MAIL_CONFIGURE_BIN": str(stub_configure),
            "MAIL_HEALTHCHECK_BIN": str(stub_healthcheck),
            "PERSISTENT_MOUNT": str(persistent_mount),
        },
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "configure",
        f"{config} --smtp-inactive",
    ]


def test_configured_mail_keeps_exim_stopped_while_persistent_hold_exists(tmp_path):
    readiness = PROJECT_ROOT / "exordos/images/workspace-mail-runtime-readiness.sh"
    hold = tmp_path / "smtp-ingress-hold.json"
    log = tmp_path / "systemctl.log"
    fake_systemctl = tmp_path / "systemctl"
    hold.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instance_id": "mail-test",
                "gate_ids": ["11111111-1111-4111-8111-111111111111"],
                "held_at": "2026-07-19T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    fake_systemctl.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >>{log!s}\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    held = subprocess.run(
        [
            "bash",
            "-c",
            (
                'set -eu; source "$1"; '
                'workspace_mail_reconcile_configured_exim "$2" "$3"'
            ),
            "reconcile-exim",
            str(readiness),
            str(hold),
            str(fake_systemctl),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert held.returncode == 0, held.stderr
    assert log.read_text(encoding="utf-8") == "stop exim4.service\n"


def test_image_installs_universal_agent_secret_umask_before_config_delivery():
    backend_install = _read("exordos/images/backend-install.sh")
    mail_install = _read("exordos/images/mail-install.sh")
    umask_install = _read("exordos/images/install-universal-agent-umask.sh")

    backend_umask = backend_install.index("install-universal-agent-umask.sh")
    backend_packages = backend_install.index(
        "sudo env DEBIAN_FRONTEND=noninteractive apt-get"
    )
    mail_umask = mail_install.index("install-universal-agent-umask.sh")
    mail_packages = mail_install.index("sudo apt update")
    assert backend_umask < backend_packages
    assert mail_umask < mail_packages

    assert "UNIT=exordos-universal-agent.service" in umask_install
    assert "UMask=0077" in umask_install
    assert "systemd-analyze verify" in umask_install
    assert "systemctl daemon-reload" in umask_install


def test_dovecot_rebuildable_indexes_are_created_on_the_runtime_filesystem():
    configure_script = _read("exordos/images/workspace-mail-configure.sh")
    dovecot = _read("etc/dovecot/99-workspace-messenger.conf")
    install_script = _read("exordos/images/mail-install.sh")

    runtime_parent = configure_script.index(
        "install -d -m 0755 -o root -g root /run/workspace"
    )
    runtime_indexes = configure_script.index("/run/workspace/dovecot-indexes")
    assert runtime_parent < runtime_indexes
    assert "/run/workspace/dovecot-indexes" in configure_script
    assert "-m 0750 -o workspace -g workspace" in configure_script
    assert "mail_index_path = /run/workspace/dovecot-indexes/" in dovecot
    assert "mail_index_path = /var/lib/workspace" not in dovecot
    assert "doveconf -n | /usr/local/bin/workspace-dovecot-validate" in (
        configure_script
    )
    assert "workspace-dovecot-validate.py" in install_script


def test_mail_healthcheck_accepts_an_empty_authenticated_mailbox(
    monkeypatch,
    tmp_path,
):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("smtp-connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, context):
            calls.append(("smtp-starttls", context))

        def login(self, username, password):
            calls.append(("smtp-login", username, password))

        def noop(self):
            calls.append(("smtp-noop",))
            return 250, b"OK"

    class FakeIMAP:
        def __init__(self, host, port, timeout):
            calls.append(("imap-connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, ssl_context):
            calls.append(("imap-starttls", ssl_context))

        def login(self, username, password):
            calls.append(("imap-login", username, password))
            return "OK", [b"authenticated"]

        def noop(self):
            calls.append(("imap-noop",))
            return "OK", [b"still here"]

        def select(self, *_args, **_kwargs):
            raise AssertionError("readiness must not require an existing mailbox")

    config_file = tmp_path / "workspace.conf"
    config_file.write_text(
        "[messenger_mail]\n"
        "smtp_host = mail.internal\n"
        "smtp_port = 25\n"
        "smtp_username = workspace-service\n"
        "smtp_password = smtp-secret\n"
        f"smtp_ca_file = {tmp_path / 'ca.crt'}\n"
        "imap_host = mail.internal\n"
        "imap_port = 1143\n"
        "imap_master_username = workspace-service\n"
        "imap_master_password = imap-secret\n"
        f"imap_ca_file = {tmp_path / 'ca.crt'}\n"
        "technical_domain = messenger.invalid\n",
        encoding="utf-8",
    )
    tls_context = object()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(imaplib, "IMAP4", FakeIMAP)
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda **_kwargs: tls_context,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["workspace-mail-healthcheck", str(config_file)],
    )

    runpy.run_path(
        str(PROJECT_ROOT / "exordos/images/workspace-mail-healthcheck.py"),
        run_name="__main__",
    )

    assert ("smtp-noop",) in calls
    assert ("imap-noop",) in calls


def test_mail_healthcheck_under_hold_requires_inactive_smtp_and_checks_imap(
    monkeypatch,
    tmp_path,
):
    calls = []

    class FakeIMAP:
        def __init__(self, host, port, timeout):
            calls.append(("imap-connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, ssl_context):
            calls.append(("imap-starttls", ssl_context))

        def login(self, username, password):
            calls.append(("imap-login", username, password))

        def noop(self):
            calls.append(("imap-noop",))
            return "OK", [b"still here"]

    class Inactive:
        returncode = 3

    config_file = tmp_path / "workspace.conf"
    config_file.write_text(
        "[messenger_mail]\n"
        "smtp_host = mail.internal\n"
        "smtp_port = 25\n"
        f"smtp_ca_file = {tmp_path / 'ca.crt'}\n"
        "imap_host = mail.internal\n"
        "imap_port = 1143\n"
        "imap_master_username = workspace-service\n"
        "imap_master_password = imap-secret\n"
        f"imap_ca_file = {tmp_path / 'ca.crt'}\n"
        "technical_domain = messenger.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        smtplib,
        "SMTP",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SMTP must not be contacted under a persistent hold")
        ),
    )
    monkeypatch.setattr(imaplib, "IMAP4", FakeIMAP)
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: Inactive())
    monkeypatch.setattr(
        sys,
        "argv",
        ["workspace-mail-healthcheck", str(config_file), "--smtp-inactive"],
    )

    runpy.run_path(
        str(PROJECT_ROOT / "exordos/images/workspace-mail-healthcheck.py"),
        run_name="__main__",
    )

    assert ("imap-noop",) in calls


def test_effective_dovecot_config_rejects_index_path_overrides():
    validator = PROJECT_ROOT / "exordos/images/workspace-dovecot-validate.py"
    runtime_path = "/run/workspace/dovecot-indexes/%{user | domain}/%{user | username}"

    valid = subprocess.run(
        [sys.executable, validator],
        input=f"mail_driver = maildir\nmail_index_path = {runtime_path}\n",
        capture_output=True,
        check=False,
        text=True,
    )
    assert valid.returncode == 0

    invalid_configs = (
        "mail_driver = maildir\n",
        "mail_index_path = /var/lib/workspace/messenger/mail/indexes\n",
        f"mail_index_path = {runtime_path}\n  mail_index_path = {runtime_path}\n",
        f"  mail_index_path = {runtime_path}\n",
        f"mail_index_path = {runtime_path}\nmail_index_path = /tmp/override\n",
        (
            f"mail_index_path = {runtime_path}\n"
            "mail_control_path = /run/workspace/dovecot-control/%{user}\n"
        ),
        (
            f"mail_index_path = {runtime_path}\n"
            "  mail_control_path = /tmp/namespace-control\n"
        ),
        (
            f"mail_index_path = {runtime_path}\n"
            "mail_cache_path = /var/lib/workspace/dovecot-cache/%{user}\n"
        ),
        (
            f"mail_index_path = {runtime_path}\n"
            "mail_index_private_path = /var/lib/workspace/dovecot-private/%{user}\n"
        ),
        (
            f"mail_index_path = {runtime_path}\n"
            "  mail_cache_path = /tmp/namespace-cache\n"
        ),
    )
    for effective_config in invalid_configs:
        invalid = subprocess.run(
            [sys.executable, validator],
            input=effective_config,
            capture_output=True,
            check=False,
            text=True,
        )
        assert invalid.returncode == 1


def test_deployment_mail_option_names_match_runtime_factory():
    local_config = _read("etc/workspace/workspace.conf")
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    configure_script = _read("exordos/images/workspace-mail-configure.sh")
    healthcheck = _read("exordos/images/workspace-mail-healthcheck.py")
    runtime = _read("workspace/messenger_mail/runtime.py")
    mail_options = _read("workspace/common/messenger_mail_opts.py")

    for option in (
        "imap_master_username",
        "imap_master_password",
        "event_mailbox_prefix",
    ):
        assert option in local_config
        assert option in manifest
        assert option in runtime

    for content in (local_config, manifest):
        assert "state_mailbox = Workspace/State" in content
        assert "event_mailbox_prefix = Workspace/Events" in content
        assert "Workspace.State" not in content
        assert "Workspace.Events" not in content

    assert 'config["messenger_mail"]["imap_master_username"]' in configure_script
    assert 'config["messenger_mail"]["imap_master_password"]' in configure_script
    assert "mail['imap_master_username']" in healthcheck
    assert 'mail["imap_master_password"]' in healthcheck
    assert 'smtp.login(mail["smtp_username"], mail["smtp_password"])' in healthcheck
    assert "smtp.starttls(context=smtp_context)" in healthcheck
    assert "imap.starttls(ssl_context=imap_context)" in healthcheck
    assert "status, _ = imap.noop()" in healthcheck
    assert 'imap.select("INBOX")' not in healthcheck
    assert 'ssl.create_default_context(cafile=mail["smtp_ca_file"])' in healthcheck
    assert 'ssl.create_default_context(cafile=mail["imap_ca_file"])' in healthcheck

    assert 'cfg.StrOpt("smtp-ca-file", default=None)' in mail_options
    assert 'cfg.StrOpt("imap-ca-file", default=None)' in mail_options

    for stale_option in ("master_username", "master_password", "events_mailbox"):
        pattern = rf"(?m)^\s*{re.escape(stale_option)}\s*="
        assert re.search(pattern, local_config) is None
        assert re.search(pattern, manifest) is None


def test_internal_mail_certificate_is_persistent_and_not_manifest_managed():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    mail_install = _read("exordos/images/mail-install.sh")
    backend_install = _read("exordos/images/backend-install.sh")
    pki = _read("exordos/images/workspace-mail-pki.sh")
    ca_server = _read("exordos/images/workspace-mail-ca-server.py")
    ca_sync = _read("exordos/images/workspace-mail-ca-sync.py")
    reload_config = _read("exordos/images/workspace-reload-config.sh")

    assert "$core.secret.certificates:" not in manifest
    assert "kind: internal_ca" not in manifest
    assert "workspace-mail-pki.sh" in mail_install
    assert "workspace-mail-ca-server.py" in mail_install
    assert "workspace-mail-ca-sync.py" in backend_install
    assert "TLS_STORE/v1" in pki
    assert "os.lstat(parent)" in pki
    assert "Persistent TLS store parent must be a real directory" in pki
    assert 'install -d -m 0700 "$TLS_STORE"' not in pki
    assert "ca.key" in pki
    assert "leaf.key" in pki
    assert "workspace-mail.pem" in pki
    assert "workspace-mail-ca-v1" in ca_server
    assert "workspace-mail-ca-v1" in ca_sync
    assert "secrets.token_hex(32)" in ca_sync
    assert "nonce.encode()" in ca_server
    assert "requested_hostname.encode()" in ca_server
    assert "hmac.compare_digest" in ca_sync
    assert '"realm_id": pki["realm_id"]' in pki
    assert (
        "bootstrap_secret"
        not in pki.split("write_realm_metadata", 1)[1].split(
            "validate_realm_metadata",
            1,
        )[0]
    )
    assert "checkend" in pki
    assert "-no_check_time" in pki
    assert "mv -Tf" in pki
    assert "validate_permissions" in pki
    assert "Certificate Sign, CRL Sign" in pki
    assert "request_queue_size = 8" in ca_server
    assert "request.settimeout(5)" in ca_server
    assert "workspace-mail-ca-sync" in reload_config
    assert "name: workspace-mail-ca" in manifest
    assert "path: /usr/local/bin/workspace-mail-ca-server" in manifest
    assert "user: workspace-pki" in manifest
    assert "workspace_mail_ca_bootstrap_secret" in manifest
    assert "[mail_pki]" in manifest


def test_persistent_mail_pki_and_authenticated_public_ca_sync(tmp_path):
    pki = PROJECT_ROOT / "exordos/images/workspace-mail-pki.sh"
    ca_server = PROJECT_ROOT / "exordos/images/workspace-mail-ca-server.py"
    ca_sync = PROJECT_ROOT / "exordos/images/workspace-mail-ca-sync.py"
    store = tmp_path / "store"
    live = tmp_path / "live"
    mail_config = tmp_path / "mail-pki.conf"
    mail_config.write_text(
        "[mail_pki]\n"
        "hostname = workspace-mail.localhost\n"
        "realm_id = test-realm-id\n"
        "bootstrap_secret = stable-random-secret\n"
        f"ca_file = {tmp_path / 'backend-ca.crt'}\n"
        f"realm_file = {tmp_path / 'backend-realm.json'}\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "WORKSPACE_MAIL_TLS_SKIP_CHOWN": "1",
    }
    first = subprocess.run(
        ["bash", pki, mail_config, store, live],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    first_ca = (store / "v1/ca.crt").read_bytes()
    first_leaf = (store / "v1/current/leaf.crt").read_bytes()

    second = subprocess.run(
        ["bash", pki, mail_config, store, live],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    assert (store / "v1/ca.crt").read_bytes() == first_ca
    assert (store / "v1/current/leaf.crt").read_bytes() == first_leaf
    assert (store / "v1/ca.key").stat().st_mode & 0o777 == 0o600
    assert (store / "v1/current/leaf.key").stat().st_mode & 0o777 == 0o600
    assert (store / "v1/current/workspace-mail.pem").stat().st_mode & 0o777 == 0o640

    renewal_environment = {
        **environment,
        "WORKSPACE_MAIL_LEAF_RENEW_SECONDS": "999999999",
    }
    renewed = subprocess.run(
        ["bash", pki, mail_config, store, live],
        capture_output=True,
        check=False,
        env=renewal_environment,
        text=True,
    )
    assert renewed.returncode == 0, renewed.stderr
    assert (store / "v1/ca.crt").read_bytes() == first_ca
    assert (store / "v1/current/leaf.crt").read_bytes() != first_leaf
    assert (live / "workspace-mail.pem").is_symlink()
    assert not (live / "workspace-mail-ca.crt").is_symlink()
    assert not (live / "workspace-mail-realm.json").is_symlink()

    old_leaf = store / "v1/leaves/leaf-initial"
    old_timestamp = time.time() - 120
    os.utime(old_leaf, (old_timestamp, old_timestamp))
    pruning_environment = {
        **environment,
        "WORKSPACE_MAIL_LEAF_RETENTION_MINUTES": "0",
    }
    pruned = subprocess.run(
        ["bash", pki, mail_config, store, live],
        capture_output=True,
        check=False,
        env=pruning_environment,
        text=True,
    )
    assert pruned.returncode == 0, pruned.stderr
    assert not old_leaf.exists()
    assert (store / "v1/current/leaf.key").is_file()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = subprocess.Popen(
        [
            sys.executable,
            ca_server,
            "--config-file",
            mail_config,
            "--ca-file",
            live / "workspace-mail-ca.crt",
            "--realm-file",
            live / "workspace-mail-realm.json",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
        ],
    )
    try:
        for _ in range(50):
            with socket.socket() as connection:
                if connection.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.02)
        else:
            raise AssertionError("Workspace mail CA server did not start")

        backend_ca = tmp_path / "backend-ca.crt"
        backend_config = tmp_path / "backend.conf"
        backend_config.write_text(
            "[mail_pki]\n"
            "hostname = workspace-mail.localhost\n"
            "realm_id = test-realm-id\n"
            "bootstrap_secret = stable-random-secret\n"
            f"ca_file = {backend_ca}\n"
            f"realm_file = {tmp_path / 'backend-realm.json'}\n",
            encoding="utf-8",
        )
        synced = subprocess.run(
            [
                sys.executable,
                ca_sync,
                backend_config,
                "--port",
                str(port),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        assert synced.returncode == 0, synced.stderr
        assert backend_ca.read_bytes() == first_ca

        backend_config.write_text(
            backend_config.read_text(encoding="utf-8").replace(
                "stable-random-secret",
                "wrong-secret",
            ),
            encoding="utf-8",
        )
        rejected = subprocess.run(
            [
                sys.executable,
                ca_sync,
                backend_config,
                "--port",
                str(port),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        assert rejected.returncode != 0
        assert backend_ca.read_bytes() == first_ca
    finally:
        server.terminate()
        server.wait(timeout=5)

    mismatched_config = tmp_path / "mismatched.conf"
    mismatched_config.write_text(
        mail_config.read_text(encoding="utf-8").replace(
            "test-realm-id",
            "another-realm-id",
        ),
        encoding="utf-8",
    )
    mismatched = subprocess.run(
        ["bash", pki, mismatched_config, store, live],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert mismatched.returncode != 0
    assert (store / "v1/ca.crt").read_bytes() == first_ca

    rotated_secret_config = tmp_path / "rotated-secret.conf"
    rotated_secret_config.write_text(
        mail_config.read_text(encoding="utf-8").replace(
            "stable-random-secret",
            "replacement-random-secret",
        ),
        encoding="utf-8",
    )
    rotated_secret = subprocess.run(
        ["bash", pki, rotated_secret_config, store, live],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert rotated_secret.returncode == 0, rotated_secret.stderr
    assert (store / "v1/ca.crt").read_bytes() == first_ca

    partial_store = tmp_path / "partial-store"
    (partial_store / "v1").mkdir(parents=True)
    partial = subprocess.run(
        ["bash", pki, mail_config, partial_store, tmp_path / "partial-live"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert partial.returncode != 0
    assert not (partial_store / "v1/ca.key").exists()

    symlink_target = tmp_path / "symlink-target"
    symlink_target.mkdir()
    symlink_store = tmp_path / "symlink-store"
    symlink_store.symlink_to(symlink_target, target_is_directory=True)
    symlinked = subprocess.run(
        ["bash", pki, mail_config, symlink_store, tmp_path / "symlink-live"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert symlinked.returncode != 0
    assert list(symlink_target.iterdir()) == []

    (store / "v1/ca.key").chmod(0o644)
    normalized_permissions = subprocess.run(
        ["bash", pki, mail_config, store, live],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert normalized_permissions.returncode == 0, normalized_permissions.stderr
    assert (store / "v1/ca.key").stat().st_mode & 0o777 == 0o600

    ca_key = store / "v1/ca.key"
    ca_key_backup = store / "v1/ca.key.backup"
    ca_key.rename(ca_key_backup)
    ca_key.symlink_to(ca_key_backup)
    unsafe_type = subprocess.run(
        ["bash", pki, mail_config, store, live],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert unsafe_type.returncode != 0
    ca_key.unlink()
    ca_key_backup.rename(ca_key)

    (store / "v1/current/leaf.crt").write_text(
        "corrupt\n",
        encoding="utf-8",
    )
    corrupted = subprocess.run(
        ["bash", pki, mail_config, store, live],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert corrupted.returncode != 0
    assert (store / "v1/ca.crt").read_bytes() == first_ca


def test_tracked_development_config_does_not_publish_the_iam_secret():
    local_config = configparser.ConfigParser()
    local_config.read_string(_read("etc/workspace/workspace.conf"))
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    gitignore = _read(".gitignore")

    assert local_config["iam"]["hs256_jwks_decryption_key"] == ""
    assert (
        "hs256_jwks_decryption_key = "
        "{$workspace.imports.$var_hs256_jwks_encryption_key:value}"
    ) in manifest
    assert "etc/*/*local.conf" in gitignore


def test_element_builds_and_serves_the_existing_workspace_ui():
    build_config = _read("exordos/exordos.yaml")
    install_script = _read("exordos/images/backend-install.sh")
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert "../../workspace_ui" in build_config
    assert "node_modules" in build_config
    assert 'WORKSPACE_NODE_MAJOR="$(tr -d \'[:space:]\' < "$UI_PATH/.nvmrc")"' in (
        install_script
    )
    assert 'bash "$GC_PATH/exordos/images/install-node-toolchain.sh"' in install_script
    assert "npm ci --include=dev" in install_script
    assert "VITE_MESSENGER_ONLY=true npm run build --workspace=web" in install_script
    assert "packages/web/dist/index.html" in install_script
    assert "location = /logo-512x512.png {" in manifest
    assert "alias /opt/workspace-ui/packages/web/dist/pwa-512x512.png;" in manifest
    assert "root /opt/workspace-ui/packages/web/dist;" in manifest
    assert "try_files $uri $uri/ /index.html;" in manifest
    assert "127.0.0.1:5173" not in manifest


def test_element_workflow_checks_out_compatible_ui_and_publishes():
    workflow = _read(".github/workflows/exordos-element.yml")

    assert "WORKSPACE_UI_REF: a8cb5c990cedd57bba19085ff96007e744d2d16e" in workflow
    assert 'ui_dir="${GITHUB_WORKSPACE}/../workspace_ui"' in workflow
    assert 'fetch --depth=1 origin "${WORKSPACE_UI_REF}"' in workflow
    assert (
        "https://github.com/exordos/exordos/releases/download/"
        "3.0.2/exordos-linux" in workflow
    )
    assert (
        "469007b01253f69b5fcf540b8f6605a360c2539019a5b148fbabb0353bee6a5b" in workflow
    )
    for curl_option in (
        "--connect-timeout 30",
        "--max-time 180",
        "--retry 4",
        "--retry-all-errors",
        "--retry-delay 5",
        "--retry-max-time 600",
    ):
        assert curl_option in workflow
    assert '--output "${exordos_download}"' in workflow
    assert 'mv "${exordos_download}" "${exordos_bin}"' in workflow
    assert '"${EXORDOS_BIN}" version' in workflow
    assert '"${EXORDOS_BIN}" build .' in workflow
    assert '"${EXORDOS_BIN}" push .' in workflow
    publish_step = workflow.split("      - name: Publish element", maxsplit=1)[1]
    assert "\n        if:" not in publish_step.split("\n      - name:", maxsplit=1)[0]
