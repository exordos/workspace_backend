# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
import os
import pathlib
import subprocess

import pytest
import yaml


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
VERIFIER = PROJECT_ROOT / "exordos/ci/verify-production-migration-manifest.py"
BACKEND_IMAGE = "urn:images:11111111-1111-1111-1111-111111111111"
ROLLBACK_BUILD_IMAGE = "urn:images:22222222-2222-2222-2222-222222222222"
CURRENT_MAIL_IMAGE = "urn:images:33333333-3333-3333-3333-333333333333"
COMPATIBILITY_MAIL_IMAGE = "urn:images:44444444-4444-4444-4444-444444444444"


def _run_verifier(
    tmp_path,
    backend_disks,
    *,
    profile="stage",
    inventory_backend_image=BACKEND_IMAGE,
    manifest_mail_image=None,
    missing_mail_service=None,
    missing_backend_service=None,
):
    version = "0.0.1-dev+20260718000000.abcdef12"
    compatibility = profile == "compatibility"
    manifest_path = tmp_path / "workspace.yaml"
    inventory_path = tmp_path / "inventory.json"
    manifest = {
                "name": "workspace",
                "version": version,
                "resources": {
                    "$core.compute.nodes": {
                        "workspace_backend": {
                            "disk_spec": {"disks": backend_disks}
                        },
                        "workspace_mail": {
                            "disk_spec": {
                                "disks": [
                                    {
                                        "size": 10,
                                        "label": "root",
                                        "image": manifest_mail_image
                                        or COMPATIBILITY_MAIL_IMAGE,
                                    },
                                    {"size": 20, "label": "data"},
                                ]
                            }
                        },
                    },
                    "$core.config.configs": {
                        "workspace_mail_config": {
                            "target": {
                                "node": "$core.compute.nodes.$workspace_mail:uuid"
                            },
                            "path": "/etc/workspace/mail.conf",
                            "on_change": {
                                "kind": "shell",
                                "command": "/usr/local/bin/workspace-mail-reload",
                            },
                        },
                        "workspace_mail_pki_config": {
                            "target": {
                                "node": "$core.compute.nodes.$workspace_mail:uuid"
                            },
                            "path": "/etc/workspace/mail-pki.conf",
                            "on_change": {
                                "kind": "shell",
                                "command": "/usr/local/bin/workspace-mail-reload",
                            },
                        },
                        (
                            "workspace_backend_config_remote_mail_v1"
                            if compatibility
                            else "workspace_backend"
                        ): {
                            "target": {
                                "node": "$core.compute.nodes.$workspace_backend:uuid"
                            },
                            "body": {
                                "content": (
                                    "[messenger_storage]\n"
                                    "mode = mail_projection\n"
                                    "canonical_cutover_confirmed = false\n"
                                    "[messenger_mail]\n"
                                    "smtp_host = workspace-mail.example.invalid\n"
                                    "smtp_security = starttls\n"
                                )
                            },
                        },
                        **(
                            {
                                "workspace_smtp_writer_gate_compatibility_marker": {
                                    "target": {
                                        "node": (
                                            "$core.compute.nodes."
                                            "$workspace_mail:uuid"
                                        )
                                    },
                                    "path": (
                                        "/etc/workspace/"
                                        "smtp-writer-gate.compatibility"
                                    ),
                                    "on_change": {
                                        "kind": "shell",
                                        "command": (
                                            "/usr/local/bin/workspace-mail-reload"
                                        ),
                                    },
                                    "body": {
                                        "content": "compatibility-prestart-v1"
                                    },
                                }
                            }
                            if compatibility
                            else {
                                "workspace_smtp_writer_gate_enforced_marker_v1": {
                                    "target": {
                                        "node": (
                                            "$core.compute.nodes."
                                            "$workspace_mail:uuid"
                                        )
                                    },
                                    "path": (
                                        "/etc/workspace/"
                                        "smtp-writer-gate.enforced"
                                    ),
                                    "on_change": {
                                        "kind": "shell",
                                        "command": (
                                            "/usr/local/bin/workspace-mail-reload"
                                        ),
                                    },
                                },
                                "workspace_smtp_writer_gate_config": {
                                    "target": {
                                        "node": (
                                            "$core.compute.nodes."
                                            "$workspace_mail:uuid"
                                        )
                                    },
                                    "path": (
                                        "/etc/workspace/"
                                        "smtp-writer-gate.conf"
                                    ),
                                }
                            }
                        ),
                    },
                    "$core.em.services": {
                        **(
                            {
                                name: {
                                    "target": {
                                        "node": (
                                            "$core.compute.nodes."
                                            "$workspace_backend:uuid"
                                        )
                                    }
                                }
                                for name in {
                                    "workspace_bootstrap",
                                    "workspace_nginx",
                                    "workspace_api_remote_mail_v1",
                                    "workspace_messenger_api_remote_mail_v1",
                                    "workspace_messenger_worker_remote_mail_v1",
                                    "workspace_external_bridge_control",
                                    "workspace_messenger_events_remote_mail_v1",
                                }
                            }
                            if compatibility
                            else {}
                        ),
                        "workspace_mail_bootstrap": {
                            "target": {
                                "node": "$core.compute.nodes.$workspace_mail:uuid"
                            }
                        },
                        "workspace_mail_ca": {
                            "target": {
                                "node": "$core.compute.nodes.$workspace_mail:uuid"
                            }
                        },
                        **(
                            {}
                            if compatibility
                            else {
                                "workspace_smtp_ingress_attester": {
                                    "target": {
                                        "node": (
                                            "$core.compute.nodes."
                                            "$workspace_mail:uuid"
                                        )
                                    },
                                    "path": (
                                        "/usr/local/bin/"
                                        "workspace-smtp-ingress-attester run"
                                    ),
                                }
                            }
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
    if missing_mail_service is not None:
        del manifest["resources"]["$core.em.services"][missing_mail_service]
    if missing_backend_service is not None:
        del manifest["resources"]["$core.em.services"][missing_backend_service]
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    inventory_path.write_text(
        json.dumps(
            {
                "version": version,
                "index": {
                    "images": {
                        inventory_backend_image.split(":", 2)[2]: (
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
    return subprocess.run(
        [str(VERIFIER), profile, str(manifest_path), str(inventory_path)],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "CURRENT_MAIL_IMAGE_URN": CURRENT_MAIL_IMAGE,
            "CURRENT_BACKEND_IMAGE_URN": BACKEND_IMAGE,
            "EXPECTED_ELEMENT_VERSION": version,
            "STAGE_BACKEND_IMAGE_URN": BACKEND_IMAGE,
            "COMPATIBILITY_MAIL_IMAGE_URN": COMPATIBILITY_MAIL_IMAGE,
        },
        text=True,
    )


def test_stage_verifier_rejects_missing_backend_data_disk(tmp_path):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 10, "label": "root", "image": BACKEND_IMAGE},
            {"size": 2, "label": "external-bridge-control"},
        ],
    )

    assert result.returncode != 0
    assert BACKEND_IMAGE not in result.stderr
    assert CURRENT_MAIL_IMAGE not in result.stderr
    assert COMPATIBILITY_MAIL_IMAGE not in result.stderr


def test_compatibility_verifier_accepts_exact_current_backend_and_built_mail(
    tmp_path,
):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 10, "label": "root", "image": BACKEND_IMAGE},
            {"size": 20, "label": "data"},
            {"size": 2, "label": "external-bridge-control"},
        ],
        profile="compatibility",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == COMPATIBILITY_MAIL_IMAGE


def test_compatibility_verifier_rejects_backend_service_identity_drift(tmp_path):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 10, "label": "root", "image": BACKEND_IMAGE},
            {"size": 20, "label": "data"},
            {"size": 2, "label": "external-bridge-control"},
        ],
        profile="compatibility",
        missing_backend_service="workspace_api_remote_mail_v1",
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("profile", "missing_service"),
    (
        ("compatibility", "workspace_mail_bootstrap"),
        ("compatibility", "workspace_mail_ca"),
        ("stage", "workspace_mail_bootstrap"),
        ("stage", "workspace_mail_ca"),
    ),
)
def test_verifier_rejects_each_missing_ordinary_mail_service(
    tmp_path, profile, missing_service
):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 10, "label": "root", "image": BACKEND_IMAGE},
            {"size": 20, "label": "data"},
            {"size": 2, "label": "external-bridge-control"},
        ],
        profile=profile,
        missing_mail_service=missing_service,
    )

    assert result.returncode != 0


