# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import pathlib

import jinja2
import yaml

PROJECT_ROOT = pathlib.Path(__file__).parents[3]


def _read(relative_path):
    return (PROJECT_ROOT / relative_path).read_text()


def test_element_builds_only_the_backend_image():
    build_config = _read("exordos/exordos.yaml")

    assert "name: workspace-backend" in build_config
    assert "script: images/backend-install.sh" in build_config
    assert "workspace-mail" not in build_config
    assert "mail-install.sh" not in build_config


def test_manifest_has_one_postgresql_messenger_runtime():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    forbidden = (
        "mail_migration_",
        "mail_projection",
        "messenger_storage",
        "canonical_cutover",
        "retain_legacy_mail",
        "writer_gate",
        "workspace_mail",
        "workspace-mail",
        "messenger_mail",
    )

    assert all(value not in manifest for value in forbidden)
    assert "workspace_backend_config:" in manifest
    assert "name: workspace-backend" in manifest
    assert "external-bridge-control" not in manifest
    assert "[messenger_files_s3]" in manifest
    assert manifest.count("command: /usr/local/bin/workspace-bootstrap") >= 3


def test_manifest_scales_s3_disk_size_by_core_profile():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    expected_profile_sizes = {
        "develop": 8,
        "small": 51,
        "medium": 100,
        "large": 1024,
        "legacy": 1024,
    }

    for profile, size in expected_profile_sizes.items():
        assert f'link: "$core.vs.profiles.${profile}"' in manifest
        assert (
            f"profile: $workspace.imports.$profile_{profile}:uuid\n"
            f"            value: {size}"
        ) in manifest
    assert "disk_size: $core.vs.variables.$workspace_s3_disk_size:value" in manifest


def test_manifest_scales_projection_disk_only_for_high_storage_profiles():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    variable = manifest.split("workspace_projection_disk_size:", 1)[1].split(
        "$core.iam.permissions:",
        1,
    )[0]
    expected_profile_sizes = {
        "develop": 30,
        "small": 30,
        "medium": 30,
        "large": 512,
        "legacy": 512,
    }

    for profile, size in expected_profile_sizes.items():
        assert (
            f"profile: $workspace.imports.$profile_{profile}:uuid\n"
            f"            value: {size}"
        ) in variable
    assert (
        "disk_size: $core.vs.variables.$workspace_projection_disk_size:value"
        in manifest
    )


def test_postgresql_runtime_has_bounded_connection_lifetimes():
    expected = {
        "connection_connect_timeout": 30,
        "connection_statement_timeout": 240,
        "connection_transaction_timeout": 300,
        "connection_idle_in_transaction_session_timeout": 240,
        "connection_tcp_user_timeout": 300,
        "connection_keepalives_idle": 60,
        "connection_keepalives_interval": 30,
        "connection_keepalives_count": 5,
        "connection_pool_max_size": 2,
    }

    for config_path in (
        "etc/workspace/workspace.conf",
        "exordos/manifests/workspace.yaml.j2",
    ):
        config = _read(config_path)
        for name, value in expected.items():
            assert f"{name} = {value}" in config


def test_workspace_runtime_has_sixteen_workers_with_bounded_pool_budget():
    for config_path in (
        "etc/workspace/workspace.conf",
        "exordos/manifests/workspace.yaml.j2",
    ):
        config = _read(config_path)
        workspace = config.split("[workspace_api]", 1)[1].split("[", 1)[0]
        assert "workers = 16" in workspace
        assert "connection_pool_max_size = 2" in config

    workspace_source = _read("workspace/cmd/workspace_api.py")
    assert '"workspace-api"' in workspace_source


def test_postgresql_runtime_has_import_scale_session_tuning():
    for config_path in (
        "etc/workspace/workspace.conf",
        "exordos/manifests/workspace.yaml.j2",
    ):
        config = _read(config_path)
        assert "?options=-c%20work_mem%3D32MB%20-c%20jit%3Doff" in config


