# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
import os
import pathlib
import re
import subprocess

import yaml


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
RUNBOOK = PROJECT_ROOT / "docs/postgresql_canonical_production_cutover.md"
MIGRATION_RUNBOOK = PROJECT_ROOT / "docs/postgresql_canonical_messenger_migration.md"
WORKFLOW_RUNBOOK = PROJECT_ROOT / "docs/production_migration_workflow.md"
MANIFEST = PROJECT_ROOT / "exordos/manifests/workspace.yaml.j2"


def _read(path):
    return path.read_text(encoding="utf-8")


def _shell_blocks(body):
    return "\n".join(re.findall(r"```shell\n(.*?)```", body, flags=re.DOTALL))


def test_production_cutover_runbook_orders_all_required_phases():
    body = _read(RUNBOOK)
    headings = (
        "## 1. Deploy the compatibility mail root with the current backend",
        "## 2. Deploy the migration stage in `mail_projection`",
        "## 3. Deploy the exact Provider-only bridge",
        "## 4. Inventory, stage, and apply while mail remains authoritative",
        "## 5. Close writers, freeze, apply the final delta, and prove parity",
        "## 6. Deploy the exact published canonical version",
        "## 7. Accept the cutover and cross the first-write boundary deliberately",
        "## 8. Retire legacy mail resources in a later deployment",
    )

    positions = [body.index(heading) for heading in headings]
    assert positions == sorted(positions)


def test_production_cutover_runbook_pins_images_and_retains_mail_at_cutover():
    body = _read(RUNBOOK)
    template = _read(MANIFEST)

    for variable in (
        "workspace_backend_image",
        "workspace_mail_image",
        "messenger_storage_mode",
        "messenger_canonical_cutover_confirmed",
        "retain_legacy_mail_resources",
    ):
        assert variable in template

    canonical_section = body.split(
        "## 6. Deploy the exact published canonical version", 1
    )[1].split("## 7.", 1)[0]
    assert "CANONICAL_MANIFEST" in canonical_section
    assert "STAGE_BACKEND_IMAGE_URN" in canonical_section
    assert "COMPATIBILITY_MAIL_IMAGE_URN" in canonical_section
    assert "CANONICAL_VERSION" in canonical_section
    assert "canonical mode and confirmation are both explicit" in canonical_section
    assert "existing mail node/data disk" in canonical_section
    assert (
        'update_element_exact "$WORKSPACE_REPOSITORY_UUID" workspace'
        in canonical_section
    )
    assert "exordos build" not in canonical_section


def test_production_cutover_runbook_requires_four_ordered_immutable_versions():
    body = _read(RUNBOOK)

    for variable in (
        "COMPATIBILITY_VERSION",
        "STAGE_VERSION",
        "CANONICAL_VERSION",
        "ROLLBACK_VERSION",
    ):
        assert variable in body

    assert "four immutable, uniquely versioned artifacts" in body
    assert (
        "COMPATIBILITY_VERSION < STAGE_VERSION < CANONICAL_VERSION < "
        "ROLLBACK_VERSION" in body
    )
    assert "distinct build metadata alone is not ordering evidence" in body.replace(
        "\n", " "
    )
    assert "migration versions do not use consecutive patch bases" in body
    assert "migration version cores are not strictly ordered" in body


def test_production_cutover_runbook_uses_repository_exact_version_updates():
    body = _read(RUNBOOK)
    shell = _shell_blocks(body)

    assert "WORKSPACE_REPOSITORY_UUID" in shell
    assert "BRIDGE_REPOSITORY_UUID" in shell
    assert 'exordos repo refresh "$repository_uuid"' in shell
    assert "--fields uuid --fields name --fields version" in shell
    assert "--fields repository --fields status" in shell
    assert "[.[] | select(.name == $name and .version == $version)]" in shell
    assert "if ((count > 1)); then" in shell
    assert "if ((count == 1)); then" in shell
    assert '[[ "$actual_repository" != "$repository_uuid" ]]' in shell
    assert '[[ "$status" == "AVAILABLE" ]]' in shell
    assert '"${report_prefix}-repository-available.json"' in shell
    assert (
        'wait_repo_version_available "$repository_uuid" "$name" "$version"'
        in shell
    )
    assert 'exordos em elements update "$name" --version "$version"' in shell
    assert 'exordos repo elements show "$repo_element_uuid" --output json' in shell
    assert 'parse_cli_literal("manifest") != expected_manifest' in shell
    assert 'parse_cli_literal("inventory") != expected_inventory' in shell
    assert 'actual.get("uuid") != repo_element_uuid' in shell
    assert shell.index('exordos repo elements show "$repo_element_uuid"') < (
        shell.index('exordos em elements update "$name" --version "$version"')
    )
    assert shell.index("repository-available.json") < shell.index(
        'exordos em elements update "$name" --version "$version"'
    )
    assert 'update_element_exact "$WORKSPACE_REPOSITORY_UUID" workspace' in shell
    assert '"$COMPATIBILITY_VERSION"' in shell
    assert '"$STAGE_VERSION"' in shell
    assert '"$CANONICAL_VERSION"' in shell
    assert '"$ROLLBACK_VERSION"' in shell
    for prefix in ("COMPATIBILITY", "STAGE", "CANONICAL", "ROLLBACK"):
        assert f"{prefix}_MANIFEST" in shell
        assert f"{prefix}_INVENTORY" in shell


