# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import pathlib
import types
import uuid as sys_uuid

import pytest

from workspace.cmd import messenger_migrate
from workspace.messenger_mail import external_bridge_codec
from workspace.messenger_mail import external_bridge_data_plane
from workspace.messenger_mail import repository as mail_repository
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import helpers as messenger_helpers
from workspace.messenger_migration import mail_source
from workspace.messenger_migration import legacy_provider_outbox
from workspace.messenger_migration import postgres_target
from workspace.messenger_migration import service
from workspace.messenger_migration import snapshot


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")
REALM_UUID = sys_uuid.UUID("30000000-0000-0000-0000-000000000003")
BRIDGE_UUID = sys_uuid.UUID("40000000-0000-0000-0000-000000000004")
ACCOUNT_UUID = sys_uuid.UUID("50000000-0000-0000-0000-000000000005")
ENROLLMENT_SECRET = "unit-test-legacy-provider-conversion-secret"


def _snapshot(*items):
    return snapshot.CanonicalSnapshot(
        PROJECT_UUID,
        snapshot.SourceCheckpoint(77, 12, 13, 90),
        tuple(sorted(items, key=lambda item: (item.collection, item.entity_key))),
    )


def _signed_legacy_provider_row(
    *,
    public_status="queued",
    legacy_status="queued",
    attempt=1,
    raw_override=None,
):
    operation_uuid = sys_uuid.uuid4()
    record_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    occurred_at = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    record = {
        "schema": external_bridge_codec.SCHEMA,
        "schema_version": external_bridge_codec.SCHEMA_VERSION,
        "record_kind": "operation",
        "record_uuid": str(record_uuid),
        "operation_uuid": str(operation_uuid),
        "attempt": attempt,
        "operation_sha256": "0" * 64,
        "account_uuid": str(ACCOUNT_UUID),
        "project_uuid": str(PROJECT_UUID),
        "origin": "workspace",
        "causal_lane": f"chat:{ACCOUNT_UUID}:channel:test",
        "sequence": 1,
        "predecessor_operation_uuid": None,
        "created_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "expires_at": (occurred_at + datetime.timedelta(hours=24))
        .isoformat()
        .replace("+00:00", "Z"),
        "operation": {
            "kind": "message.create",
            "entity_uuid": str(message_uuid),
            "actor_uuid": str(USER_UUID),
            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
            "provider": {
                "kind": "zulip",
                "chat_id": "channel:test",
                "entity_id": None,
                "revision": None,
            },
            "payload": {
                "stream_uuid": str(sys_uuid.uuid4()),
                "topic_uuid": str(sys_uuid.uuid4()),
                "author_uuid": str(USER_UUID),
                "payload": {"kind": "markdown", "content": "legacy payload"},
                "reply_to_message_uuid": None,
            },
            "extensions": {},
        },
    }
    record["operation_sha256"] = external_bridge_codec.operation_sha256(record)
    key = external_bridge_codec.derive_direction_key(
        ENROLLMENT_SECRET,
        REALM_UUID,
        BRIDGE_UUID,
        1,
        "workspace-to-zulip",
    )
    raw_message = external_bridge_codec.build_message(
        record,
        "workspace-to-zulip",
        key,
        external_bridge_data_plane.WORKSPACE_SENDER,
        external_bridge_data_plane.BRIDGE_ADDRESS,
    )
    row = {
        "external_operation_uuid": operation_uuid,
        "public_status": public_status,
        "public_attempt": attempt,
        "can_retry": public_status in {"failed", "manual_reconciliation_required"},
        "safe_error": "manual retry" if public_status != "queued" else None,
        "external_account_uuid": ACCOUNT_UUID,
        "owner_user_uuid": USER_UUID,
        "action": "message.create",
        "target_type": "message",
        "target_uuid": message_uuid,
        "provider_operation_uuid": None,
        "record_uuid": record_uuid,
        "legacy_attempt": attempt,
        "project_uuid": PROJECT_UUID,
        "operation_sha256": record["operation_sha256"],
        "raw_message": raw_message if raw_override is None else raw_override,
        "legacy_status": legacy_status,
        "created_at": occurred_at,
        "sent_at": occurred_at if legacy_status == "sent" else None,
    }
    return row, record


