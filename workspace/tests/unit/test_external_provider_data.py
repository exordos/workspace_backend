# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import types
import uuid as sys_uuid
import datetime

import pytest

from workspace.external_bridge_control import provider_data


class LeaseResponse:
    def __init__(self, *, one=None, all_rows=()):
        self.one = one
        self.all_rows = list(all_rows)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all_rows


class CapabilityLeaseSession:
    def __init__(self, capabilities, now):
        self.capabilities = capabilities
        self.now = now
        self.allowed_kinds = None

    def execute(self, statement, params):
        if 'FROM "m_external_bridge_instances_v2"' in statement:
            return LeaseResponse(
                one={
                    "status": "active",
                    "capabilities": self.capabilities,
                    "last_heartbeat_at": self.now,
                }
            )
        if 'AND "lease_uuid" = %s' in statement and statement.lstrip().startswith(
            "SELECT"
        ):
            return LeaseResponse(all_rows=[])
        if "WITH candidates AS" in statement:
            self.allowed_kinds = params[2]
            return LeaseResponse(all_rows=[])
        return LeaseResponse()


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        ({"messenger.message.send": {"revision": 1}}, False),
        (
            {
                "messenger.message.send": {"revision": 1},
                "messenger.message.read": {"revision": 1},
            },
            True,
        ),
    ],
)
def test_read_state_lease_fails_closed_without_advertised_capability(
    capabilities,
    expected,
):
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    session = CapabilityLeaseSession(capabilities, now)
    identity = types.SimpleNamespace(
        bridge_instance_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
        identity_generation=1,
    )

    result = provider_data.lease_provider_operations(
        session,
        identity,
        request_uuid=sys_uuid.uuid4(),
        limit=10,
        lease_seconds=30,
        now=now,
    )

    assert result["operations"] == []
    assert ("read_state.set" in session.allowed_kinds) is expected
    assert provider_data._required_capability("read_state.set") == (
        "messenger.message.read"
    )


