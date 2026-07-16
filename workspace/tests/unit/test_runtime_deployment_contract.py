# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import configparser
import imaplib
import json
import os
import pathlib
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
event_mailbox_prefix = Workspace/Events"""


def _read(relative_path):
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _messenger_mail_section(config_body):
    return config_body.split("[messenger_mail]\n", 1)[1].split(
        "\n\n\n[messenger_files]",
        1,
    )[0]


def _render_workspace_manifest(*, stage1, cutover=False):
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    manifest = re.sub(
        r"^# \{% set mail_migration_(?:stage1|cutover)_enabled = .*? %\}\n",
        "",
        manifest,
        flags=re.MULTILINE,
    )
    manifest = re.sub(
        r"# \{% if mail_migration_stage1_enabled and not .*? %\}"
        r".*?"
        r"# \{% endif %\}\n",
        "",
        manifest,
        flags=re.DOTALL,
    )
    manifest = re.sub(
        r"# \{% if mail_migration_stage1_enabled and "
        r"mail_migration_cutover_enabled %\}"
        r".*?"
        r"# \{% endif %\}\n",
        "",
        manifest,
        flags=re.DOTALL,
    )
    conditional = re.compile(
        r"[ \t]*# \{% if mail_migration_stage1_enabled %\}"
        r"(?P<enabled>.*?)"
        r"(?:[ \t]*# \{% elif mail_migration_cutover_enabled %\}"
        r"(?P<cutover>.*?))?"
        r"(?:[ \t]*# \{% else %\}(?P<disabled>.*?))?"
        r"[ \t]*# \{% endif %\}",
        flags=re.DOTALL,
    )
    while conditional.search(manifest):
        manifest = conditional.sub(
            lambda match: (
                match.group("enabled")
                if stage1
                else (
                    (
                        match.group("cutover")
                        if match.group("cutover") is not None
                        else (match.group("disabled") or "")
                    )
                    if cutover
                    else (match.group("disabled") or "")
                )
            ),
            manifest,
        )
    replacements = {
        "{{ version }}": "test",
        "{{ images['workspace_backend'] }}": "urn:images:built-backend",
        "{{ workspace_backend_image }}": "urn:images:current-live-backend",
        (
            "{{ workspace_mail_image | "
            "default(images['workspace_mail_raw_zst'], true) }}"
        ): "urn:images:built-mail",
        (
            "{{ '127.0.0.1' if mail_migration_stage1_enabled else "
            "'workspace-mail.{$workspace.imports.$core_local_domain:name}' }}"
        ): (
            "127.0.0.1"
            if stage1
            else "workspace-mail.{$workspace.imports.$core_local_domain:name}"
        ),
            (
                "{{ 'plain' if mail_migration_stage1_enabled else "
                "'starttls' }}"
            ): "plain" if stage1 else "starttls",
            (
                "{{ '' if mail_migration_stage1_enabled else "
                "'\\n          smtp_ca_file = "
                "/etc/workspace/tls/workspace-mail-ca.crt"
                "\\n          smtp_username = workspace-service"
                "\\n          smtp_password = "
                "{$core.secret.passwords.$workspace_mail_master_password:value}' }}"
            ): (
                ""
                if stage1
                else (
                    "\n          smtp_ca_file = "
                    "/etc/workspace/tls/workspace-mail-ca.crt"
                    "\n          smtp_username = workspace-service"
                    "\n          smtp_password = "
                    "{$core.secret.passwords.$workspace_mail_master_password:value}"
                )
            ),
            (
                "{{ '' if mail_migration_stage1_enabled else "
                "'\\n          imap_ca_file = "
                "/etc/workspace/tls/workspace-mail-ca.crt' }}"
            ): (
                ""
                if stage1
                else (
                    "\n          imap_ca_file = "
                    "/etc/workspace/tls/workspace-mail-ca.crt"
                )
            ),
        (
            "{{ '' if mail_migration_stage1_enabled else "
            "'_remote_mail_v1' }}"
        ): "" if stage1 else "_remote_mail_v1",
        (
            "{{ '' if mail_migration_stage1_enabled else "
            "'/usr/local/bin/workspace-wait-ready ' }}"
        ): "" if stage1 else "/usr/local/bin/workspace-wait-ready ",
    }
    for expression, value in replacements.items():
        manifest = manifest.replace(expression, value)
    assert "{{" not in manifest
    assert "{%" not in manifest
    return yaml.safe_load(manifest)


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

    assert (
        "ExecStart=workspace-messenger-api --config-file /etc/workspace/workspace.conf"
        in _read("etc/systemd/workspace-messenger-api.service")
    )
    assert (
        "ExecStart=workspace-api --config-file /etc/workspace/workspace.conf"
        in _read("etc/systemd/workspace-api.service")
    )
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


def test_legacy_public_messenger_routes_are_absent():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert "/api/messenger/" not in manifest
    assert "/api/messenger/ws" not in manifest
    assert "/api/workspace/v1/messenger/events/ws" not in manifest


def test_backend_manifest_references_the_local_build_artifact():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    # Exordos CLI normalizes image names by replacing hyphens with underscores
    # before exposing them to the Jinja manifest context.
    assert 'image: "{{ images[\'workspace_backend\'] }}"' in manifest
    assert "images['workspace-backend']" not in manifest
    assert 'image: "workspace-backend"' not in manifest
    assert "{{ repository" not in manifest
    assert ".raw.zst" not in manifest


def test_backend_root_disk_fits_the_built_image():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    build_config = _read("exordos/exordos.yaml")

    assert 'disk_size: "10G"' in build_config
    assert re.search(
        r'(?ms)^\s{8}kind: "root_disk"\n'
        r'\s{8}size: 10\n'
        r'\s{8}image: "\{\{ images\[\'workspace_backend\'\] \}\}"',
        manifest,
    )


def test_element_password_resources_include_the_required_project_scope():
    manifest = _render_workspace_manifest(stage1=False, cutover=True)
    passwords = manifest["resources"]["$core.secret.passwords"]

    assert set(passwords) == {
        "workspace_projection_db_password",
        "workspace_mail_master_password",
        "workspace_mail_ca_bootstrap_secret",
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


def test_backend_services_wait_inside_the_managed_process_until_ready():
    manifest = _render_workspace_manifest(stage1=False, cutover=True)
    services = manifest["resources"]["$core.em.services"]
    wait_ready = _read("exordos/images/workspace-wait-ready.sh")

    service_commands = {
        "workspace_api": "/usr/bin/workspace-api",
        "workspace_messenger_api": "/usr/bin/workspace-messenger-api",
        "workspace_messenger_worker": "/usr/bin/workspace-messenger-worker",
        "workspace_messenger_events": "/usr/bin/workspace-messenger-events",
    }
    for service_name, command in service_commands.items():
        assert service_name not in services
        service = services[f"{service_name}_remote_mail_v1"]
        assert service["name"] == service_name.replace("_", "-")
        assert service["path"].startswith(
            f"/usr/local/bin/workspace-wait-ready {command} "
        )
        assert "before" not in service

    assert 'exec "$@"' in wait_ready


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


def test_backend_bootstrap_defers_mail_readiness_to_config_on_change():
    bootstrap = _read("exordos/images/backend-bootstrap.sh")
    reload_config = _read("exordos/images/workspace-reload-config.sh")

    assert "workspace-mail-healthcheck" not in bootstrap
    assert "workspace-mail-healthcheck" in reload_config

    clear_ready = reload_config.index('rm -f "$READY_FILE"')
    check_mail = reload_config.index('until "$MAIL_HEALTHCHECK"')
    bootstrap_config = reload_config.index('"$WORKSPACE_BOOTSTRAP"')
    restart_services = reload_config.index('"$RESTART_SERVICES"')

    assert clear_ready < check_mail < bootstrap_config < restart_services


def test_backend_config_reload_defers_until_remote_mail_ca_is_delivered():
    reload_config = _read("exordos/images/workspace-reload-config.sh")

    require_workspace = reload_config.index(
        'if [ ! -s "$WORKSPACE_CONFIG" ]'
    )
    check_starttls = reload_config.index(
        "smtp|imap)_security"
    )
    check_pki = reload_config.index(
        '[ "$STARTTLS_REQUIRED" -eq 1 ] && [ ! -s "$PKI_CONFIG" ]'
    )
    deferred_exit = reload_config.index("exit 0", check_pki)
    optional_pki = reload_config.index('if [ -s "$PKI_CONFIG" ]')
    sync_ca = reload_config.index('"$CA_SYNC" "$PKI_CONFIG"')
    check_ca = reload_config.index('[ ! -s "$TLS_CA" ]')
    clear_ready = reload_config.index('rm -f "$READY_FILE"')

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
    reload_config = (
        PROJECT_ROOT / "exordos/images/workspace-reload-config.sh"
    )
    workspace_config = tmp_path / "workspace.conf"
    pki_config = tmp_path / "mail-pki.conf"
    ca_file = tmp_path / "workspace-mail-ca.crt"
    ready_file = tmp_path / "bootstrap.ready"
    action_log = tmp_path / "actions.log"
    workspace_config.write_text(
        "[messenger_mail]\n"
        "smtp_security = starttls\n"
        "imap_security = starttls\n",
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
        'printf "sync\\n" >> "$MOCK_ACTION_LOG"\n'
        'printf "ca\\n" > "$MOCK_CA_FILE"\n',
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


def test_backend_bootstrap_defers_successfully_until_config_delivery():
    bootstrap = _read("exordos/images/backend-bootstrap.sh")

    missing_config = bootstrap.index('if [ ! -s "$WORKSPACE_CONFIG" ]')
    deferred_exit = bootstrap.index("exit 0", missing_config)
    read_config = bootstrap.index('python3 - "$WORKSPACE_CONFIG"')
    publish_ready = bootstrap.index('touch "$READY_FILE"')

    assert missing_config < deferred_exit < read_config < publish_ready


def test_backend_image_supports_platform_managed_ssh_keys():
    install_script = _read("exordos/images/backend-install.sh")

    assert "    openssh-server \\\n" in install_script
    assert "sudo systemctl enable ssh.service" in install_script


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


def test_workspace_uses_dedicated_mail_node_and_a_secondary_projection_database():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    backend_install = _read("exordos/images/backend-install.sh")
    backend_bootstrap = _read("exordos/images/backend-bootstrap.sh")
    mail_install = _read("exordos/images/mail-install.sh")
    mail_bootstrap = _read("exordos/images/mail-bootstrap.sh")
    build_config = _read("exordos/exordos.yaml")

    assert "workspace_projection_cluster" in manifest
    assert "rebuildable PostgreSQL projection" in manifest
    assert "postgresql-client" in backend_install
    assert "ra-apply-migration" in backend_bootstrap
    assert "workspace_mail:" in manifest
    assert "name: workspace-mail" in manifest
    assert (
        "image: \"{{ workspace_mail_image | "
        "default(images['workspace_mail_raw_zst'], true) }}\""
        in manifest
    )
    assert "name: workspace-mail" in build_config
    assert "script: images/mail-install.sh" in build_config
    assert "label: data" in manifest
    assert "[messenger_mail]" in manifest
    assert "'127.0.0.1' if mail_migration_stage1_enabled" in manifest
    assert "workspace-mail.{$workspace.imports.$core_local_domain:name}" in manifest
    assert "imap_host = {{ '127.0.0.1' if mail_migration_stage1_enabled" in (
        manifest
    )
    assert "imap_port = 1143" in manifest
    assert "dovecot-imapd" in mail_install
    assert "exim4-daemon-light" in mail_install
    assert "dovecot-imapd" not in backend_install
    assert "exim4-daemon-light" not in backend_install
    assert "prepare_persistent_disk" in mail_bootstrap
    assert "workspace-mail-configure" in mail_bootstrap
    assert "prepare_persistent_disk" not in backend_bootstrap


def test_mail_root_image_can_be_pinned_for_backend_only_releases():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert (
        "image: \"{{ workspace_mail_image | "
        "default(images['workspace_mail_raw_zst'], true) }}\""
        in manifest
    )


def test_mail_multidisk_root_uses_built_image_and_data_disk_is_image_less():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    mail_node = re.search(
        r'(?ms)^    workspace_mail:\n'
        r'.*?^      disk_spec:\n'
        r'        kind: "disks"\n'
        r'        disks:\n'
        r'          - size: 10\n'
        r'            image: "(?P<root_image>[^\n]+)"\n'
        r'            label: root\n'
        r'          - size: 20\n'
        r'(?P<data_fields>(?:            [^\n]+\n)+?)'
        r'(?=^\n|^  \$core\.)',
        manifest,
    )

    assert mail_node is not None
    assert mail_node.group("root_image") == (
        "{{ workspace_mail_image | "
        "default(images['workspace_mail_raw_zst'], true) }}"
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
    assert final["resources"]["$core.compute.nodes"]["workspace_backend"][
        "disk_spec"
    ]["kind"] == "root_disk"

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
    stage1_backend = stage1["resources"]["$core.compute.nodes"][
        "workspace_backend"
    ]
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

    cutover_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_cutover_keep_legacy_disk": True,
        }
    )
    assert cutover_result.returncode == 0, cutover_result.stderr
    cutover = yaml.safe_load(cutover_result.stdout)
    cutover_resources = cutover["resources"]
    assert cutover_resources["$core.compute.nodes"]["workspace_backend"][
        "disk_spec"
    ]["disks"] == [
        {"size": 10, "image": "urn:images:built-backend", "label": "root"},
        {"size": 20, "label": "data"},
    ]
    cutover_configs = cutover_resources["$core.config.configs"]
    assert "workspace_backend_mail_pki_config" in cutover_configs
    assert "workspace_backend_config" not in cutover_configs
    cutover_config = cutover_configs[
        "workspace_backend_config_remote_mail_v1"
    ]["body"]["content"]
    assert "smtp_host = workspace-mail." in cutover_config
    assert "smtp_username = workspace-service" in cutover_config

    missing_pin_result = _render_workspace_manifest_with_jinja(
        {
            "version": "test",
            "images": images,
            "mail_migration_stage1": True,
        }
    )
    assert missing_pin_result.returncode != 0
    assert "missing_required_manifest_var" in missing_pin_result.stderr

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
    assert "smtp_username = workspace-service" in backend_config
    assert "smtp_security = starttls" in backend_config
    assert "smtp_ca_file = /etc/workspace/tls/workspace-mail-ca.crt" in (
        backend_config
    )
    assert "imap_security = starttls" in backend_config
    assert "imap_ca_file = /etc/workspace/tls/workspace-mail-ca.crt" in (
        backend_config
    )
    assert "smtp_host = 127.0.0.1" not in backend_config


def test_final_mail_migration_uses_root_only_backend_and_remote_mail():
    manifest = _render_workspace_manifest(stage1=False)
    resources = manifest["resources"]
    nodes = resources["$core.compute.nodes"]
    configs = resources["$core.config.configs"]
    services = resources["$core.em.services"]

    assert nodes["workspace_backend"]["disk_spec"] == {
        "kind": "root_disk",
        "size": 10,
        "image": "urn:images:built-backend",
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
    assert "smtp_ca_file = /etc/workspace/tls/workspace-mail-ca.crt" in (
        backend_config
    )
    assert "imap_security = starttls" in backend_config
    assert "imap_ca_file = /etc/workspace/tls/workspace-mail-ca.crt" in (
        backend_config
    )
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
        "address: $core.compute.nodes.$workspace_mail:default_network:ipv4"
        in manifest
    )
    assert "workspace-mail.{$workspace.imports.$core_local_domain:name}" in manifest


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
    assert (
        "ssl_server_cert_file = /etc/workspace/tls/workspace-mail.pem" in dovecot
    )
    assert (
        "ssl_server_key_file = /etc/workspace/tls/workspace-mail.pem" in dovecot
    )
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
    assert 'server_advertise_condition = ${if eq{$tls_in_cipher}{}{}{*}}' in (
        exim_auth
    )
    assert "MAIN_TLS_ENABLE = true" in exim_tls
    assert (
        "MAIN_TLS_CERTIFICATE = /etc/workspace/tls/workspace-mail.pem" in exim_tls
    )
    assert (
        "MAIN_TLS_PRIVATEKEY = /etc/workspace/tls/workspace-mail.pem" in exim_tls
    )
    assert "workspace-messenger-tls.conf" in install_script
    assert "/etc/exim4/conf.d/main/01_workspace_messenger" in install_script
    assert "/etc/exim4/workspace-smtp.passwd" in exim_auth
    assert "$authenticated_id" in exim_router
    assert "domains = messenger.workspace.invalid" in exim_router
    assert (
        "local_parts = nwildlsearch,ret=key;"
        "/etc/exim4/workspace-messenger-local-parts"
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
    assert 'if [[ -z "$PERSISTENT_DISK" ]]' in bootstrap
    assert "workspace-mail-pki" in bootstrap
    assert "TLS_STORE_RELATIVE=workspace-mail-pki" in bootstrap
    assert "$PERSISTENT_MOUNT/$TLS_STORE_RELATIVE" in bootstrap
    deferred_exit = bootstrap.index("exit 0")
    mount_disk = bootstrap.index('mountpoint -q "$PERSISTENT_MOUNT"')
    create_tls = bootstrap.index("/usr/local/bin/workspace-mail-pki")
    configure = bootstrap.index("workspace-mail-configure")
    healthcheck = bootstrap.index("workspace-mail-healthcheck")
    assert deferred_exit < mount_disk < create_tls < configure < healthcheck
    assert "sleep 1" not in bootstrap


def test_image_installs_universal_agent_secret_umask_before_config_delivery():
    backend_install = _read("exordos/images/backend-install.sh")
    mail_install = _read("exordos/images/mail-install.sh")
    umask_install = _read("exordos/images/install-universal-agent-umask.sh")

    for install in (backend_install, mail_install):
        source_umask = install.index("install-universal-agent-umask.sh")
        install_packages = install.index("sudo apt update")
        assert source_umask < install_packages

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
    runtime_indexes = configure_script.index(
        "/run/workspace/dovecot-indexes"
    )
    assert runtime_parent < runtime_indexes
    assert "/run/workspace/dovecot-indexes" in configure_script
    assert "-m 0750 -o workspace -g workspace" in configure_script
    assert "mail_index_path = /run/workspace/dovecot-indexes/" in dovecot
    assert "mail_index_path = /var/lib/workspace" not in dovecot
    assert "doveconf -n | /usr/local/bin/workspace-dovecot-validate" in (
        configure_script
    )
    assert (
        "workspace-dovecot-validate.py" in install_script
    )


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
            raise AssertionError(
                "readiness must not require an existing mailbox"
            )

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


def test_effective_dovecot_config_rejects_index_path_overrides():
    validator = PROJECT_ROOT / "exordos/images/workspace-dovecot-validate.py"
    runtime_path = (
        "/run/workspace/dovecot-indexes/%{user | domain}/%{user | username}"
    )

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

    assert 'cfg.StrOpt("smtp-ca-file", default=None)' in runtime
    assert 'cfg.StrOpt("imap-ca-file", default=None)' in runtime

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
    assert "bootstrap_secret" not in pki.split("write_realm_metadata", 1)[1].split(
        "validate_realm_metadata",
        1,
    )[0]
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
    assert (
        (store / "v1/current/workspace-mail.pem").stat().st_mode & 0o777
        == 0o640
    )

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
    assert "npm ci --include=dev" in install_script
    assert "VITE_MESSENGER_ONLY=true npm run build --workspace=web" in install_script
    assert "packages/web/dist/index.html" in install_script
    assert "root /opt/workspace-ui/packages/web/dist;" in manifest
    assert "try_files $uri $uri/ /index.html;" in manifest
    assert "127.0.0.1:5173" not in manifest


def test_element_workflow_checks_out_compatible_ui_and_publishes():
    workflow = _read(".github/workflows/exordos-element.yml")

    assert (
        "WORKSPACE_UI_REF: c421c408d1f97dea2852fe15f25927c79cc07ce6"
        in workflow
    )
    assert 'ui_dir="${GITHUB_WORKSPACE}/../workspace_ui"' in workflow
    assert 'fetch --depth=1 origin "${WORKSPACE_UI_REF}"' in workflow
    assert (
        "https://github.com/exordos/exordos/releases/download/"
        "3.0.2/exordos-linux"
        in workflow
    )
    assert (
        "469007b01253f69b5fcf540b8f6605a360c2539019a5b148fbabb0353bee6a5b"
        in workflow
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
    assert "\n        if:" not in publish_step.split(
        "\n      - name:", maxsplit=1
    )[0]