def _set_provider_projection(row, item, **overrides):
    row.update(
        {
            "provider_operation_uuid": item["uuid"],
            "provider_bridge_instance_uuid": item["bridge_instance_uuid"],
            "provider_external_account_uuid": item["external_account_uuid"],
            "provider_project_id": item["project_id"],
            "provider_operation_kind": item["operation_kind"],
            "provider_payload": item["payload"],
            "provider_status": item["status"],
            "provider_attempt": item["attempt"],
            "provider_safe_error": item["safe_error"],
            "provider_created_at": item["created_at"],
            "provider_completed_at": item["completed_at"],
        }
    )
    row.update(overrides)


def test_snapshot_digest_and_urn_inventory_are_deterministic():
    entity_uuid = sys_uuid.uuid4()
    first = _snapshot(
        snapshot.SnapshotItem(
            "messages",
            str(entity_uuid),
            "upsert",
            {
                "uuid": entity_uuid,
                "created_at": datetime.datetime(
                    2026, 7, 18, tzinfo=datetime.timezone.utc
                ),
                "payload": {"content": f"![x](urn:image:{entity_uuid}?size=3)"},
            },
        )
    )
    second = _snapshot(*reversed(first.items))

    assert first.digest == second.digest
    assert first.state_digest == second.state_digest
    assert first.urn_inventory["count"] == 1
    assert first.urn_inventory["urns"] == (f"urn:image:{entity_uuid}?size=3",)


def test_mail_source_keeps_only_retained_events_and_records_watermark(monkeypatch):
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    old = types.SimpleNamespace(
        raw_message=b"old",
        uid=3,
    )
    current = types.SimpleNamespace(
        raw_message=b"current",
        uid=8,
    )

    class Imap:
        def ensure_mailbox(self, path):
            return None

        def select(self, path):
            return types.SimpleNamespace(uid_validity=41, uid_next=9)

        def search(self, criteria):
            return (3, 8)

        def fetch(self, uids):
            return (old, current)

    repository = types.SimpleNamespace(
        project_uuid=PROJECT_UUID,
        imap_client=Imap(),
        event_mailbox=lambda user_uuid: f"events/{user_uuid}",
        _check_project=lambda project_uuid: None,
    )

    def decode(raw):
        occurred_at = now - datetime.timedelta(days=8 if raw == b"old" else 1)
        return mail_repository.EventRecord(
            project_uuid=PROJECT_UUID,
            event_uuid=sys_uuid.uuid5(PROJECT_UUID, raw.decode()),
            operation_uuid=sys_uuid.uuid4(),
            user_uuid=USER_UUID,
            object_type="message",
            action="created",
            payload={"kind": "message.created"},
            occurred_at=occurred_at,
        )

    monkeypatch.setattr(mail_source.mail_repository, "decode_event", decode)
    source = mail_source.MailProjectionSource(repository, now=now)

    items, quarantined, watermarks = source._event_items((USER_UUID,))

    assert len(items) == 1
    assert items[0].entity_key == str(sys_uuid.uuid5(PROJECT_UUID, "current"))
    assert quarantined == []
    assert watermarks[str(USER_UUID)] == {
        "source_epoch_generation": "41",
        "source_current_epoch_version": 8,
        "source_minimum_epoch_version": 3,
        "destination_strategy": "new_generation_with_retained_suffix",
    }


def test_stage_rejects_run_reuse_for_another_project(monkeypatch):
    other_project = sys_uuid.uuid4()

    class Result:
        def fetchone(self):
            return {
                "project_id": other_project,
                "phase": "staged",
                "source_uid_validity": 77,
            }

    session = types.SimpleNamespace(execute=lambda statement, params: Result())
    target = postgres_target.PostgreSQLImportTarget()
    monkeypatch.setattr(type(target), "session", property(lambda self: session))
    capture = mail_source.SourceCapture(_snapshot())

    with pytest.raises(ValueError, match="another project"):
        target.stage(sys_uuid.uuid4(), capture)


def test_apply_sql_orders_dependencies_before_limit_and_uses_savepoints():
    source = pathlib.Path(postgres_target.__file__).read_text()

    assert "sql_store" not in source
    assert "messenger_mail" not in source
    assert "WHEN 'streams' THEN 20" in source
    assert "WHEN 'bindings' THEN 30" in source
    assert source.index("ORDER BY\n                CASE") < source.index("LIMIT %s")
    assert "WHERE run_uuid = %s AND status = 'staged'" in source
    assert "SAVEPOINT {savepoint}" in source
    assert "ROLLBACK TO SAVEPOINT {savepoint}" in source