def test_repository_show_preflight_compares_private_manifest_and_inventory(tmp_path):
    body = _read(RUNBOOK)
    shell = _shell_blocks(body)
    function = shell.split("verify_repo_element_evidence() {", 1)[1].split(
        "\n}", 1
    )[0]
    function = "verify_repo_element_evidence() {" + function + "\n}"
    manifest = {"name": "workspace", "version": "1.2.3", "resources": {}}
    inventory = {"version": "1.2.3", "index": {"images": {}}}
    manifest_path = tmp_path / "workspace.yaml"
    inventory_path = tmp_path / "inventory.json"
    readback_path = tmp_path / "readback-source.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    readback_values = {
        "uuid": "11111111-1111-4111-8111-111111111111",
        "repository": "22222222-2222-4222-8222-222222222222",
        "name": "workspace",
        "version": "1.2.3",
        "status": "AVAILABLE",
        "manifest": repr(manifest),
        "inventory": repr(inventory),
    }
    readback = [
        {"field": field, "value": value}
        for field, value in readback_values.items()
    ]
    readback_path.write_text(json.dumps(readback), encoding="utf-8")
    fake_exordos = bin_dir / "exordos"
    fake_exordos.write_text(
        "#!/usr/bin/env bash\nset -eu\ncat \"$REPO_SHOW_FIXTURE\"\n",
        encoding="utf-8",
    )
    fake_exordos.chmod(0o755)
    report_prefix = tmp_path / "preflight"
    command = (
        "set -euo pipefail\n"
        f"{function}\n"
        "verify_repo_element_evidence "
        "11111111-1111-4111-8111-111111111111 "
        "22222222-2222-4222-8222-222222222222 workspace 1.2.3 "
        f"{manifest_path!s} {inventory_path!s} {report_prefix!s}\n"
    )
    environment = {
        **os.environ,
        "PATH": f"{bin_dir!s}:{os.environ['PATH']}",
        "REPO_SHOW_FIXTURE": str(readback_path),
    }
    valid = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert valid.returncode == 0, valid.stderr
    assert (tmp_path / "preflight-repository-element.json").is_file()

    readback_values["inventory"] = repr({"version": "different"})
    readback = [
        {"field": field, "value": value}
        for field, value in readback_values.items()
    ]
    readback_path.write_text(json.dumps(readback), encoding="utf-8")
    mismatch = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert mismatch.returncode != 0


def test_production_migration_publication_order_and_evidence_states():
    body = _read(WORKFLOW_RUNBOOK)

    assert "push compatibility, stage, rollback, and canonical in" in body
    assert re.search(r"Canonical is\s+published last", body)
    assert "with state `prepared` before the first repository push" in body
    assert "prepared -> published" in body
    assert "prepared -> publication_failed" in body
    assert "prepared -> verified_only" in body
    assert re.search(r"retaining completed\s+push logs", body)
    assert "relative\nevidence digest is recomputed" in body
    assert "evidence state is `published`" in body


def test_production_cutover_runbook_requires_stable_active_readback():
    body = _read(RUNBOOK)
    shell = _shell_blocks(body)

    assert "wait_element_stably_active" in shell
    assert '.status == "ACTIVE"' in shell
    assert "consecutive=$((consecutive + 1))" in shell
    assert "if ((consecutive == 3)); then" in shell
    assert 'printf \'%s\\n\' "$result" >"${report_prefix}-active.json"' in shell
    assert 'exordos em elements show "$name"' in shell


def test_production_cutover_runbook_covers_provider_only_and_migration_reports():
    body = _read(RUNBOOK)

    for required in (
        "provider_api",
        "file_api",
        "workspace_zulip_bridge",
        "inventory.json",
        "stage.json",
        "apply.json",
        "freeze.json",
        "final-delta.json",
        "final-apply.json",
        "parity.json",
        "provider-outbox-conversion.json",
        "writer-gate-status",
        "external_bridge",
    ):
        assert required in body


