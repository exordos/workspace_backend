# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import collections
import inspect
import json
import pathlib
import subprocess
import sys
import types
import uuid as sys_uuid

import pytest

import workspace.tests.scale.fixture as fixture
from workspace.external_bridge_control import provider_data, sql_state
from workspace.tests.scale import application_boundary
from workspace.tests.scale import concrete_adapter
from workspace.tests.scale import concrete_inventory
from workspace.messenger_api import file_storage


PROFILE_DIRECTORY = pathlib.Path(__file__).parents[1] / "scale" / "profiles"
LOAD_DIRECTORY = pathlib.Path(__file__).parents[1] / "load"


def _small_profile():
    return {
        "schema_version": fixture.PROFILE_SCHEMA_VERSION,
        "profile_id": "test-v1",
        "seed": 17,
        "dimensions": {
            "users": 6,
            "streams": {"channel": 2, "group_dm": 1, "direct_dm": 1},
            "topics": 8,
            "messages": 7,
            "provider_messages": 3,
            "reactions": 2,
            "files": 2,
            "visible_event_deliveries": 72,
            "live_users": 3,
            "zulip_accounts": 3,
            "provider_synced_streams": 3,
        },
        "membership": {"channel": 4, "group_dm": 3, "direct_dm": 2},
        "message_mix": {"channel": 4, "group_dm": 2, "direct_dm": 1},
        "distribution": {"kind": "zipf", "exponent": 1.15},
        "provider_workload": {
            "steady_messages_per_minute": {
                "minimum": 100,
                "target": 150,
                "maximum": 200,
            },
            "steady_minutes": 30,
            "burst_messages_per_minute": 400,
            "burst_minutes": 5,
            "inbound_ratio": 0.6,
            "burst_reconnect_ratio": 0.2,
        },
    }


def _credential_bundle(profile, target, project_id, run_id):
    logical_users = fixture._ordered_ids(
        profile["seed"],
        "user",
        profile["dimensions"]["users"],
    )
    return {
        "schema_version": application_boundary.CREDENTIALS_SCHEMA_VERSION,
        "environment": "isolated_test",
        "target_sha256": fixture.sha256(target.encode()),
        "test_project_id": str(project_id),
        "run_id": str(run_id),
        "workspace_identity_mappings": [
            {
                "ordinal": ordinal,
                "logical_user_uuid": logical_user_uuid,
                "iam_user_uuid": fixture.stable_uuid(
                    profile["seed"],
                    "iam-user",
                    ordinal,
                ),
                "access_token": f"private-test-token-{ordinal}",
            }
            for ordinal, logical_user_uuid in enumerate(logical_users)
        ],
        "external_account_credentials": [
            {
                "credential_ref": f"zulip-account-{ordinal:03d}",
                "server_url": "https://zulip.fixture.invalid",
                "email": f"fixture-{ordinal}@example.invalid",
                "api_key": f"private-api-key-{ordinal}",
            }
            for ordinal in range(profile["dimensions"]["zulip_accounts"])
        ],
    }


def _plan_records(path):
    return [
        record
        for _, unit in fixture.read_json_lines(path)
        for record in unit["records"]
    ]


def test_full_profile_has_exact_required_concurrency_and_provider_shape():
    profile = fixture.load_profile(PROFILE_DIRECTORY / "db-300x120x100k-v1.json")

    assert profile["dimensions"]["live_users"] == 150
    assert profile["dimensions"]["zulip_accounts"] == 150
    assert profile["dimensions"]["provider_synced_streams"] == 90
    assert profile["provider_workload"]["steady_messages_per_minute"] == {
        "minimum": 100,
        "target": 150,
        "maximum": 200,
    }
    assert profile["provider_workload"]["burst_messages_per_minute"] == 400


def test_provider_stream_owner_is_authorized_and_mapping_is_unique():
    profile = _small_profile()
    users = fixture._ordered_ids(profile["seed"], "user", 6)
    accounts = [
        {
            "uuid": fixture.stable_uuid(profile["seed"], "zulip-account", index),
            "owner_user_uuid": user_uuid,
        }
        for index, user_uuid in enumerate(users[:3])
    ]

    streams = fixture._stream_rows(profile, users, accounts)
    provider_streams = [row for row in streams if row["provider_synced"]]

    assert len({row["external_account_uuid"] for row in provider_streams}) == 3
    assert all(row["owner_user_uuid"] in row["members"] for row in provider_streams)


def test_fixture_manifest_and_ledger_are_deterministic(tmp_path):
    first = fixture.build_fixture(_small_profile(), tmp_path / "first")
    second = fixture.build_fixture(_small_profile(), tmp_path / "second")

    assert first == second
    assert first["expected_row_counts"] == {
        "users": 6,
        "streams": 4,
        "topics": 8,
        "messages": 7,
        "provider_messages": 3,
        "reactions": 2,
        "files": 2,
        "canonical_broadcast_events": 21,
        "visible_event_deliveries": 72,
        "zulip_accounts": 3,
    }
    assert first["correctness_ledger"]["checks"] == [
        "loss",
        "duplication",
        "cross_account",
        "owner",
        "mapping",
        "direction",
        "payload_integrity",
        "cursor_monotonicity",
        "outbox_idempotency",
    ]
    assert first["application_plan"] == second["application_plan"]
    plan_path = tmp_path / "first" / first["application_plan"]["path"]
    assert fixture.sha256(plan_path.read_bytes()) == first["application_plan"]["sha256"]
    units = [row for _, row in fixture.read_json_lines(plan_path)]
    assert len(units) == first["application_plan"]["units"]
    assert all(
        unit["schema_version"] == fixture.APPLICATION_PLAN_SCHEMA_VERSION
        for unit in units
    )
    assert len({unit["unit_id"] for unit in units}) == len(units)
    mappings = first["provider_stream_mappings"]
    assert len(mappings) == 3
    assert len({row["stream_uuid"] for row in mappings}) == 3
    assert len({row["external_account_uuid"] for row in mappings}) == 3
    ledger_rows = [
        json.loads(line)
        for line in (tmp_path / "first" / "expected-ledger.jsonl")
        .read_text()
        .splitlines()
    ]
    account_by_stream = {
        row["stream_uuid"]: row["external_account_uuid"] for row in mappings
    }
    assert all(
        row["account_uuid"] == account_by_stream[row["stream_uuid"]]
        for row in ledger_rows
    )
    sidecar = first["s3_objects"][0]["sidecar"]
    assert set(sidecar) == {
        "acl",
        "content_type",
        "created_at",
        "description",
        "name",
        "owner_uuid",
        "project_id",
        "schema_version",
        "sha256",
        "size_bytes",
        "stream_uuid",
        "uuid",
    }
    assert sidecar["schema_version"] == 1
    assert sidecar["acl"] == {
        "mode": "stream_members",
        "stream_uuid": sidecar["stream_uuid"],
    }
    metadata = file_storage.WorkspaceFileMetadata.from_json(
        fixture.canonical_json(sidecar).encode()
    )
    assert json.loads(metadata.to_json()) == sidecar
    assert (tmp_path / "first" / "expected-ledger.jsonl").read_bytes() == (
        tmp_path / "second" / "expected-ledger.jsonl"
    ).read_bytes()
    assert (
        plan_path.read_bytes()
        == (tmp_path / "second" / first["application_plan"]["path"]).read_bytes()
    )


