# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Worker-owned boundary for applying deterministic scale fixture units."""

import importlib
import json
import os
import pathlib
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.storage.sql import engines

from workspace.tests.scale import fixture


CREDENTIALS_SCHEMA_VERSION = "workspace.messenger.fixture-credentials/v2"
RUN_SCHEMA_VERSION = "workspace.messenger.fixture-application-run/v1"
CLEANUP_SCHEMA_VERSION = "workspace.messenger.fixture-cleanup/v1"


def _atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_credentials(path, target, project_id, run_id):
    values = json.loads(path.read_text(encoding="utf-8"))
    if values["schema_version"] != CREDENTIALS_SCHEMA_VERSION:
        raise ValueError("unsupported fixture credential schema")
    if values["environment"] != "isolated_test":
        raise ValueError("fixture application requires an isolated test target")
    if values["target_sha256"] != fixture.sha256(target.encode("utf-8")):
        raise ValueError("fixture target does not match the credential bundle")
    if values["test_project_id"] != str(project_id):
        raise ValueError("fixture project does not match the credential bundle")
    if values["run_id"] != str(run_id):
        raise ValueError("fixture run does not match the credential bundle")
    mappings = values.get("workspace_identity_mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("fixture credentials require private IAM identity mappings")
    ordinals = set()
    logical_users = set()
    iam_users = set()
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) != {
            "ordinal",
            "logical_user_uuid",
            "iam_user_uuid",
            "access_token",
        }:
            raise ValueError("fixture IAM identity mapping schema is invalid")
        if (
            not isinstance(mapping["ordinal"], int)
            or mapping["ordinal"] < 0
            or not isinstance(mapping["access_token"], str)
            or not mapping["access_token"]
        ):
            raise ValueError("fixture IAM identity mapping value is invalid")
        logical_uuid = str(sys_uuid.UUID(mapping["logical_user_uuid"]))
        iam_uuid = str(sys_uuid.UUID(mapping["iam_user_uuid"]))
        if (
            mapping["ordinal"] in ordinals
            or logical_uuid in logical_users
            or iam_uuid in iam_users
        ):
            raise ValueError("fixture IAM identity mappings must be unique")
        ordinals.add(mapping["ordinal"])
        logical_users.add(logical_uuid)
        iam_users.add(iam_uuid)
    account_credentials = values.get("external_account_credentials")
    if not isinstance(account_credentials, list):
        raise ValueError("fixture credentials require private external accounts")
    credential_refs = set()
    for credential in account_credentials:
        if not isinstance(credential, dict) or set(credential) != {
            "credential_ref",
            "server_url",
            "email",
            "api_key",
        }:
            raise ValueError("fixture external account credential schema is invalid")
        if any(
            not isinstance(credential[name], str) or not credential[name]
            for name in credential
        ):
            raise ValueError("fixture external account credential value is invalid")
        if credential["credential_ref"] in credential_refs:
            raise ValueError("fixture external account credentials must be unique")
        credential_refs.add(credential["credential_ref"])
    return values


def _validate_profile_identity_mappings(credentials, profile):
    logical_users = fixture._ordered_ids(
        profile["seed"],
        "user",
        profile["dimensions"]["users"],
    )
    mappings = sorted(
        credentials["workspace_identity_mappings"],
        key=lambda mapping: mapping["ordinal"],
    )
    if len(mappings) != len(logical_users):
        raise ValueError("fixture IAM identity mapping count does not match profile")
    for ordinal, (mapping, logical_user_uuid) in enumerate(
        zip(mappings, logical_users)
    ):
        if (
            mapping["ordinal"] != ordinal
            or mapping["logical_user_uuid"] != logical_user_uuid
        ):
            raise ValueError(
                "fixture IAM identity mapping does not match logical fixture"
            )
    expected_credential_refs = {
        f"zulip-account-{ordinal:03d}"
        for ordinal in range(profile["dimensions"]["zulip_accounts"])
    }
    actual_credential_refs = {
        value["credential_ref"] for value in credentials["external_account_credentials"]
    }
    if actual_credential_refs != expected_credential_refs:
        raise ValueError(
            "fixture external account credential coverage does not match profile"
        )


def _load_concrete_adapter(credentials):
    adapter_path = os.environ.get("WORKSPACE_FIXTURE_CONCRETE_ADAPTER")
    if adapter_path is None:
        raise ValueError("fixture apply requires a concrete application adapter")
    module_name, callable_name = adapter_path.rsplit(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, callable_name)(credentials=credentials)


class ApplicationRunner:
    """Apply resumable units; every adapter call gets one outer transaction."""

    def __init__(self, adapter, session_context_factory):
        self._adapter = adapter
        self._session_context_factory = session_context_factory

    def _transaction(self, method, *args):
        with self._session_context_factory() as session:
            return method(session, *args)

    def _apply_unit(self, run, unit):
        with self._session_context_factory() as session:
            result = self._adapter.apply_unit(session, run, unit)
            if result["unit_id"] != unit["unit_id"]:
                raise ValueError("adapter acknowledged a different fixture unit")

    def run(self, run, units):
        prepared = self._transaction(self._adapter.prepare, run)
        completed = set(prepared["completed_unit_ids"])
        declared = {unit["unit_id"] for unit in units}
        if not completed.issubset(declared):
            raise ValueError("adapter returned units outside this fixture plan")
        for unit in units:
            if unit["unit_id"] in completed:
                continue
            self._apply_unit(run, unit)
        verified = self._transaction(self._adapter.prepare, run)
        if set(verified["completed_unit_ids"]) != declared:
            raise ValueError("adapter did not durably complete every fixture unit")
        observed = self._transaction(self._adapter.export_observed, run)
        cleanup = self._transaction(self._adapter.cleanup_manifest, run)
        return list(observed), cleanup

    def export_inventory(self, run):
        return self._transaction(self._adapter.export_inventory, run)


