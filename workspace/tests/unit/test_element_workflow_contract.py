# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import pathlib


PROJECT_ROOT = pathlib.Path(__file__).parents[3]


def test_element_workflow_builds_and_optionally_publishes_one_element():
    workflow = (PROJECT_ROOT / ".github/workflows/exordos-element.yml").read_text()

    assert workflow.count('"${EXORDOS_BIN}" build .') == 1
    assert workflow.count('"${EXORDOS_BIN}" push .') == 1
    assert "PUBLISH_REQUESTED" in workflow
    assert "profile:" not in workflow
    assert "production_migration" not in workflow
    assert "manifest-var" not in workflow
    assert "workspace-mail" not in workflow
    assert "prepare-workspace-ui-source.sh" not in workflow
    assert "WORKSPACE_UI_REPOSITORY" not in workflow


def test_element_packages_only_the_backend_source():
    build_config = (PROJECT_ROOT / "exordos/exordos.yaml").read_text()
    install = (PROJECT_ROOT / "exordos/images/backend-install.sh").read_text()

    assert "workspace-ui" not in build_config
    assert "workspace-ui" not in install
    assert "node-toolchain" not in install


def test_repository_has_no_python_package_publication_workflow():
    workflows = PROJECT_ROOT / ".github/workflows"

    assert not (workflows / "publish-to-pypi.yml").exists()
    assert not any("pypi" in path.name.lower() for path in workflows.iterdir())
