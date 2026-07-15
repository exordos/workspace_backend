# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import configparser
import pathlib
import re

import pytest
import yaml


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
PROVIDER_KINDS = ("zulip", "mail", "calendar")


def _read(relative_path):
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


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


def _provider_manifest(kind):
    expected_name = f"workspace-{kind}-provider"
    path = PROJECT_ROOT / f"exordos/manifests/{expected_name}.yaml.j2"
    assert path.is_file(), f"missing dedicated manifest for {expected_name}"
    content = path.read_text(encoding="utf-8")
    assert re.search(
        rf'(?m)^name:\s*["\']{re.escape(expected_name)}["\']\s*$',
        content,
    )
    return path, content


def _nginx_server_blocks(manifest):
    lines = manifest.splitlines()
    blocks = []
    for index, line in enumerate(lines):
        if line.strip() != "server {":
            continue
        depth = 0
        block = []
        for nested_line in lines[index:]:
            block.append(nested_line)
            depth += nested_line.count("{") - nested_line.count("}")
            if depth == 0:
                blocks.append("\n".join(block))
                break
    return blocks


def _provider_env_assignments(manifest):
    return dict(
        re.findall(
            r"(?m)^\s{10}(WORKSPACE_[A-Z_]+|DATABASE_URL)=(.+)$",
            manifest,
        )
    )


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


def test_backend_exports_node_for_provider_elements():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert re.search(
        r"(?ms)^exports:\s*$.*?^\s{2}backend_node:\s*$"
        r'.*?^\s{4}link:\s*["\']?\$core\.compute\.nodes\.\$workspace_backend["\']?\s*$',
        manifest,
    )


def test_backend_manifest_references_build_image_by_name():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert re.search(
        r'(?m)^\s+image:\s+["\']workspace-backend["\']\s*$',
        manifest,
    )
    assert "{{ repository" not in manifest
    assert ".raw.zst" not in manifest


def test_provider_service_api_uses_a_separate_platform_internal_listener():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    server_blocks = _nginx_server_blocks(manifest)
    browser_servers = [block for block in server_blocks if "listen 80;" in block]
    provider_servers = [block for block in server_blocks if "listen 21085;" in block]

    assert len(browser_servers) == 1
    assert "/api/workspace-service/" not in browser_servers[0]
    assert len(provider_servers) == 1
    assert "location /api/workspace-service/v1/ {" in provider_servers[0]
    assert "proxy_pass http://127.0.0.1:21083/v1/;" in provider_servers[0]
    assert sum("/api/workspace-service/" in block for block in server_blocks) == 1


def test_backend_runs_provider_service_api_but_not_provider_daemons():
    pyproject = _read("pyproject.toml")
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    install_script = _read("exordos/images/backend-install.sh")
    restart_script = _read("exordos/images/workspace-restart-services.sh")

    assert (
        'workspace-provider-api = "workspace.cmd.workspace_provider_api:main"'
        in pyproject
    )
    assert _configured_port("workspace_provider_api") == 21083
    assert _manifest_port(manifest, "workspace_provider_api") == 21083
    assert "workspace-provider-api" in install_script
    assert "workspace-provider-api" in restart_script
    assert "name: workspace-provider-api" in manifest
    assert (
        "ExecStart=workspace-provider-api --config-file /etc/workspace/workspace.conf"
        in _read("etc/systemd/workspace-provider-api.service")
    )


def test_workspace_backend_runtime_does_not_start_provider_daemons():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    install_script = _read("exordos/images/backend-install.sh")
    restart_script = _read("exordos/images/workspace-restart-services.sh")

    for kind in PROVIDER_KINDS:
        daemon = f"workspace-{kind}-provider"
        assert daemon not in manifest
        assert daemon not in install_script
        assert daemon not in restart_script
        assert not (PROJECT_ROOT / f"etc/systemd/{daemon}.service").exists()


