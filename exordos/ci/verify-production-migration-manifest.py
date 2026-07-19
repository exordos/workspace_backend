#!/usr/bin/env python3

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
import os
import pathlib
import re
import sys

import yaml


IMAGE_URN = re.compile(
    r"^urn:images:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
BACKEND_NODE = "$core.compute.nodes.$workspace_backend:uuid"
MAIL_NODE = "$core.compute.nodes.$workspace_mail:uuid"
COMPATIBILITY_BACKEND_SERVICES = {
    "workspace_bootstrap",
    "workspace_nginx",
    "workspace_api_remote_mail_v1",
    "workspace_messenger_api_remote_mail_v1",
    "workspace_messenger_worker_remote_mail_v1",
    "workspace_external_bridge_control",
    "workspace_messenger_events_remote_mail_v1",
}


class VerificationError(Exception):
    pass


def _require(condition, message):
    if not condition:
        raise VerificationError(message)


def _load_manifest(path):
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VerificationError("rendered manifest cannot be read") from exc
    _require(isinstance(value, dict), "rendered manifest must be an object")
    return value


def _load_inventory(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError("element inventory cannot be read") from exc
    _require(isinstance(value, dict), "element inventory must be an object")
    return value


def _node_disks(resources, node_name):
    nodes = resources["$core.compute.nodes"]
    node = nodes[node_name]
    disks = node["disk_spec"]["disks"]
    _require(isinstance(disks, list), "node disks must be a list")
    labels = [disk["label"] for disk in disks]
    _require(len(labels) == len(set(labels)), "node disk labels must be unique")
    return {disk["label"]: disk for disk in disks}


def _targeted_content(resources, resource_kind, target_node):
    bodies = []
    for resource in resources.get(resource_kind, {}).values():
        target = resource.get("target", {})
        if target.get("node") != target_node:
            continue
        body = resource.get("body", {})
        if isinstance(body, dict):
            content = body.get("content", "")
            if isinstance(content, str):
                bodies.append(content)
    return "\n".join(bodies)


def _targeted_services(resources, target_node):
    result = []
    for name, resource in resources.get("$core.em.services", {}).items():
        if resource.get("target", {}).get("node") == target_node:
            result.append(name)
    return result


def _verify_mail_services(resources, expected, profile):
    actual = set(_targeted_services(resources, MAIL_NODE))
    _require(actual == set(expected), f"{profile} mail service set mismatch")


def _built_image(inventory, filename, description):
    images = inventory["index"]["images"]
    matches = [
        image_uuid
        for image_uuid, image_filename in images.items()
        if image_filename == filename
    ]
    _require(len(matches) == 1, f"inventory must contain one built {description} image")
    result = f"urn:images:{matches[0]}"
    _require(IMAGE_URN.fullmatch(result), f"built {description} image URN is invalid")
    return result


def _built_backend_image(inventory):
    return _built_image(inventory, "workspace-backend.raw.zst", "backend")


def _built_mail_image(inventory):
    return _built_image(inventory, "workspace-mail.raw.zst", "mail")


def _verify_common(manifest, inventory, expected_mail_image):
    expected_version = os.environ["EXPECTED_ELEMENT_VERSION"]
    _require(manifest.get("name") == "workspace", "manifest element is not Workspace")
    _require(manifest.get("version") == expected_version, "manifest version mismatch")
    _require(inventory.get("version") == expected_version, "inventory version mismatch")
    _require(IMAGE_URN.fullmatch(expected_mail_image), "expected mail image is invalid")

    resources = manifest["resources"]
    backend_disks = _node_disks(resources, "workspace_backend")
    mail_disks = _node_disks(resources, "workspace_mail")
    _require(
        set(backend_disks) == {"root", "data", "external-bridge-control"},
        "backend persistent disks mismatch",
    )
    _require(
        backend_disks["data"] == {"size": 20, "label": "data"},
        "backend data disk must remain image-less",
    )
    _require(
        set(backend_disks["root"]) == {"size", "image", "label"}
        and backend_disks["root"]["size"] == 10,
        "backend root disk specification mismatch",
    )
    _require(
        backend_disks["external-bridge-control"]
        == {"size": 2, "label": "external-bridge-control"},
        "backend bridge control disk must remain image-less",
    )
    _require(set(mail_disks) == {"root", "data"}, "retained mail disks mismatch")
    _require(
        mail_disks["root"]
        == {"size": 10, "image": expected_mail_image, "label": "root"},
        "mail root disk specification mismatch",
    )
    _require(
        mail_disks["data"] == {"size": 20, "label": "data"},
        "mail data disk must remain image-less",
    )
    configs = resources.get("$core.config.configs", {})
    for name, path in (
        ("workspace_mail_config", "/etc/workspace/mail.conf"),
        ("workspace_mail_pki_config", "/etc/workspace/mail-pki.conf"),
    ):
        resource = configs.get(name, {})
        _require(
            resource.get("target", {}).get("node") == MAIL_NODE,
            f"retained {name} target mismatch",
        )
        _require(resource.get("path") == path, f"retained {name} path mismatch")
        _require(
            resource.get("on_change")
            == {"kind": "shell", "command": "/usr/local/bin/workspace-mail-reload"},
            f"retained {name} reload action mismatch",
        )
    mail_record = resources.get(
        "$workspace.imports.$core_local_domain.records", {}
    ).get("workspace_mail", {})
    _require(mail_record.get("type") == "A", "retained mail DNS type mismatch")
    _require(
        mail_record.get("record")
        == {
            "kind": "A",
            "name": "workspace-mail",
            "address": "$core.compute.nodes.$workspace_mail:default_network:ipv4",
        },
        "retained mail DNS record mismatch",
    )
    return resources, backend_disks


def _verify_enforced_mail_guard(resources, profile):
    configs = resources.get("$core.config.configs", {})
    marker = configs.get("workspace_smtp_writer_gate_enforced_marker_v1", {})
    _require(
        marker.get("target", {}).get("node") == MAIL_NODE,
        f"{profile} SMTP writer-gate marker target mismatch",
    )
    _require(
        marker.get("path") == "/etc/workspace/smtp-writer-gate.enforced",
        f"{profile} SMTP writer-gate marker path mismatch",
    )
    _require(
        marker.get("on_change")
        == {"kind": "shell", "command": "/usr/local/bin/workspace-mail-reload"},
        f"{profile} SMTP writer-gate marker reload action mismatch",
    )
    writer_gate_config = configs.get("workspace_smtp_writer_gate_config", {})
    _require(
        writer_gate_config.get("target", {}).get("node") == MAIL_NODE,
        f"{profile} SMTP writer-gate config target mismatch",
    )
    _require(
        writer_gate_config.get("path") == "/etc/workspace/smtp-writer-gate.conf",
        f"{profile} SMTP writer-gate config path mismatch",
    )
    _require(
        "on_change" not in writer_gate_config,
        f"{profile} SMTP writer-gate config must not restart Exim",
    )


def _verify_compatibility(manifest, inventory):
    current_backend_image = os.environ["CURRENT_BACKEND_IMAGE_URN"]
    _require(
        IMAGE_URN.fullmatch(current_backend_image),
        "current backend image secret is invalid",
    )
    built_mail_image = _built_mail_image(inventory)
    _built_backend_image(inventory)
    resources, backend_disks = _verify_common(manifest, inventory, built_mail_image)
    _require(
        backend_disks["root"].get("image") == current_backend_image,
        "compatibility manifest does not preserve the current backend image",
    )
    backend_config = _targeted_content(resources, "$core.config.configs", BACKEND_NODE)
    _require("[messenger_storage]" in backend_config, "storage config is missing")
    _require("mode = mail_projection" in backend_config, "compatibility mode mismatch")
    _require(
        "canonical_cutover_confirmed = false" in backend_config,
        "compatibility cutover confirmation mismatch",
    )
    _require("[messenger_mail]" in backend_config, "compatibility mail is missing")
    _require(
        "smtp_host = workspace-mail." in backend_config,
        "compatibility SMTP host drift",
    )
    _require(
        "smtp_security = starttls" in backend_config,
        "compatibility SMTP security drift",
    )
    configs = resources.get("$core.config.configs", {})
    services = resources.get("$core.em.services", {})
    backend_config_resource = configs.get(
        "workspace_backend_config_remote_mail_v1", {}
    )
    _require(
        backend_config_resource.get("target", {}).get("node") == BACKEND_NODE,
        "compatibility backend config identity mismatch",
    )
    _require(
        set(_targeted_services(resources, BACKEND_NODE))
        == COMPATIBILITY_BACKEND_SERVICES,
        "compatibility backend service identities mismatch",
    )
    _require(
        "workspace_smtp_writer_gate_config" not in configs,
        "compatibility manifest provisions the SMTP writer gate",
    )
    _require(
        "workspace_backend_smtp_gate_role_config" not in configs,
        "compatibility manifest provisions the SMTP gate role",
    )
    _require(
        "workspace_smtp_ingress_attester" not in services,
        "compatibility manifest enables the SMTP ingress attester",
    )
    marker = configs.get("workspace_smtp_writer_gate_compatibility_marker", {})
    _require(
        marker.get("target", {}).get("node") == MAIL_NODE,
        "compatibility SMTP marker target mismatch",
    )
    _require(
        marker.get("path") == "/etc/workspace/smtp-writer-gate.compatibility",
        "compatibility SMTP marker path mismatch",
    )
    _require(
        marker.get("on_change")
        == {"kind": "shell", "command": "/usr/local/bin/workspace-mail-reload"},
        "compatibility SMTP marker reload action mismatch",
    )
    _require(
        marker.get("body", {}).get("content") == "compatibility-prestart-v1",
        "compatibility SMTP marker content mismatch",
    )
    _verify_mail_services(
        resources,
        {"workspace_mail_bootstrap", "workspace_mail_ca"},
        "compatibility",
    )
    return built_mail_image


def _verify_stage(manifest, inventory, expected_mail_image):
    built_backend_image = _built_backend_image(inventory)
    _built_mail_image(inventory)
    resources, backend_disks = _verify_common(
        manifest, inventory, expected_mail_image
    )
    _require(
        backend_disks["root"].get("image") == built_backend_image,
        "stage manifest does not select its built backend image",
    )
    backend_config = _targeted_content(resources, "$core.config.configs", BACKEND_NODE)
    _require("[messenger_storage]" in backend_config, "storage config is missing")
    _require("mode = mail_projection" in backend_config, "stage mode mismatch")
    _require(
        "canonical_cutover_confirmed = false" in backend_config,
        "stage cutover confirmation mismatch",
    )
    _require("[messenger_mail]" in backend_config, "stage mail runtime is missing")
    _verify_mail_services(
        resources,
        {
            "workspace_mail_bootstrap",
            "workspace_mail_ca",
            "workspace_smtp_ingress_attester",
        },
        "stage",
    )
    services = resources["$core.em.services"]
    attester = services.get("workspace_smtp_ingress_attester", {})
    _require(
        attester.get("target", {}).get("node") == MAIL_NODE,
        "stage SMTP ingress attester target mismatch",
    )
    _require(
        attester.get("path")
        == "/usr/local/bin/workspace-smtp-ingress-attester run",
        "stage SMTP ingress attester executable mismatch",
    )
    _verify_enforced_mail_guard(resources, "stage")
    return built_backend_image


def _verify_canonical(manifest, inventory, expected_mail_image):
    resources, backend_disks = _verify_common(manifest, inventory, expected_mail_image)
    expected_backend_image = os.environ["STAGE_BACKEND_IMAGE_URN"]
    _require(
        IMAGE_URN.fullmatch(expected_backend_image),
        "stage backend image URN is invalid",
    )
    _require(
        backend_disks["root"].get("image") == expected_backend_image,
        "canonical manifest does not reuse the stage backend image",
    )
    # The build must still contain the backend artifact even though the rendered
    # canonical manifest intentionally selects the already-published stage root.
    # Do not infer image identity from content: exact manifest-root reuse is the
    # compatibility boundary that this verifier must enforce.
    _built_backend_image(inventory)
    _built_mail_image(inventory)
    backend_config = _targeted_content(resources, "$core.config.configs", BACKEND_NODE)
    _require("[messenger_storage]" in backend_config, "storage config is missing")
    _require(
        "mode = postgresql_canonical" in backend_config,
        "canonical mode mismatch",
    )
    _require(
        "canonical_cutover_confirmed = true" in backend_config,
        "canonical cutover confirmation mismatch",
    )
    _require(
        "[messenger_mail]" not in backend_config,
        "canonical backend still contains Messenger mail config",
    )
    _verify_mail_services(resources, set(), "canonical")
    _verify_enforced_mail_guard(resources, "canonical")


def _verify_rollback(manifest, inventory, expected_mail_image):
    resources, backend_disks = _verify_common(manifest, inventory, expected_mail_image)
    expected_backend_image = os.environ["STAGE_BACKEND_IMAGE_URN"]
    _require(
        IMAGE_URN.fullmatch(expected_backend_image),
        "stage backend image URN is invalid",
    )
    _require(
        backend_disks["root"].get("image") == expected_backend_image,
        "rollback manifest does not reuse the stage backend image",
    )
    _built_backend_image(inventory)
    _built_mail_image(inventory)
    backend_config = _targeted_content(resources, "$core.config.configs", BACKEND_NODE)
    _require("[messenger_storage]" in backend_config, "storage config is missing")
    _require("mode = mail_projection" in backend_config, "rollback mode mismatch")
    _require(
        "canonical_cutover_confirmed = false" in backend_config,
        "rollback cutover confirmation mismatch",
    )
    _require("[messenger_mail]" in backend_config, "rollback mail runtime is missing")
    _verify_mail_services(
        resources,
        {
            "workspace_mail_bootstrap",
            "workspace_mail_ca",
            "workspace_smtp_ingress_attester",
        },
        "rollback",
    )
    _verify_enforced_mail_guard(resources, "rollback")


def main():
    _require(len(sys.argv) == 4, "expected profile, manifest, and inventory")
    profile = sys.argv[1]
    _require(
        profile in {"compatibility", "stage", "canonical", "rollback"},
        "unknown verification profile",
    )
    current_mail_image = os.environ["CURRENT_MAIL_IMAGE_URN"]
    _require(IMAGE_URN.fullmatch(current_mail_image), "mail image secret is invalid")
    manifest = _load_manifest(pathlib.Path(sys.argv[2]))
    inventory = _load_inventory(pathlib.Path(sys.argv[3]))

    if profile == "compatibility":
        print(_verify_compatibility(manifest, inventory))
    elif profile == "stage":
        print(
            _verify_stage(
                manifest, inventory, os.environ["COMPATIBILITY_MAIL_IMAGE_URN"]
            )
        )
    elif profile == "canonical":
        _verify_canonical(
            manifest, inventory, os.environ["COMPATIBILITY_MAIL_IMAGE_URN"]
        )
    else:
        _verify_rollback(
            manifest, inventory, os.environ["COMPATIBILITY_MAIL_IMAGE_URN"]
        )


if __name__ == "__main__":
    try:
        main()
    except (KeyError, TypeError, VerificationError):
        print("Production migration artifact verification failed", file=sys.stderr)
        raise SystemExit(1)
