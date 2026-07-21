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


def test_manifest_exposes_only_api_routes_from_the_backend_node():
    manifest = _read("exordos/manifests/workspace.yaml.j2")

    assert "location /api/workspace/" in manifest
    assert "location = /api/workspace/v1/events/ws" in manifest
    assert "root /opt/workspace-ui" not in manifest
    assert "alias /opt/workspace-ui" not in manifest
    assert "location / {\n                  return 404;" in manifest


def test_backend_bootstrap_has_no_secondary_storage_gate():
    bootstrap = _read("exordos/images/backend-bootstrap.sh")
    install = _read("exordos/images/backend-install.sh")
    reload_config = _read("exordos/images/workspace-reload-config.sh")

    assert "ra-apply-migration" in bootstrap
    assert "psql" in bootstrap
    assert "writer-gate" not in bootstrap
    assert "workspace-mail" not in install
    assert "mail" not in reload_config.lower()
    assert '"$WORKSPACE_BOOTSTRAP"' in reload_config
    assert '"$RESTART_SERVICES"' in reload_config


def test_event_retention_migration_follows_canonical_schema_directly():
    migration = _read(
        "migrations/0111-index-Messenger-event-retention-cutoff-117285.py"
    )

    assert "0109-add-scalable-Messenger-visibility-views-0ae35f.py" in migration
    assert "canonical-import-ledger" not in migration
