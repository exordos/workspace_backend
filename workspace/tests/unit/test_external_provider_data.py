# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import datetime
import threading
import types
import uuid as sys_uuid

import pytest

from workspace.external_bridge_control import provider_data
from workspace.messenger_api.dm import read_state


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
        self.candidate_params = None
        self.database_now_calls = 0
        self.lease_fence_value = None
        self.last_heartbeat_at = now

    def execute(self, statement, params):
        if "SELECT statement_timestamp() AS current_time" in statement:
            self.database_now_calls += 1
            return LeaseResponse(one={"current_time": self.now})
        if "WITH bridge_capabilities AS MATERIALIZED" in statement:
            assert self.lease_fence_value is not None
            self.allowed_kinds = params[4]
            self.candidate_params = params
            return LeaseResponse(all_rows=[])
        if "workspace.provider_read_snapshot_lease_v2" in statement:
            self.lease_fence_value = params[0]
            return LeaseResponse()
        if 'FROM "m_external_bridge_instances_v2"' in statement:
            return LeaseResponse(
                one={
                    "status": "active",
                    "capabilities": self.capabilities,
                    "last_heartbeat_at": self.last_heartbeat_at,
                }
            )
        if 'AND "lease_uuid" = %s' in statement and statement.lstrip().startswith(
            "SELECT"
        ):
            return LeaseResponse(all_rows=[])
        if "COUNT(*) AS page_count" in statement:
            return LeaseResponse(one={"page_count": 0})
        if "FROM m_external_provider_read_snapshots_v1 AS snapshot" in statement:
            return LeaseResponse(all_rows=[])
        return LeaseResponse()


