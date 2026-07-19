#!/usr/bin/env python3
# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Generate a deterministic, content-free Messenger fixture plan."""

import argparse
import importlib
import json
import os
import pathlib
import uuid as sys_uuid

from workspace.tests.scale import fixture


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_PROFILE = pathlib.Path(__file__).with_name("profiles") / "db-ci-v1.json"


def _outside_repository(path):
    try:
        path.resolve().relative_to(REPOSITORY_ROOT)
    except ValueError:
        return True
    return False


def _arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Write fixture-manifest.json and expected-ledger.jsonl without "
            "contacting Workspace, Zulip, PostgreSQL, or S3."
        )
    )
    parser.add_argument("--profile", type=pathlib.Path, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=os.environ.get("WORKSPACE_FIXTURE_OUTPUT_DIR"),
        required=os.environ.get("WORKSPACE_FIXTURE_OUTPUT_DIR") is None,
        help="Artifact directory outside the repository.",
    )
    parser.add_argument("--seed", type=int, help="Override the immutable profile seed.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Invoke the explicitly configured application-boundary adapter.",
    )
    parser.add_argument(
        "--project-id",
        type=sys_uuid.UUID,
        help="Explicit isolated test project UUID; required with --apply.",
    )
    parser.add_argument(
        "--run-id",
        type=sys_uuid.UUID,
        help="Unique fixture application run UUID; required with --apply.",
    )
    return parser.parse_args()


def _application_boundary():
    if os.environ.get("WORKSPACE_FIXTURE_EXECUTE") != "1":
        raise SystemExit("apply requires WORKSPACE_FIXTURE_EXECUTE=1")
    target = os.environ.get("WORKSPACE_FIXTURE_TARGET")
    credentials = os.environ.get("WORKSPACE_FIXTURE_CREDENTIALS_FILE")
    if not target or not credentials:
        raise SystemExit("apply requires target and credential file")
    credentials_path = pathlib.Path(credentials)
    if credentials_path.stat().st_mode & 0o077:
        raise SystemExit("fixture credential file must not be group/world accessible")
    module = importlib.import_module("workspace.tests.scale.application_boundary")
    return module.apply_fixture, target, credentials_path


def main():
    args = _arguments()
    if not _outside_repository(args.output_dir):
        raise SystemExit("fixture artifacts must be written outside the repository")
    profile = fixture.load_profile(args.profile)
    if args.seed is not None:
        profile["seed"] = args.seed
    boundary = None
    if args.apply:
        if args.project_id is None or args.run_id is None:
            raise SystemExit("apply requires --project-id and --run-id")
        boundary = _application_boundary()
    # The logical manifest remains a dry-run artifact until the fixed boundary
    # writes a separate completed application result. A failed adapter must not
    # leave an artifact that claims the fixture was applied.
    manifest = fixture.build_fixture(profile, args.output_dir, dry_run=True)
    if boundary is not None:
        adapter, target, credentials_path = boundary
        adapter(
            profile=profile,
            manifest_path=args.output_dir / "fixture-manifest.json",
            expected_ledger_path=args.output_dir
            / manifest["correctness_ledger"]["path"],
            application_plan_path=args.output_dir
            / manifest["application_plan"]["path"],
            target=target,
            credentials_path=credentials_path,
            project_id=args.project_id,
            run_id=args.run_id,
            output_directory=args.output_dir,
        )
    print(
        json.dumps(
            {
                "dry_run": not args.apply,
                "manifest": str(args.output_dir / "fixture-manifest.json"),
                "ledger": str(args.output_dir / manifest["correctness_ledger"]["path"]),
                "profile_id": manifest["profile_id"],
                "seed": manifest["seed"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