def test_parity_is_blocked_when_files_exist_without_object_verifier(monkeypatch):
    file_uuid = sys_uuid.uuid4()
    source_snapshot = _snapshot(
        snapshot.SnapshotItem(
            "files",
            str(file_uuid),
            "upsert",
            {"uuid": str(file_uuid), "hash": "abc", "size_bytes": 1},
        )
    )
    capture = mail_source.SourceCapture(source_snapshot)

    class Session:
        def execute(self, statement, params):
            if "phase = 'final_delta'" in statement:
                return types.SimpleNamespace(
                    fetchone=lambda: {
                        "source_uid_validity": source_snapshot.checkpoint.uid_validity,
                        "source_checkpoint_uid": source_snapshot.checkpoint.checkpoint_uid,
                        "snapshot_digest": source_snapshot.digest,
                        "project_id": PROJECT_UUID,
                        "details": {"writer_gate": {"gate_id": str(sys_uuid.uuid4())}},
                    }
                )
            return types.SimpleNamespace(fetchone=lambda: None)

    target = types.SimpleNamespace(
        capture_destination=lambda project_id, source: source_snapshot,
        session=Session(),
    )
    coordinator = service.ImportCoordinator(
        types.SimpleNamespace(capture=lambda: capture), target
    )
    monkeypatch.setattr(
        service.writer_gate,
        "validate_closed_gate",
        lambda *args, **kwargs: {},
    )

    report = coordinator.parity(sys_uuid.uuid4())

    assert report["ok"] is False
    assert report["file_objects"] == {
        "checked": 0,
        "failed": 1,
        "ok": False,
        "blocked": True,
    }


def test_cli_requires_authoritative_writer_gate_id():
    parser = messenger_migrate.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--project-id",
                str(PROJECT_UUID),
                "--run-id",
                str(sys_uuid.uuid4()),
                "freeze",
            ]
        )
    gate_uuid = sys_uuid.uuid4()
    freeze = parser.parse_args(
        [
            "--project-id",
            str(PROJECT_UUID),
            "--run-id",
            str(sys_uuid.uuid4()),
            "freeze",
            "--gate-id",
            str(gate_uuid),
        ]
    )
    final_delta = parser.parse_args(
        [
            "--project-id",
            str(PROJECT_UUID),
            "--run-id",
            str(sys_uuid.uuid4()),
            "final-delta",
        ]
    )

    assert freeze.gate_id == gate_uuid
    assert not hasattr(final_delta, "freeze_confirmed")
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--project-id",
                str(PROJECT_UUID),
                "legacy-provider-outbox-convert",
            ]
        )
    conversion = parser.parse_args(
        [
            "--project-id",
            str(PROJECT_UUID),
            "legacy-provider-outbox-convert",
            "--gate-id",
            str(gate_uuid),
        ]
    )
    assert conversion.gate_id == gate_uuid
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--project-id",
                str(PROJECT_UUID),
                "--run-id",
                str(sys_uuid.uuid4()),
                "freeze",
                "--writer-gate-proof",
                "forged.json",
            ]
        )


def test_legacy_provider_conversion_blocks_before_first_insert(monkeypatch):
    external_operation_uuid = sys_uuid.uuid4()
    rows = [{"external_operation_uuid": external_operation_uuid}]
    monkeypatch.setattr(
        legacy_provider_outbox,
        "_required_rows",
        lambda session, project_id: rows,
    )
    monkeypatch.setattr(
        legacy_provider_outbox,
        "_conversion_plan",
        lambda *args, **kwargs: (
            [],
            [
                {
                    "external_operation_uuid": str(external_operation_uuid),
                    "code": "legacy_transport_record_invalid",
                }
            ],
            0,
        ),
    )

    class NoWrites:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("a blocked conversion must not write the database")

    report = legacy_provider_outbox.convert_required_operations(
        NoWrites(),
        project_id=PROJECT_UUID,
        realm_uuid=sys_uuid.uuid4(),
        bridge_instance_uuid=sys_uuid.uuid4(),
        identity_generation=1,
        enrollment_secret="test-secret",
    )

    assert report["ok"] is False
    assert report["converted"] == 0
    assert report["blockers"] == [
        {
            "external_operation_uuid": str(external_operation_uuid),
            "code": "legacy_transport_record_invalid",
        }
    ]


