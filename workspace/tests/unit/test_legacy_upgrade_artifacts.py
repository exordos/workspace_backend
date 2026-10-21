# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import json
import pathlib

import jinja2
import yaml

ROOT = pathlib.Path(__file__).parents[3]


def _render_manifest_for_legacy_consumer():
    return yaml.safe_load(
        jinja2.Template(
            (ROOT / "exordos/manifests/workspace.yaml.j2").read_text()
        ).render(
            version="2.0.1",
            images={
                "workspace_backend": "urn:images:00000000-0000-4000-8000-000000000001"
            },
        )
    )


def _legacy_resource_contract():
    return json.loads(
        (
            ROOT
            / "workspace/tests/fixtures/legacy_workspace_1_3_1_resource_contract.json"
        ).read_text()
    )


def test_native_upgrade_keeps_legacy_iam_catalog_with_existing_user_bindings():
    manifest = _render_manifest_for_legacy_consumer()
    for namespace, resources in _legacy_resource_contract()["iam"].items():
        current = manifest["resources"][namespace]
        assert resources.keys() <= current.keys(), namespace
        for name, resource in resources.items():
            if namespace == "$core.iam.permissionbinding":
                assert current[name] == resource
            else:
                assert current[name]["name"] == resource["name"]