def test_inventory_verifier_fails_closed_without_actual_postgresql_and_s3(
    tmp_path,
):
    fixture.build_fixture(_small_profile(), tmp_path)
    report_path = tmp_path / "inventory-report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(PROFILE_DIRECTORY.parent / "verify_messenger_fixture.py"),
            "--manifest",
            str(tmp_path / "fixture-manifest.json"),
            "--report",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["inventory"]["status"] == "BLOCKED"
    assert report["correctness"] == "NOT RUN"
    assert "expected_row_counts" not in report
    assert "normalized_digests" not in report


def test_inventory_verifier_recomputes_manifest_and_run_binding(tmp_path):
    manifest = fixture.build_fixture(_small_profile(), tmp_path)
    manifest_path = tmp_path / "fixture-manifest.json"
    inventory_path = tmp_path / "actual-inventory.json"
    application_result_path = tmp_path / "fixture-application-result.json"
    report_path = tmp_path / "inventory-report.json"
    run_id = str(sys_uuid.uuid4())
    project_id = str(sys_uuid.uuid4())
    manifest_sha256 = fixture.sha256(manifest_path.read_bytes())
    expected_provider_ledgers = fixture.provider_persistence_ledger_rows(
        [
            row
            for _, row in fixture.read_json_lines(
                tmp_path / manifest["correctness_ledger"]["path"]
            )
        ]
    )
    inventory = {
        "schema_version": "workspace.messenger.fixture-actual-inventory/v1",
        "run_id": run_id,
        "test_project_id": project_id,
        "profile_id": manifest["profile_id"],
        "manifest_sha256": manifest_sha256,
        "status": "PASS",
        "passed": True,
        "expected_row_counts": manifest["expected_row_counts"],
        "relationship_counts": manifest["relationship_counts"],
        "normalized_digests": manifest["normalized_digests"],
        "canonical_event_age_buckets": manifest["canonical_event_age_buckets"],
        "s3_objects": manifest["s3_objects"],
        "provider_stream_mappings": manifest["provider_stream_mappings"],
        "provider_mapping_counts": manifest["provider_mapping_counts"],
        **expected_provider_ledgers,
        "extra_project_objects": [],
        "storage_faults": [],
        "mismatches": [],
    }
    application_result = {
        "schema_version": application_boundary.RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "test_project_id": project_id,
        "profile_id": manifest["profile_id"],
        "manifest_sha256": manifest_sha256,
        "actual_inventory": inventory_path.name,
        "completed": True,
    }

    def verify(current_inventory):
        inventory_path.write_text(
            json.dumps(current_inventory),
            encoding="utf-8",
        )
        application_result_path.write_text(
            json.dumps(application_result),
            encoding="utf-8",
        )
        return subprocess.run(
            [
                sys.executable,
                str(PROFILE_DIRECTORY.parent / "verify_messenger_fixture.py"),
                "--manifest",
                str(manifest_path),
                "--actual-inventory",
                str(inventory_path),
                "--application-result",
                str(application_result_path),
                "--report",
                str(report_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    assert verify(inventory).returncode == 0

    stale = json.loads(json.dumps(inventory))
    stale["run_id"] = str(sys_uuid.uuid4())
    stale["passed"] = True
    assert verify(stale).returncode == 1

    tampered = json.loads(json.dumps(inventory))
    tampered["expected_row_counts"]["messages"] += 1
    tampered["passed"] = True
    tampered["status"] = "PASS"
    tampered["mismatches"] = []
    assert verify(tampered).returncode == 1

    mapping_tampered = json.loads(json.dumps(inventory))
    mapping_tampered["provider_stream_mappings"][0]["provider_chat_key"] = (
        "wrong-provider-chat"
    )
    mapping_tampered["passed"] = True
    mapping_tampered["status"] = "PASS"
    mapping_tampered["mismatches"] = []
    assert verify(mapping_tampered).returncode == 1

    ledger_tampered = json.loads(json.dumps(inventory))
    ledger_tampered["provider_operation_ledger"][0]["queue_status"] = "succeeded"
    ledger_tampered["passed"] = True
    ledger_tampered["status"] = "PASS"
    ledger_tampered["mismatches"] = []
    assert verify(ledger_tampered).returncode == 1


def test_actual_inventory_reconstructs_provider_ledgers_from_observed_rows():
    logical_user_uuid = str(sys_uuid.uuid4())
    iam_user_uuid = str(sys_uuid.uuid4())
    project_uuid = str(sys_uuid.uuid4())
    account_uuid = str(sys_uuid.uuid4())
    stream_uuid = str(sys_uuid.uuid4())
    topic_uuid = str(sys_uuid.uuid4())
    inbound_message_uuid = str(sys_uuid.uuid4())
    outbound_message_uuid = str(sys_uuid.uuid4())
    provider_event_uuid = str(sys_uuid.uuid4())
    external_operation_uuid = str(sys_uuid.uuid4())
    inbound_payload = {"kind": "markdown", "content": "fixture message 0"}
    outbound_payload = {"kind": "markdown", "content": "fixture message 1"}
    inbound_event = {
        "provider_event_uuid": provider_event_uuid,
        "external_account_uuid": account_uuid,
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": "logical-project",
        "provider_sequence": "1",
        "kind": "message.upsert",
        "payload": {
            "resource": {
                "uuid": inbound_message_uuid,
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "user_uuid": logical_user_uuid,
                "payload": inbound_payload,
            }
        },
    }
    inbound_ledger = {
        "direction": "inbound",
        "operation_uuid": str(sys_uuid.uuid4()),
        "provider_event_uuid": provider_event_uuid,
        "account_uuid": account_uuid,
        "owner_user_uuid": logical_user_uuid,
        "workspace_message_uuid": inbound_message_uuid,
        "payload_sha256": fixture.sha256(inbound_payload),
        "provider_contract": {"event": inbound_event},
    }
    outbound_resource = {
        "uuid": outbound_message_uuid,
        "stream_uuid": stream_uuid,
        "topic_uuid": topic_uuid,
        "user_uuid": logical_user_uuid,
        "payload": outbound_payload,
    }
    outbound_ledger = {
        "direction": "outbound",
        "operation_uuid": external_operation_uuid,
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "account_uuid": account_uuid,
        "owner_user_uuid": logical_user_uuid,
        "workspace_message_uuid": outbound_message_uuid,
        "payload_sha256": fixture.sha256(outbound_payload),
        "provider_contract": {
            "arguments": {
                "project_id": "logical-project",
                "operation_kind": "message.create",
                "target_type": "message",
                "target_uuid": outbound_message_uuid,
                "payload": outbound_resource,
            }
        },
    }
    runtime_event = json.loads(json.dumps(inbound_event))
    runtime_event["project_id"] = project_uuid
    runtime_event["payload"]["resource"]["user_uuid"] = iam_user_uuid
    expected = collections.defaultdict(list)
    expected["user"] = [
        {"record_kind": "user", "record_key": logical_user_uuid, "values": {}}
    ]
    expected["message"] = [
        {
            "record_kind": "message",
            "record_key": inbound_message_uuid,
            "values": {
                "project_id": "logical-project",
                "sender_uuid": logical_user_uuid,
                "recipients": [],
                "provider_operation": inbound_ledger,
            },
        },
        {
            "record_kind": "message",
            "record_key": outbound_message_uuid,
            "values": {
                "project_id": "logical-project",
                "sender_uuid": logical_user_uuid,
                "recipients": [],
                "provider_operation": outbound_ledger,
            },
        },
    ]
    actual = {
        "users": [{"uuid": iam_user_uuid}],
        "streams": [],
        "bindings": [],
        "topics": [],
        "messages": [
            {
                "uuid": inbound_message_uuid,
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "user_uuid": iam_user_uuid,
                "payload": inbound_payload,
                "external_account_uuid": account_uuid,
                "provider_external_id": "provider-message-0",
            },
            {
                "uuid": outbound_message_uuid,
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "user_uuid": iam_user_uuid,
                "payload": outbound_payload,
                "external_account_uuid": None,
                "provider_external_id": None,
            },
        ],
        "reactions": [],
        "files": [],
        "events": [],
        "deliveries": [],
        "accounts": [],
        "chats": [],
        "provider_operations": [
            {
                "external_operation_uuid": external_operation_uuid,
                "external_account_uuid": account_uuid,
                "owner_user_uuid": iam_user_uuid,
                "action": "message.create",
                "target_type": "message",
                "target_uuid": outbound_message_uuid,
                "public_status": "queued",
                "public_attempt": 0,
                "project_id": project_uuid,
                "queue_account_uuid": account_uuid,
                "operation_kind": "message.create",
                "payload": outbound_resource,
                "queue_status": "queued",
                "queue_attempt": 0,
            }
        ],
        "provider_events": [
            {
                "provider_event_uuid": provider_event_uuid,
                "external_account_uuid": account_uuid,
                "project_id": project_uuid,
                "provider_sequence": "1",
                "event_kind": "message.upsert",
                "payload_sha256": fixture.sha256(runtime_event),
                "status": "applied",
                "target_uuid": inbound_message_uuid,
            }
        ],
    }

    class Storage:
        def list_object_ids(self):
            return []

    class Exporter(concrete_inventory.ActualInventoryExporter):
        def _query(self, session, run, units):
            del session, run, units
            return expected, actual

    exporter = Exporter(
        [
            {
                "logical_user_uuid": logical_user_uuid,
                "iam_user_uuid": iam_user_uuid,
            }
        ],
        storage=Storage(),
    )
    manifest = {
        "expected_row_counts": {},
        "relationship_counts": {},
        "normalized_digests": {},
        "canonical_event_age_buckets": {},
        "provider_mapping_counts": {},
        "s3_objects": [],
        "provider_stream_mappings": [],
    }
    run = {
        "run_id": str(sys_uuid.uuid4()),
        "test_project_id": project_uuid,
        "profile_id": "provider-ledger-test",
        "manifest_sha256": "provider-ledger-manifest",
    }
    report = exporter.export(object(), run, manifest, [{"records": []}])
    expected_ledgers = fixture.provider_persistence_ledger_rows(
        [inbound_ledger, outbound_ledger]
    )
    assert report["provider_event_ledger"] == expected_ledgers["provider_event_ledger"]
    assert (
        report["provider_operation_ledger"]
        == expected_ledgers["provider_operation_ledger"]
    )
    assert "provider_event_ledger" not in report["mismatches"]
    assert "provider_operation_ledger" not in report["mismatches"]

    actual["provider_events"][0]["payload_sha256"] = "0" * 64
    report = exporter.export(object(), run, manifest, [{"records": []}])
    assert "provider_event_ledger" in report["mismatches"]

    actual["provider_events"][0]["payload_sha256"] = fixture.sha256(runtime_event)
    actual["provider_operations"][0]["payload"] = {"tampered": True}
    report = exporter.export(object(), run, manifest, [{"records": []}])
    assert "provider_operation_ledger" in report["mismatches"]


def test_application_plan_has_complete_native_stream_and_provider_contracts(tmp_path):
    manifest = fixture.build_fixture(_small_profile(), tmp_path)
    records = _plan_records(tmp_path / manifest["application_plan"]["path"])
    streams = {
        record["record_key"]: record["values"]
        for record in records
        if record["record_kind"] == "stream"
    }
    chats = {
        record["record_key"]: record["values"]
        for record in records
        if record["record_kind"] == "external_chat"
    }

    assert len(streams) == 4
    assert len(chats) == 3
    for stream in streams.values():
        assert stream["owner_user_uuid"] in stream["members"]
        assert stream["default_topic_uuid"] in {
            topic["uuid"] for topic in stream["topics"]
        }
        assert stream["private"] is (stream["kind"] != "channel")
        assert stream["invite_only"] is (stream["kind"] != "channel")
        assert {binding["user_uuid"] for binding in stream["bindings"]} == set(
            stream["members"]
        )
        assert len({binding["uuid"] for binding in stream["bindings"]}) == len(
            stream["bindings"]
        )
        assert {binding["role"] for binding in stream["bindings"]} <= {
            "owner",
            "member",
        }

    for chat_uuid, chat in chats.items():
        projection = chat["workspace_projection"]
        assert projection["stream_uuid"] == str(
            sql_state._projection_uuid(
                sys_uuid.UUID(chat_uuid),
                "stream",
                "canonical",
            )
        )
        for topic in projection["topics"]:
            assert topic["uuid"] == str(
                sql_state._projection_uuid(
                    sys_uuid.UUID(chat_uuid),
                    "topic",
                    topic["provider_topic_id"],
                )
            )
        catalog = chat["catalog_report_spec"]
        assert catalog["source"]["provider_chat_key"] == chat["provider_chat_key"]
        assert sum(item["is_owner"] for item in catalog["participants"]) == 1
        assert len(
            {item["provider_user_id"] for item in catalog["participants"]}
        ) == len(catalog["participants"])
        assert len({item["provider_topic_id"] for item in catalog["topics"]}) == len(
            catalog["topics"]
        )


def test_provider_messages_use_exact_directional_service_specs(tmp_path):
    manifest = fixture.build_fixture(_small_profile(), tmp_path)
    records = _plan_records(tmp_path / manifest["application_plan"]["path"])
    messages = {
        record["record_key"]: record["values"]
        for record in records
        if record["record_kind"] == "message"
    }
    ledger = [
        row for _, row in fixture.read_json_lines(tmp_path / "expected-ledger.jsonl")
    ]
    enqueue_parameters = set(
        inspect.signature(provider_data.enqueue_provider_operation).parameters
    )
    enqueue_parameters.discard("session")

    assert {row["direction"] for row in ledger} == {"inbound", "outbound"}
    for row in ledger:
        message = messages[row["workspace_message_uuid"]]
        contract = row["provider_contract"]
        assert message["external_chat_uuid"] == row["external_chat_uuid"]
        assert message["provider_operation"] == row
        if row["direction"] == "inbound":
            event = contract["event"]
            assert contract["service"] == "ProviderEventBatch.events[]"
            assert set(event) == {
                "provider_event_uuid",
                "external_account_uuid",
                "external_chat_uuid",
                "project_id",
                "provider_sequence",
                "kind",
                "payload",
            }
            assert event["kind"] == "message.upsert"
            assert event["external_chat_uuid"] == row["external_chat_uuid"]
            resource = event["payload"]["resource"]
            assert resource["uuid"] == row["workspace_message_uuid"]
            assert resource["stream_uuid"] == row["stream_uuid"]
            assert resource["topic_uuid"] == row["topic_uuid"]
        else:
            assert contract["service"] == "enqueue_provider_operation"
            assert contract["external_chat_uuid"] == row["external_chat_uuid"]
            assert set(contract["arguments"]) == enqueue_parameters
            assert contract["arguments"]["operation_kind"] == "message.create"
            assert contract["arguments"]["target_uuid"] == row["workspace_message_uuid"]


def test_file_recipe_and_event_records_are_reconstructable_and_fail_closed(tmp_path):
    manifest = fixture.build_fixture(_small_profile(), tmp_path)
    records = _plan_records(tmp_path / manifest["application_plan"]["path"])
    files = [record["values"] for record in records if record["record_kind"] == "file"]
    events = [
        record["values"] for record in records if record["record_kind"] == "event"
    ]
    message_index_by_uuid = {
        record["record_key"]: record["values"]["message_index"]
        for record in records
        if record["record_kind"] == "message"
    }

    assert len(events) == manifest["expected_row_counts"]["canonical_broadcast_events"]
    for file_record in files:
        content = fixture.file_content_from_recipe(file_record["content_recipe"])
        assert len(content) == file_record["size_bytes"]
        assert fixture.sha256(content) == file_record["binary_sha256"]
        assert file_record["sidecar"]["size_bytes"] == len(content)
        assert file_record["sidecar"]["sha256"] == fixture.sha256(content)
        assert file_record["sidecar_sha256"] == fixture.sha256(file_record["sidecar"])
    for event in events:
        policy = event["retention_policy"]
        assert policy["bucket"] in manifest["canonical_event_age_buckets"]
        assert event["application_contract"] == {
            "schema_version": fixture.EVENT_APPLICATION_CONTRACT_VERSION,
            "status": "ready",
            "required_service": "create_deterministic_fixture_broadcast_event",
        }
        assert event["event_uuid"] == fixture.stable_uuid(
            manifest["seed"],
            f"broadcast-event:{event['event_kind']}",
            message_index_by_uuid[event["message_uuid"]],
        )


def test_private_fixture_credentials_bind_every_logical_user_and_do_not_leak(tmp_path):
    profile = _small_profile()
    target = "postgresql://fixture.invalid/test"
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    credentials = _credential_bundle(profile, target, project_id, run_id)
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(credentials), encoding="utf-8")

    loaded = application_boundary._load_credentials(
        path,
        target,
        project_id,
        run_id,
    )
    application_boundary._validate_profile_identity_mappings(loaded, profile)
    manifest = fixture.build_fixture(profile, tmp_path / "fixture")
    serialized_artifacts = (
        tmp_path / "fixture" / "fixture-manifest.json"
    ).read_text() + (
        tmp_path / "fixture" / manifest["application_plan"]["path"]
    ).read_text()

    mappings = loaded["workspace_identity_mappings"]
    assert [mapping["ordinal"] for mapping in mappings] == list(
        range(profile["dimensions"]["users"])
    )
    assert [
        mapping["logical_user_uuid"] for mapping in mappings
    ] == fixture._ordered_ids(
        profile["seed"],
        "user",
        profile["dimensions"]["users"],
    )
    assert "access_token" not in serialized_artifacts
    assert "private-test-token" not in serialized_artifacts
    assert "private-api-key" not in serialized_artifacts


def test_concrete_adapter_maps_logical_users_and_writes_exact_s3_bytes(
    tmp_path,
    monkeypatch,
):
    profile = _small_profile()
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    credentials = _credential_bundle(
        profile,
        "postgresql://fixture.invalid/test",
        project_id,
        run_id,
    )
    adapter = concrete_adapter.ConcreteFixtureAdapter(credentials)
    logical_uuid = credentials["workspace_identity_mappings"][0]["logical_user_uuid"]
    iam_uuid = credentials["workspace_identity_mappings"][0]["iam_user_uuid"]
    assert adapter._map_user(logical_uuid) == sys_uuid.UUID(iam_uuid)

    manifest = fixture.build_fixture(profile, tmp_path)
    file_record = next(
        record
        for record in _plan_records(tmp_path / manifest["application_plan"]["path"])
        if record["record_kind"] == "file"
    )
    stored = {}

    class Storage:
        storage_type = "s3"
        storage_id = "fixture-bucket"

        def save(self, file_uuid, data, storage_object_id):
            stored["file_uuid"] = str(file_uuid)
            stored["data"] = data
            stored["storage_object_id"] = storage_object_id
            return types.SimpleNamespace(
                storage_type=self.storage_type,
                storage_id=self.storage_id,
                storage_object_id=storage_object_id,
            )

        def read(self, file_uuid, storage_object_id):
            if "data" not in stored:
                raise FileNotFoundError
            assert str(file_uuid) == stored["file_uuid"]
            assert storage_object_id == stored["storage_object_id"]
            return stored["data"]

        def save_metadata(self, file_uuid, metadata):
            stored["metadata"] = metadata

        def read_metadata(self, file_uuid):
            if "metadata" not in stored:
                raise FileNotFoundError
            assert str(file_uuid) == stored["file_uuid"]
            return stored["metadata"]

        def delete(self, file_uuid, storage_object_id):
            stored.pop("data", None)

        def delete_metadata(self, file_uuid):
            stored.pop("metadata", None)

    session = object()
    calls = []
    monkeypatch.setattr(
        concrete_adapter.file_storage,
        "get_workspace_file_storage",
        lambda storage_type: Storage(),
    )

    def create_file(*args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(uuid=sys_uuid.UUID(file_record["record_key"]))

    monkeypatch.setattr(
        concrete_adapter.helpers,
        "create_workspace_file",
        create_file,
    )
    actual_uuid, cleanup = adapter._apply_file(
        session,
        {"test_project_id": str(project_id)},
        file_record,
    )

    values = file_record["values"]
    expected = fixture.file_content_from_recipe(values["content_recipe"])
    assert actual_uuid == sys_uuid.UUID(file_record["record_key"])
    assert stored["data"] == expected
    assert fixture.sha256(stored["data"]) == values["binary_sha256"]
    assert calls[0][1]["session"] is session
    assert calls[0][1]["hash"] == values["binary_sha256"]
    assert cleanup == {
        "storage_type": "s3",
        "storage_id": "fixture-bucket",
        "storage_object_id": values["object_name"],
        "metadata_object_id": values["sidecar_object_name"],
    }
    assert "private-api-key" not in json.dumps(cleanup)


def test_concrete_adapter_event_handler_passes_one_caller_session(monkeypatch):
    profile = _small_profile()
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    adapter = concrete_adapter.ConcreteFixtureAdapter(
        _credential_bundle(
            profile,
            "postgresql://fixture.invalid/test",
            project_id,
            run_id,
        )
    )
    session = object()
    message_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    event_uuid = sys_uuid.uuid4()
    recipients = [sys_uuid.uuid4()]
    calls = []
    message = types.SimpleNamespace(
        uuid=message_uuid,
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        get_recipients=lambda *, session: (
            calls.append(("recipients", session)) or recipients
        ),
    )

    class Objects:
        def get_one(self, *, filters, session):
            calls.append(("message", session, filters))
            return message

    monkeypatch.setattr(
        concrete_adapter.models,
        "WorkspaceMessage",
        types.SimpleNamespace(objects=Objects()),
    )

    def create_event(
        project_id,
        entity_uuid,
        current_recipients,
        kind,
        payload,
        current_event_uuid,
        created_at,
        current_session,
    ):
        calls.append(
            (
                "event",
                current_session,
                project_id,
                entity_uuid,
                current_recipients,
                kind,
                payload,
                current_event_uuid,
                created_at,
            )
        )

    monkeypatch.setattr(
        concrete_adapter.messenger_events,
        "create_deterministic_fixture_broadcast_event",
        create_event,
    )
    created_at = "2026-07-18T12:30:00+00:00"
    actual_uuid, cleanup = adapter._apply_event(
        session,
        {"test_project_id": str(project_id)},
        {
            "values": {
                "message_uuid": str(message_uuid),
                "event_uuid": str(event_uuid),
                "event_kind": "message.created",
                "created_at": created_at,
            }
        },
    )

    assert actual_uuid == event_uuid
    assert cleanup == {}
    assert [call[1] for call in calls] == [session, session, session]
    event_call = calls[-1]
    assert event_call[2:6] == (
        project_id,
        message_uuid,
        recipients,
        "message.created",
    )


def test_concrete_adapter_does_not_fabricate_outbound_destination_evidence(
    tmp_path,
    monkeypatch,
):
    profile = _small_profile()
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    credentials = _credential_bundle(
        profile,
        "postgresql://fixture.invalid/test",
        project_id,
        run_id,
    )
    adapter = concrete_adapter.ConcreteFixtureAdapter(credentials)
    manifest = fixture.build_fixture(profile, tmp_path)
    record = next(
        item
        for item in _plan_records(tmp_path / manifest["application_plan"]["path"])
        if item["record_kind"] == "message"
        and item["values"]["provider_operation"] is not None
        and item["values"]["provider_operation"]["direction"] == "outbound"
    )
    session = object()
    calls = []
    monkeypatch.setattr(
        concrete_adapter.helpers,
        "create_workspace_user_message",
        lambda *args, **kwargs: calls.append(("message", kwargs["session"])),
    )
    monkeypatch.setattr(
        adapter,
        "_identity_for_account",
        lambda current_session, account_uuid: types.SimpleNamespace(
            bridge_instance_uuid=sys_uuid.uuid4()
        ),
    )
    monkeypatch.setattr(
        concrete_adapter.provider_data,
        "enqueue_provider_operation",
        lambda current_session, **kwargs: calls.append(("enqueue", current_session)),
    )
    monkeypatch.setattr(
        adapter,
        "_record_observation",
        lambda *args: pytest.fail("outbound enqueue is not destination evidence"),
    )

    adapter._apply_message(
        session,
        {"run_id": str(run_id), "test_project_id": str(project_id)},
        record,
    )

    assert calls == [("message", session), ("enqueue", session)]


@pytest.mark.parametrize(
    "failure",
    ("save_metadata", "read_metadata", "create_file"),
)
def test_concrete_adapter_compensates_new_s3_objects_after_failure(
    failure,
    tmp_path,
    monkeypatch,
):
    profile = _small_profile()
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    adapter = concrete_adapter.ConcreteFixtureAdapter(
        _credential_bundle(
            profile,
            "postgresql://fixture.invalid/test",
            project_id,
            run_id,
        )
    )
    manifest = fixture.build_fixture(profile, tmp_path)
    record = next(
        item
        for item in _plan_records(tmp_path / manifest["application_plan"]["path"])
        if item["record_kind"] == "file"
    )
    state = {"deleted": [], "metadata_deleted": 0}

    class Storage:
        storage_type = "s3"
        storage_id = "fixture-bucket"

        def read(self, file_uuid, storage_object_id):
            if "binary" not in state:
                raise FileNotFoundError
            return state["binary"]

        def save(self, file_uuid, data, storage_object_id):
            state["binary"] = data
            return types.SimpleNamespace(
                storage_type=self.storage_type,
                storage_id=self.storage_id,
                storage_object_id=storage_object_id,
            )

        def delete(self, file_uuid, storage_object_id):
            state["deleted"].append(storage_object_id)
            state.pop("binary", None)

        def read_metadata(self, file_uuid):
            if "metadata" not in state:
                raise FileNotFoundError
            if failure == "read_metadata" and state.get("metadata_saved"):
                raise RuntimeError("injected metadata readback failure")
            return state["metadata"]

        def save_metadata(self, file_uuid, metadata):
            if failure == "save_metadata":
                raise RuntimeError("injected metadata save failure")
            state["metadata"] = metadata
            state["metadata_saved"] = True

        def delete_metadata(self, file_uuid):
            state["metadata_deleted"] += 1
            state.pop("metadata", None)

    monkeypatch.setattr(
        concrete_adapter.file_storage,
        "get_workspace_file_storage",
        lambda storage_type: Storage(),
    )
    monkeypatch.setattr(
        concrete_adapter.helpers,
        "create_workspace_file",
        lambda *args, **kwargs: (
            (_ for _ in ()).throw(RuntimeError("injected database failure"))
            if failure == "create_file"
            else types.SimpleNamespace(uuid=sys_uuid.UUID(record["record_key"]))
        ),
    )

    with pytest.raises(RuntimeError):
        adapter._apply_file(
            object(),
            {"test_project_id": str(project_id)},
            record,
        )

    assert "binary" not in state
    assert "metadata" not in state
    assert state["deleted"] == [record["values"]["object_name"]]
    assert state["metadata_deleted"] == 1


def test_concrete_adapter_preserves_identical_preexisting_s3_on_db_failure(
    tmp_path,
    monkeypatch,
):
    profile = _small_profile()
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    credentials = _credential_bundle(
        profile,
        "postgresql://fixture.invalid/test",
        project_id,
        run_id,
    )
    adapter = concrete_adapter.ConcreteFixtureAdapter(credentials)
    manifest = fixture.build_fixture(profile, tmp_path)
    record = next(
        item
        for item in _plan_records(tmp_path / manifest["application_plan"]["path"])
        if item["record_kind"] == "file"
    )
    values = record["values"]
    sidecar = dict(values["sidecar"])
    logical_owner = sidecar["owner_uuid"]
    sidecar["owner_uuid"] = str(adapter._map_user(logical_owner))
    sidecar["project_id"] = str(project_id)
    state = {
        "binary": fixture.file_content_from_recipe(values["content_recipe"]),
        "metadata": file_storage.WorkspaceFileMetadata.from_json(
            json.dumps(sidecar).encode()
        ),
        "deleted": 0,
    }

    class Storage:
        storage_type = "s3"
        storage_id = "fixture-bucket"

        def read(self, file_uuid, storage_object_id):
            return state["binary"]

        def read_metadata(self, file_uuid):
            return state["metadata"]

        def delete(self, file_uuid, storage_object_id):
            state["deleted"] += 1

        def delete_metadata(self, file_uuid):
            state["deleted"] += 1

    monkeypatch.setattr(
        concrete_adapter.file_storage,
        "get_workspace_file_storage",
        lambda storage_type: Storage(),
    )
    monkeypatch.setattr(
        concrete_adapter.helpers,
        "create_workspace_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected database failure")
        ),
    )

    with pytest.raises(RuntimeError):
        adapter._apply_file(
            object(),
            {"test_project_id": str(project_id)},
            record,
        )

    assert state["deleted"] == 0
    assert state["binary"] == fixture.file_content_from_recipe(values["content_recipe"])


def test_concrete_adapter_cleanup_is_dependency_ordered_and_sanitized():
    profile = _small_profile()
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    credentials = _credential_bundle(
        profile,
        "postgresql://fixture.invalid/test",
        project_id,
        run_id,
    )
    adapter = concrete_adapter.ConcreteFixtureAdapter(credentials)
    rows = [
        {
            "record_kind": "external_account",
            "logical_uuid": sys_uuid.uuid4(),
            "actual_uuid": sys_uuid.uuid4(),
            "cleanup": {"credential_ref": f"zulip-account-{ordinal:03d}"},
        }
        for ordinal in range(profile["dimensions"]["zulip_accounts"])
    ]
    rows.extend(
        {
            "record_kind": record_kind,
            "logical_uuid": sys_uuid.uuid4(),
            "actual_uuid": sys_uuid.uuid4(),
            "cleanup": {},
        }
        for record_kind in ("stream", "message", "file", "reaction", "event")
    )

    class Result:
        def fetchall(self):
            return list(reversed(rows))

    class Session:
        def execute(self, statement, values):
            assert values == (str(run_id),)
            return Result()

    manifest = adapter.cleanup_manifest(
        Session(),
        {"run_id": str(run_id), "test_project_id": str(project_id)},
    )

    kinds = [resource["record_kind"] for resource in manifest["delete_order"]]
    assert kinds == [
        "reaction",
        "file",
        "event",
        "message",
        "stream",
        "external_account",
        "external_account",
        "external_account",
    ]
    assert "credential_ref" not in json.dumps(manifest)
    assert "private-api-key" not in json.dumps(manifest)


def test_private_fixture_credentials_reject_incomplete_or_ambiguous_mappings(tmp_path):
    profile = _small_profile()
    target = "postgresql://fixture.invalid/test"
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    credentials = _credential_bundle(profile, target, project_id, run_id)
    path = tmp_path / "credentials.json"

    incomplete = json.loads(json.dumps(credentials))
    incomplete["workspace_identity_mappings"].pop()
    path.write_text(json.dumps(incomplete), encoding="utf-8")
    loaded = application_boundary._load_credentials(
        path,
        target,
        project_id,
        run_id,
    )
    with pytest.raises(ValueError, match="count does not match profile"):
        application_boundary._validate_profile_identity_mappings(loaded, profile)

    duplicate = json.loads(json.dumps(credentials))
    duplicate["workspace_identity_mappings"][1]["iam_user_uuid"] = duplicate[
        "workspace_identity_mappings"
    ][0]["iam_user_uuid"]
    path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(ValueError, match="must be unique"):
        application_boundary._load_credentials(
            path,
            target,
            project_id,
            run_id,
        )

    incomplete_accounts = json.loads(json.dumps(credentials))
    incomplete_accounts["external_account_credentials"].pop()
    path.write_text(json.dumps(incomplete_accounts), encoding="utf-8")
    loaded = application_boundary._load_credentials(
        path,
        target,
        project_id,
        run_id,
    )
    with pytest.raises(ValueError, match="credential coverage"):
        application_boundary._validate_profile_identity_mappings(loaded, profile)


def test_fixture_provider_cursor_ordinals_are_persistent_per_account(tmp_path):
    fixture.build_fixture(_small_profile(), tmp_path)
    rows = [
        row for _, row in fixture.read_json_lines(tmp_path / "expected-ledger.jsonl")
    ]
    ordinals = {}
    for row in rows:
        ordinals.setdefault(row["cursor_scope"], []).append(row["cursor_ordinal"])

    assert all(
        sorted(values) == list(range(1, len(values) + 1))
        for values in ordinals.values()
    )


def test_correctness_verifier_accepts_idempotent_retries_and_rejects_leaks(tmp_path):
    fixture.build_fixture(_small_profile(), tmp_path)
    expected_path = tmp_path / "expected-ledger.jsonl"
    observed_path = tmp_path / "observed-ledger.jsonl"
    rows = [json.loads(line) for line in expected_path.read_text().splitlines()]
    observed = []
    for index, row in enumerate(rows):
        value = dict(row)
        value["provider_result_id"] = f"provider-{index}"
        observed.append(value)
        if row["expected_attempts"] == 2:
            observed.append(dict(value))
    observed_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in observed),
        encoding="utf-8",
    )

    report = fixture.verify_ledgers(expected_path, observed_path)

    assert report["passed"] is True

    observed[1]["account_uuid"] = fixture.stable_uuid(17, "wrong-account", 0)
    observed_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in observed[1:]),
        encoding="utf-8",
    )

    report = fixture.verify_ledgers(expected_path, observed_path)

    assert report["passed"] is False
    assert rows[0]["operation_uuid"] in report["loss"]
    assert observed[1]["operation_uuid"] in report["cross_account"]