def test_legacy_provider_conversion_preserves_queued_identity_and_attempt():
    row, record = _signed_legacy_provider_row(attempt=3)

    plan, blockers, already_provider = legacy_provider_outbox._conversion_plan(
        [row],
        realm_uuid=REALM_UUID,
        bridge_instance_uuid=BRIDGE_UUID,
        identity_generation=1,
        enrollment_secret=ENROLLMENT_SECRET,
    )

    assert blockers == []
    assert already_provider == 0
    assert len(plan) == 1
    converted = plan[0]
    assert converted["uuid"] == sys_uuid.UUID(record["record_uuid"])
    assert converted["external_operation_uuid"] == sys_uuid.UUID(
        record["operation_uuid"]
    )
    assert converted["status"] == "queued"
    # Provider leasing increments before delivery, so a legacy attempt N that
    # has not been sent is represented as N-1 in the queue.
    assert converted["attempt"] == 2
    assert converted["payload"]["uuid"] == record["operation"]["entity_uuid"]
    assert converted["payload"]["payload"] == {
        "kind": "markdown",
        "content": "legacy payload",
    }


def test_legacy_provider_conversion_preserves_manual_retry_boundary():
    row, record = _signed_legacy_provider_row(
        public_status="manual_reconciliation_required",
        legacy_status="sent",
        attempt=2,
    )

    plan, blockers, _already_provider = legacy_provider_outbox._conversion_plan(
        [row],
        realm_uuid=REALM_UUID,
        bridge_instance_uuid=BRIDGE_UUID,
        identity_generation=1,
        enrollment_secret=ENROLLMENT_SECRET,
    )

    assert blockers == []
    assert plan[0]["uuid"] == sys_uuid.UUID(record["record_uuid"])
    assert plan[0]["external_operation_uuid"] == sys_uuid.UUID(record["operation_uuid"])
    assert plan[0]["status"] == "failed"
    assert plan[0]["attempt"] == 2
    assert plan[0]["safe_error"] == "manual retry"


def test_legacy_provider_conversion_validates_existing_provider_projection():
    row, _record = _signed_legacy_provider_row(attempt=3)
    plan, blockers, already_provider = legacy_provider_outbox._conversion_plan(
        [row],
        realm_uuid=REALM_UUID,
        bridge_instance_uuid=BRIDGE_UUID,
        identity_generation=1,
        enrollment_secret=ENROLLMENT_SECRET,
    )
    assert blockers == []
    assert already_provider == 0
    _set_provider_projection(row, plan[0])

    repeated_plan, repeated_blockers, already_provider = (
        legacy_provider_outbox._conversion_plan(
            [row],
            realm_uuid=REALM_UUID,
            bridge_instance_uuid=BRIDGE_UUID,
            identity_generation=1,
            enrollment_secret=ENROLLMENT_SECRET,
        )
    )

    assert repeated_plan == []
    assert repeated_blockers == []
    assert already_provider == 1


@pytest.mark.parametrize(
    ("override", "value"),
    (
        ("provider_operation_uuid", sys_uuid.uuid4()),
        ("provider_external_account_uuid", sys_uuid.uuid4()),
        ("provider_project_id", sys_uuid.uuid4()),
        ("provider_operation_kind", "message.delete"),
        ("provider_payload", {"uuid": str(sys_uuid.uuid4())}),
        ("provider_attempt", 99),
        ("provider_status", "failed"),
    ),
)
def test_legacy_provider_conversion_rejects_existing_provider_mismatch(
    override,
    value,
):
    row, _record = _signed_legacy_provider_row(attempt=3)
    plan, blockers, _already_provider = legacy_provider_outbox._conversion_plan(
        [row],
        realm_uuid=REALM_UUID,
        bridge_instance_uuid=BRIDGE_UUID,
        identity_generation=1,
        enrollment_secret=ENROLLMENT_SECRET,
    )
    assert blockers == []
    _set_provider_projection(row, plan[0], **{override: value})

    repeated_plan, repeated_blockers, already_provider = (
        legacy_provider_outbox._conversion_plan(
            [row],
            realm_uuid=REALM_UUID,
            bridge_instance_uuid=BRIDGE_UUID,
            identity_generation=1,
            enrollment_secret=ENROLLMENT_SECRET,
        )
    )

    assert repeated_plan == []
    assert repeated_blockers == [
        {
            "external_operation_uuid": str(row["external_operation_uuid"]),
            "code": "provider_operation_mismatch",
        }
    ]
    assert already_provider == 0


