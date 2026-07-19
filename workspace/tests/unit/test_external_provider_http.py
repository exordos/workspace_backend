# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import types
import uuid as sys_uuid

import pytest

from workspace.external_bridge_control import provider_data
from workspace.external_bridge_control import provider_service


NOW = datetime.datetime(2026, 7, 18, 9, 0, tzinfo=datetime.timezone.utc)


class Result:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


class Session:
    def __init__(self, values):
        self.values = iter(values)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if statement.startswith(("SAVEPOINT", "ROLLBACK", "RELEASE")):
            return Result(None)
        return Result(next(self.values))


def _identity():
    return types.SimpleNamespace(
        bridge_instance_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
        identity_generation=3,
    )


def _healthy_bridge(capabilities=None):
    return {
        "status": "active",
        "capabilities": (
            {"messenger.message.send": {"revision": 1}}
            if capabilities is None
            else capabilities
        ),
        "last_heartbeat_at": NOW,
    }


def _leased_row(identity, request_uuid):
    return {
        "uuid": sys_uuid.uuid4(),
        "external_operation_uuid": sys_uuid.uuid4(),
        "bridge_instance_uuid": identity.bridge_instance_uuid,
        "external_account_uuid": sys_uuid.uuid4(),
        "project_id": sys_uuid.uuid4(),
        "operation_kind": "message.create",
        "payload": {"content": "hello"},
        "attempt": 1,
        "lease_uuid": request_uuid,
        "lease_expires_at": NOW + datetime.timedelta(seconds=30),
    }


def test_lease_is_fifo_idempotent_and_reuses_request_session(monkeypatch):
    identity = _identity()
    request_uuid = sys_uuid.uuid4()
    row = _leased_row(identity, request_uuid)
    session = Session([_healthy_bridge(), [], None, [row], None])
    events = []
    monkeypatch.setattr(
        provider_data,
        "_emit_operation_event",
        lambda *args: events.append(args),
    )

    response = provider_data.lease_provider_operations(
        session,
        identity,
        request_uuid=request_uuid,
        limit=20,
        lease_seconds=30,
        now=NOW,
    )

    assert response["request_uuid"] == str(request_uuid)
    assert response["operations"][0]["required_capability"] == (
        "messenger.message.send"
    )
    assert "FOR UPDATE SKIP LOCKED" in session.statements[3][0]
    assert session.statements[3][1][3] == 20
    assert "m_external_operations_v2" in session.statements[4][0]
    assert events == [
        (
            session,
            row["external_operation_uuid"],
            row["project_id"],
            provider_data.messenger_events.EXTERNAL_OPERATION_UPDATED_EVENT,
        )
    ]

    repeated = Session([_healthy_bridge(), [row]])
    assert (
        provider_data.lease_provider_operations(
            repeated,
            identity,
            request_uuid=request_uuid,
            limit=20,
            lease_seconds=30,
            now=NOW,
        )
        == response
    )
    assert len(repeated.statements) == 2


def test_lease_requires_current_compatible_heartbeat():
    identity = _identity()
    stale = {
        **_healthy_bridge(),
        "last_heartbeat_at": NOW - datetime.timedelta(seconds=61),
    }
    session = Session([stale])

    with pytest.raises(provider_data.ProviderUnavailableError):
        provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=1,
            lease_seconds=30,
            now=NOW,
        )


def test_missing_capability_keeps_known_operation_out_of_lease():
    identity = _identity()
    session = Session([_healthy_bridge({}), [], None])

    response = provider_data.lease_provider_operations(
        session,
        identity,
        request_uuid=sys_uuid.uuid4(),
        limit=10,
        lease_seconds=30,
        now=NOW,
    )

    assert response["operations"] == []
    assert len(session.statements) == 3
    assert not any("ANY(" in statement for statement, _params in session.statements)


def test_disabled_capability_descriptor_is_not_leasable():
    identity = _identity()
    session = Session(
        [
            _healthy_bridge(
                {
                    "messenger.message.send": {
                        "available": False,
                        "revision": 1,
                    }
                }
            ),
            [],
            None,
        ]
    )

    response = provider_data.lease_provider_operations(
        session,
        identity,
        request_uuid=sys_uuid.uuid4(),
        limit=10,
        lease_seconds=30,
        now=NOW,
    )

    assert response["operations"] == []
    assert len(session.statements) == 3


