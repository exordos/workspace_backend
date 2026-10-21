# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import hashlib
import json
import pathlib

import jinja2
import pytest
import yaml

ROOT = pathlib.Path(__file__).parents[3]


def test_native_handoff_pins_its_immutable_artifacts_and_keeps_secrets_in_config():
    template = ROOT / "exordos/manifests/workspace.yaml.j2"
    manifest = yaml.safe_load(
        jinja2.Template(template.read_text()).render(
            version="2.0.0.dev1",
            repository="https://repo.example.test/elements",
            images={"workspace_backend": "https://repo.example.test/backend.zst"},
        )
    )
    resource = manifest["resources"]["$core.config.configs"]["workspace_legacy_upgrade"]
    assert resource["mode"] == "0600"
    assert resource["target"]["kind"] == "node_set"
    command = resource["on_change"]["command"]
    body = resource["body"]["content"]
    core = ROOT / "exordos/artifacts/upgrade/workspace-upgrade-core.py"
    bridge = ROOT / "exordos/artifacts/upgrade/workspace-upgrade-bridge.py"
    assert hashlib.sha256(core.read_bytes()).hexdigest() in command
    assert hashlib.sha256(bridge.read_bytes()).hexdigest() in body
    assert "/workspace/2.0.0.dev1/artifacts/workspace-upgrade-core.py" in command
    assert "/workspace/2.0.0.dev1/artifacts/workspace-upgrade-bridge.py" in body
    assert "encryption_key" not in command
    assert "connection_url" not in command
    assert "$core.secret.passwords.$workspace_legacy_upgrade_key:value" in body
    assert "workspace_zulip_bridge" not in manifest["requirements"]


def _core_ui_fixture(monkeypatch, tmp_path, current_version, candidates=True):
    import configparser
    import importlib.util
    import sys
    import types
    from unittest import mock

    path = ROOT / "exordos/artifacts/upgrade/workspace-upgrade-core.py"
    spec = importlib.util.spec_from_file_location("workspace_core_upgrade_test", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    current = mock.Mock(version=current_version)
    target = mock.Mock(uuid="00000000-0000-4000-8000-000000000092")
    target.repository.priority = 1024
    objects = mock.Mock()
    objects.get_all.side_effect = [
        [current] if current_version is not None else [],
        [target] if candidates else [],
    ]
    models = types.ModuleType("exordos_core.repo.dm.models")
    models.RepoElement = types.SimpleNamespace(objects=objects)
    for name in ("exordos_core", "exordos_core.repo", "exordos_core.repo.dm"):
        module = types.ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["exordos_core.repo.dm"].models = models
    monkeypatch.setitem(sys.modules, "exordos_core.repo.dm.models", models)
    factory = mock.Mock()
    monkeypatch.setattr(helper.engines, "engine_factory", factory)
    # The native RepoElement model owns its transaction through this context.
    from restalchemy.common import contexts

    monkeypatch.setattr(contexts, "Context", mock.MagicMock())
    core = tmp_path / "core.conf"
    core.write_text("[db]\nconnection_url=postgresql://test.invalid/core\n")
    settings = tmp_path / "upgrade.ini"
    settings.write_text("[workspace_upgrade]\nconnection_url=unused\n")
    original_read = configparser.ConfigParser.read

    def read_config(parser, filename, **kwargs):
        if filename == "/etc/exordos_core/exordos_core.conf":
            filename = core
        return original_read(parser, filename, **kwargs)

    monkeypatch.setattr(configparser.ConfigParser, "read", read_config)
    monkeypatch.setattr(sys, "argv", [str(path), "--config-file", str(settings)])
    monkeypatch.setattr(helper, "prepare", mock.Mock(return_value=0))
    return helper, current, target, objects


@pytest.mark.parametrize("current_version", ["0.9.1", "0.9.2-dev+20261020.cassi"])
def test_native_upgrade_updates_incompatible_ui_even_without_legacy_credentials(
    monkeypatch, tmp_path, current_version
):
    helper, current, target, objects = _core_ui_fixture(
        monkeypatch, tmp_path, current_version
    )
    helper.main()
    current.upgrade.assert_called_once_with(str(target.uuid))
    assert objects.get_all.call_count == 2


@pytest.mark.parametrize("current_version", ["0.9.2", "0.10.0"])
def test_native_upgrade_keeps_a_compatible_ui(monkeypatch, tmp_path, current_version):
    helper, current, _target, objects = _core_ui_fixture(
        monkeypatch, tmp_path, current_version
    )
    helper.main()
    current.upgrade.assert_not_called()
    assert objects.get_all.call_count == 1


def test_native_upgrade_does_not_install_an_absent_ui(monkeypatch, tmp_path):
    helper, current, _target, objects = _core_ui_fixture(monkeypatch, tmp_path, None)
    helper.main()
    current.upgrade.assert_not_called()
    assert objects.get_all.call_count == 1


def test_missing_compatible_ui_stops_before_legacy_handoff(monkeypatch, tmp_path):
    helper, current, _target, _objects = _core_ui_fixture(
        monkeypatch, tmp_path, "0.9.1", candidates=False
    )
    with pytest.raises(RuntimeError, match="released UI 0.9.2"):
        helper.main()
    current.upgrade.assert_not_called()
    helper.prepare.assert_not_called()


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


def test_native_upgrade_keeps_imports_used_by_installed_legacy_bridge():
    manifest = _render_manifest_for_legacy_consumer()
    exported = {entry["link"] for entry in manifest["exports"].values()}
    for link in _legacy_resource_contract()["bridge_imports"].values():
        namespace, name = link.rsplit(".$", 1)
        assert name in manifest["resources"][namespace], link
        assert link in exported


def test_native_upgrade_keeps_legacy_iam_catalog_with_existing_user_bindings():
    manifest = _render_manifest_for_legacy_consumer()
    for namespace, resources in _legacy_resource_contract()["iam"].items():
        current = manifest["resources"][namespace]
        assert resources.keys() <= current.keys(), namespace
        if namespace == "$core.iam.permissionbinding":
            for name, binding in resources.items():
                assert current[name] == binding