def test_legacy_provider_conversion_rejects_signed_public_mismatch():
    row, _record = _signed_legacy_provider_row()
    row["target_uuid"] = sys_uuid.uuid4()

    plan, blockers, already_provider = legacy_provider_outbox._conversion_plan(
        [row],
        realm_uuid=REALM_UUID,
        bridge_instance_uuid=BRIDGE_UUID,
        identity_generation=1,
        enrollment_secret=ENROLLMENT_SECRET,
    )

    assert plan == []
    assert blockers == [
        {
            "external_operation_uuid": str(row["external_operation_uuid"]),
            "code": "legacy_public_operation_mismatch",
        }
    ]
    assert already_provider == 0


def test_legacy_provider_conversion_rejects_attempt_mismatch():
    row, _record = _signed_legacy_provider_row(
        public_status="manual_reconciliation_required",
        legacy_status="sent",
        attempt=2,
    )
    row["public_attempt"] = 3

    plan, blockers, already_provider = legacy_provider_outbox._conversion_plan(
        [row],
        realm_uuid=REALM_UUID,
        bridge_instance_uuid=BRIDGE_UUID,
        identity_generation=1,
        enrollment_secret=ENROLLMENT_SECRET,
    )

    assert plan == []
    assert blockers == [
        {
            "external_operation_uuid": str(row["external_operation_uuid"]),
            "code": "legacy_attempt_mismatch",
        }
    ]
    assert already_provider == 0


@pytest.mark.parametrize(
    ("public_status", "legacy_status", "raw_override", "blocker"),
    (
        ("queued", "sent", None, "legacy_delivery_state_ambiguous"),
        ("running", "queued", None, "legacy_delivery_state_ambiguous"),
        ("queued", "queued", b"not-a-signed-record", "legacy_transport_record_invalid"),
    ),
)
def test_legacy_provider_conversion_blocks_unsafe_transport_state(
    public_status,
    legacy_status,
    raw_override,
    blocker,
):
    row, _record = _signed_legacy_provider_row(
        public_status=public_status,
        legacy_status=legacy_status,
        raw_override=raw_override,
    )

    plan, blockers, _already_provider = legacy_provider_outbox._conversion_plan(
        [row],
        realm_uuid=REALM_UUID,
        bridge_instance_uuid=BRIDGE_UUID,
        identity_generation=1,
        enrollment_secret=ENROLLMENT_SECRET,
    )

    assert plan == []
    assert blockers == [
        {
            "external_operation_uuid": str(row["external_operation_uuid"]),
            "code": blocker,
        }
    ]


def test_freeze_rejects_source_that_advances_behind_writer_gate(monkeypatch):
    run_uuid = sys_uuid.uuid4()

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    responses = iter(
        (
            {"phase": "applying", "project_id": PROJECT_UUID},
            {"count": 0},
            {"count": 0},
        )
    )
    session = types.SimpleNamespace(
        execute=lambda statement, params: Result(next(responses))
    )
    first = mail_source.SourceCapture(_snapshot())
    second = mail_source.SourceCapture(
        snapshot.CanonicalSnapshot(
            PROJECT_UUID,
            snapshot.SourceCheckpoint(77, 13, 14, 91),
            (),
        )
    )
    captures = iter((first, second))
    coordinator = service.ImportCoordinator(
        types.SimpleNamespace(capture=lambda: next(captures)),
        types.SimpleNamespace(session=session),
    )
    gate_uuid = sys_uuid.uuid4()
    monkeypatch.setattr(
        service.writer_gate,
        "validate_closed_gate",
        lambda *args, **kwargs: {
            "gate_id": str(gate_uuid),
            "acquired_at": "2026-07-18T00:00:00+00:00",
            "lease_expires_at": "2026-07-18T00:10:00+00:00",
            "blocked_writer_classes": sorted(
                service.writer_gate.REQUIRED_WRITER_CLASSES
            ),
        },
    )

    with pytest.raises(ValueError, match="advanced"):
        coordinator.freeze(run_uuid, gate_uuid=gate_uuid)


