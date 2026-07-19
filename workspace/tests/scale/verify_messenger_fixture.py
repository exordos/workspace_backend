#!/usr/bin/env python3
# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Verify deterministic fixture manifests and delivery correctness ledgers."""

import argparse
import json
import pathlib

import fixture


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--observed-ledger", type=pathlib.Path)
    parser.add_argument("--actual-inventory", type=pathlib.Path)
    parser.add_argument("--application-result", type=pathlib.Path)
    parser.add_argument(
        "--expected-run-ledger",
        action="append",
        default=[],
        type=pathlib.Path,
    )
    parser.add_argument(
        "--observed-run-ledger",
        action="append",
        default=[],
        type=pathlib.Path,
    )
    parser.add_argument("--report", required=True, type=pathlib.Path)
    return parser.parse_args()


def main():
    args = _arguments()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest["schema_version"] != fixture.SCHEMA_VERSION:
        raise SystemExit("unsupported fixture manifest schema")

    expected_ledger = args.manifest.parent / manifest["correctness_ledger"]["path"]
    if (
        fixture.sha256(expected_ledger.read_bytes())
        != manifest["correctness_ledger"]["sha256"]
    ):
        raise SystemExit("expected correctness ledger digest does not match manifest")
    expected_provider_ledgers = fixture.provider_persistence_ledger_rows(
        [row for _, row in fixture.read_json_lines(expected_ledger)]
    )

    inventory = None
    inventory_matches = False
    if args.actual_inventory is not None:
        if args.application_result is None:
            raise SystemExit("actual inventory requires its application result")
        inventory = json.loads(args.actual_inventory.read_text(encoding="utf-8"))
        if inventory.get("schema_version") != (
            "workspace.messenger.fixture-actual-inventory/v1"
        ):
            raise SystemExit("unsupported actual inventory schema")
        application_result = json.loads(
            args.application_result.read_text(encoding="utf-8")
        )
        manifest_sha256 = fixture.sha256(args.manifest.read_bytes())
        binding_matches = all(
            (
                application_result.get("schema_version")
                == "workspace.messenger.fixture-application-run/v1",
                application_result.get("completed") is True,
                application_result.get("profile_id") == manifest["profile_id"],
                application_result.get("manifest_sha256") == manifest_sha256,
                application_result.get("actual_inventory")
                == args.actual_inventory.name,
                inventory.get("profile_id") == manifest["profile_id"],
                inventory.get("manifest_sha256") == manifest_sha256,
                inventory.get("run_id") == application_result.get("run_id"),
                inventory.get("test_project_id")
                == application_result.get("test_project_id"),
            )
        )
        section_matches = all(
            inventory.get(name) == manifest[name]
            for name in (
                "expected_row_counts",
                "relationship_counts",
                "normalized_digests",
                "canonical_event_age_buckets",
                "provider_stream_mappings",
                "provider_mapping_counts",
            )
        )
        expected_s3 = {
            (
                row["uuid"],
                row["object_name"],
                row["binary_sha256"],
                row["sidecar_object_name"],
                row["sidecar_sha256"],
            )
            for row in manifest["s3_objects"]
        }
        actual_s3 = {
            (
                row["uuid"],
                row["object_name"],
                row["binary_sha256"],
                row["sidecar_object_name"],
                row["sidecar_sha256"],
            )
            for row in inventory.get("s3_objects", ())
        }
        inventory_matches = all(
            (
                binding_matches,
                section_matches,
                all(
                    inventory.get(name) == expected_rows
                    for name, expected_rows in expected_provider_ledgers.items()
                ),
                actual_s3 == expected_s3,
                inventory.get("status") == "PASS",
                inventory.get("passed") is True,
                inventory.get("mismatches") == [],
                inventory.get("extra_project_objects") == [],
                inventory.get("storage_faults") == [],
            )
        )

    report = {
        "schema_version": "workspace.messenger.fixture-inventory-report/v1",
        "passed": False,
        "profile_id": manifest["profile_id"],
        "inventory": (
            inventory
            if inventory is not None
            else {
                "status": "BLOCKED",
                "reason": "actual PostgreSQL and S3 inventory was not supplied",
            }
        ),
        "correctness": "NOT RUN",
    }
    if args.observed_ledger is not None:
        report["correctness"] = fixture.verify_ledgers(
            expected_ledger,
            args.observed_ledger,
        )

    report["passed"] = bool(
        inventory_matches
        and (
            args.observed_ledger is None or report["correctness"].get("passed") is True
        )
    )

    if args.expected_run_ledger or args.observed_run_ledger:
        if not args.expected_run_ledger or not args.observed_run_ledger:
            raise SystemExit(
                "run verification requires expected and observed run ledgers"
            )
        report["run_correctness"] = fixture.verify_run_ledgers(
            args.expected_run_ledger,
            args.observed_run_ledger,
        )
        report["passed"] = bool(
            report["passed"] and report["run_correctness"].get("passed") is True
        )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"passed": report["passed"], "report": str(args.report)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