def test_terminal_result_updates_queue_and_public_operation_once(monkeypatch):
    identity = _identity()
    result_uuid = sys_uuid.uuid4()
    operation_uuid = sys_uuid.uuid4()
    external_operation_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    lease_uuid = sys_uuid.uuid4()
    session = Session(
        [
            None,
            {
                "external_operation_uuid": external_operation_uuid,
                "project_id": project_uuid,
                "status": "leased",
                "lease_uuid": lease_uuid,
                "attempt": 2,
            },
            {"result_uuid": result_uuid},
            None,
            None,
        ]
    )
    events = []
    monkeypatch.setattr(
        provider_data,
        "_emit_operation_event",
        lambda *args: events.append(args),
    )

    response = provider_data.report_provider_result(
        session,
        identity,
        {
            "result_uuid": str(result_uuid),
            "provider_operation_uuid": str(operation_uuid),
            "lease_uuid": str(lease_uuid),
            "status": "succeeded",
            "safe_error": None,
        },
        now=NOW,
    )

    assert response == {"result_uuid": str(result_uuid), "status": "applied"}
    assert "m_external_provider_operation_results_v1" in session.statements[2][0]
    assert "m_external_provider_operations_v1" in session.statements[3][0]
    assert "m_external_operations_v2" in session.statements[4][0]
    assert events == [
        (
            session,
            external_operation_uuid,
            project_uuid,
            provider_data.messenger_events.EXTERNAL_OPERATION_UPDATED_EVENT,
        )
    ]


def test_result_batch_partially_accepts_and_deduplicates_items():
    identity = _identity()
    result_uuid = sys_uuid.uuid4()
    operation_uuid = sys_uuid.uuid4()
    lease_uuid = sys_uuid.uuid4()
    valid = {
        "result_uuid": str(result_uuid),
        "provider_operation_uuid": str(operation_uuid),
        "lease_uuid": str(lease_uuid),
        "status": "failed",
        "safe_error": "provider unavailable",
    }
    session = Session(
        [
            {
                "operation_uuid": operation_uuid,
                "payload_sha256": provider_data._sha256(valid),
            }
        ]
    )

    response = provider_data.report_provider_results(
        session,
        identity,
        [valid, {"result_uuid": "not-a-uuid"}],
        now=NOW,
    )

    assert response["results"] == [
        {"result_uuid": str(result_uuid), "status": "duplicate"},
        {"result_uuid": "not-a-uuid", "status": "rejected"},
    ]


def test_concurrent_result_uuid_conflict_does_not_complete_operation():
    identity = _identity()
    result_uuid = sys_uuid.uuid4()
    operation_uuid = sys_uuid.uuid4()
    lease_uuid = sys_uuid.uuid4()
    result = {
        "result_uuid": str(result_uuid),
        "provider_operation_uuid": str(operation_uuid),
        "lease_uuid": str(lease_uuid),
        "status": "succeeded",
    }
    session = Session(
        [
            None,
            {
                "external_operation_uuid": sys_uuid.uuid4(),
                "status": "leased",
                "lease_uuid": lease_uuid,
                "attempt": 1,
            },
            None,
            {
                "operation_uuid": sys_uuid.uuid4(),
                "payload_sha256": provider_data._sha256(result),
            },
        ]
    )

    response = provider_data.report_provider_result(
        session,
        identity,
        result,
        now=NOW,
    )

    assert response == {"result_uuid": str(result_uuid), "status": "conflict"}
    assert "ON CONFLICT" in session.statements[2][0]
    assert not any(
        'UPDATE "m_external_provider_operations_v1"' in statement
        for statement, _params in session.statements
    )


def test_inbound_event_batch_uses_one_transaction_and_deduplicates():
    identity = _identity()
    event_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    target_uuid = sys_uuid.uuid4()
    event = {
        "provider_event_uuid": str(event_uuid),
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(project_uuid),
        "kind": "message.create",
        "payload": {"content": "hello"},
    }
    session = Session(
        [
            _healthy_bridge(),
            {"exists": 1},
            {"provider_event_uuid": event_uuid},
            None,
        ]
    )
    applied = []

    response = provider_data.apply_provider_event_batch(
        session,
        identity,
        [event],
        lambda item, request_session, request_identity: (
            applied.append((item, request_session, request_identity)) or target_uuid
        ),
        now=NOW,
    )

    assert response["results"][0]["status"] == "applied"
    assert response["results"][0]["target_uuid"] == str(target_uuid)
    assert applied == [(event, session, identity)]
    assert "m_external_bridge_desired_resources_v1" in session.statements[1][0]
    assert session.statements[1][1][2] == identity.bridge_instance_uuid
    assert session.statements[1][1][4] == identity.bridge_instance_uuid
    assert "m_external_provider_events_v1" in session.statements[2][0]


