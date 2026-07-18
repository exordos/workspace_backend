# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
import os
import pathlib
import subprocess
import textwrap

import yaml


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOW = PROJECT_ROOT / ".github/workflows/exordos-element.yml"
VERIFIER = PROJECT_ROOT / "exordos/ci/verify-production-migration-manifest.py"
DOCUMENTATION = PROJECT_ROOT / "docs/production_migration_workflow.md"
BACKEND_IMAGE = "urn:images:11111111-1111-1111-1111-111111111111"
CANONICAL_BUILD_IMAGE = "urn:images:22222222-2222-2222-2222-222222222222"
CURRENT_MAIL_IMAGE = "urn:images:33333333-3333-3333-3333-333333333333"
COMPATIBILITY_MAIL_IMAGE = "urn:images:44444444-4444-4444-4444-444444444444"


def _manifest(version, *, canonical):
    backend_config = (
        "[messenger_storage]\n"
        f"mode = {'postgresql_canonical' if canonical else 'mail_projection'}\n"
        f"canonical_cutover_confirmed = {'true' if canonical else 'false'}\n"
    )
    if not canonical:
        backend_config += "[messenger_mail]\nsmtp_host = internal\n"
    backend_disks = [
        {"size": 10, "label": "root", "image": BACKEND_IMAGE},
        {"size": 20, "label": "data"},
        {"size": 2, "label": "external-bridge-control"},
    ]
    services = {}
    if not canonical:
        services["workspace_mail_bootstrap"] = {
            "target": {"node": "$core.compute.nodes.$workspace_mail:uuid"}
        }
        services["workspace_mail_ca"] = {
            "target": {"node": "$core.compute.nodes.$workspace_mail:uuid"}
        }
    return {
        "name": "workspace",
        "version": version,
        "resources": {
            "$core.compute.nodes": {
                "workspace_backend": {"disk_spec": {"disks": backend_disks}},
                "workspace_mail": {
                    "disk_spec": {
                        "disks": [
                            {
                                "size": 10,
                                "label": "root",
                                "image": COMPATIBILITY_MAIL_IMAGE,
                            },
                            {"size": 20, "label": "data"},
                        ]
                    }
                },
            },
            "$core.config.configs": {
                "workspace_mail_config": {
                    "target": {"node": "$core.compute.nodes.$workspace_mail:uuid"},
                    "path": "/etc/workspace/mail.conf",
                    "on_change": {
                        "kind": "shell",
                        "command": "/usr/local/bin/workspace-mail-reload",
                    },
                },
                "workspace_mail_pki_config": {
                    "target": {"node": "$core.compute.nodes.$workspace_mail:uuid"},
                    "path": "/etc/workspace/mail-pki.conf",
                    "on_change": {
                        "kind": "shell",
                        "command": "/usr/local/bin/workspace-mail-reload",
                    },
                },
                "workspace_backend": {
                    "target": {"node": "$core.compute.nodes.$workspace_backend:uuid"},
                    "body": {"content": backend_config},
                },
                "workspace_smtp_writer_gate_config": {
                    "target": {"node": "$core.compute.nodes.$workspace_mail:uuid"},
                    "path": "/etc/workspace/smtp-writer-gate.conf",
                },
                "workspace_smtp_writer_gate_enforced_marker_v1": {
                    "target": {"node": "$core.compute.nodes.$workspace_mail:uuid"},
                    "path": "/etc/workspace/smtp-writer-gate.enforced",
                    "on_change": {
                        "kind": "shell",
                        "command": "/usr/local/bin/workspace-mail-reload",
                    },
                },
            },
            "$core.em.services": {
                **services,
                **(
                    {
                        "workspace_smtp_ingress_attester": {
                            "target": {
                                "node": "$core.compute.nodes.$workspace_mail:uuid"
                            },
                            "path": (
                                "/usr/local/bin/"
                                "workspace-smtp-ingress-attester run"
                            ),
                        }
                    }
                    if not canonical
                    else {}
                ),
            },
            "$workspace.imports.$core_local_domain.records": {
                "workspace_mail": {
                    "type": "A",
                    "record": {
                        "kind": "A",
                        "name": "workspace-mail",
                        "address": (
                            "$core.compute.nodes.$workspace_mail:"
                            "default_network:ipv4"
                        ),
                    },
                }
            },
        },
    }


