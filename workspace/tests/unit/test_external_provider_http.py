# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import types
import uuid as sys_uuid

import pytest
from restalchemy.storage import exceptions as storage_exceptions

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
        if (
            statement.startswith(("SAVEPOINT", "ROLLBACK", "RELEASE"))
            or "pg_advisory_xact_lock" in statement
            or "set_config(" in statement
        ):
            return Result(None)
        if (
            "SELECT DISTINCT project_id" in statement
            and "FROM m_workspace_messages" in statement
        ):
            return Result([])
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
    session = Session(
        [
            _healthy_bridge(),
            _healthy_bridge(),
            [],
            None,
            [row],
            [{"uuid": row["external_operation_uuid"]}],
        ]
    )
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
    lease_statement, lease_params = next(
        (statement, params)
        for statement, params in session.statements
        if "FOR UPDATE OF operation SKIP LOCKED" in statement
    )
    assert "FOR UPDATE OF operation SKIP LOCKED" in lease_statement
    assert 'JOIN "m_external_provider_policies_v1" AS policy' in lease_statement
    assert 'policy."emergency_suspended" = FALSE' in lease_statement
    assert "FOR SHARE OF policy" in lease_statement
    assert lease_statement.index("page_snapshot") < lease_statement.index("LIMIT %s")
    assert lease_params[5] == 20
    assert any(
        'UPDATE "m_external_operations_v2"' in statement
        for statement, _params in session.statements
    )
    assert events == [
        (
            session,
            row["external_operation_uuid"],
            row["project_id"],
            provider_data.messenger_events.EXTERNAL_OPERATION_UPDATED_EVENT,
        )
    ]

    repeated = Session([_healthy_bridge(), _healthy_bridge(), [row]])
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
    assert len(repeated.statements) == 5


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
    session = Session([_healthy_bridge({}), _healthy_bridge({}), [], None])

    response = provider_data.lease_provider_operations(
        session,
        identity,
        request_uuid=sys_uuid.uuid4(),
        limit=10,
        lease_seconds=30,
        now=NOW,
    )

    assert response["operations"] == []
    assert len(session.statements) == 6
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
    assert len(session.statements) == 6


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
                "operation_kind": "message.create",
            },
            None,
            {"result_uuid": result_uuid},
            None,
            {"nonterminal_count": 0, "attempt": 2},
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
    assert session.statements[0][1] == (
        provider_data.read_state.READ_STATE_SCHEMA_LOCK_KEY,
    )
    assert "m_external_provider_operation_results_v1" in session.statements[5][0]
    assert "m_external_provider_operations_v1" in session.statements[6][0]
    assert "m_external_operations_v2" in session.statements[9][0]
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
                "operation_kind": "message.upsert",
            },
            None,
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
    assert "ON CONFLICT" in session.statements[5][0]
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
            {"matched": 1, "assignments": []},
            [{"provider_event_uuid": event_uuid}],
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
    project_lock = next(
        (statement, params)
        for statement, params in session.statements
        if "pg_advisory_xact_lock(hashtextextended(%s::text" in statement
    )
    assert project_lock[1] == (project_uuid,)
    route_gate, route_params = next(
        (statement, params)
        for statement, params in session.statements
        if "m_external_bridge_desired_resources_v1" in statement
    )
    assert route_params[5] == identity.bridge_instance_uuid
    assert route_params[8] == identity.bridge_instance_uuid
    assert route_params[10] == identity.bridge_instance_uuid
    assert any(
        "m_external_provider_events_v1" in statement
        for statement, _params in session.statements
    )
    assert not hasattr(session, "_workspace_provider_event_batch_cache")


def test_inbound_account_identity_event_does_not_require_chat_assignment():
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
        "kind": "identity.upsert",
        "payload": {
            "resource": {
                "uuid": str(target_uuid),
                "provider_metadata": {"chat_key": "account"},
            }
        },
    }
    session = Session(
        [
            _healthy_bridge(),
            {"matched": 1, "assignments": []},
            [{"provider_event_uuid": event_uuid}],
            None,
        ]
    )

    response = provider_data.apply_provider_event_batch(
        session,
        identity,
        [event],
        lambda *_args: target_uuid,
        now=NOW,
    )

    assert response["results"][0]["status"] == "applied"
    route_gate, params = next(
        (statement, params)
        for statement, params in session.statements
        if "requested.account_global" in statement
    )
    assert "requested.account_global" in route_gate
    assert "settings,default_project_id" in route_gate
    assert params[3] == [True]
    assert params[5] == identity.bridge_instance_uuid


@pytest.mark.parametrize("chat_key", [None, "channel:42"])
def test_chat_scoped_identity_event_still_requires_chat_assignment(chat_key):
    resource = {"uuid": str(sys_uuid.uuid4())}
    if chat_key is not None:
        resource["provider_metadata"] = {"chat_key": chat_key}
    event = {
        "kind": "identity.upsert",
        "payload": {"resource": resource},
    }

    assert provider_data._is_account_global_identity_event(event) is False