def test_stage_verifier_accepts_exact_image_less_backend_data_disk(tmp_path):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 10, "label": "root", "image": BACKEND_IMAGE},
            {"size": 20, "label": "data"},
            {"size": 2, "label": "external-bridge-control"},
        ],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == BACKEND_IMAGE


def test_stage_verifier_rejects_pinned_pre_migration_mail_root(tmp_path):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 10, "label": "root", "image": BACKEND_IMAGE},
            {"size": 20, "label": "data"},
            {"size": 2, "label": "external-bridge-control"},
        ],
        manifest_mail_image=CURRENT_MAIL_IMAGE,
    )

    assert result.returncode != 0
    assert CURRENT_MAIL_IMAGE not in result.stderr
    assert COMPATIBILITY_MAIL_IMAGE not in result.stderr


def test_rollback_verifier_accepts_exact_stage_backend_image_reuse(tmp_path):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 10, "label": "root", "image": BACKEND_IMAGE},
            {"size": 20, "label": "data"},
            {"size": 2, "label": "external-bridge-control"},
        ],
        profile="rollback",
        inventory_backend_image=ROLLBACK_BUILD_IMAGE,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_rollback_verifier_rejects_backend_image_drift(tmp_path):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 10, "label": "root", "image": ROLLBACK_BUILD_IMAGE},
            {"size": 20, "label": "data"},
            {"size": 2, "label": "external-bridge-control"},
        ],
        profile="rollback",
        inventory_backend_image=ROLLBACK_BUILD_IMAGE,
    )

    assert result.returncode != 0
    assert BACKEND_IMAGE not in result.stderr
    assert ROLLBACK_BUILD_IMAGE not in result.stderr


def test_stage_verifier_rejects_backend_control_disk_image_drift(tmp_path):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 10, "label": "root", "image": BACKEND_IMAGE},
            {"size": 20, "label": "data"},
            {
                "size": 2,
                "label": "external-bridge-control",
                "image": ROLLBACK_BUILD_IMAGE,
            },
        ],
    )

    assert result.returncode != 0
    assert BACKEND_IMAGE not in result.stderr
    assert ROLLBACK_BUILD_IMAGE not in result.stderr


def test_stage_verifier_rejects_backend_root_size_drift(tmp_path):
    result = _run_verifier(
        tmp_path,
        [
            {"size": 11, "label": "root", "image": BACKEND_IMAGE},
            {"size": 20, "label": "data"},
            {"size": 2, "label": "external-bridge-control"},
        ],
    )

    assert result.returncode != 0
    assert BACKEND_IMAGE not in result.stderr