def test_correctness_verifier_rejects_content_and_mapping_drift(tmp_path):
    fixture.build_fixture(_small_profile(), tmp_path)
    expected_path = tmp_path / "expected-ledger.jsonl"
    observed_path = tmp_path / "observed-ledger.jsonl"
    rows = [json.loads(line) for line in expected_path.read_text().splitlines()]
    observed = [
        dict(row, provider_result_id=f"provider-{index}")
        for index, row in enumerate(rows)
    ]
    target = observed[0]
    target["owner_user_uuid"] = fixture.stable_uuid(17, "wrong-owner", 0)
    target["stream_uuid"] = fixture.stable_uuid(17, "wrong-stream", 0)
    target["direction"] = "outbound" if target["direction"] == "inbound" else "inbound"
    target["payload_sha256"] = "0" * 64
    target["outbox_idempotency_key"] = fixture.stable_uuid(17, "wrong-key", 0)
    observed_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in observed),
        encoding="utf-8",
    )

    report = fixture.verify_ledgers(expected_path, observed_path)

    operation_uuid = target["operation_uuid"]
    assert report["passed"] is False
    assert operation_uuid in report["owner"]
    assert operation_uuid in report["mapping"]
    assert operation_uuid in report["direction"]
    assert operation_uuid in report["payload_integrity"]
    assert operation_uuid in report["outbox_idempotency"]