def test_inbound_event_batch_writes_provider_ledger_in_bulk():
    identity = _identity()
    project_uuid = sys_uuid.uuid4()
    events = [
        {
            "provider_event_uuid": str(sys_uuid.uuid4()),
            "external_account_uuid": str(sys_uuid.uuid4()),
            "external_chat_uuid": str(sys_uuid.uuid4()),
            "project_id": str(project_uuid),
            "kind": "message.create",
            "payload": {"content": f"message-{index}"},
        }
        for index in range(2)
    ]
    events[0]["provider_sequence"] = "evt-42"
    events[1]["provider_sequence"] = "001"
    inserted = [
        {"provider_event_uuid": sys_uuid.UUID(event["provider_event_uuid"])}
        for event in events
    ]
    session = Session(
        [_healthy_bridge(), {"matched": 2, "assignments": []}, inserted, None]
    )
    targets = {event["provider_event_uuid"]: sys_uuid.uuid4() for event in events}

    response = provider_data.apply_provider_event_batch(
        session,
        identity,
        events,
        lambda event, *_args: targets[event["provider_event_uuid"]],
        now=NOW,
    )

    assert [result["status"] for result in response["results"]] == [
        "applied",
        "applied",
    ]
    ledger_statements = [
        statement
        for statement, _params in session.statements
        if "m_external_provider_events_v1" in statement
    ]
    assert len(ledger_statements) == 2
    assert "FROM unnest(" in ledger_statements[0]
    assert "%s::text[]" in ledger_statements[0]
    assert "%s::bigint[]" not in ledger_statements[0]
    assert "WITH applied" in ledger_statements[1]
    ledger_params = next(
        params
        for statement, params in session.statements
        if 'INSERT INTO "m_external_provider_events_v1"' in statement
    )
    assert ledger_params[4] == ["evt-42", "001"]


def test_inbound_event_batch_flushes_broadcasts_before_completing_ledger(monkeypatch):
    identity = _identity()
    event_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    event = {
        "provider_event_uuid": str(event_uuid),
        "external_account_uuid": str(sys_uuid.uuid4()),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(project_uuid),
        "kind": "message.create",
        "payload": {"content": "hello"},
    }
    session = Session(
        [
            _healthy_bridge(),
            {"matched": 1, "assignments": []},
            [{"provider_event_uuid": event_uuid}],
            None,
        ]
    )

    def flush(request_session):
        request_session.statements.append(("FLUSH BROADCAST EVENTS", None))
        return [71]

    monkeypatch.setattr(
        provider_data.messenger_events,
        "flush_buffered_resource_broadcast_events",
        flush,
    )

    provider_data.apply_provider_event_batch(
        session,
        identity,
        [event],
        lambda *_args: sys_uuid.uuid4(),
        now=NOW,
    )

    statements = [statement for statement, _params in session.statements]
    flush_index = statements.index("FLUSH BROADCAST EVENTS")
    ledger_update_index = next(
        index
        for index, statement in enumerate(statements)
        if "WITH applied" in statement
    )
    assert flush_index < ledger_update_index


def test_inbound_event_batch_primes_authorized_assignments(monkeypatch):
    identity = _identity()
    event_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    assignment = {
        "account_uuid": str(account_uuid),
        "chat_uuid": str(chat_uuid),
        "project_id": str(project_uuid),
    }
    event = {
        "provider_event_uuid": str(event_uuid),
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(chat_uuid),
        "project_id": str(project_uuid),
        "kind": "message.create",
        "payload": {"content": "hello"},
    }
    session = Session(
        [
            _healthy_bridge(),
            {"matched": 1, "assignments": [assignment]},
            [{"provider_event_uuid": event_uuid}],
            None,
        ]
    )
    primed = []
    monkeypatch.setattr(
        provider_data.provider_event_apply,
        "prime_assignment_cache",
        lambda *args: primed.append(args),
    )

    provider_data.apply_provider_event_batch(
        session,
        identity,
        [event],
        lambda *_args: sys_uuid.uuid4(),
        now=NOW,
    )

    assert primed == [(session, identity, [assignment])]
    assignment_gate = next(
        statement
        for statement, _params in session.statements
        if "account_uuid, chat_uuid, project_id, account_global" in statement
    )
    assert "workspace_projection,stream,uuid" in assignment_gate
    assert 'chat."projection_stream_uuid"::text' in assignment_gate