@pytest.mark.parametrize("kind", PROVIDER_KINDS)
def test_provider_element_has_isolated_node_image_database_and_daemon(kind):
    path, manifest = _provider_manifest(kind)
    element_name = f"workspace-{kind}-provider"

    assert "$core.compute.nodes:" in manifest
    assert re.search(
        rf'(?m)^\s+image:\s+["\']{re.escape(element_name)}["\']\s*$',
        manifest,
    )
    assert "{{ repository" not in manifest
    assert ".raw.zst" not in manifest

    assert "$core.secret.passwords:" in manifest
    assert "$dbaas.types.postgres.instances:" in manifest
    assert re.search(
        r"(?m)^\s*\$dbaas\.types\.postgres\.instances\.\$[^.\s:]+\.users:\s*$",
        manifest,
    )
    assert re.search(
        r"(?m)^\s*\$dbaas\.types\.postgres\.instances\.\$[^.\s:]+\.databases:\s*$",
        manifest,
    )
    assert "postgresql://" in manifest
    assert "WORKSPACE_BACKEND_URL='http://" in manifest
    assert ":21085'" in manifest

    assert re.search(r"(?m)^\s{2}workspace:\s*$", manifest)
    assert 'element: "$workspace"' in manifest
    assert 'link: "$workspace.backend_node"' in manifest
    assert "join(ipsv4)" in manifest

    daemon_paths = re.findall(
        r"(?m)^\s+path:\s+[^\n]*"
        r"(workspace-(?:zulip|mail|calendar)-provider)[^\n]*$",
        manifest,
    )
    assert daemon_paths == [element_name]

    build_config = _read("exordos/exordos.yaml")
    relative_manifest = path.relative_to(PROJECT_ROOT / "exordos")
    assert f"manifest: {relative_manifest}" in build_config
    assert f"name: {element_name}" in build_config


def test_provider_manifests_do_not_reference_another_provider_runtime():
    manifests = {}
    provider_uuids = set()

    for kind in PROVIDER_KINDS:
        path, manifest = _provider_manifest(kind)
        namespace = f"workspace_{kind}_provider"
        manifests[kind] = path

        assert path.name == f"workspace-{kind}-provider.yaml.j2"
        assert f"{namespace}_cluster_pg" in manifest
        assert f"{namespace}_db_user" in manifest
        assert f"{namespace}_db" in manifest
        assert f"WORKSPACE_PROVIDER_BINARY='workspace-{kind}-provider'" in manifest

        for other_kind in set(PROVIDER_KINDS) - {kind}:
            assert f"workspace_{other_kind}_provider" not in manifest
            assert f"workspace-{other_kind}-provider" not in manifest

        provider_uuid = re.search(
            r"WORKSPACE_PROVIDER_UUID=\{\{\s*shell_quote\(provider_uuid\s*"
            r"\|\s*default\('(?P<uuid>[^']+)'\)\)\s*\}\}",
            manifest,
        )
        assert provider_uuid is not None
        provider_uuids.add(provider_uuid.group("uuid"))

    assert len(set(manifests.values())) == len(PROVIDER_KINDS)
    assert len(provider_uuids) == len(PROVIDER_KINDS)


def test_provider_builds_are_one_manifest_and_one_image_per_element():
    build_config = _read("exordos/exordos.yaml")

    for kind in PROVIDER_KINDS:
        manifest = f"manifests/workspace-{kind}-provider.yaml.j2"
        image = f"workspace-{kind}-provider"

        assert build_config.count(f"manifest: {manifest}") == 1
        assert build_config.count(f"name: {image}") == 1


@pytest.mark.parametrize("kind", PROVIDER_KINDS)
def test_provider_manifest_is_parseable_before_jinja_render(kind):
    _, manifest = _provider_manifest(kind)

    assert yaml.safe_load(manifest)["name"] == f"workspace-{kind}-provider"


def test_provider_entry_points_are_distinct_daemons():
    pyproject = _read("pyproject.toml")

    for kind in PROVIDER_KINDS:
        assert (
            f'workspace-{kind}-provider = "workspace_providers.{kind}.main:main"'
        ) in pyproject


@pytest.mark.parametrize("kind", PROVIDER_KINDS)
def test_provider_environment_is_posix_shell_quoted(kind):
    _, manifest = _provider_manifest(kind)
    macro = re.search(
        r"(?s)\{%\s*macro shell_quote\(value\).*?"
        r"\{%[-]?\s*endmacro\s*%\}",
        manifest,
    )
    assignments = _provider_env_assignments(manifest)

    assert macro is not None
    assert r"""| replace("'", "'\"'\"'")""" in macro.group()

    assert set(assignments) == {
        "WORKSPACE_PROVIDER_BINARY",
        "WORKSPACE_PROVIDER_UUID",
        "WORKSPACE_PROVIDER_NAME",
        "WORKSPACE_BACKEND_URL",
        "DATABASE_URL",
    }
    for key, value in assignments.items():
        if key in ("WORKSPACE_PROVIDER_UUID", "WORKSPACE_PROVIDER_NAME"):
            assert re.fullmatch(r"\{\{\s*shell_quote\(.+\)\s*\}\}", value)
        else:
            assert value.startswith("'") and value.endswith("'")


def test_provider_image_explicitly_installs_process_control_tools():
    install_script = _read("exordos/images/provider-install.sh")
    install_command = re.search(
        r"(?ms)^sudo apt install -y (?P<packages>.+?)(?:\n\n|$)",
        install_script,
    )

    assert install_command is not None
    assert re.search(r"(?m)(?:^|\s)procps(?:\s|$)", install_command.group("packages"))