def apply_fixture(
    *,
    profile,
    manifest_path,
    expected_ledger_path,
    application_plan_path,
    target,
    credentials_path,
    project_id,
    run_id,
    output_directory,
):
    """Apply through one explicit adapter; never synthesize canonical SQL here."""
    credentials = _load_credentials(
        credentials_path,
        target,
        project_id,
        run_id,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["schema_version"] != fixture.SCHEMA_VERSION:
        raise ValueError("unsupported fixture manifest schema")
    if manifest["profile_id"] != profile["profile_id"]:
        raise ValueError("fixture profile ID does not match manifest")
    if manifest["seed"] != profile["seed"]:
        raise ValueError("fixture profile seed does not match manifest")
    if manifest["profile_sha256"] != fixture.sha256(profile):
        raise ValueError("fixture profile digest does not match manifest")
    _validate_profile_identity_mappings(credentials, profile)
    if (
        fixture.sha256(application_plan_path.read_bytes())
        != manifest["application_plan"]["sha256"]
    ):
        raise ValueError("fixture application plan digest does not match manifest")
    units = [row for _, row in fixture.read_json_lines(application_plan_path)]
    if len(units) != manifest["application_plan"]["units"]:
        raise ValueError("fixture application plan row count does not match manifest")
    for unit_index, unit in enumerate(units):
        if unit["schema_version"] != fixture.APPLICATION_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported fixture application unit schema")
        if unit["unit_index"] != unit_index:
            raise ValueError("fixture application unit order is not contiguous")
        if unit["records_sha256"] != fixture.sha256(unit["records"]):
            raise ValueError("fixture application unit record digest does not match")
        if (
            unit["unit_id"]
            != fixture._application_unit(
                profile["seed"],
                unit_index,
                unit["records"],
            )["unit_id"]
        ):
            raise ValueError("fixture application unit ID does not match its records")
    if (
        fixture.sha256(expected_ledger_path.read_bytes())
        != manifest["correctness_ledger"]["sha256"]
    ):
        raise ValueError("fixture expected ledger digest does not match manifest")
    expected_rows = [row for _, row in fixture.read_json_lines(expected_ledger_path)]
    if len(expected_rows) != manifest["correctness_ledger"]["rows"]:
        raise ValueError("fixture expected ledger row count does not match manifest")
    if any(
        row["schema_version"] != fixture.LEDGER_SCHEMA_VERSION for row in expected_rows
    ):
        raise ValueError("unsupported fixture expected ledger schema")
    adapter = _load_concrete_adapter(credentials)
    binder = getattr(adapter, "bind_inventory_contract", None)
    if binder is None:
        raise ValueError("fixture adapter does not provide actual inventory export")
    binder(manifest, units)
    run = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": str(run_id),
        "test_project_id": str(project_id),
        "profile_id": profile["profile_id"],
        "profile_sha256": manifest["profile_sha256"],
        "manifest_sha256": fixture.sha256(manifest_path.read_bytes()),
        "expected_ledger_sha256": fixture.sha256(expected_ledger_path.read_bytes()),
        "application_plan_sha256": manifest["application_plan"]["sha256"],
    }
    engines.engine_factory.configure_factory(db_url=target)
    try:
        runner = ApplicationRunner(
            adapter,
            lambda: contexts.Context().session_manager(),
        )
        observed, cleanup = runner.run(run, units)
        inventory = runner.export_inventory(run)
    finally:
        engines.engine_factory.destroy_all_engines()
    output_directory = pathlib.Path(output_directory)
    for row in observed:
        if row["schema_version"] != fixture.RUN_LEDGER_SCHEMA_VERSION:
            raise ValueError("adapter exported an unsupported observed ledger row")
        if row["run_id"] != str(run_id):
            raise ValueError("adapter exported an observation for another run")
    observed_path = output_directory / "observed-run-ledger.jsonl"
    fixture.write_json_lines(observed_path, observed)
    cleanup_path = output_directory / "cleanup-manifest.json"
    inventory_path = output_directory / "actual-inventory.json"
    _atomic_json(inventory_path, inventory)
    cleanup_document = {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "run_id": str(run_id),
        "test_project_id": str(project_id),
        "adapter": cleanup,
    }
    _atomic_json(cleanup_path, cleanup_document)
    _atomic_json(
        output_directory / "fixture-application-result.json",
        {
            **run,
            "observed_ledger": observed_path.name,
            "observed_rows": len(observed),
            "cleanup_manifest": cleanup_path.name,
            "actual_inventory": inventory_path.name,
            "completed": True,
        },
    )