def test_inbound_quiet_message_backfill_uses_read_state_project_lock():
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
        "kind": "message.upsert",
        "payload": {"resource": {"provider_metadata": {"delivery_class": "backfill"}}},
    }
    session = Session(
        [
            _healthy_bridge(),
            {"matched": 1, "assignments": []},
            [{"provider_event_uuid": event_uuid}],
            None,
        ]
    )

    response = provider_data.apply_provider_event_batch(
        session,
        identity,
        [event],
        lambda *_args: target_uuid,
        now=NOW,
    )

    assert response["results"][0]["status"] == "applied"
    lock_statement = next(
        (statement, params)
        for statement, params in session.statements
        if "pg_advisory_xact_lock(hashtextextended(%s::text" in statement
    )
    assert lock_statement[1] == (project_uuid,)


def test_inbound_duplicate_message_upserts_lock_structure_before_projects(
    monkeypatch,
):
    identity = _identity()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    events = [
        {
            "provider_event_uuid": str(sys_uuid.uuid4()),
            "external_account_uuid": str(account_uuid),
            "external_chat_uuid": str(chat_uuid),
            "project_id": str(project_uuid),
            "kind": "message.upsert",
            "payload": {"resource": {"uuid": str(message_uuid)}},
        }
        for _index in range(2)
    ]
    session = Session(
        [
            _healthy_bridge(),
            {
                "matched": 1,
                "assignments": [
                    {
                        "account_uuid": str(account_uuid),
                        "chat_uuid": str(chat_uuid),
                        "project_id": str(project_uuid),
                    }
                ],
            },
            [
                {"provider_event_uuid": sys_uuid.UUID(event["provider_event_uuid"])}
                for event in events
            ],
            None,
        ]
    )
    lock_calls = []

    def capture_lock(
        current_session,
        project_ids,
        message_uuids,
        *,
        structural_batch,
    ):
        lock_calls.append(
            (current_session, project_ids, message_uuids, structural_batch)
        )
        return project_ids

    monkeypatch.setattr(
        provider_data,
        "_lock_provider_event_projects",
        capture_lock,
    )
    monkeypatch.setattr(
        provider_data.provider_event_apply,
        "prime_assignment_cache",
        lambda *_args: None,
    )

    response = provider_data.apply_provider_event_batch(
        session,
        identity,
        events,
        lambda *_args: message_uuid,
        now=NOW,
    )

    assert [result["status"] for result in response["results"]] == [
        "applied",
        "applied",
    ]
    assert lock_calls == [
        (
            session,
            [project_uuid],
            [message_uuid, message_uuid],
            True,
        )
    ]


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

    assert len(session.statements) == 2
    assert "workspace-read-state-schema-v1" in session.statements[0][1]


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
    session = Session([_healthy_bridge(), {"matched": 0}])

    with pytest.raises(provider_data.ProviderBatchError, match="not assigned"):
        provider_data.apply_provider_event_batch(
            session,
            requesting_bridge,
            [event],
            lambda *_args: pytest.fail("foreign assignment must not be applied"),
            now=NOW,
        )

    route_params = next(
        params
        for statement, params in session.statements
        if "requested.account_global" in statement
    )
    assert route_params[5] == requesting_bridge.bridge_instance_uuid
    assert route_params[5] != assigned_bridge.bridge_instance_uuid


def test_inbound_event_batch_reports_storage_conflicts_as_batch_rejections():
    identity = _identity()
    event_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
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
            {"matched": 1, "assignments": []},
            [{"provider_event_uuid": event_uuid}],
        ]
    )

    with pytest.raises(provider_data.ProviderBatchError, match="stale identity"):
        provider_data.apply_provider_event_batch(
            session,
            identity,
            [event],
            lambda *_args: (_ for _ in ()).throw(
                storage_exceptions.ConflictRecords(
                    model="WorkspaceMessage",
                    msg="stale identity",
                )
            ),
            now=NOW,
        )

    assert not hasattr(session, "_workspace_provider_event_batch_cache")


def test_provider_http_service_dispatches_only_private_provider_routes():
    identity = _identity()
    database_now = datetime.datetime.now(datetime.timezone.utc)
    healthy = _healthy_bridge()
    healthy["last_heartbeat_at"] = database_now
    session = Session(
        [
            {"current_time": database_now},
            healthy,
            {"current_time": database_now},
            healthy,
            [],
            None,
            [],
        ]
    )
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
    session = Session(
        [
            _healthy_bridge(capabilities),
            _healthy_bridge(capabilities),
            [],
            None,
            [],
            [],
        ]
    )

    provider_data.lease_provider_operations(
        session,
        identity,
        request_uuid=sys_uuid.uuid4(),
        limit=100,
        lease_seconds=30,
        now=NOW,
    )

    allowed = next(
        params[4]
        for statement, params in session.statements
        if "WITH bridge_capabilities AS MATERIALIZED" in statement
    )
    assert "message.create" in allowed
    assert "unknown.operation" not in allowed