def test_enqueue_operation_reuses_caller_transaction(monkeypatch):
    inserted = []
    events = []

    class FakeOperation:
        def __init__(self, **values):
            values.setdefault("safe_error", None)
            values.setdefault("can_retry", False)
            values.setdefault("can_discard", False)
            values.setdefault("duplicate_risk", False)
            values.setdefault("retry_requires_confirmation", False)
            values.setdefault("original_url", None)
            values.setdefault("reconciliation_reason", None)
            values.setdefault(
                "updated_at",
                datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc),
            )
            self.values = values
            self.__dict__.update(values)

        def insert(self, session=None):
            inserted.append((self.values, session))

    statements = []
    session = types.SimpleNamespace(
        execute=lambda statement, params: (
            statements.append((statement, params))
            or types.SimpleNamespace(fetchone=lambda: None)
        )
    )
    monkeypatch.setattr(
        provider_data.external_models,
        "ExternalOperation",
        FakeOperation,
    )
    monkeypatch.setattr(
        provider_data.messenger_events,
        "create_external_resource_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    operation_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    owner_user_uuid = sys_uuid.uuid4()

    _operation, record_uuid = provider_data.enqueue_provider_operation(
        session,
        operation_uuid=operation_uuid,
        bridge_instance_uuid=sys_uuid.uuid4(),
        external_account_uuid=sys_uuid.uuid4(),
        project_id=project_uuid,
        owner_user_uuid=owner_user_uuid,
        operation_kind="message.create",
        target_type="message",
        target_uuid=sys_uuid.uuid4(),
        payload={"payload": {"kind": "markdown", "content": "hello"}},
    )

    assert inserted[0][1] is session
    assert inserted[0][0]["uuid"] == operation_uuid
    assert isinstance(record_uuid, sys_uuid.UUID)
    assert 'INSERT INTO "m_external_provider_operations_v1"' in statements[0][0]
    assert events[0][0][0:2] == (project_uuid, owner_user_uuid)
    assert events[0][0][2] is _operation
    assert (
        events[0][0][3]
        == provider_data.messenger_events.EXTERNAL_OPERATION_CREATED_EVENT
    )
    assert events[0][1]["session"] is session


def test_resolve_provider_target_intersects_account_and_chat_capabilities(monkeypatch):
    account = types.SimpleNamespace(
        uuid=sys_uuid.uuid4(),
        provider="zulip",
        live_ready=True,
        capabilities={"messenger.message.send": {"available": True}},
    )
    chat = types.SimpleNamespace(
        capabilities={"messenger.message.send": {"available": False}},
    )

    class OneObject:
        def get_one(self, **kwargs):
            return account

    class ChatObjects:
        def get_all(self, **kwargs):
            return [chat]

    class BridgeObjects:
        def get_all(self, **kwargs):
            raise AssertionError("bridge lookup must not run after chat rejection")

    monkeypatch.setattr(
        provider_data.external_models.ExternalAccount, "objects", OneObject()
    )
    monkeypatch.setattr(
        provider_data.external_models.ExternalChat, "objects", ChatObjects()
    )
    monkeypatch.setattr(
        provider_data.external_models.ExternalBridgeInstance,
        "objects",
        BridgeObjects(),
    )

    with pytest.raises(provider_data.ProviderUnavailableError):
        provider_data.resolve_provider_target(
            object(),
            project_id=sys_uuid.uuid4(),
            owner_user_uuid=sys_uuid.uuid4(),
            external_account_uuid=account.uuid,
            stream_uuid=sys_uuid.uuid4(),
            capability_name="messenger.message.send",
        )


def test_publish_operation_event_updates_target_delivery_in_same_transaction(
    monkeypatch,
):
    project_uuid = sys_uuid.uuid4()
    target_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    updated_at = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    operation = types.SimpleNamespace(
        uuid=sys_uuid.uuid4(),
        owner_user_uuid=owner_uuid,
        target_type="message",
        target_uuid=target_uuid,
        status="succeeded",
        safe_error=None,
        can_retry=False,
        can_discard=False,
        updated_at=updated_at,
        duplicate_risk=False,
        retry_requires_confirmation=False,
        original_url="https://zulip.example.invalid/#narrow/id/42",
        reconciliation_reason=None,
    )
    statements = []
    session = types.SimpleNamespace(
        execute=lambda statement, params: (
            statements.append((statement, params))
            or types.SimpleNamespace(fetchone=lambda: {"uuid": target_uuid})
        )
    )
    target_resource = object()
    target_queries = []
    monkeypatch.setattr(
        provider_data.models.WorkspaceUserMessage,
        "objects",
        types.SimpleNamespace(
            get_all=lambda **kwargs: target_queries.append(kwargs) or [target_resource]
        ),
    )
    external_events = []
    target_events = []
    monkeypatch.setattr(
        provider_data.messenger_events,
        "create_external_resource_event",
        lambda *args, **kwargs: external_events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        provider_data.messenger_events,
        "create_message_updated_event",
        lambda *args, **kwargs: target_events.append((args, kwargs)),
    )

    provider_data.publish_operation_event(
        session,
        operation,
        project_uuid,
        provider_data.messenger_events.EXTERNAL_OPERATION_UPDATED_EVENT,
    )

    assert external_events[0][1]["session"] is session
    assert "UPDATE m_workspace_messages" in statements[0][0]
    assert statements[0][1][1:4] == ("delivered", None, updated_at)
    assert target_queries[0]["session"] is session
    assert target_events == [((target_resource,), {"session": session})]


def test_retry_operation_requeues_existing_provider_row():
    operation_uuid = sys_uuid.uuid4()
    row_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    statements = []
    session = types.SimpleNamespace(
        execute=lambda statement, params: (
            statements.append((statement, params))
            or types.SimpleNamespace(
                fetchone=lambda: {"uuid": row_uuid, "project_id": project_uuid}
            )
        )
    )

    result = provider_data.retry_provider_operation(
        session,
        external_operation_uuid=operation_uuid,
        next_attempt=3,
    )

    assert result == {"uuid": row_uuid, "project_id": project_uuid}
    assert statements[0][1] == (3, operation_uuid)
    assert "\"status\" = 'queued'" in statements[0][0]
    assert '"attempt" = %s - 1' in statements[0][0]
    assert '"lease_uuid" = NULL' in statements[0][0]


def test_discard_operation_prevents_future_provider_lease():
    operation_uuid = sys_uuid.uuid4()
    row_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    statements = []
    session = types.SimpleNamespace(
        execute=lambda statement, params: (
            statements.append((statement, params))
            or types.SimpleNamespace(
                fetchone=lambda: {"uuid": row_uuid, "project_id": project_uuid}
            )
        )
    )

    result = provider_data.discard_provider_operation(
        session,
        external_operation_uuid=operation_uuid,
    )

    assert result == {"uuid": row_uuid, "project_id": project_uuid}
    assert statements[0][1] == (operation_uuid,)
    assert "\"status\" = 'discarded'" in statements[0][0]


class ProviderEventSession:
    def __init__(self, results):
        self.results = iter(results)
        self.statements = []

    def execute(self, statement, params):
        self.statements.append((statement, params))
        row = next(self.results)
        return types.SimpleNamespace(fetchone=lambda: row)


def test_provider_event_is_deduplicated_before_canonical_mutation():
    event_uuid = sys_uuid.uuid4()
    target_uuid = sys_uuid.uuid4()
    session = ProviderEventSession(
        [{"provider_event_uuid": event_uuid}, None],
    )
    applied = []

    result = provider_data.apply_provider_event(
        session,
        bridge_instance_uuid=sys_uuid.uuid4(),
        external_account_uuid=sys_uuid.uuid4(),
        project_id=sys_uuid.uuid4(),
        event={
            "provider_event_uuid": str(event_uuid),
            "kind": "message.create",
            "payload": {"kind": "markdown", "content": "hello"},
        },
        apply=lambda event, current_session: (
            applied.append((event, current_session)) or target_uuid
        ),
    )

    assert result["status"] == "applied"
    assert result["duplicate"] is False
    assert result["target_uuid"] == str(target_uuid)
    assert applied[0][1] is session
    assert "SET \"status\" = 'applied'" in session.statements[1][0]


def test_duplicate_provider_event_does_not_repeat_mutation():
    event_uuid = sys_uuid.uuid4()
    event = {
        "provider_event_uuid": str(event_uuid),
        "kind": "message.create",
        "payload": {"kind": "markdown", "content": "hello"},
    }
    session = ProviderEventSession(
        [
            None,
            {
                "payload_sha256": provider_data._sha256(event),
                "status": "applied",
                "target_uuid": sys_uuid.uuid4(),
                "safe_error": None,
            },
        ],
    )

    result = provider_data.apply_provider_event(
        session,
        bridge_instance_uuid=sys_uuid.uuid4(),
        external_account_uuid=sys_uuid.uuid4(),
        project_id=sys_uuid.uuid4(),
        event=event,
        apply=lambda event, current_session: pytest.fail(
            "duplicate event must not mutate canonical state"
        ),
    )

    assert result["status"] == "applied"
    assert result["duplicate"] is True
    assert isinstance(result["target_uuid"], str)


def test_provider_event_uuid_reuse_with_different_payload_is_rejected():
    event_uuid = sys_uuid.uuid4()
    session = ProviderEventSession(
        [
            None,
            {
                "payload_sha256": "0" * 64,
                "status": "applied",
                "target_uuid": None,
                "safe_error": None,
            },
        ],
    )

    with pytest.raises(ValueError, match="reused with different input"):
        provider_data.apply_provider_event(
            session,
            bridge_instance_uuid=sys_uuid.uuid4(),
            external_account_uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            event={
                "provider_event_uuid": str(event_uuid),
                "kind": "message.delete",
            },
            apply=lambda event, current_session: None,
        )