def test_final_delta_aborts_on_gate_loss_before_source_capture(monkeypatch):
    run_uuid = sys_uuid.uuid4()
    gate_uuid = sys_uuid.uuid4()
    responses = iter(
        (
            {
                "phase": "frozen",
                "project_id": PROJECT_UUID,
                "source_uid_validity": 77,
                "freeze_confirmed_at": datetime.datetime.now(datetime.timezone.utc),
            },
            {
                "source_uid_validity": 77,
                "source_checkpoint_uid": 12,
                "snapshot_digest": "digest",
                "details": {"writer_gate": {"gate_id": str(gate_uuid)}},
            },
        )
    )
    session = types.SimpleNamespace(
        execute=lambda statement, params: types.SimpleNamespace(
            fetchone=lambda: next(responses)
        )
    )
    captures = []
    coordinator = service.ImportCoordinator(
        types.SimpleNamespace(capture=lambda: captures.append(True)),
        types.SimpleNamespace(session=session),
    )
    monkeypatch.setattr(
        service.writer_gate,
        "validate_closed_gate",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("gate lost")),
    )

    with pytest.raises(ValueError, match="gate lost"):
        coordinator.final_delta(run_uuid)

    assert captures == []


def test_parity_aborts_on_expired_ack_before_source_capture(monkeypatch):
    run_uuid = sys_uuid.uuid4()
    gate_uuid = sys_uuid.uuid4()
    row = {
        "source_uid_validity": 77,
        "source_checkpoint_uid": 12,
        "snapshot_digest": "digest",
        "details": {"writer_gate": {"gate_id": str(gate_uuid)}},
        "project_id": PROJECT_UUID,
    }
    session = types.SimpleNamespace(
        execute=lambda statement, params: types.SimpleNamespace(fetchone=lambda: row)
    )
    captures = []
    coordinator = service.ImportCoordinator(
        types.SimpleNamespace(capture=lambda: captures.append(True)),
        types.SimpleNamespace(session=session),
    )
    monkeypatch.setattr(
        service.writer_gate,
        "validate_closed_gate",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("ack expired")),
    )

    with pytest.raises(ValueError, match="ack expired"):
        coordinator.parity(run_uuid)

    assert captures == []


def test_import_service_does_not_own_nested_session_boundaries():
    sources = "\n".join(
        pathlib.Path(module.__file__).read_text()
        for module in (postgres_target, service)
    )

    assert "session_manager(" not in sources


def test_message_created_event_is_one_logical_row_for_many_recipients(monkeypatch):
    message_uuid = sys_uuid.uuid4()
    recipients = tuple(sys_uuid.uuid4() for _ in range(300))
    statements = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    def execute(statement, params):
        statements.append((statement, params))
        if "RETURNING epoch_version" in statement:
            return Result({"epoch_version": 81})
        return Result()

    session = types.SimpleNamespace(execute=execute)
    message = types.SimpleNamespace(uuid=message_uuid)
    monkeypatch.setattr(
        messenger_events,
        "_message_from_event_payload",
        lambda value, session=None: {
            "uuid": str(message_uuid),
            "payload": {"kind": "markdown", "content": "hello"},
        },
    )
    monkeypatch.setattr(
        messenger_events.models,
        "WorkspaceUserMessage",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_all=lambda **kwargs: [
                    types.SimpleNamespace(uuid=message_uuid, user_uuid=recipient)
                    for recipient in recipients
                ]
            )
        ),
    )

    result = messenger_events.create_message_events(
        PROJECT_UUID, message, recipients, session=session, compact=True
    )

    assert result == [81]
    assert len(statements) == 4
    assert "m_workspace_event_audience_snapshots_v1" in statements[0][0]
    assert "m_workspace_event_audience_members_v1" in statements[1][0]
    assert statements[1][1][1] == sorted(recipients, key=str)
    assert "m_workspace_broadcast_message_events_v1" in statements[2][0]
    assert "audience_snapshot_uuid" in statements[2][0]
    assert "UPDATE m_workspace_event_audience_snapshots_v1" in statements[3][0]
    assert "m_workspace_event_cursors" not in "\n".join(sql for sql, _ in statements)