def test_production_cutover_runbook_uses_one_strict_multi_project_driver():
    body = _read(RUNBOOK)
    shell = _shell_blocks(body)

    assert "set -Eeuo pipefail" in shell
    assert 'for PROJECT_ID in "${PROJECT_IDS[@]}"' in shell
    assert "blocks in independent shells" in body
    assert "per-project JSON reports" in body
    assert "freeze_and_verify_all_projects" in shell
    assert "|| return 1" in shell


def test_production_cutover_gate_abort_is_exact_persisted_and_single_resume():
    body = _read(RUNBOOK)
    gate_section = body.split(
        "## 5. Close writers, freeze, apply the final delta, and prove parity", 1
    )[1].split("## 6.", 1)[0]

    for required in (
        "writer-gate-close-intent.json",
        "writer-gate-close",
        '--gate-id "$gate_id"',
        "rehydrate_attempt_gate_ids",
        "release_attempt_gates_and_resume_smtp",
        'exact_gate_state "$status_report" "$gate_id" closed',
        'exact_gate_state "$status_report" "$gate_id" open',
        "writer gate was replaced",
        "Prove every exact generation is open",
    ):
        assert required in gate_section
    assert gate_section.count(
        "sudo /usr/local/bin/workspace-smtp-ingress-attester"
    ) == 1
    assert gate_section.count('resume --gate-id "$resume_gate_id"') == 1
    assert gate_section.index("Prove every exact generation is open") < (
        gate_section.index('resume --gate-id "$resume_gate_id"')
    )


def test_production_cutover_canonical_release_is_restart_safe_and_irreversible():
    body = _read(RUNBOOK)
    acceptance = body.split(
        "## 7. Accept the cutover and cross the first-write boundary deliberately",
        1,
    )[1].split("### Rollback boundary", 1)[0]

    assert "canonical-gate-release-started.json" in body
    assert "first-canonical-gate-release.json" in acceptance
    assert "release_all_canonical_gates" in acceptance
    assert 'exact_gate_state "$status_report" "$gate_id" closed' in acceptance
    assert 'exact_gate_state "$status_report" "$gate_id" open' in acceptance
    assert "partial canonical gate release" in acceptance
    assert "The first successfully released project gate" in acceptance
    assert "never deploy the mail-projection rollback after the first release" in (
        acceptance.replace("\n", " ")
    )


def test_stage_acceptance_requires_attester_and_exim_independently():
    body = _read(RUNBOOK)

    assert 'systemctl is-active --quiet "$ATTESTER_UNIT"' in body
    assert "systemctl is-active --quiet exim4.service" in body
    assert 'systemctl is-active "$ATTESTER_UNIT" exim4.service' not in body


def test_migration_runbook_routes_multi_project_abort_through_one_resume():
    body = _read(MIGRATION_RUNBOOK)

    assert "set -Eeuo pipefail" in body
    assert "release_attempt_gates_and_resume_smtp" in body
    assert "releases only exact still-closed generations" in body
    assert "resumes the shared SMTP hold exactly once" in body
    assert "writer-gate-release --gate-id" not in body


def test_production_cutover_runbook_defines_the_irreversible_write_boundary():
    body = _read(RUNBOOK)
    rollback_section = body.split("### Rollback boundary", 1)[1].split("## 8.", 1)[0]

    assert "Before gate release and before the first canonical write" in rollback_section
    assert "prebuilt `ROLLBACK_VERSION`" in rollback_section
    assert '"$ROLLBACK_VERSION"' in rollback_section
    assert "while the exact gate remains closed" in body
    assert "After gate release or the first canonical write" in rollback_section
    assert "never switch back to `mail_projection`" in rollback_section
    assert "Do **not**\nrun the SMTP-ingress `resume` command" in body
    assert "retain_legacy_mail_resources=false" in body
    assert "separate maintenance change" in body


def test_production_cutover_commands_are_non_destructive_and_sanitized():
    body = _read(RUNBOOK)
    shell = _shell_blocks(body)

    assert "exordos deploy --element-dir" not in shell
    assert "--force" not in shell
    assert "bootstrap --force" not in body
    assert "exordos em elements clear" not in body
    assert "exordos em elements uninstall" not in body
    assert re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", body) is None
    assert (
        re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            body,
            flags=re.IGNORECASE,
        )
        is None
    )