def test_provider_profile_is_e2e_and_does_not_bypass_connector():
    script = (LOAD_DIRECTORY / "k6" / "zulip_provider.js").read_text()

    assert "sendInboundE2E" in script
    assert "sendOutboundE2E" in script
    assert "Zulip -> connector -> Workspace" in script
    assert "workspace_provider_token" not in script
    assert "/api/workspace-provider/v1" not in script
    assert "external_accounts/${account.external_account_uuid}" in script
    assert "cursor_ordinal_base" in script
    assert "cursor_ordinal_limit" in script
    assert "exec.scenario.iterationInTest" in script
    assert "WORKSPACE_RUN_EXPECTED_V1" in script
    assert "WORKSPACE_RUN_DIAGNOSTIC_V1" in script
    assert "WORKSPACE_RUN_OBSERVED_V1" not in script
    assert "provider expectation requires an explicit direction" in script
    assert 'row.direction = "inbound";\n    runExpectation(row);' in script
    assert 'row.direction = "outbound";\n    runExpectation(row);' in script


class _RecordingAdapter:
    def __init__(self, completed=()):
        self.completed = set(completed)
        self.sessions = []

    def prepare(self, session, run):
        self.sessions.append(("prepare", session, run["run_id"]))
        return {"completed_unit_ids": sorted(self.completed)}

    def apply_unit(self, session, run, unit):
        self.sessions.append(("apply", session, unit["unit_id"]))
        self.completed.add(unit["unit_id"])
        return {"unit_id": unit["unit_id"]}

    def export_observed(self, session, run):
        self.sessions.append(("export", session, run["run_id"]))
        return []

    def cleanup_manifest(self, session, run):
        self.sessions.append(("cleanup", session, run["run_id"]))
        return {"delete_order": []}

    def export_inventory(self, session, run):
        self.sessions.append(("inventory", session, run["run_id"]))
        return {"passed": True}


