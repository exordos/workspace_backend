# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import pathlib


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
    assert "label: external-bridge-control" in manifest
    assert "[messenger_files_s3]" in manifest
    assert manifest.count("command: /usr/local/bin/workspace-bootstrap") >= 5


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


def test_messenger_runtime_has_two_workers_with_bounded_pool_budget():
    for config_path in (
        "etc/workspace/workspace.conf",
        "exordos/manifests/workspace.yaml.j2",
    ):
        config = _read(config_path)
        messenger = config.split("[messenger_api]", 1)[1].split("[", 1)[0]
        assert "workers = 2" in messenger
        assert "connection_pool_max_size = 2" in config

    messenger_source = _read("workspace/cmd/messenger_api.py")
    provider_source = _read("workspace/cmd/external_bridge_api.py")
    assert '"workspace-messenger-api"' in messenger_source
    assert '"workspace-provider-control"' in provider_source


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
        assert "user_list_limit = 4" in config
        assert "user_list_max_entries_per_message" not in config


def test_compact_read_state_rollout_is_opt_in():
    for config_path in (
        "etc/workspace/workspace.conf",
        "exordos/manifests/workspace.yaml.j2",
    ):
        config = _read(config_path)
        assert "read_state_compaction_enabled = false" in config
        assert "read_state_cleanup_enabled = false" in config
        assert "only after every Workspace API and worker" in config


def test_manifest_provisions_unassigned_external_integration_roles():
    manifest = _read("exordos/manifests/workspace.yaml.j2")
    account_permissions = {
        "workspace.external_account.read",
        "workspace.external_account.create",
        "workspace.external_account.update",
        "workspace.external_account.reconnect",
        "workspace.external_account.disconnect",
        "workspace.external_account.delete",
    }
    admin_permissions = {
        "workspace.external_provider_policy.read",
        "workspace.external_provider_policy.update",
        "workspace.external_provider_policy.suspend",
        "workspace.external_provider_policy.resume",
        "workspace.external_provider_health.read",
        "workspace.external_bridge_instance.read",
        "workspace.external_bridge_instance.suspend",
        "workspace.external_bridge_instance.resume",
        "workspace.external_bridge_instance.revoke",
    }

    for permission in account_permissions | admin_permissions:
        assert f'name: "{permission}"' in manifest
    assert 'name: "workspace-external-integration"' in manifest
    assert 'name: "workspace-external-integration-admin"' in manifest
    assert "  $core.iam.rolebinding:" not in manifest
    assert "  $core.iam.role_bindings:" not in manifest
    assert "  $core.iam.permission_bindings:" not in manifest

    bindings = manifest.split("  $core.iam.permissionbinding:\n", 1)[1].split(
        "\n  $core.compute.nodes:", 1
    )[0]
    assert bindings.count(
        "role: $core.iam.roles.$workspace_external_integration:uuid"
    ) == len(account_permissions)
    assert bindings.count(
        "role: $core.iam.roles.$workspace_external_integration_admin:uuid"
    ) == len(admin_permissions)


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


def test_manifest_proxies_core_api_over_https():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert "proxy_pass https://workspace_core_api/api/core/;" in manifest
    assert "proxy_pass http://workspace_core_api/api/core/;" not in manifest
    assert 'f"server {$workspace.imports.$var_core_ip_address:value}:443;"' in manifest
    assert (
        'f"server {$workspace.imports.$var_core_ip_address:value}:80;"' not in manifest
    )


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
