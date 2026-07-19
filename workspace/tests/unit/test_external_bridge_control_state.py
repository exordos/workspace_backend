# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import pathlib
import uuid as sys_uuid

import pytest
import yaml

from workspace.external_bridge_control import pki
from workspace.external_bridge_control import state


REALM_UUID = sys_uuid.UUID("11111111-1111-4111-8111-111111111111")
INSTANCE_UUID = sys_uuid.UUID("22222222-2222-4222-8222-222222222222")
OTHER_INSTANCE_UUID = sys_uuid.UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime.datetime(2026, 1, 10, tzinfo=datetime.timezone.utc)
ROOT = pathlib.Path(__file__).parents[3]


def _identity(instance_uuid=INSTANCE_UUID):
    return pki.BridgeIdentity(
        realm_uuid=REALM_UUID,
        provider_kind="zulip",
        bridge_instance_uuid=instance_uuid,
        identity_generation=1,
        uri_san="test",
    )


def _control_state(tmp_path):
    repository = state.PersistentControlState(tmp_path / "state", REALM_UUID)
    repository.initialize()
    return repository


def _account(resource_uuid=None, generation=1):
    resource_uuid = resource_uuid or sys_uuid.uuid4()
    return {
        "resource_type": "external_account",
        "uuid": str(resource_uuid),
        "generation": generation,
        "owner_user_uuid": str(sys_uuid.uuid4()),
        "settings": {
            "kind": "zulip",
            "server_url": "https://zulip.example.test",
            "selection_mode": "explicit",
            "history_depth": "30_days",
            "default_project_id": str(sys_uuid.uuid4()),
        },
        "synchronization_enabled": True,
        "credential_envelope": None,
    }


def _chat(account_uuid, resource_uuid=None, generation=1):
    return {
        "resource_type": "external_chat_assignment",
        "uuid": str(resource_uuid or sys_uuid.uuid4()),
        "generation": generation,
        "external_account_uuid": str(account_uuid),
        "provider_chat": {
            "kind": "zulip",
            "chat_type": "channel",
            "provider_chat_key": "engineering",
        },
        "project_id": str(sys_uuid.uuid4()),
        "selected": True,
    }


def _custom_ca(resource_uuid=None, generation=1):
    return {
        "resource_type": "custom_ca_bundle",
        "uuid": str(resource_uuid or sys_uuid.uuid4()),
        "generation": generation,
        "name": "provider-ca",
        "pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
    }


def test_control_schema_version_matches_runtime_and_openapi_contract(tmp_path):
    specification = yaml.safe_load(
        (ROOT / "docs/zulip_bridge_control_api_v1.yaml").read_text(encoding="utf-8")
    )
    documented_versions = specification["components"]["schemas"][
        "ControlSchemaVersion"
    ]["enum"]
    repository = _control_state(tmp_path)
    identity = _identity()
    cursor = repository.initial_cursor(identity)

    batch = repository.changes(identity, cursor, now=NOW)
    snapshot, _ = repository.create_snapshot(identity, sys_uuid.uuid4(), now=NOW)
    after_snapshot = repository.changes(identity, snapshot["anchor_cursor"], now=NOW)

    assert documented_versions == ["v1"]
    assert state.CONTROL_SCHEMA_VERSION == documented_versions[0]
    assert batch["control_schema_version"] == documented_versions[0]
    assert after_snapshot["control_schema_version"] == documented_versions[0]

    persisted = repository._read()
    old_payload = repository._verify(persisted, cursor)
    old_payload["control_schema_version"] = "1"
    old_cursor = repository._sign(persisted, old_payload)
    with pytest.raises(state.CursorExpiredError) as raised:
        repository.changes(identity, old_cursor, now=NOW)
    assert raised.value.reason == "schema_mismatch"


def test_custom_ca_bundle_is_in_default_changes_and_snapshot(tmp_path):
    repository = _control_state(tmp_path)
    identity = _identity()
    cursor = repository.initial_cursor(identity)
    custom_ca = _custom_ca()
    repository.upsert_resource(identity, custom_ca, now=NOW)

    batch = repository.changes(identity, cursor, now=NOW)
    assert [item["resource_type"] for item in batch["changes"]] == ["custom_ca_bundle"]
    snapshot, _ = repository.create_snapshot(identity, sys_uuid.uuid4(), now=NOW)
    page = repository.snapshot_page(identity, snapshot["snapshot_token"], now=NOW)
    assert [item["resource_type"] for item in page["resources"]] == ["custom_ca_bundle"]


def test_unrelated_identity_sequence_gap_is_not_cursor_expiry(tmp_path):
    repository = _control_state(tmp_path)
    cursor = repository.initial_cursor(_identity())
    repository.upsert_resource(_identity(OTHER_INSTANCE_UUID), _account(), now=NOW)
    wanted = _account()
    repository.upsert_resource(_identity(), wanted, now=NOW)

    batch = repository.changes(_identity(), cursor, now=NOW)
    assert [item["resource_uuid"] for item in batch["changes"]] == [wanted["uuid"]]


def test_unselected_resource_type_sequence_gap_is_not_cursor_expiry(tmp_path):
    repository = _control_state(tmp_path)
    identity = _identity()
    cursor = repository.initial_cursor(identity, ("external_account",))
    account = _account()
    repository.upsert_resource(identity, _chat(account["uuid"]), now=NOW)
    repository.upsert_resource(identity, account, now=NOW)

    batch = repository.changes(identity, cursor, ("external_account",), now=NOW)
    assert [item["resource_type"] for item in batch["changes"]] == ["external_account"]


def test_pruned_relevant_change_without_later_change_returns_410_signal(tmp_path):
    repository = _control_state(tmp_path)
    identity = _identity()
    cursor = repository.initial_cursor(identity, ("external_account",))
    repository.upsert_resource(
        identity,
        _account(),
        now=NOW - state.CHANGE_RETENTION - datetime.timedelta(seconds=1),
    )

    with pytest.raises(state.CursorExpiredError) as raised:
        repository.changes(identity, cursor, ("external_account",), now=NOW)
    assert raised.value.reason == "retention"


def test_selected_multi_type_cursor_uses_maximum_pruned_watermark(tmp_path):
    repository = _control_state(tmp_path)
    identity = _identity()
    account = _account()
    repository.upsert_resource(
        identity,
        account,
        now=NOW - state.CHANGE_RETENTION - datetime.timedelta(seconds=2),
    )
    snapshot, _ = repository.create_snapshot(
        identity,
        sys_uuid.uuid4(),
        ("external_account", "external_chat_assignment"),
        now=NOW - datetime.timedelta(seconds=1),
    )
    repository.upsert_resource(
        identity,
        _chat(account["uuid"]),
        now=NOW - state.CHANGE_RETENTION - datetime.timedelta(seconds=1),
    )

    with pytest.raises(state.CursorExpiredError) as raised:
        repository.changes(
            identity,
            snapshot["anchor_cursor"],
            ("external_account", "external_chat_assignment"),
            now=NOW,
        )
    assert raised.value.reason == "retention"