@pytest.mark.parametrize(
    ("capabilities", "expected", "materializes"),
    [
        ({"messenger.message.send": {"revision": 1}}, False, False),
        (
            {
                "messenger.message.send": {"revision": 1},
                "messenger.message.read": {"revision": 1},
            },
            True,
            False,
        ),
        (
            {
                "messenger.message.send": {"revision": 1},
                "messenger.message.read": {"revision": 2},
            },
            True,
            True,
        ),
    ],
)
def test_read_state_lease_materializes_only_paging_revision(
    capabilities,
    expected,
    materializes,
    monkeypatch,
):
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    session = CapabilityLeaseSession(capabilities, now)
    identity = types.SimpleNamespace(
        bridge_instance_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
        identity_generation=1,
    )
    materialize_calls = []
    monkeypatch.setattr(
        provider_data,
        "_materialize_provider_read_pages",
        lambda *args, **kwargs: materialize_calls.append((args, kwargs)),
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
    assert bool(materialize_calls) is materializes
    expected_fence = (
        "on"
        if provider_data._capability_revision(
            capabilities,
            "messenger.message.read",
        )
        >= provider_data.PROVIDER_READ_PAGING_REVISION
        else "off"
    )
    assert session.lease_fence_value == expected_fence
    assert provider_data._required_capability("read_state.set") == (
        "messenger.message.read"
    )


def test_read_state_operation_uses_published_physical_identity():
    public_uuid = sys_uuid.uuid4()
    physical_uuid = sys_uuid.uuid4()
    lease_uuid = sys_uuid.uuid4()
    now = datetime.datetime(2026, 8, 27, tzinfo=datetime.timezone.utc)

    operation = provider_data._operation_dict(
        {
            "uuid": physical_uuid,
            "external_operation_uuid": public_uuid,
            "lease_uuid": lease_uuid,
            "lease_expires_at": now,
            "external_account_uuid": sys_uuid.uuid4(),
            "project_id": sys_uuid.uuid4(),
            "operation_kind": "read_state.set",
            "attempt": 1,
            "payload": {"message_uuids": []},
        },
    )

    assert operation["external_operation_uuid"] == str(physical_uuid)
    assert operation["provider_operation_uuid"] == str(physical_uuid)
    assert operation["payload"] == {"message_uuids": []}


def test_lease_reacquires_database_clock_after_materializer_lock():
    database_now = datetime.datetime(2026, 8, 27, tzinfo=datetime.timezone.utc)
    session = CapabilityLeaseSession(
        {"messenger.message.read": {"revision": 1}},
        database_now,
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
    )

    assert session.database_now_calls == 2
    assert session.candidate_params[3] == database_now
    assert session.candidate_params[7] == database_now + datetime.timedelta(seconds=30)
    assert session.candidate_params[8] == database_now


@pytest.mark.parametrize("refresh_heartbeat", [False, True])
def test_lease_revalidates_clock_and_heartbeat_after_materializer_wait(
    refresh_heartbeat,
):
    before_wait = datetime.datetime(2026, 8, 27, tzinfo=datetime.timezone.utc)
    after_wait = before_wait + datetime.timedelta(seconds=61)
    lock_started = threading.Event()
    release_lock = threading.Event()

    class BlockingClockLeaseSession(CapabilityLeaseSession):
        def execute(self, statement, params):
            if "SELECT statement_timestamp() AS current_time" in statement:
                current_time = (before_wait, after_wait)[self.database_now_calls]
                self.database_now_calls += 1
                self.now = current_time
                return LeaseResponse(one={"current_time": current_time})
            if params and "provider-read-materialize-v1" in str(params[0]):
                lock_started.set()
                assert release_lock.wait(timeout=3)
                return LeaseResponse()
            return super().execute(statement, params)

    session = BlockingClockLeaseSession(
        {"messenger.message.read": {"revision": 1}},
        before_wait,
    )
    identity = types.SimpleNamespace(
        bridge_instance_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
        identity_generation=1,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            provider_data.lease_provider_operations,
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=10,
            lease_seconds=10,
        )
        assert lock_started.wait(timeout=3)
        if refresh_heartbeat:
            session.last_heartbeat_at = after_wait
        release_lock.set()
        if refresh_heartbeat:
            assert future.result(timeout=3)["operations"] == []
        else:
            with pytest.raises(provider_data.ProviderUnavailableError):
                future.result(timeout=3)

    assert session.database_now_calls == 2
    if refresh_heartbeat:
        assert session.candidate_params[7] == after_wait + datetime.timedelta(
            seconds=10
        )
    else:
        assert session.candidate_params is None


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


@pytest.mark.parametrize(
    "operation_kind",
    ["stream.notification.update", "topic.notification.update"],
)
def test_notification_lease_requires_write_capability(operation_kind):
    now = datetime.datetime(2026, 8, 23, tzinfo=datetime.timezone.utc)
    session = CapabilityLeaseSession(
        {"messenger.notification.write": {"revision": 1}},
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
        "messenger.notification.write"
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
    bridge_instance_uuid = sys_uuid.uuid4()
    external_account_uuid = sys_uuid.uuid4()
    causal_lane = sys_uuid.uuid4()

    _operation, record_uuid = provider_data.enqueue_provider_operation_in_lane(
        session,
        operation_uuid=operation_uuid,
        bridge_instance_uuid=bridge_instance_uuid,
        external_account_uuid=external_account_uuid,
        project_id=project_uuid,
        owner_user_uuid=owner_user_uuid,
        operation_kind="message.create",
        target_type="message",
        target_uuid=sys_uuid.uuid4(),
        payload={"payload": {"kind": "markdown", "content": "hello"}},
        causal_lane=causal_lane,
    )

    assert inserted[0][1] is session
    assert inserted[0][0]["uuid"] == operation_uuid
    assert isinstance(record_uuid, sys_uuid.UUID)
    assert statements[0][1] == (read_state.READ_STATE_SCHEMA_LOCK_KEY,)
    assert "provider-causal-lane-v1" in statements[1][1][0]
    assert str(bridge_instance_uuid) in statements[1][1][0]
    assert str(external_account_uuid) in statements[1][1][0]
    assert str(causal_lane) in statements[1][1][0]
    assert 'INSERT INTO "m_external_provider_operations_v1"' in statements[2][0]
    assert "COALESCE(%s, statement_timestamp())" in statements[2][0]
    assert statements[2][1][7] == causal_lane
    assert statements[2][1][8:11] == (None, None, None)
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
    assert "pg_advisory_xact_lock_shared" in statements[0][0]
    assert "UPDATE m_workspace_messages" in statements[1][0]
    assert statements[1][1][1:4] == ("delivered", None, updated_at)
    assert target_queries[0]["session"] is session
    assert target_events == [((project_uuid, target_resource), {"session": session})]


def test_publish_operation_locks_schema_before_project_event(monkeypatch):
    order = []
    operation = types.SimpleNamespace(owner_user_uuid=sys_uuid.uuid4())
    session = object()
    monkeypatch.setattr(
        provider_data.read_state,
        "lock_read_state_schema_shared",
        lambda current_session: order.append(("schema", current_session)),
    )
    monkeypatch.setattr(
        provider_data.messenger_events,
        "create_external_resource_event",
        lambda *args, **kwargs: order.append(("project-event", args, kwargs)),
    )
    monkeypatch.setattr(
        provider_data,
        "sync_operation_target_delivery",
        lambda *args, **kwargs: order.append(("target", args, kwargs)),
    )

    provider_data.publish_operation_event(
        session,
        operation,
        sys_uuid.uuid4(),
        "external_operation.updated",
    )

    assert [item[0] for item in order] == ["schema", "project-event", "target"]
    assert order[0][1] is session
    assert order[2][2] == {"_event_order_locked": True}


def test_direct_target_sync_locks_project_before_snapshot(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    target_uuid = sys_uuid.uuid4()
    updated_at = datetime.datetime(2026, 8, 27, tzinfo=datetime.timezone.utc)
    operation = types.SimpleNamespace(
        uuid=sys_uuid.uuid4(),
        target_type="stream",
        target_uuid=target_uuid,
        status="succeeded",
        safe_error=None,
        can_retry=False,
        can_discard=False,
        updated_at=updated_at,
        duplicate_risk=False,
        retry_requires_confirmation=False,
        original_url=None,
        reconciliation_reason=None,
    )
    order = []

    def execute(statement, params):
        order.append(("sql", statement, params))
        return types.SimpleNamespace(fetchone=lambda: {"uuid": target_uuid})

    session = types.SimpleNamespace(execute=execute)
    monkeypatch.setattr(
        provider_data,
        "_emit_target_updated_events",
        lambda *args: order.append(("snapshot", args)),
    )

    provider_data.sync_operation_target_delivery(
        session,
        operation,
        project_uuid,
    )

    assert "pg_advisory_xact_lock_shared" in order[0][1]
    assert "pg_advisory_xact_lock(" in order[1][1]
    assert "UPDATE m_workspace_streams" in order[2][1]
    assert order[3][0] == "snapshot"


@pytest.mark.parametrize("target_type", ["stream", "topic"])
def test_compact_target_delivery_uses_bounded_snapshots(monkeypatch, target_type):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    target_uuid = stream_uuid if target_type == "stream" else sys_uuid.uuid4()
    recipients = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    resources = [object(), object()]
    statements = []

    def execute(statement, params):
        statements.append((statement, params))
        return types.SimpleNamespace(fetchone=lambda: {"stream_uuid": stream_uuid})

    session = types.SimpleNamespace(execute=execute)
    monkeypatch.setattr(
        provider_data.read_state,
        "uses_compact_state",
        lambda current_session, current_project: (
            current_session is session and current_project == project_uuid
        ),
    )
    recipient_queries = []
    monkeypatch.setattr(
        provider_data.models,
        "get_stream_recipients",
        lambda *args, **kwargs: recipient_queries.append((args, kwargs)) or recipients,
    )
    for model in (
        provider_data.models.WorkspaceUserStream,
        provider_data.models.WorkspaceUserTopic,
    ):
        monkeypatch.setattr(
            model,
            "objects",
            types.SimpleNamespace(
                get_all=lambda **_kwargs: pytest.fail("global view was queried")
            ),
        )
    snapshot_queries = []
    monkeypatch.setattr(
        provider_data.messenger_helpers,
        "get_compact_workspace_user_stream_snapshots",
        lambda *args, **kwargs: (
            snapshot_queries.append(("stream", args, kwargs)) or resources
        ),
    )
    monkeypatch.setattr(
        provider_data.messenger_helpers,
        "get_compact_workspace_user_topic_snapshots",
        lambda *args, **kwargs: (
            snapshot_queries.append(("topic", args, kwargs)) or resources
        ),
    )
    events = []
    monkeypatch.setattr(
        provider_data.messenger_events,
        "create_stream_updated_events",
        lambda *args, **kwargs: events.append(("stream", args, kwargs)),
    )
    monkeypatch.setattr(
        provider_data.messenger_events,
        "create_topic_updated_events",
        lambda *args, **kwargs: events.append(("topic", args, kwargs)),
    )

    provider_data._emit_target_updated_events(
        session,
        project_uuid,
        target_type,
        target_uuid,
    )

    if target_type == "topic":
        assert "FROM m_workspace_stream_topics" in statements[0][0]
        assert statements[0][1] == (project_uuid, target_uuid)
    else:
        assert statements == []
    assert recipient_queries == [((project_uuid, stream_uuid), {"session": session})]
    assert snapshot_queries == [
        (
            target_type,
            (project_uuid, target_uuid, recipients),
            {"session": session},
        )
    ]
    assert events == [
        (
            target_type,
            (project_uuid, resources),
            {"session": session, "compact": True},
        )
    ]


def test_noncompact_target_delivery_keeps_global_projection(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    session = object()
    resource = object()
    queries = []
    events = []
    monkeypatch.setattr(
        provider_data.read_state,
        "uses_compact_state",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        provider_data.models.WorkspaceUserStream,
        "objects",
        types.SimpleNamespace(
            get_all=lambda **kwargs: queries.append(kwargs) or [resource]
        ),
    )
    monkeypatch.setattr(
        provider_data.messenger_events,
        "create_stream_updated_events",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    provider_data._emit_target_updated_events(
        session,
        project_uuid,
        "stream",
        stream_uuid,
    )

    assert queries == [
        {
            "filters": {
                "project_id": provider_data.dm_filters.EQ(project_uuid),
                "uuid": provider_data.dm_filters.EQ(stream_uuid),
            },
            "session": session,
        }
    ]
    assert events == [
        ((project_uuid, [resource]), {"session": session, "compact": True})
    ]


def test_retry_operation_requeues_existing_provider_row():
    operation_uuid = sys_uuid.uuid4()
    row_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    statements = []
    responses = iter(
        [
            None,
            None,
            [{"uuid": row_uuid, "operation_kind": "message.create"}],
            [],
            [{"uuid": row_uuid, "project_id": project_uuid}],
        ]
    )

    def execute(statement, params):
        statements.append((statement, params))
        rows = next(responses)
        return types.SimpleNamespace(fetchone=lambda: rows, fetchall=lambda: rows)

    session = types.SimpleNamespace(execute=execute)

    result = provider_data.retry_provider_operation(
        session,
        external_operation_uuid=operation_uuid,
        next_attempt=3,
    )

    assert result == {"uuid": row_uuid, "project_id": project_uuid}
    assert statements[0][1] == (read_state.READ_STATE_SCHEMA_LOCK_KEY,)
    assert statements[1][1] == (operation_uuid,)
    assert statements[4][1] == (3, operation_uuid)
    assert "\"status\" = 'queued'" in statements[4][0]
    assert '"attempt" = %s - 1' in statements[4][0]
    assert '"lease_uuid" = NULL' in statements[4][0]


def test_retry_read_operation_renews_bridge_delivery_identity():
    operation_uuid = sys_uuid.uuid4()
    old_row_uuid = sys_uuid.uuid4()
    new_row_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    bridge_uuid = sys_uuid.uuid4()
    statements = []
    responses = iter(
        [
            None,
            {"bridge_instance_uuid": bridge_uuid},
            None,
            [{"uuid": old_row_uuid, "operation_kind": "read_state.set"}],
            [{"uuid": new_row_uuid, "project_id": project_uuid}],
            [],
        ]
    )

    def execute(statement, params):
        statements.append((statement, params))
        rows = next(responses)
        return types.SimpleNamespace(fetchone=lambda: rows, fetchall=lambda: rows)

    result = provider_data.retry_provider_operation(
        types.SimpleNamespace(execute=execute),
        external_operation_uuid=operation_uuid,
        next_attempt=2,
    )

    assert result == {"uuid": new_row_uuid, "project_id": project_uuid}
    assert statements[0][1] == (read_state.READ_STATE_SCHEMA_LOCK_KEY,)
    assert "retry_source AS MATERIALIZED" in statements[4][0]
    assert statements[4][1] == (operation_uuid, operation_uuid, 2)
    assert "gen_random_uuid()" in statements[4][0]
    assert "m_external_provider_operation_results_v1" not in statements[4][0]


def test_discard_operation_prevents_future_provider_lease():
    operation_uuid = sys_uuid.uuid4()
    row_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    statements = []
    responses = iter(
        [
            None,
            None,
            {"uuid": row_uuid, "project_id": project_uuid},
        ]
    )
    session = types.SimpleNamespace(
        execute=lambda statement, params: (
            statements.append((statement, params))
            or types.SimpleNamespace(fetchone=lambda: next(responses))
        )
    )

    result = provider_data.discard_provider_operation(
        session,
        external_operation_uuid=operation_uuid,
    )

    assert result == {"uuid": row_uuid, "project_id": project_uuid}
    assert statements[0][1] == (read_state.READ_STATE_SCHEMA_LOCK_KEY,)
    assert statements[1][1] == (operation_uuid,)
    assert "\"status\" = 'discarded'" in statements[3][0]
    assert len(statements) == 4


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