def test_application_runner_owns_one_outer_session_per_application_unit():
    sessions = []

    @contextlib.contextmanager
    def session_context():
        session = object()
        sessions.append(session)
        yield session

    units = [{"unit_id": str(sys_uuid.uuid4())} for _ in range(3)]
    adapter = _RecordingAdapter(completed=(units[0]["unit_id"],))
    runner = application_boundary.ApplicationRunner(adapter, session_context)

    observed, cleanup = runner.run({"run_id": str(sys_uuid.uuid4())}, units)

    assert observed == []
    assert cleanup == {"delete_order": []}
    assert [row[0] for row in adapter.sessions] == [
        "prepare",
        "apply",
        "apply",
        "prepare",
        "export",
        "cleanup",
    ]
    assert [row[1] for row in adapter.sessions] == sessions
    assert len({id(value) for value in sessions}) == len(sessions)

    inventory = runner.export_inventory({"run_id": "inventory-run"})
    assert inventory == {"passed": True}
    assert adapter.sessions[-1] == ("inventory", sessions[-1], "inventory-run")
    assert len({id(value) for value in sessions}) == len(sessions)


def test_fixture_apply_fails_closed_without_concrete_adapter(tmp_path, monkeypatch):
    target = "postgresql://fixture.invalid/test"
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    credentials = _credential_bundle(_small_profile(), target, project_id, run_id)
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(credentials), encoding="utf-8")
    monkeypatch.delenv("WORKSPACE_FIXTURE_CONCRETE_ADAPTER", raising=False)

    loaded = application_boundary._load_credentials(
        path,
        target,
        project_id,
        run_id,
    )

    try:
        application_boundary._load_concrete_adapter(loaded)
    except ValueError as error:
        assert str(error) == "fixture apply requires a concrete application adapter"
    else:
        raise AssertionError("fixture apply accepted a missing concrete adapter")