def test_canonical_message_create_has_one_total_event_row_for_300_recipients(
    monkeypatch,
):
    author_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    recipients = (author_uuid,) + tuple(sys_uuid.uuid4() for _ in range(299))
    statements = []

    class Result:
        def fetchone(self):
            return {"epoch_version": 81}

    class Session:
        def execute(self, statement, params):
            statements.append((statement, params))
            return Result()

    class Message:
        def __init__(self, **values):
            self.__dict__.update(values)

        def insert(self, session=None):
            assert isinstance(session, Session)

        def get_recipients(self, session=None):
            assert isinstance(session, Session)
            return recipients

    class Flags:
        def __init__(self, **values):
            self.values = values

        def insert(self, session=None):
            assert isinstance(session, Session)

    monkeypatch.setattr(messenger_helpers.models, "WorkspaceMessage", Message)
    monkeypatch.setattr(
        messenger_helpers.models,
        "WorkspaceUserMessageFlags",
        Flags,
    )
    monkeypatch.setattr(
        messenger_events,
        "_message_from_event_payload",
        lambda value, session=None: {
            "uuid": str(value.uuid),
            "payload": {"kind": "markdown", "content": "hello"},
        },
    )
    monkeypatch.setattr(
        messenger_events.models,
        "WorkspaceUserMessage",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_all=lambda **kwargs: [
                    types.SimpleNamespace(uuid=message_uuid, user_uuid=recipient)
                    for recipient in recipients
                ]
            )
        ),
    )
    compact_unread_calls = []
    monkeypatch.setattr(
        messenger_helpers,
        "_create_compact_messages_unread_updated_events",
        lambda **kwargs: compact_unread_calls.append(kwargs),
    )

    result = messenger_helpers.create_workspace_user_message(
        project_id=PROJECT_UUID,
        user_uuid=author_uuid,
        uuid=message_uuid,
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        payload={"kind": "markdown", "content": "hello"},
        source_name="native",
        source={},
        session=Session(),
        return_visible=False,
        compact_events=True,
    )

    assert result.uuid == message_uuid
    assert len(statements) == 4
    assert (
        sum(
            "INSERT INTO m_workspace_event_audience_snapshots_v1" in sql
            for sql, _ in statements
        )
        == 1
    )
    assert (
        sum("m_workspace_event_audience_members_v1" in sql for sql, _ in statements)
        == 1
    )
    assert (
        sum("m_workspace_broadcast_message_events_v1" in sql for sql, _ in statements)
        == 1
    )
    assert sum("m_workspace_event_cursors" in sql for sql, _ in statements) == 0
    assert all('INSERT INTO "m_workspace_events"' not in sql for sql, _ in statements)
    assert compact_unread_calls[0]["user_uuids"] == list(recipients[1:])


def test_broadcast_payload_uses_modal_common_snapshot_and_minimal_override():
    author_uuid = sys_uuid.uuid4()
    recipients = [author_uuid, *(sys_uuid.uuid4() for _ in range(299))]
    resources = [
        types.SimpleNamespace(
            user_uuid=user_uuid,
            read=user_uuid == author_uuid,
            pinned=False,
            starred=False,
            is_own=user_uuid == author_uuid,
        )
        for user_uuid in recipients
    ]

    common, overrides = messenger_events._split_common_recipient_payloads(
        resources,
        lambda item, session=None: {
            "user_uuid": str(item.user_uuid),
            "read": item.read,
            "pinned": item.pinned,
            "starred": item.starred,
            "is_own": item.is_own,
            "payload": {"kind": "markdown", "content": "hello"},
            "provider": None,
            "delivery": None,
            "reactions": {},
        },
    )

    assert common["payload"]["content"] == "hello"
    assert common["read"] is False
    assert common["is_own"] is False
    assert common["provider"] is None
    assert common["reactions"] == {}
    assert overrides == {
        str(author_uuid): {"read": True, "is_own": True},
    }
    assert "user_uuid" not in common
    assert "payload" not in overrides[str(author_uuid)]