def _run_verifier(
    tmp_path,
    *,
    canonical,
    backend_image=BACKEND_IMAGE,
    canonical_build_image=CANONICAL_BUILD_IMAGE,
    missing_retained_resource=None,
):
    version = "0.0.1-dev+20260718000000.abcdef12"
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / "workspace.yaml"
    inventory_path = tmp_path / "inventory.json"
    manifest = _manifest(version, canonical=canonical)
    if missing_retained_resource == "mail_config":
        del manifest["resources"]["$core.config.configs"]["workspace_mail_config"]
    elif missing_retained_resource == "mail_pki":
        del manifest["resources"]["$core.config.configs"][
            "workspace_mail_pki_config"
        ]
    elif missing_retained_resource == "mail_dns":
        del manifest["resources"][
            "$workspace.imports.$core_local_domain.records"
        ]["workspace_mail"]
    manifest["resources"]["$core.compute.nodes"]["workspace_backend"]["disk_spec"][
        "disks"
    ][0]["image"] = backend_image
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    inventory_path.write_text(
        json.dumps(
            {
                "version": version,
                "index": {
                    "images": {
                        canonical_build_image.split(":", 2)[2]
                        if canonical
                        else BACKEND_IMAGE.split(":", 2)[2]: (
                            "workspace-backend.raw.zst"
                        ),
                        COMPATIBILITY_MAIL_IMAGE.split(":", 2)[2]: (
                            "workspace-mail.raw.zst"
                        ),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "CURRENT_MAIL_IMAGE_URN": CURRENT_MAIL_IMAGE,
        "EXPECTED_ELEMENT_VERSION": version,
        "STAGE_BACKEND_IMAGE_URN": BACKEND_IMAGE,
        "COMPATIBILITY_MAIL_IMAGE_URN": COMPATIBILITY_MAIL_IMAGE,
    }
    return subprocess.run(
        [
            str(VERIFIER),
            "canonical" if canonical else "stage",
            str(manifest_path),
            str(inventory_path),
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def test_production_migration_workflow_builds_four_secret_scoped_artifacts():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    migration = workflow.split(
        "      - name: Build, verify, and optionally publish production migration artifacts",
        1,
    )[1]

    assert "production_migration" in workflow
    assert "WORKSPACE_PRODUCTION_CURRENT_BACKEND_IMAGE_URN" in migration
    assert "WORKSPACE_PRODUCTION_CURRENT_MAIL_IMAGE_URN" in migration
    assert "WORKSPACE_PRODUCTION_MIGRATION_EVIDENCE_DIR" in migration
    inputs = workflow.split("    inputs:", 1)[1].split("env:", 1)[0]
    assert "CURRENT_MAIL_IMAGE_URN" not in inputs
    assert "EVIDENCE_ARCHIVE_ROOT" not in inputs
    first_push = migration.index('push_immutable "${compatibility_dir}"')
    assert migration.index("messenger_storage_mode=mail_projection") < first_push
    assert "mail_migration_compatibility=true" in migration
    assert "mail_migration_stage1=true" not in migration
    assert migration.index("mail_migration_cutover_keep_legacy_disk=true") < first_push
    assert migration.index("STAGE_BACKEND_IMAGE_URN") < migration.index(
        "messenger_storage_mode=postgresql_canonical"
    )
    assert migration.index("COMPATIBILITY_MAIL_IMAGE_URN") < migration.index(
        "messenger_storage_mode=postgresql_canonical"
    )
    stage_build = migration.split(
        '> "${archive_tmp}/stage-build.log" 2>&1', 1
    )[0].rsplit('if ! "${EXORDOS_BIN}" build .', 1)[1]
    assert 'workspace_mail_image="${CURRENT_MAIL_IMAGE_URN}"' not in stage_build
    assert migration.count(
        'workspace_mail_image="${COMPATIBILITY_MAIL_IMAGE_URN}"'
    ) == 3
    assert (
        migration.index(
            'rollback_version="${rollback_version_base}-${version_channel}+'
        )
        < first_push
    )
    assert migration.index('rollback "${rollback_manifest}"') < first_push
    assert migration.count('"${EXORDOS_BIN}" push .') == 1
    assert migration.count("push_immutable ") == 4
    assert "push_immutable()" in migration
    assert "local status" in migration
    assert "status=$?" in migration
    assert "PIPESTATUS" not in migration
    assert "tee " not in migration
    assert 'grep -Fq " already exists." "${push_log}"' in migration
    assert (
        'push_immutable "${stage_dir}" "${archive_final}/stage-push.log"' in migration
    )
    compatibility_call = migration.index('push_immutable "${compatibility_dir}"')
    assert migration.index(
        '"${archive_final}/compatibility-push.log"', compatibility_call
    ) < migration.index("then", compatibility_call)
    assert (
        'push_immutable "${rollback_dir}" "${archive_final}/rollback-push.log"'
    ) in migration
    assert (
        'push_immutable "${canonical_dir}" "${archive_final}/canonical-push.log"'
    ) in migration
    compatibility_push = migration.index('push_immutable "${compatibility_dir}"')
    stage_push = migration.index('push_immutable "${stage_dir}"')
    rollback_push = migration.index('push_immutable "${rollback_dir}"')
    canonical_push = migration.index('push_immutable "${canonical_dir}"')
    assert compatibility_push < stage_push < rollback_push < canonical_push
    assert "--force" not in migration
    assert "git commit --allow-empty" in migration
    assert 'commit_list="$(git rev-list "${original_head}")"' in migration
    assert 'done <<< "${commit_list}"' in migration
    assert 'done < <(git rev-list "${original_head}")' not in migration
    assert "--max-count" not in migration
    assert "awk '/^[0-9]+\\.[0-9]+\\.[0-9]+$/ { print }'" in migration
    assert (
        'compatibility_version_base="${version_major}.${version_minor}.'
        '$((10#${version_patch} + 1))"' in migration
    )
    assert (
        'stage_version_base="${version_major}.${version_minor}.'
        '$((10#${version_patch} + 2))"' in migration
    )
    assert (
        'canonical_version_base="${version_major}.${version_minor}.'
        '$((10#${version_patch} + 3))"' in migration
    )
    assert (
        'rollback_version_base="${version_major}.${version_minor}.'
        '$((10#${version_patch} + 4))"' in migration
    )
    assert 'stage_version="${stage_version_base}-${version_channel}+' in migration
    assert 'git tag "${compatibility_version}"' in migration
    assert 'git tag "${stage_version}"' in migration
    assert 'git tag "${canonical_version}"' in migration
    assert 'git tag "${rollback_version}"' in migration
    assert '"${EXORDOS_BIN}" get-version .' not in migration
    assert migration.count('= "${source_tree}"') >= 4
    assert migration.count('git -C "${ui_dir}" diff --quiet') == 5
    assert migration.count("workspace-backend.raw.zst") >= 3
    assert migration.count("workspace-mail.raw.zst") >= 3
    assert migration.count("PUBLISH_REQUESTED") >= 5
    assert '[[ "${PUBLISH_REQUESTED:-false}" == "true" ]]' in migration
    assert 'mkdir -m 0700 "${archive_tmp}"' in migration
    assert 'test -w "${archive_tmp}"' in migration
    assert 'find "${archive_tmp}" -type d -exec chmod 0700 {} +' in migration
    assert 'find "${archive_tmp}" -type f -exec chmod 0600 {} +' in migration
    assert 'mv "${archive_tmp}" "${archive_final}"' in migration
    assert migration.index('mkdir -m 0700 "${archive_tmp}"') < first_push
    assert migration.index('mv "${archive_tmp}" "${archive_final}"') < first_push
    assert migration.index("printf '%s\\n' prepared") < first_push
    terminal_readback = migration.index(
        'readback_evidence_state "${final_evidence_state}"'
    )
    assert first_push < terminal_readback
    assert terminal_readback < migration.index("printf 'stage_version=%s\\n'")
    assert "upload-artifact" not in migration
    assert '-printf "%p' not in migration


def test_default_workflow_publishes_automatically_except_manual_opt_out():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    normal_condition = (
        "github.event_name != 'workflow_dispatch' || "
        "inputs.profile != 'production_migration'"
    )
    build_step = workflow.split("      - name: Build element", 1)[1].split(
        "      - name:", 1
    )[0]
    publish_step = workflow.split("      - name: Publish element", 1)[1].split(
        "      - name:", 1
    )[0]

    assert normal_condition in build_step
    assert '"${EXORDOS_BIN}" build .' in build_step
    assert publish_step.count('"${EXORDOS_BIN}" push .') == 1
    assert "--force" in publish_step
    assert "\n        if:" not in publish_step
    assert '"${GITHUB_EVENT_NAME}" == "workflow_dispatch"' in publish_step
    assert '"${BUILD_PROFILE:-default}" == "production_migration"' in publish_step
    assert "PUBLISH_REQUESTED: ${{ inputs.publish }}" in publish_step
    assert '"${PUBLISH_REQUESTED:-false}" != "true"' in publish_step
    assert publish_step.index('"${PUBLISH_REQUESTED:-false}" != "true"') < (
        publish_step.index(': "${PUSH_CFG:?')
    )


def test_production_migration_version_search_reaches_old_release_tag(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "config", "user.name", "CASSI CI Test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "cassi@exordos.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "--message", "release"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(["git", "tag", "2.3.4"], cwd=tmp_path, check=True)
    for number in range(101):
        subprocess.run(
            [
                "git",
                "commit",
                "--allow-empty",
                "--message",
                f"post-release-{number}",
            ],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

    workflow = WORKFLOW.read_text(encoding="utf-8")
    version_block = workflow.split('          base_version=""', 1)[1].split(
        "\n\n          git commit --allow-empty", 1
    )[0]
    assert 'git tag --points-at "${commit}" --list' in version_block
    assert "show-ref" not in version_block
    assert "|| true" not in version_block
    script = (
        "set -euo pipefail\n"
        'original_head="$(git rev-parse HEAD)"\n'
        'base_version=""'
        f"{version_block}\n"
        'printf "%s\\n%s\\n%s\\n%s\\n%s\\n" '
        '"${compatibility_version_base}" "${stage_version_base}" '
        '"${canonical_version_base}" '
        '"${rollback_version_base}" "${version_channel}"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        env={**os.environ, "GITHUB_REF_NAME": "feature"},
        text=True,
    )

    assert result.returncode == 0, result.stderr
    values = result.stdout.splitlines()
    assert values == ["2.3.5", "2.3.6", "2.3.7", "2.3.8", "dev"]
    versions = [tuple(int(part) for part in value.split(".")) for value in values[:4]]
    assert versions[0] < versions[1] < versions[2] < versions[3]


def test_production_migration_version_search_fails_when_rev_list_fails(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "config", "user.name", "CASSI CI Test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "cassi@exordos.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "--message", "release"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [[ "${{1:-}}" == "rev-list" ]]; then
              echo "injected rev-list failure" >&2
              exit 73
            fi
            exec {subprocess.check_output(["which", "git"], text=True).strip()} "$@"
            """
        ),
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    workflow = WORKFLOW.read_text(encoding="utf-8")
    version_block = workflow.split('          base_version=""', 1)[1].split(
        "\n\n          git commit --allow-empty", 1
    )[0]
    script = (
        "set -euo pipefail\n"
        'original_head="$(git rev-parse HEAD)"\n'
        'base_version=""'
        f"{version_block}\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
    )

    assert result.returncode == 73
    assert "injected rev-list failure" in result.stderr
    assert "Git history lookup failed" in result.stderr


def test_production_migration_version_search_fails_when_tag_lookup_fails(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "config", "user.name", "CASSI CI Test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "cassi@exordos.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "--message", "release"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [[ "${{1:-}}" == "tag" && "${{2:-}}" == "--points-at" ]]; then
              echo "injected tag lookup failure" >&2
              exit 73
            fi
            exec {subprocess.check_output(["which", "git"], text=True).strip()} "$@"
            """
        ),
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    workflow = WORKFLOW.read_text(encoding="utf-8")
    version_block = workflow.split('          base_version=""', 1)[1].split(
        "\n\n          git commit --allow-empty", 1
    )[0]
    script = (
        "set -euo pipefail\n"
        'original_head="$(git rev-parse HEAD)"\n'
        'base_version=""'
        f"{version_block}\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
    )

    assert result.returncode == 73
    assert "injected tag lookup failure" in result.stderr


def test_immutable_publish_wrapper_rejects_cli_zero_exit_collision(tmp_path):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    function_body = workflow.split("          push_immutable() {", 1)[1].split(
        "\n          }", 1
    )[0]
    function_body = "push_immutable() {" + function_body + "\n}"
    fake_exordos = tmp_path / "exordos"
    fake_exordos.write_text(
        """#!/usr/bin/env bash
case "${FAKE_PUSH_BEHAVIOR}" in
  success) echo "Published immutable element" ;;
  exists) echo "Element workspace version fake already exists." ;;
  failure) echo "Repository unavailable"; exit 7 ;;
esac
""",
        encoding="utf-8",
    )
    fake_exordos.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        f"EXORDOS_BIN={fake_exordos!s}\n"
        f"push_cfg_path={tmp_path / 'push.yaml'!s}\n"
        f"{function_body}\n"
        f"push_immutable target {tmp_path / 'push.log'!s}\n"
    )

    success = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        env={**os.environ, "FAKE_PUSH_BEHAVIOR": "success"},
        text=True,
    )
    collision = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        env={**os.environ, "FAKE_PUSH_BEHAVIOR": "exists"},
        text=True,
    )
    failure = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        env={**os.environ, "FAKE_PUSH_BEHAVIOR": "failure"},
        text=True,
    )

    assert success.returncode == 0, success.stderr
    assert collision.returncode != 0
    assert "Immutable element version already exists" in collision.stderr
    assert failure.returncode != 0
    assert "Immutable element publication failed" in failure.stderr


def test_production_migration_evidence_is_private_atomic_and_not_uploaded():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    migration = workflow.split(
        "      - name: Build, verify, and optionally publish production migration artifacts",
        1,
    )[1]

    assert (
        "EVIDENCE_ARCHIVE_ROOT: "
        "${{ secrets.WORKSPACE_PRODUCTION_MIGRATION_EVIDENCE_DIR }}"
    ) in migration
    assert ': "${EVIDENCE_ARCHIVE_ROOT:?' in migration
    assert 'archive_tmp="${EVIDENCE_ARCHIVE_ROOT}/.${archive_name}.tmp"' in migration
    assert 'archive_final="${EVIDENCE_ARCHIVE_ROOT}/${archive_name}"' in migration
    assert migration.count("archive_status=$?") >= 3
    assert migration.count("set -euo pipefail") >= 4
    assert 'chmod 0700 "${EVIDENCE_ARCHIVE_ROOT}"' in migration
    assert 'mkdir -m 0700 "${archive_tmp}"' in migration
    assert 'chmod 0600 "${archive_tmp}/evidence.sha256"' in migration
    assert "! -name evidence.sha256" in migration
    assert 'cd "${archive_tmp}"' in migration
    assert 'mv "${archive_tmp}" "${archive_final}"' in migration
    assert "actions/upload-artifact" not in migration
    assert 'echo "${EVIDENCE_ARCHIVE_ROOT}"' not in migration
    assert 'echo "${STAGE_BACKEND_IMAGE_URN}"' not in migration
    assert 'echo "${COMPATIBILITY_MAIL_IMAGE_URN}"' not in migration
    assert 'echo "${CURRENT_BACKEND_IMAGE_URN}"' not in migration
    assert 'echo "${CURRENT_MAIL_IMAGE_URN}"' not in migration
    assert "-printf" not in migration
    assert 'cd "${stage_dir}"' in migration
    assert 'cd "${canonical_dir}"' in migration
    assert 'cd "${rollback_dir}"' in migration
    assert migration.count("printf '%s\\n' \"zstd -t: passed\"") == 4
    assert '"${archive_tmp}/github-run-id.txt"' in migration
    assert '"${archive_tmp}/github-run-attempt.txt"' in migration
    assert '"${archive_tmp}/exordos-download.sha256"' in migration
    assert "printf 'stage_version=%s\\n'" in migration
    assert "printf 'canonical_version=%s\\n'" in migration
    assert "printf 'rollback_version=%s\\n'" in migration
    assert "printf '%s\\n' prepared" in migration
    assert "update_evidence_state publication_failed" in migration
    assert "published|publication_failed|verified_only" in migration
    assert 'test "$(cat "${archive_final}/state.txt")" = prepared' in migration
    assert "final_evidence_state=published" in migration
    assert "final_evidence_state=verified_only" in migration
    assert "readback_evidence_state()" in migration
    assert "sha256sum --check --status evidence.sha256" in migration
    assert 'readback_evidence_state "${final_evidence_state}"' in migration
    assert '[[ "${final_evidence_state}" == "published" ]]' in migration
    assert "verified only and not deployable" in migration
    assert "production_migration_compatibility_version" in workflow
    assert "production_migration_stage_version" in workflow
    assert "production_migration_canonical_version" in workflow
    assert "production_migration_rollback_version" in workflow

    compatibility_build = migration.index(
        '> "${archive_tmp}/compatibility-build.log" 2>&1'
    )
    stage_build = migration.index('> "${archive_tmp}/stage-build.log" 2>&1')
    canonical_build = migration.index('> "${archive_tmp}/canonical-build.log" 2>&1')
    rollback_build = migration.index('> "${archive_tmp}/rollback-build.log" 2>&1')
    first_push = migration.index('push_immutable "${compatibility_dir}"')
    final_move = migration.index('mv "${archive_tmp}" "${archive_final}"')
    assert (
        compatibility_build
        < stage_build
        < canonical_build
        < rollback_build
        < final_move
        < first_push
    )


def test_production_migration_evidence_digest_survives_atomic_rename(tmp_path):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    migration = workflow.split(
        "      - name: Build, verify, and optionally publish production migration artifacts",
        1,
    )[1]
    start = migration.index(
        "          set +e\n          (\n            set -euo pipefail\n"
        '            find "${archive_tmp}" -type d'
    )
    end = migration.index(
        '          echo "Private production migration evidence archived"', start
    )
    finalize = textwrap.dedent(migration[start:end])
    archive_tmp = tmp_path / ".migration.tmp"
    archive_final = tmp_path / "migration"
    archive_tmp.mkdir(mode=0o755)
    (archive_tmp / "manifest.yaml").write_text("name: workspace\n", encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail\n"
                f"archive_tmp={archive_tmp!s}\n"
                f"archive_final={archive_final!s}\n"
                f"{finalize}"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not archive_tmp.exists()
    assert archive_final.stat().st_mode & 0o777 == 0o700
    assert all(
        path.stat().st_mode & 0o777 == 0o600
        for path in archive_final.iterdir()
        if path.is_file()
    )
    digest = subprocess.run(
        ["sha256sum", "--check", "evidence.sha256"],
        cwd=archive_final,
        capture_output=True,
        check=False,
        text=True,
    )
    assert digest.returncode == 0, digest.stderr


def test_production_migration_evidence_early_failure_prevents_finalization(tmp_path):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    migration = workflow.split(
        "      - name: Build, verify, and optionally publish production migration artifacts",
        1,
    )[1]
    start = migration.index(
        "          set +e\n          (\n            set -euo pipefail\n"
        '            find "${archive_tmp}" -type d'
    )
    end = migration.index(
        '          echo "Private production migration evidence archived"', start
    )
    finalize = textwrap.dedent(migration[start:end]).replace(
        "set -euo pipefail\n",
        'set -euo pipefail\nfalse\ntouch "${archive_tmp}/unexpected-marker"\n',
        1,
    )
    archive_tmp = tmp_path / ".migration.tmp"
    archive_final = tmp_path / "migration"
    archive_tmp.mkdir(mode=0o700)
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail\n"
                f"archive_tmp={archive_tmp!s}\n"
                f"archive_final={archive_final!s}\n"
                f"{finalize}"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert archive_tmp.exists()
    assert not archive_final.exists()
    assert not (archive_tmp / "unexpected-marker").exists()


def test_publication_failure_state_recomputes_durable_evidence(tmp_path):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    migration = workflow.split(
        "      - name: Build, verify, and optionally publish production migration artifacts",
        1,
    )[1]
    function_body = migration.split("          update_evidence_state() {", 1)[1].split(
        "\n          }", 1
    )[0]
    function_body = "update_evidence_state() {" + function_body + "\n}"
    archive_final = tmp_path / "migration"
    archive_final.mkdir(mode=0o700)
    (archive_final / "state.txt").write_text("prepared\n", encoding="utf-8")
    (archive_final / "stage-push.log").write_text(
        "immutable publication failed\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail\n"
                f"archive_final={archive_final!s}\n"
                f"{function_body}\n"
                "update_evidence_state publication_failed\n"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (archive_final / "state.txt").read_text(encoding="utf-8") == (
        "publication_failed\n"
    )
    digest = subprocess.run(
        ["sha256sum", "--check", "evidence.sha256"],
        cwd=archive_final,
        capture_output=True,
        check=False,
        text=True,
    )
    assert digest.returncode == 0, digest.stderr


def test_evidence_state_transition_rejects_invalid_and_repeated_states(tmp_path):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    migration = workflow.split(
        "      - name: Build, verify, and optionally publish production migration artifacts",
        1,
    )[1]
    function_body = migration.split("          update_evidence_state() {", 1)[1].split(
        "\n          }", 1
    )[0]
    function_body = "update_evidence_state() {" + function_body + "\n}"
    archive_final = tmp_path / "migration"
    archive_final.mkdir(mode=0o700)
    (archive_final / "state.txt").write_text("prepared\n", encoding="utf-8")

    invalid = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail\n"
                f"archive_final={archive_final!s}\n"
                f"{function_body}\n"
                "update_evidence_state invalid_state\n"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert invalid.returncode != 0
    assert (archive_final / "state.txt").read_text(encoding="utf-8") == "prepared\n"

    first = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail\n"
                f"archive_final={archive_final!s}\n"
                f"{function_body}\n"
                "update_evidence_state published\n"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    repeated = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail\n"
                f"archive_final={archive_final!s}\n"
                f"{function_body}\n"
                "update_evidence_state verified_only\n"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    assert repeated.returncode != 0
    assert (archive_final / "state.txt").read_text(encoding="utf-8") == "published\n"


def test_terminal_evidence_readback_checks_digest_and_exact_metadata(tmp_path):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    migration = workflow.split(
        "      - name: Build, verify, and optionally publish production migration artifacts",
        1,
    )[1]
    function_body = migration.split("          readback_evidence_state() {", 1)[
        1
    ].split("\n          }", 1)[0]
    function_body = "readback_evidence_state() {" + function_body + "\n}"
    archive_final = tmp_path / "migration"
    archive_final.mkdir(mode=0o700)
    values = {
        "state.txt": "published",
        "source-commit.txt": "source-commit",
        "source-tree.txt": "source-tree",
        "workspace-ui-commit.txt": "ui-commit",
        "compatibility-commit.txt": "compatibility-commit",
        "stage-commit.txt": "stage-commit",
        "canonical-commit.txt": "canonical-commit",
        "rollback-commit.txt": "rollback-commit",
        "compatibility-version.txt": "2.3.5-dev+20260719000000.00000000",
        "stage-version.txt": "2.3.5-dev+20260719000000.11111111",
        "canonical-version.txt": "2.3.6-dev+20260719000000.22222222",
        "rollback-version.txt": "2.3.7-dev+20260719000000.33333333",
        "stage-backend-image.urn": BACKEND_IMAGE,
        "compatibility-mail-image.urn": COMPATIBILITY_MAIL_IMAGE,
        "current-backend-image.urn": BACKEND_IMAGE,
        "current-mail-image.urn": CURRENT_MAIL_IMAGE,
        "publication-requested.txt": "true",
        "github-run-id.txt": "1234",
        "github-run-attempt.txt": "2",
        "exordos-download.sha256": "a" * 64,
    }
    for name, value in values.items():
        (archive_final / name).write_text(f"{value}\n", encoding="utf-8")

    digest_script = (
        "find . -type f ! -name evidence.sha256 -print0 | sort -z "
        "| xargs -0 sha256sum > evidence.sha256"
    )
    subprocess.run(["bash", "-c", digest_script], cwd=archive_final, check=True)
    variables = (
        "original_head=source-commit\n"
        "source_tree=source-tree\n"
        "ui_head=ui-commit\n"
        "compatibility_head=compatibility-commit\n"
        "stage_head=stage-commit\n"
        "canonical_head=canonical-commit\n"
        "rollback_head=rollback-commit\n"
        f"compatibility_version={values['compatibility-version.txt']}\n"
        f"stage_version={values['stage-version.txt']}\n"
        f"canonical_version={values['canonical-version.txt']}\n"
        f"rollback_version={values['rollback-version.txt']}\n"
        f"STAGE_BACKEND_IMAGE_URN={BACKEND_IMAGE}\n"
        f"COMPATIBILITY_MAIL_IMAGE_URN={COMPATIBILITY_MAIL_IMAGE}\n"
        f"CURRENT_BACKEND_IMAGE_URN={BACKEND_IMAGE}\n"
        f"CURRENT_MAIL_IMAGE_URN={CURRENT_MAIL_IMAGE}\n"
        "PUBLISH_REQUESTED=true\n"
        "GITHUB_RUN_ID=1234\n"
        "GITHUB_RUN_ATTEMPT=2\n"
        f"EXORDOS_RELEASE_SHA256={'a' * 64}\n"
    )
    script = (
        "set -euo pipefail\n"
        f"archive_final={archive_final!s}\n"
        f"{variables}"
        f"{function_body}\n"
        "readback_evidence_state published\n"
    )
    valid = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )
    assert valid.returncode == 0, valid.stderr

    (archive_final / "stage-version.txt").write_text(
        "2.3.8-dev+20260719000000.44444444\n", encoding="utf-8"
    )
    subprocess.run(["bash", "-c", digest_script], cwd=archive_final, check=True)
    mismatch = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )
    assert mismatch.returncode != 0


def test_manifest_verifier_accepts_stage_and_exact_canonical_reuse(tmp_path):
    stage = _run_verifier(tmp_path / "stage", canonical=False)
    canonical = _run_verifier(tmp_path / "canonical", canonical=True)

    assert stage.returncode == 0, stage.stderr
    assert stage.stdout.strip() == BACKEND_IMAGE
    assert canonical.returncode == 0, canonical.stderr
    assert canonical.stdout == ""


def test_manifest_verifier_does_not_assume_content_distinct_canonical_image(
    tmp_path,
):
    canonical = _run_verifier(
        tmp_path,
        canonical=True,
        canonical_build_image=BACKEND_IMAGE,
    )

    assert canonical.returncode == 0, canonical.stderr


def test_manifest_verifier_rejects_canonical_backend_drift_without_leaking_pin(
    tmp_path,
):
    result = _run_verifier(
        tmp_path, canonical=True, backend_image=CANONICAL_BUILD_IMAGE
    )

    assert result.returncode != 0
    assert BACKEND_IMAGE not in result.stderr
    assert CURRENT_MAIL_IMAGE not in result.stderr
    assert COMPATIBILITY_MAIL_IMAGE not in result.stderr


def test_manifest_verifier_rejects_missing_canonical_mail_control_plane(tmp_path):
    for missing in ("mail_config", "mail_pki", "mail_dns"):
        result = _run_verifier(
            tmp_path / missing,
            canonical=True,
            missing_retained_resource=missing,
        )
        assert result.returncode != 0


def test_production_migration_workflow_is_documented_without_internal_values():
    body = DOCUMENTATION.read_text(encoding="utf-8")

    assert "production_migration" in body
    assert "WORKSPACE_PRODUCTION_CURRENT_BACKEND_IMAGE_URN" in body
    assert "WORKSPACE_PRODUCTION_CURRENT_MAIL_IMAGE_URN" in body
    assert "without force-overwriting" in body
    assert "never deploys an element" in body