def test_fixture_apply_validates_all_artifacts_before_adapter_or_database(
    tmp_path,
    monkeypatch,
):
    profile = _small_profile()
    output_directory = tmp_path / "fixture"
    fixture.build_fixture(profile, output_directory)
    manifest_path = output_directory / "fixture-manifest.json"
    plan_path = output_directory / "application-plan.jsonl"
    ledger_path = output_directory / "expected-ledger.jsonl"
    original_manifest = manifest_path.read_bytes()
    original_plan = plan_path.read_bytes()
    original_ledger = ledger_path.read_bytes()
    target = "postgresql://fixture.invalid/test"
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        json.dumps(_credential_bundle(profile, target, project_id, run_id)),
        encoding="utf-8",
    )
    side_effects = []

    def adapter_loader(_credentials):
        side_effects.append("adapter")
        raise AssertionError("adapter loaded before fixture validation")

    def database_configurer(**_kwargs):
        side_effects.append("database")
        raise AssertionError("database configured before fixture validation")

    monkeypatch.setattr(application_boundary, "_load_concrete_adapter", adapter_loader)
    monkeypatch.setattr(
        application_boundary.engines.engine_factory,
        "configure_factory",
        database_configurer,
    )

    def apply(candidate_profile=profile):
        application_boundary.apply_fixture(
            profile=candidate_profile,
            manifest_path=manifest_path,
            expected_ledger_path=ledger_path,
            application_plan_path=plan_path,
            target=target,
            credentials_path=credentials_path,
            project_id=project_id,
            run_id=run_id,
            output_directory=output_directory,
        )

    manifest = json.loads(original_manifest)
    manifest["schema_version"] = "unsupported"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest schema"):
        apply()

    manifest_path.write_bytes(original_manifest)
    changed_profile = json.loads(json.dumps(profile))
    changed_profile["provider_workload"]["steady_minutes"] += 1
    with pytest.raises(ValueError, match="profile digest"):
        apply(changed_profile)

    plan_path.write_bytes(original_plan + b"{}\n")
    with pytest.raises(ValueError, match="application plan digest"):
        apply()

    plan_path.write_bytes(original_plan)
    ledger_path.write_bytes(original_ledger + b"{}\n")
    with pytest.raises(ValueError, match="expected ledger digest"):
        apply()

    assert side_effects == []