def test_reaction_user_lists_have_bounded_runtime_defaults():
    for config_path in (
        "etc/workspace/workspace.conf",
        "exordos/manifests/workspace.yaml.j2",
    ):
        config = _read(config_path)
        assert "[messenger_reactions]" in config
        assert "user_list_limit = 5" in config
        assert "user_list_max_entries_per_message" not in config


def test_retired_compact_read_state_is_not_configured():
    for config_path in (
        "etc/workspace/workspace.conf",
        "exordos/manifests/workspace.yaml.j2",
    ):
        config = _read(config_path)
        assert "read_state_compaction_enabled" not in config
        assert "read_state_cleanup_enabled" not in config


def test_messenger_v2_projection_worker_is_explicitly_enabled():
    for config_path in (
        "etc/workspace/workspace.conf",
        "exordos/manifests/workspace.yaml.j2",
    ):
        config = _read(config_path)
        assert "v2_projection_enabled = true" in config
        assert "v2_projection_workers = 4" in config
        assert "v2_projection_idle_sleep_seconds = 0.5" in config
        assert "v2_metrics_log_interval_seconds = 30" in config


def test_manifest_provisions_generic_permissions_without_assigning_user_roles():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    for action in ("read", "create", "update", "delete"):
        assert f'name: "workspace.external_account.{action}"' in manifest
    assert "  $core.iam.rolebinding:" not in manifest


def test_manifest_provisions_topic_summary_admin_and_encryption_secret():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert 'name: "workspace.topic_summary_endpoint.manage"' in manifest
    assert 'name: "workspace.topic_summary_settings.manage"' in manifest
    assert 'name: "workspace-topic-summary-admin"' in manifest
    bindings = manifest.split("  $core.iam.permissionbinding:\n", 1)[1].split(
        "\n  $core.compute.nodes:", 1
    )[0]
    assert (
        bindings.count("role: $core.iam.roles.$workspace_topic_summary_admin:uuid") == 2
    )
    assert "workspace_topic_summary_secret_key:" in manifest
    assert (
        "secret_encryption_key = "
        "{$core.secret.passwords.$workspace_topic_summary_secret_key:value}"
    ) in manifest
    assert "[topic_summary]" in manifest
    assert "connect_timeout_seconds = 30" in manifest
    assert "request_timeout_seconds = 1500" in manifest
    assert "topic_claim_seconds = 5400" in manifest
    assert "endpoint_claim_seconds = 1800" in manifest


def test_manifest_provisions_unassigned_sticker_catalog_admin():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert 'name: "workspace.sticker_catalog.manage"' in manifest
    assert 'name: "workspace-sticker-catalog-admin"' in manifest
    assert "  $core.iam.rolebinding:" not in manifest
    assert "  $core.iam.role_bindings:" not in manifest
    assert "  $core.iam.permission_bindings:" not in manifest
    bindings = manifest.split("  $core.iam.permissionbinding:\n", 1)[1].split(
        "\n  $core.compute.nodes:", 1
    )[0]
    assert (
        bindings.count("role: $core.iam.roles.$workspace_sticker_catalog_admin:uuid")
        == 1
    )
    assert (
        "permission: $core.iam.permissions.$workspace_sticker_catalog_manage:uuid"
        in bindings
    )


def test_manifest_bounds_websocket_heartbeat_timeout():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    events_config = manifest.split("[messenger_events]", 1)[1].split(
        "[messenger_worker_agent]",
        1,
    )[0]

    assert "heartbeat_interval = 25" in events_config
    assert "heartbeat_timeout = 60" in events_config


def test_manifest_exposes_only_api_routes_from_the_backend_node():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert "location /api/workspace/" in manifest
    assert "location = /api/workspace/v1/events/ws" in manifest
    assert "root /opt/workspace-ui" not in manifest
    assert "alias /opt/workspace-ui" not in manifest
    assert "location / {\n                  return 404;" in manifest


