# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import pathlib
import shutil
import subprocess


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
    assert "prepare-workspace-ui-source.sh" in workflow
    assert "WORKSPACE_UI_REF" not in workflow


def test_element_packages_one_resolved_workspace_ui_master_source():
    build_config = (PROJECT_ROOT / "exordos/exordos.yaml").read_text()
    install = (PROJECT_ROOT / "exordos/images/backend-install.sh").read_text()

    assert "../build/workspace-ui-source" in build_config
    assert "dst: /opt/workspace-ui" in build_config
    assert "workspace-ui-beta" not in build_config
    assert "workspace-ui-extreme" not in build_config
    assert "build-workspace-ui.sh" in install


def _run_git(repository, *args):
    subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_workspace_ui_source_resolver_uses_master_head(tmp_path, monkeypatch):
    repository = tmp_path / "workspace_ui"
    repository.mkdir()
    _run_git(repository, "init", "--initial-branch=master")
    _run_git(repository, "config", "user.name", "CASSI Test")
    _run_git(repository, "config", "user.email", "cassi@example.test")

    marker = repository / "channel.txt"
    marker.write_text("release\n")
    _run_git(repository, "add", "channel.txt")
    _run_git(repository, "commit", "-m", "release")
    _run_git(repository, "tag", "0.1.0")
    marker.write_text("master\n")
    _run_git(repository, "commit", "-am", "master head")
    master_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    output_dir = tmp_path / "workspace-ui-source"
    minimum_ref = tmp_path / "workspace-ui.minimum-ref"
    minimum_ref.write_text(f"{master_sha}\n")
    monkeypatch.setenv("WORKSPACE_UI_REPOSITORY", str(repository))
    monkeypatch.setenv("WORKSPACE_UI_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKSPACE_UI_MINIMUM_REF_FILE", str(minimum_ref))
    subprocess.run(
        [str(PROJECT_ROOT / "exordos/ci/prepare-workspace-ui-source.sh")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert (output_dir / "channel.txt").read_text() == "master\n"
    assert (output_dir / ".workspace-ui-ref").read_text() == (
        f"ref=master\ncommit={master_sha}\n"
    )
    assert (output_dir / "resolved-ref.env").read_text() == (
        f"WORKSPACE_UI_REF=master\nWORKSPACE_UI_SHA={master_sha}\n"
    )


def test_workspace_ui_source_resolver_rejects_missing_minimum_commit(
    tmp_path, monkeypatch
):
    repository = tmp_path / "workspace_ui"
    repository.mkdir()
    _run_git(repository, "init", "--initial-branch=master")
    _run_git(repository, "config", "user.name", "CASSI Test")
    _run_git(repository, "config", "user.email", "cassi@example.test")
    (repository / "channel.txt").write_text("master\n")
    _run_git(repository, "add", "channel.txt")
    _run_git(repository, "commit", "-m", "master head")

    output_dir = tmp_path / "workspace-ui-source"
    minimum_ref = tmp_path / "workspace-ui.minimum-ref"
    missing_sha = "0" * 40
    minimum_ref.write_text(f"{missing_sha}\n")
    monkeypatch.setenv("WORKSPACE_UI_REPOSITORY", str(repository))
    monkeypatch.setenv("WORKSPACE_UI_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKSPACE_UI_MINIMUM_REF_FILE", str(minimum_ref))

    result = subprocess.run(
        [str(PROJECT_ROOT / "exordos/ci/prepare-workspace-ui-source.sh")],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not contain required commit" in result.stderr
    assert not output_dir.exists()


def test_workspace_ui_source_resolver_rejects_missing_minimum_ref_file(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "workspace-ui-source"
    minimum_ref = tmp_path / "missing.minimum-ref"
    monkeypatch.setenv("WORKSPACE_UI_REPOSITORY", str(tmp_path / "not-cloned"))
    monkeypatch.setenv("WORKSPACE_UI_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKSPACE_UI_MINIMUM_REF_FILE", str(minimum_ref))

    result = subprocess.run(
        [str(PROJECT_ROOT / "exordos/ci/prepare-workspace-ui-source.sh")],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stderr == f"Minimum UI reference file not found at {minimum_ref}\n"
    assert not output_dir.exists()


def test_workspace_ui_source_resolver_rejects_empty_minimum_ref_file(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "workspace-ui-source"
    minimum_ref = tmp_path / "workspace-ui.minimum-ref"
    minimum_ref.write_text(" \n\t")
    monkeypatch.setenv("WORKSPACE_UI_REPOSITORY", str(tmp_path / "not-cloned"))
    monkeypatch.setenv("WORKSPACE_UI_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKSPACE_UI_MINIMUM_REF_FILE", str(minimum_ref))

    result = subprocess.run(
        [str(PROJECT_ROOT / "exordos/ci/prepare-workspace-ui-source.sh")],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stderr == f"Minimum UI commit SHA in {minimum_ref} is empty\n"
    assert not output_dir.exists()


def test_workspace_ui_bundle_builder_uses_root_base_path(tmp_path, monkeypatch):
    ui_path = tmp_path / "workspace-ui"
    (ui_path / "packages/web").mkdir(parents=True)
    (ui_path / ".nvmrc").write_text("22\n")
    (ui_path / ".workspace-ui-ref").write_text(
        "ref=master\ncommit=0123456789abcdef\n"
    )

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    node_path = shutil.which("node")
    assert node_path is not None
    (mock_bin / "node").symlink_to(node_path)
    npm_log = tmp_path / "npm.log"
    npm = mock_bin / "npm"
    npm.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s|%s|%s\\n' "$PWD" "${VITE_PUBLIC_BASE_PATH:-}" "$*" >> "$NPM_LOG"
if [[ "$*" == "run build --workspace=web" ]]; then
    mkdir -p packages/web/dist/assets
    printf '<script src="/assets/app.js"></script>\\n' \
        > packages/web/dist/index.html
    printf '{"scope":"/","start_url":"/"}\\n' \
        > packages/web/dist/manifest.webmanifest
fi
"""
    )
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{mock_bin}:{pathlib.Path('/usr/bin')}")
    monkeypatch.setenv("NPM_LOG", str(npm_log))
    monkeypatch.setenv("WORKSPACE_UI_PATH", str(ui_path))

    subprocess.run(
        [str(PROJECT_ROOT / "exordos/images/build-workspace-ui.sh")],
        check=True,
        capture_output=True,
        text=True,
    )

    build_lines = [
        line for line in npm_log.read_text().splitlines() if "run build" in line
    ]
    assert len(build_lines) == 1
    assert build_lines[0].split("|")[1] == ""
    assert (ui_path / "packages/web/dist/build-ref.txt").read_text() == (
        "ref=master\ncommit=0123456789abcdef\n"
    )


def test_repository_has_no_python_package_publication_workflow():
    workflows = PROJECT_ROOT / ".github/workflows"

    assert not (workflows / "publish-to-pypi.yml").exists()
    assert not any("pypi" in path.name.lower() for path in workflows.iterdir())