def test_fixture_cli_does_not_claim_success_when_adapter_is_missing(tmp_path):
    target = "postgresql://fixture.invalid/test"
    project_id = sys_uuid.uuid4()
    run_id = sys_uuid.uuid4()
    profile = fixture.load_profile(PROFILE_DIRECTORY / "db-ci-v1.json")
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        json.dumps(_credential_bundle(profile, target, project_id, run_id)),
        encoding="utf-8",
    )
    credentials_path.chmod(0o600)
    output_directory = tmp_path / "fixture"
    script = (
        pathlib.Path(__file__).parents[1] / "scale" / "generate_messenger_fixture.py"
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "WORKSPACE_FIXTURE_EXECUTE": "1",
        "WORKSPACE_FIXTURE_TARGET": target,
        "WORKSPACE_FIXTURE_CREDENTIALS_FILE": str(credentials_path),
    }

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--profile",
            str(PROFILE_DIRECTORY / "db-ci-v1.json"),
            "--output-dir",
            str(output_directory),
            "--apply",
            "--project-id",
            str(project_id),
            "--run-id",
            str(run_id),
        ],
        cwd=pathlib.Path(__file__).parents[3],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    manifest = json.loads((output_directory / "fixture-manifest.json").read_text())
    assert manifest["dry_run"] is True
    assert not (output_directory / "fixture-application-result.json").exists()