def test_core_api_proxy_requires_and_verifies_the_core_https_listener():
    manifest = yaml.safe_load(
        jinja2.Template(_read("exordos/manifests/workspace.yaml.j2")).render(
            version="test", images={"workspace_backend": "fixture-image"}
        )
    )
    assert manifest["requirements"]["core_api_https"] == {
        "from_version": "0.4.9-dev+20261118142019"
    }
    resources = manifest["resources"]
    configs = resources["$core.config.configs"]
    nginx = configs["workspace_backend_nginx"]["body"]["content"]
    core_location = nginx.split("location /api/core/ {", 1)[1].split(
        "location = /api/workspace", 1
    )[0]
    core_name = "core.{$workspace.imports.$core_local_domain:name}"
    assert f"proxy_pass https://{core_name}:443/api/core/;" in core_location
    assert "proxy_ssl_server_name on;" in core_location
    assert f"proxy_ssl_name {core_name};" in core_location
    assert f"proxy_set_header Host {core_name};" in core_location
    assert "proxy_ssl_verify on;" in core_location
    assert "proxy_set_header X-Forwarded-Proto https;" in core_location

    trust = configs["workspace_backend_nginx_core_trust"]
    assert f"proxy_ssl_trusted_certificate {trust['path']};" in core_location
    assert trust["body"]["content"] == "$workspace.imports.$core_api_trust_pem:value"
    assert manifest["imports"]["core_api_trust_pem"] == {
        "element": "$core_api_https",
        "kind": "resource",
        "link": "$core.vs.variables.$core_api_trust_pem",
    }
    assert trust["target"] == configs["workspace_backend_nginx"]["target"]
    assert trust["on_change"] == configs["workspace_backend_nginx"]["on_change"]
    assert "workspace_core_api_transport" not in resources["$core.vs.variables"]

    # Updating the main config changes the scheme and destination together.
    assert "workspace_backend_nginx_core_upstream" not in configs
    assert "var_core_ip_address" not in manifest["imports"]


def test_backend_iam_uses_the_same_verified_core_proxy():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    assert "iam_endpoint = http://127.0.0.1/api/core/v1/iam/clients/default" in manifest


def test_mobile_push_dependencies_use_core_versions_instead_of_pep440():
    import re

    for mode in ("direct", "mesh"):
        manifest = _read(f"exordos/manifests/workspace_{mode}_firebase_push.yaml.j2")
        versions = re.findall(r'from_version:\s*"([^"]+)"', manifest)
        assert versions
        for version in versions:
            assert re.fullmatch(r"\d+\.\d+\.\d+(?:-.+)?", version)


def test_backend_bootstrap_has_no_secondary_storage_gate():
    bootstrap = _read("exordos/images/backend-bootstrap.sh")
    install = _read("exordos/images/backend-install.sh")
    reload_config = _read("exordos/images/workspace-reload-config.sh")

    assert "ra-apply-migration" in bootstrap
    assert "psql" in bootstrap
    assert "writer-gate" not in bootstrap
    assert "workspace-mail" not in install
    assert "mail" not in reload_config.lower()
    assert "ConfigParser(interpolation=None)" in bootstrap
    assert '"$WORKSPACE_BOOTSTRAP"' in reload_config
    assert '"$RESTART_SERVICES"' in reload_config


def test_event_retention_migration_follows_canonical_schema_directly():
    migration = _read(
        "migrations/0111-index-Messenger-event-retention-cutoff-117285.py"
    )

    assert "0109-add-scalable-Messenger-visibility-views-0ae35f.py" in migration
    assert "canonical-import-ledger" not in migration


def test_only_workspace_rest_service_is_deployed():
    for relative_path in (
        "pyproject.toml",
        "etc/workspace/workspace.conf",
        "exordos/manifests/workspace.yaml.j2",
        "exordos/images/backend-install.sh",
        "exordos/images/workspace-restart-services.sh",
    ):
        source = _read(relative_path)
        assert "workspace-messenger-api" not in source
        assert "[messenger_api]" not in source
        assert "21081" not in source

    manifest = _read("exordos/manifests/workspace.yaml.j2")
    assert "location /api/workspace/v1/messenger/" not in manifest
    assert "proxy_pass http://127.0.0.1:21084/;" in manifest
    assert "proxy_pass http://127.0.0.1:21082/v1/events/ws;" in manifest