def test_broadcast_sql_normalizes_only_minimal_recipient_override():
    author_uuid = sys_uuid.uuid4()
    recipients = [author_uuid, *(sys_uuid.uuid4() for _ in range(299))]
    statements = []

    class Result:
        def fetchone(self):
            return {"epoch_version": 81}

    resources = [
        types.SimpleNamespace(
            user_uuid=user_uuid,
            uuid=sys_uuid.uuid4(),
            read=user_uuid == author_uuid,
        )
        for user_uuid in recipients
    ]
    messenger_events.create_resource_broadcast_event(
        PROJECT_UUID,
        sys_uuid.uuid4(),
        messenger_events.MESSAGE_CREATED_EVENT,
        resources,
        lambda item, session=None: {
            "user_uuid": str(item.user_uuid),
            "read": item.read,
            "payload": {"kind": "markdown", "content": "hello"},
            "provider": None,
        },
        session=types.SimpleNamespace(
            execute=lambda statement, params: (
                statements.append((statement, params)) or Result()
            )
        ),
    )

    override_statements = [
        item
        for item in statements
        if "m_workspace_event_recipient_payloads_v1" in item[0]
    ]
    assert len(override_statements) == 1
    overrides = __import__("json").loads(override_statements[0][1][1])
    assert overrides == {str(author_uuid): {"read": True}}
    broadcast_params = next(
        params
        for sql, params in statements
        if "INSERT INTO m_workspace_broadcast_message_events_v1" in sql
    )
    common_payload = __import__("json").loads(broadcast_params[-1])
    assert common_payload["payload"]["content"] == "hello"
    assert common_payload["read"] is False
    assert "m_workspace_event_cursors" not in "\n".join(sql for sql, _ in statements)


def test_compact_unread_keeps_topic_and_stream_events_without_folder_snapshots(
    monkeypatch,
):
    recipients = [sys_uuid.uuid4() for _ in range(300)]
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    user_streams = [
        types.SimpleNamespace(user_uuid=value, uuid=stream_uuid) for value in recipients
    ]
    user_topics = [
        types.SimpleNamespace(user_uuid=value, uuid=topic_uuid) for value in recipients
    ]
    monkeypatch.setattr(
        messenger_helpers.models,
        "WorkspaceUserStream",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_all=lambda **kwargs: user_streams)
        ),
    )
    monkeypatch.setattr(
        messenger_helpers.models,
        "WorkspaceUserTopic",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_all=lambda **kwargs: user_topics)
        ),
    )
    calls = []
    monkeypatch.setattr(
        messenger_events,
        "create_stream_updated_events",
        lambda *args, **kwargs: calls.append(("stream.updated", args, kwargs)),
    )
    monkeypatch.setattr(
        messenger_events,
        "create_topic_updated_events",
        lambda *args, **kwargs: calls.append(("topic.updated", args, kwargs)),
    )
    monkeypatch.setattr(
        messenger_events,
        "create_folder_updated_events",
        lambda *args, **kwargs: pytest.fail("message path must not snapshot folders"),
    )

    messenger_helpers._create_compact_messages_unread_updated_events(
        PROJECT_UUID,
        recipients,
        stream_uuid,
        topic_uuid,
        session=types.SimpleNamespace(),
    )

    assert [call[0] for call in calls] == ["topic.updated", "stream.updated"]
    assert all(call[2]["compact"] is True for call in calls)


def test_broadcast_event_migration_expands_visibility_without_recipient_rows():
    migration = (
        pathlib.Path(__file__).parents[3]
        / "migrations/0112-deduplicate-Messenger-message-recipient-events-6f42ab.py"
    ).read_text()

    assert "m_workspace_broadcast_message_events_v1" in migration
    assert "m_workspace_event_audience_snapshots_v1" in migration
    assert "m_workspace_event_audience_members_v1" in migration
    assert "m_workspace_event_recipient_payloads_v1" in migration
    assert '"audience_snapshot_uuid" UUID NOT NULL' in migration
    assert '"user_uuid", "audience_snapshot_uuid"' in migration
    assert "jsonb_build_object" in migration
    assert "m_workspace_broadcast_events_audience_epoch_idx" in migration
    assert '"recipient_payloads" JSONB' not in migration
    assert "m_workspace_event_cursors" not in migration