def _run_row(
    run_id,
    source,
    operation_uuid,
    ordinal=None,
    operation_kind="provider.message.inbound",
):
    return {
        "schema_version": fixture.RUN_LEDGER_SCHEMA_VERSION,
        "run_id": run_id,
        "source": source,
        "operation_uuid": operation_uuid,
        "operation_kind": operation_kind,
        "account_uuid": "account-a" if ordinal is not None else None,
        "owner_user_uuid": "user-a",
        "stream_uuid": "stream-a",
        "topic_uuid": "topic-a",
        "provider_event_uuid": "event-" + operation_uuid,
        "payload_sha256": "a" * 64,
        "cursor_scope": "zulip:account-a" if ordinal is not None else None,
        "cursor_ordinal": ordinal,
        "idempotency_key": "key-" + operation_uuid,
    }


def test_run_ledger_verifies_composed_sources_and_exact_operation_results(tmp_path):
    run_id = str(sys_uuid.uuid4())
    native = _run_row(run_id, "k6.native", "native", None)
    inbound = _run_row(run_id, "k6.provider", "inbound", 41)
    outbound = _run_row(
        run_id,
        "k6.provider",
        "outbound",
        42,
        "provider.message.outbound",
    )
    native_expected = tmp_path / "native-expected.jsonl"
    provider_expected = tmp_path / "provider-expected.jsonl"
    observed = tmp_path / "observed.jsonl"
    fixture.write_json_lines(native_expected, [native])
    fixture.write_json_lines(provider_expected, [inbound, outbound])
    fixture.write_json_lines(
        observed,
        [
            dict(
                native,
                evidence_source="workspace_response",
                outcome="succeeded",
                result_id="workspace-message-a",
            ),
            dict(
                inbound,
                evidence_source="provider_connector",
                outcome="succeeded",
                result_id="zulip-source-message-a",
            ),
            dict(
                inbound,
                evidence_source="workspace_backend",
                outcome="succeeded",
                result_id="workspace-destination-message-a",
            ),
            dict(
                outbound,
                evidence_source="workspace_backend",
                outcome="succeeded",
                result_id="workspace-source-message-b",
            ),
            dict(
                outbound,
                evidence_source="provider_connector",
                outcome="succeeded",
                result_id="zulip-destination-message-b",
            ),
        ],
    )

    report = fixture.verify_run_ledgers(
        [native_expected, provider_expected],
        [observed],
    )

    assert report["passed"] is True
    assert report["expected_operations"] == 3
    assert report["observed_operations"] == 3
    assert report["ignored_source_side_evidence"] == ["inbound", "outbound"]
    assert report["result_conflict"] == []


def test_run_ledger_rejects_missing_failed_and_reused_cursor_operations(tmp_path):
    run_id = str(sys_uuid.uuid4())
    first = _run_row(run_id, "k6.provider", "first", 42)
    second = _run_row(run_id, "k6.provider", "second", 42)
    expected = tmp_path / "expected.jsonl"
    observed = tmp_path / "observed.jsonl"
    fixture.write_json_lines(expected, [first, second])
    fixture.write_json_lines(
        observed,
        [
            dict(
                first,
                evidence_source="visibility_scan",
                outcome="failed",
                result_id=None,
            )
        ],
    )

    report = fixture.verify_run_ledgers([expected], [observed])

    assert report["passed"] is False
    assert any(value.endswith(":second") for value in report["missing"])
    assert any(value.endswith(":first") for value in report["missing"])
    assert report["non_authoritative_evidence"] == ["first"]
    assert report["cursor_monotonicity"] == ["first", "second"]


def test_provider_visibility_diagnostic_cannot_satisfy_run_ledger(tmp_path):
    run_id = str(sys_uuid.uuid4())
    operation = _run_row(run_id, "k6.provider", "provider-operation", 43)
    expected = tmp_path / "expected.jsonl"
    observed = tmp_path / "observed.jsonl"
    fixture.write_json_lines(expected, [operation])
    fixture.write_json_lines(
        observed,
        [
            dict(
                operation,
                evidence_source="visibility_scan",
                outcome="succeeded",
                result_id="content-match-is-not-proof",
            )
        ],
    )

    report = fixture.verify_run_ledgers([expected], [observed])

    assert report["passed"] is False
    assert any(value.endswith(":provider-operation") for value in report["missing"])
    assert report["non_authoritative_evidence"] == ["provider-operation"]


@pytest.mark.parametrize(
    ("operation_kind", "source_side_evidence"),
    (
        ("provider.message.inbound", "provider_connector"),
        ("provider.message.outbound", "workspace_backend"),
    ),
)
def test_provider_source_side_only_cannot_satisfy_destination_e2e(
    tmp_path,
    operation_kind,
    source_side_evidence,
):
    run_id = str(sys_uuid.uuid4())
    operation = _run_row(
        run_id,
        "k6.provider",
        "source-only",
        44,
        operation_kind,
    )
    expected = tmp_path / "expected.jsonl"
    observed = tmp_path / "observed.jsonl"
    fixture.write_json_lines(expected, [operation])
    fixture.write_json_lines(
        observed,
        [
            dict(
                operation,
                evidence_source=source_side_evidence,
                outcome="succeeded",
                result_id="source-result-is-not-destination-proof",
            )
        ],
    )

    report = fixture.verify_run_ledgers([expected], [observed])

    assert report["passed"] is False
    assert any(value.endswith(":source-only") for value in report["missing"])
    assert report["ignored_source_side_evidence"] == ["source-only"]


def test_unknown_provider_operation_kind_fails_closed(tmp_path):
    run_id = str(sys_uuid.uuid4())
    operation = _run_row(
        run_id,
        "k6.provider",
        "unknown-operation",
        45,
        "provider.message.future_direction",
    )
    expected = tmp_path / "expected.jsonl"
    observed = tmp_path / "observed.jsonl"
    fixture.write_json_lines(expected, [operation])
    fixture.write_json_lines(
        observed,
        [
            dict(
                operation,
                evidence_source="workspace_backend",
                outcome="succeeded",
                result_id="must-not-be-accepted",
            )
        ],
    )

    report = fixture.verify_run_ledgers([expected], [observed])

    assert report["passed"] is False
    assert report["unsupported_operation_kind"] == ["unknown-operation"]
    assert any(value.endswith(":unknown-operation") for value in report["missing"])