def test_inbound_event_batch_requires_current_heartbeat_before_account_access():
    identity = _identity()
    stale = {
        **_healthy_bridge(),
        "last_heartbeat_at": NOW - datetime.timedelta(seconds=61),
    }
    session = Session([stale])

    with pytest.raises(provider_data.ProviderUnavailableError):
        provider_data.apply_provider_event_batch(
            session,
            identity,
            [
                {
                    "provider_event_uuid": str(sys_uuid.uuid4()),
                    "external_account_uuid": str(sys_uuid.uuid4()),
                    "project_id": str(sys_uuid.uuid4()),
                    "kind": "message.upsert",
                    "payload": {"resource": {}},
                }
            ],
            lambda *_args: pytest.fail("stale bridge must not apply events"),
            now=NOW,
        )

    assert len(session.statements) == 1


def test_inbound_event_batch_rejects_another_bridge_assignment():
    assigned_bridge = _identity()
    requesting_bridge = _identity()
    requesting_bridge.provider_kind = assigned_bridge.provider_kind
    event = {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(sys_uuid.uuid4()),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(sys_uuid.uuid4()),
        "kind": "message.upsert",
        "payload": {"resource": {}},
    }
    session = Session([_healthy_bridge(), None])

    with pytest.raises(provider_data.ProviderBatchError, match="not assigned"):
        provider_data.apply_provider_event_batch(
            session,
            requesting_bridge,
            [event],
            lambda *_args: pytest.fail("foreign assignment must not be applied"),
            now=NOW,
        )

    assert session.statements[1][1][2] == requesting_bridge.bridge_instance_uuid
    assert session.statements[1][1][2] != assigned_bridge.bridge_instance_uuid


def test_provider_http_service_dispatches_only_private_provider_routes():
    identity = _identity()
    healthy = _healthy_bridge()
    healthy["last_heartbeat_at"] = datetime.datetime.now(datetime.timezone.utc)
    session = Session([healthy, [], None, []])
    api = provider_service.ProviderDataService()

    response = api.handle(
        session,
        identity,
        "POST",
        f"{provider_service.API_ROOT}/operations/actions/lease",
        {},
        {
            "request_uuid": str(sys_uuid.uuid4()),
            "limit": 5,
            "lease_seconds": 30,
        },
    )

    assert response["operations"] == []
    assert (
        api.handle(session, identity, "GET", "/v1/desired-state/changes", {}, None)
        is None
    )
    with pytest.raises(provider_service.ProviderIngressUnavailableError):
        api.handle(
            session,
            identity,
            "POST",
            f"{provider_service.API_ROOT}/events",
            {},
            {"events": [{}]},
        )


def test_rejected_result_rolls_back_savepoint_without_queue_mutation():
    identity = _identity()
    session = Session([])
    result_uuid = sys_uuid.uuid4()

    response = provider_data.report_provider_results(
        session,
        identity,
        [
            {
                "result_uuid": str(result_uuid),
                "provider_operation_uuid": str(sys_uuid.uuid4()),
                "lease_uuid": str(sys_uuid.uuid4()),
                "status": "manual_reconciliation_required",
                "reconciliation": {"reason": "not-a-supported-reason"},
            }
        ],
        now=NOW,
    )

    assert response == {
        "results": [{"result_uuid": str(result_uuid), "status": "rejected"}]
    }
    statements = [statement for statement, _params in session.statements]
    assert statements == [
        "SAVEPOINT provider_result_item",
        "ROLLBACK TO SAVEPOINT provider_result_item",
        "RELEASE SAVEPOINT provider_result_item",
    ]


def test_unknown_operation_kind_is_not_in_capability_allow_list():
    identity = _identity()
    capabilities = {
        capability: {"revision": 1}
        for capability in provider_data._OPERATION_CAPABILITIES.values()
    }
    session = Session([_healthy_bridge(capabilities), [], None, []])

    provider_data.lease_provider_operations(
        session,
        identity,
        request_uuid=sys_uuid.uuid4(),
        limit=100,
        lease_seconds=30,
        now=NOW,
    )

    allowed = session.statements[3][1][2]
    assert "message.create" in allowed
    assert "unknown.operation" not in allowed
