# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import types
import uuid as sys_uuid
import datetime

import pytest

from workspace.external_bridge_control import provider_data


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("message.upsert", True),
        ("reaction.upsert", True),
        ("reaction.delete", True),
        ("topic.upsert", True),
        ("identity.upsert", False),
        ("stream.upsert", False),
        ("read_state.set", False),
    ],
)
def test_only_fully_suppressed_backfill_events_bypass_project_lock(kind, expected):
    event = {
        "kind": kind,
        "payload": {"resource": {"provider_metadata": {"delivery_class": "backfill"}}},
    }

    assert provider_data._is_quiet_backfill_event(event) is expected


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


@pytest.mark.parametrize("operation_kind", ["membership.add", "membership.remove"])
def test_membership_lease_requires_write_capability(operation_kind):
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    session = CapabilityLeaseSession(
        {"messenger.membership.write": {"revision": 1}},
        now,
    )
    identity = types.SimpleNamespace(
        bridge_instance_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
        identity_generation=1,
    )

    provider_data.lease_provider_operations(
        session,
        identity,
        request_uuid=sys_uuid.uuid4(),
        limit=10,
        lease_seconds=30,
        now=now,
    )

    assert operation_kind in session.allowed_kinds
    assert provider_data._required_capability(operation_kind) == (
        "messenger.membership.write"
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
        status="live",
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
    policy_checks = []
    monkeypatch.setattr(
        provider_data,
        "_require_current_provider_policy",
        lambda *args, **kwargs: policy_checks.append((args, kwargs)) or {},
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
    assert policy_checks[0][0][1] == "zulip"
    assert policy_checks[0][1]["capability_name"] == "messenger.message.send"
    assert policy_checks[0][1]["capabilities"] is account.capabilities


def test_resolve_provider_target_allows_capable_chat_during_backfill(monkeypatch):
    account = types.SimpleNamespace(
        uuid=sys_uuid.uuid4(),
        provider="zulip",
        status="backfill",
        live_ready=False,
        capabilities={"messenger.message.send": {"available": True}},
    )
    chat = types.SimpleNamespace(
        uuid=sys_uuid.uuid4(),
        capabilities={"messenger.message.send": {"available": True}},
    )
    bridge = types.SimpleNamespace(uuid=sys_uuid.uuid4())

    monkeypatch.setattr(
        provider_data.external_models.ExternalAccount,
        "objects",
        types.SimpleNamespace(get_one=lambda **kwargs: account),
    )
    chat_calls = []
    monkeypatch.setattr(
        provider_data.external_models.ExternalChat,
        "objects",
        types.SimpleNamespace(
            get_all=lambda **kwargs: chat_calls.append(kwargs) or [chat]
        ),
    )
    monkeypatch.setattr(
        provider_data,
        "_require_current_provider_policy",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        provider_data,
        "_lock_associated_bridge",
        lambda *args, **kwargs: bridge,
    )
    input_checks = []
    monkeypatch.setattr(
        provider_data,
        "_require_current_provider_inputs",
        lambda *args, **kwargs: input_checks.append((args, kwargs)),
    )

    assert provider_data.resolve_provider_target(
        object(),
        project_id=sys_uuid.uuid4(),
        owner_user_uuid=sys_uuid.uuid4(),
        external_account_uuid=account.uuid,
        stream_uuid=sys_uuid.uuid4(),
        capability_name="messenger.message.send",
    ) == (account, chat, bridge)
    statuses = chat_calls[0]["filters"]["status"].value
    assert set(statuses) == {"syncing", "live"}
    assert input_checks[0][1]["chat_uuid"] == chat.uuid


def test_resolve_provider_queue_target_preserves_route_without_capability(monkeypatch):
    account = types.SimpleNamespace(
        uuid=sys_uuid.uuid4(),
        provider="zulip",
        live_ready=False,
        capabilities={},
    )
    chat = types.SimpleNamespace(capabilities={})
    bridge = types.SimpleNamespace(uuid=sys_uuid.uuid4())

    class OneObject:
        def get_one(self, **kwargs):
            return account

    class ChatObjects:
        def get_all(self, **kwargs):
            return [chat]

    monkeypatch.setattr(
        provider_data.external_models.ExternalAccount, "objects", OneObject()
    )
    monkeypatch.setattr(
        provider_data.external_models.ExternalChat, "objects", ChatObjects()
    )
    associated_bridge_calls = []
    monkeypatch.setattr(
        provider_data,
        "_lock_associated_bridge",
        lambda *args, **kwargs: (
            associated_bridge_calls.append((args, kwargs)) or bridge
        ),
    )
    policy_checks = []
    monkeypatch.setattr(
        provider_data,
        "_require_current_provider_policy",
        lambda *args, **kwargs: policy_checks.append((args, kwargs)) or {},
    )

    assert provider_data.resolve_provider_queue_target(
        object(),
        project_id=sys_uuid.uuid4(),
        owner_user_uuid=sys_uuid.uuid4(),
        external_account_uuid=account.uuid,
        stream_uuid=sys_uuid.uuid4(),
    ) == (account, chat, bridge)
    assert policy_checks[0][0][1] == "zulip"
    assert associated_bridge_calls[0][1]["account_uuid"] == account.uuid


def test_resolve_provider_queue_target_can_preserve_blocked_remove_route(monkeypatch):
    account = types.SimpleNamespace(uuid=sys_uuid.uuid4(), provider="zulip")
    chat = types.SimpleNamespace()
    bridge = types.SimpleNamespace(uuid=sys_uuid.uuid4())

    class OneObject:
        def get_one(self, **kwargs):
            return account

    class ChatObjects:
        def get_all(self, **kwargs):
            return [chat]

    monkeypatch.setattr(
        provider_data.external_models.ExternalAccount, "objects", OneObject()
    )
    monkeypatch.setattr(
        provider_data.external_models.ExternalChat, "objects", ChatObjects()
    )
    monkeypatch.setattr(
        provider_data,
        "_lock_associated_bridge",
        lambda *args, **kwargs: bridge,
    )
    monkeypatch.setattr(
        provider_data,
        "_require_current_provider_policy",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("blocked removal route must not recheck acceptance policy")
        ),
    )

    assert provider_data.resolve_provider_queue_target(
        object(),
        project_id=sys_uuid.uuid4(),
        owner_user_uuid=sys_uuid.uuid4(),
        external_account_uuid=account.uuid,
        stream_uuid=sys_uuid.uuid4(),
        allow_policy_blocked=True,
    ) == (account, chat, bridge)


def test_current_provider_policy_is_locked_and_allows_current_capability():
    statements = []
    policy = {
        "enabled": True,
        "emergency_suspended": False,
        "limits": {"max_file_bytes": 100},
    }
    session = types.SimpleNamespace(
        execute=lambda statement, params: (
            statements.append((statement, params))
            or types.SimpleNamespace(fetchone=lambda: policy)
        )
    )

    assert (
        provider_data._require_current_provider_policy(
            session,
            "zulip",
            capability_name="messenger.file.transfer",
            capabilities={
                "messenger.file.transfer": {
                    "available": True,
                    "limits": {"max_file_bytes": 100},
                }
            },
        )
        is policy
    )
    assert "FOR SHARE" in statements[0][0]
    assert statements[0][1] == ("zulip",)


@pytest.mark.parametrize(
    "policy",
    [
        {
            "enabled": False,
            "emergency_suspended": False,
            "limits": {"max_file_bytes": 100},
        },
        {
            "enabled": True,
            "emergency_suspended": True,
            "limits": {"max_file_bytes": 100},
        },
    ],
)
def test_current_provider_policy_fails_closed_for_disabled_or_suspended(policy):
    session = types.SimpleNamespace(
        execute=lambda *_args: types.SimpleNamespace(fetchone=lambda: policy)
    )

    with pytest.raises(provider_data.ProviderPolicyBlockedError):
        provider_data._require_current_provider_policy(session, "zulip")


def test_current_provider_policy_rejects_stale_relaxed_file_limit():
    policy = {
        "enabled": True,
        "emergency_suspended": False,
        "limits": {"max_file_bytes": 50},
    }
    session = types.SimpleNamespace(
        execute=lambda *_args: types.SimpleNamespace(fetchone=lambda: policy)
    )

    with pytest.raises(provider_data.ProviderPolicyBlockedError):
        provider_data._require_current_provider_policy(
            session,
            "zulip",
            capability_name="messenger.file.transfer",
            capabilities={
                "messenger.file.transfer": {
                    "available": True,
                    "limits": {"max_file_bytes": 100},
                }
            },
        )


def test_current_provider_inputs_are_locked_and_allow_advertised_capability():
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    capability = {"messenger.message.send": {"revision": 2, "limits": {}}}
    current = {
        "selected": True,
        "transition_pending": False,
        "chat_status": "live",
        "bridge_status": "active",
        "last_heartbeat_at": now,
        "bridge_capabilities": capability,
        "catalog_capabilities": capability,
    }
    statements = []
    session = types.SimpleNamespace(
        execute=lambda statement, params: (
            statements.append((statement, params))
            or types.SimpleNamespace(fetchone=lambda: current)
        )
    )

    provider_data._require_current_provider_inputs(
        session,
        chat_uuid="chat",
        bridge_uuid="bridge",
        capability_name="messenger.message.send",
        account_capabilities=capability,
        chat_capabilities=capability,
        now=now,
    )

    assert "FOR SHARE OF chat, bridge" in statements[0][0]
    assert statements[0][1] == ("chat", "bridge")


@pytest.mark.parametrize("missing_input", ["bridge", "catalog"])
def test_current_provider_inputs_fail_closed_after_capability_shrink(missing_input):
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    capability = {"messenger.message.send": {"revision": 2, "limits": {}}}
    current = {
        "selected": True,
        "transition_pending": False,
        "chat_status": "live",
        "bridge_status": "active",
        "last_heartbeat_at": now,
        "bridge_capabilities": capability if missing_input != "bridge" else {},
        "catalog_capabilities": capability if missing_input != "catalog" else {},
    }
    session = types.SimpleNamespace(
        execute=lambda *_args: types.SimpleNamespace(fetchone=lambda: current)
    )

    with pytest.raises(provider_data.ProviderUnavailableError):
        provider_data._require_current_provider_inputs(
            session,
            chat_uuid="chat",
            bridge_uuid="bridge",
            capability_name="messenger.message.send",
            account_capabilities=capability,
            chat_capabilities=capability,
            now=now,
        )


@pytest.mark.parametrize(
    "last_heartbeat_at",
    [
        None,
        datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
        - datetime.timedelta(seconds=61),
    ],
)
def test_current_provider_inputs_reject_missing_or_offline_heartbeat(
    last_heartbeat_at,
):
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    capability = {"messenger.message.send": {"revision": 2, "limits": {}}}
    current = {
        "selected": True,
        "transition_pending": False,
        "chat_status": "live",
        "bridge_status": "degraded",
        "last_heartbeat_at": last_heartbeat_at,
        "bridge_capabilities": capability,
        "catalog_capabilities": capability,
    }
    session = types.SimpleNamespace(
        execute=lambda *_args: types.SimpleNamespace(fetchone=lambda: current)
    )

    with pytest.raises(provider_data.ProviderUnavailableError):
        provider_data._require_current_provider_inputs(
            session,
            chat_uuid="chat",
            bridge_uuid="bridge",
            capability_name="messenger.message.send",
            account_capabilities=capability,
            chat_capabilities=capability,
            now=now,
        )


def test_current_provider_inputs_reject_stale_relaxed_file_limit():
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    cached = {
        "messenger.file.transfer": {
            "available": True,
            "limits": {"max_file_bytes": 100},
        }
    }
    current_capability = {
        "messenger.file.transfer": {
            "revision": 2,
            "limits": {"max_file_bytes": 50},
        }
    }
    current = {
        "selected": True,
        "transition_pending": False,
        "chat_status": "live",
        "bridge_status": "active",
        "last_heartbeat_at": now,
        "bridge_capabilities": current_capability,
        "catalog_capabilities": current_capability,
    }
    session = types.SimpleNamespace(
        execute=lambda *_args: types.SimpleNamespace(fetchone=lambda: current)
    )

    with pytest.raises(provider_data.ProviderUnavailableError):
        provider_data._require_current_provider_inputs(
            session,
            chat_uuid="chat",
            bridge_uuid="bridge",
            capability_name="messenger.file.transfer",
            account_capabilities=cached,
            chat_capabilities=cached,
            now=now,
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
        provider_data.models.WorkspaceMessage,
        "objects",
        types.SimpleNamespace(
            get_one=lambda **kwargs: target_queries.append(kwargs) or target_resource
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
        provider_data.messenger_helpers,
        "create_compact_workspace_message_updated_events",
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
    assert target_events == [
        ((project_uuid, target_resource), {"session": session})
    ]


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
