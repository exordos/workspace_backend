# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import inspect
import types
import uuid as sys_uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.external_bridge_control import provider_event_apply
from workspace.messenger_api import external_projection
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import read_state


_MISSING_PROVIDER_MESSAGE_IS_TOMBSTONED = (
    provider_event_apply._missing_provider_message_is_tombstoned
)


@pytest.fixture(autouse=True)
def _legacy_read_state(monkeypatch):
    monkeypatch.setattr(
        read_state,
        "project_mode",
        lambda _session, _project_id: read_state.PROJECT_MODE_LEGACY,
    )
    monkeypatch.setattr(
        read_state,
        "_assign_legacy_ingest_sequences",
        lambda *_args, **_kwargs: 0,
    )
    # Handler-focused unit tests use lightweight message stubs without the
    # canonical provenance columns. Account isolation is exercised with real
    # PostgreSQL rows in the integration suite.
    monkeypatch.setattr(
        provider_event_apply,
        "_validate_provider_message_scope",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        provider_event_apply,
        "_missing_provider_message_is_tombstoned",
        lambda *_args, **_kwargs: False,
    )


def test_topic_merge_uses_notification_timestamp_not_generic_updated_at():
    source = inspect.getsource(external_projection._merge_topic_flags)

    assert "notification_updated_at" in source
    assert "m_workspace_user_topic_flags.notification_updated_at" in source


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row if isinstance(self.row, list) else []


class Session:
    def __init__(self, rows):
        if not isinstance(rows, list):
            rows = [rows]
        self.rows = iter(rows)
        self.statements = []

    def execute(self, statement, params):
        self.statements.append((statement, params))
        return Result(next(self.rows, None))


def test_missing_provider_message_uses_all_operation_states_as_provenance():
    owner_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    other_account_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()

    active = Session(
        [
            [
                {"external_account_uuid": account_uuid, "deleted": True},
                {"external_account_uuid": other_account_uuid, "deleted": False},
            ]
        ]
    )
    assert _MISSING_PROVIDER_MESSAGE_IS_TOMBSTONED(
        active,
        message_uuid,
        account_uuid,
        owner_uuid,
    )
    assert "operation.status" not in active.statements[0][0]

    foreign = Session(
        [[{"external_account_uuid": other_account_uuid, "deleted": False}]]
    )
    with pytest.raises(ValueError, match="another account"):
        _MISSING_PROVIDER_MESSAGE_IS_TOMBSTONED(
            foreign,
            message_uuid,
            account_uuid,
            owner_uuid,
        )

    assert not _MISSING_PROVIDER_MESSAGE_IS_TOMBSTONED(
        Session([[]]),
        message_uuid,
        account_uuid,
        owner_uuid,
    )


def test_stream_notification_event_applies_only_newer_provider_value(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    previous = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.timezone.utc)
    incoming = previous + datetime.timedelta(minutes=1)
    session = Session({"notification_updated_at": previous})
    calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "update_workspace_user_stream_notifications",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = provider_event_apply._stream_notification_event(
        session,
        project_uuid,
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
        },
        {
            "uuid": str(stream_uuid),
            "stream_uuid": str(stream_uuid),
            "user_uuid": str(owner_uuid),
            "notification_mode": "muted",
            "notification_updated_at": incoming.isoformat(),
        },
    )

    assert result == stream_uuid
    assert calls == [
        (
            (project_uuid, owner_uuid, stream_uuid, "muted"),
            {"notification_updated_at": incoming, "session": session},
        )
    ]
    assert "FOR UPDATE" in session.statements[0][0]

    stale_session = Session({"notification_updated_at": incoming})
    calls.clear()
    provider_event_apply._stream_notification_event(
        stale_session,
        project_uuid,
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
        },
        {
            "uuid": str(stream_uuid),
            "stream_uuid": str(stream_uuid),
            "user_uuid": str(owner_uuid),
            "notification_mode": "all_messages",
            "notification_updated_at": previous.isoformat(),
        },
    )
    assert calls == []


def test_topic_notification_event_uses_canonical_stream_for_unmute(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    incoming = datetime.datetime(2026, 8, 23, 12, 1, tzinfo=datetime.timezone.utc)
    session = Session(None)
    topic = types.SimpleNamespace(uuid=topic_uuid, stream_uuid=stream_uuid)
    monkeypatch.setattr(
        provider_event_apply.models.WorkspaceStreamTopic,
        "objects",
        types.SimpleNamespace(get_one_or_none=lambda **kwargs: topic),
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "get_workspace_user_stream_access",
        lambda **kwargs: {"notification_mode": "all_messages"},
    )
    calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "update_workspace_user_stream_topic_notifications",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = provider_event_apply._topic_notification_event(
        session,
        project_uuid,
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
        },
        {
            "uuid": str(topic_uuid),
            "stream_uuid": str(stream_uuid),
            "user_uuid": str(owner_uuid),
            "notification_mode": "unmute",
            "notification_updated_at": incoming.isoformat(),
        },
    )

    assert result == topic_uuid
    assert calls == [
        (
            (project_uuid, owner_uuid, topic_uuid, "default"),
            {"notification_updated_at": incoming, "session": session},
        )
    ]


def _identity():
    return types.SimpleNamespace(
        bridge_instance_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
    )


def test_assignment_is_reused_within_provider_event_batch():
    event = _message_event(sys_uuid.uuid4())
    assignment = {
        "owner_user_uuid": sys_uuid.uuid4(),
        "projection_stream_uuid": sys_uuid.uuid4(),
        "provider_chat_id": "channel:7",
    }
    session = Session(assignment)
    session._workspace_provider_event_batch_cache = {}
    identity = _identity()

    first = provider_event_apply._assignment(session, identity, event)
    second = provider_event_apply._assignment(session, identity, event)

    assert second is first
    assert len(session.statements) == 1


def test_assignment_gate_primes_provider_batch_cache_without_second_query():
    identity = _identity()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    assignment = {
        "account_uuid": str(account_uuid),
        "chat_uuid": str(chat_uuid),
        "project_id": str(project_uuid),
        "owner_user_uuid": str(sys_uuid.uuid4()),
        "projection_stream_uuid": str(sys_uuid.uuid4()),
        "provider_chat_id": "channel:7",
        "display_name": "General",
        "source": {"chat_type": "channel"},
        "capabilities": {},
        "account_settings": {"server_url": "https://zulip.example.test"},
        "provider_realm_uuid": str(sys_uuid.uuid4()),
    }
    event = {
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(chat_uuid),
        "project_id": str(project_uuid),
    }
    session = Session([])
    session._workspace_provider_event_batch_cache = {}

    provider_event_apply.prime_assignment_cache(
        session,
        identity,
        [assignment],
    )
    cached = provider_event_apply._assignment(session, identity, event)

    assert cached[0] == account_uuid
    assert cached[1] == project_uuid
    assert cached[2]["provider_chat_id"] == "channel:7"
    assert isinstance(cached[2]["projection_stream_uuid"], sys_uuid.UUID)
    assert session.statements == []


def test_projection_materialization_is_reused_within_provider_event_batch(
    monkeypatch,
):
    projection_stream_uuid = sys_uuid.uuid4()
    session = types.SimpleNamespace(_workspace_provider_event_batch_cache={})
    identity = _identity()
    account_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    assignment = {
        "owner_user_uuid": sys_uuid.uuid4(),
        "projection_stream_uuid": projection_stream_uuid,
        "provider_chat_id": "channel:7",
        "display_name": "Engineering",
        "source": {},
        "capabilities": {},
        "account_settings": {},
    }
    calls = []
    monkeypatch.setattr(
        provider_event_apply.external_projection,
        "ensure_external_chat_stream",
        lambda **kwargs: calls.append(kwargs),
    )

    provider_event_apply._ensure_projection_owner_stream(
        session, project_uuid, assignment, identity, account_uuid
    )
    provider_event_apply._ensure_projection_owner_stream(
        session, project_uuid, assignment, identity, account_uuid
    )

    assert len(calls) == 1
    assert calls[0]["reconcile_participants"] is False


def test_existing_message_projection_skips_participant_reconciliation(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    session = object()
    stream = types.SimpleNamespace(user_uuid=owner_uuid)
    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceStream",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: stream)
        ),
    )
    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceUser",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_all=lambda **_kwargs: (_ for _ in ()).throw(
                    AssertionError("participants must not be loaded")
                )
            )
        ),
    )

    external_projection.ensure_external_chat_stream(
        session,
        project_id=project_uuid,
        owner_user_uuid=owner_uuid,
        projection_stream_uuid=stream_uuid,
        bridge_instance_uuid=sys_uuid.uuid4(),
        external_account_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
        provider_chat_id="channel:7",
        display_name="Engineering",
        source={"participants": []},
        capabilities={},
        account_settings={"server_url": "https://zulip.example.test"},
        reconcile_participants=False,
    )


def test_existing_native_direct_message_projection_accepts_either_owner(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    peer_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    session = object()
    stream = types.SimpleNamespace(
        user_uuid=peer_uuid,
        private_index=external_projection.helpers.build_private_stream_index(
            owner_uuid, peer_uuid
        ),
    )
    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceStream",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: stream)
        ),
    )

    external_projection.ensure_external_chat_stream(
        session,
        project_id=project_uuid,
        owner_user_uuid=owner_uuid,
        projection_stream_uuid=stream_uuid,
        bridge_instance_uuid=sys_uuid.uuid4(),
        external_account_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
        provider_chat_id="direct:1,2:default",
        display_name="Direct message",
        source={
            "participants": [
                {"identity_uuid": str(owner_uuid)},
                {"identity_uuid": str(peer_uuid)},
            ]
        },
        capabilities={},
        account_settings={"server_url": "https://zulip.example.test"},
        reconcile_participants=False,
    )


def test_existing_native_direct_message_projection_rejects_other_participants(
    monkeypatch,
):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    peer_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    stream = types.SimpleNamespace(
        user_uuid=peer_uuid,
        private_index=external_projection.helpers.build_private_stream_index(
            owner_uuid, peer_uuid
        ),
    )
    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceStream",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: stream)
        ),
    )

    with pytest.raises(
        ValueError,
        match="Native direct stream participants do not match assignment",
    ):
        external_projection.ensure_external_chat_stream(
            object(),
            project_id=project_uuid,
            owner_user_uuid=owner_uuid,
            projection_stream_uuid=stream_uuid,
            bridge_instance_uuid=sys_uuid.uuid4(),
            external_account_uuid=sys_uuid.uuid4(),
            provider_kind="zulip",
            provider_chat_id="direct:1,3:default",
            display_name="Direct message",
            source={
                "participants": [
                    {"identity_uuid": str(owner_uuid)},
                    {"identity_uuid": str(sys_uuid.uuid4())},
                ]
            },
            capabilities={},
            account_settings={"server_url": "https://zulip.example.test"},
            reconcile_participants=False,
        )


def test_provider_message_validation_reuses_topic_and_skips_binding(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    binding_calls = []
    topic_calls = []
    monkeypatch.setattr(
        models,
        "WorkspaceStreamBinding",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_one_or_none=lambda **kwargs: binding_calls.append(kwargs)
            )
        ),
    )
    monkeypatch.setattr(
        models,
        "WorkspaceStreamTopic",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_one_or_none=lambda **kwargs: topic_calls.append(kwargs) or object()
            )
        ),
    )
    token = models._PROVIDER_MESSAGE_VALIDATION_CACHE.set(set())
    try:
        for _index in range(2):
            models.WorkspaceMessage(
                uuid=sys_uuid.uuid4(),
                project_id=project_uuid,
                stream_uuid=stream_uuid,
                topic_uuid=topic_uuid,
                user_uuid=sys_uuid.uuid4(),
                payload=message_payloads.MarkdownPayload(content="history"),
                provider_uuid=sys_uuid.uuid4(),
                external_account_uuid=sys_uuid.uuid4(),
            )
    finally:
        models._PROVIDER_MESSAGE_VALIDATION_CACHE.reset(token)

    assert binding_calls == []
    assert len(topic_calls) == 1


def _message_event(stream_uuid):
    return {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(sys_uuid.uuid4()),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(sys_uuid.uuid4()),
        "provider_sequence": "42",
        "kind": "message.upsert",
        "payload": {
            "resource": {
                "uuid": str(sys_uuid.uuid4()),
                "user_uuid": str(sys_uuid.uuid4()),
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(sys_uuid.uuid4()),
                "payload": {"kind": "markdown", "content": "hello"},
                "created_at": "2026-07-18T12:00:00Z",
                "source_name": "zulip",
                "source": {"kind": "zulip"},
                "provider_external_id": "zulip-message-42",
                "provider_metadata": {"original_url": "https://example.test/42"},
            }
        },
    }


def _topic_event(stream_uuid):
    return {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(sys_uuid.uuid4()),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(sys_uuid.uuid4()),
        "provider_sequence": "41",
        "kind": "topic.upsert",
        "payload": {
            "resource": {
                "uuid": str(sys_uuid.uuid4()),
                "stream_uuid": str(stream_uuid),
                "name": "Provider topic",
                "source_name": "zulip",
                "source": {"kind": "zulip"},
                "provider_external_id": "zulip-topic-41",
            }
        },
    }


def _identity_event():
    return {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(sys_uuid.uuid4()),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(sys_uuid.uuid4()),
        "provider_sequence": "40",
        "kind": "identity.upsert",
        "payload": {
            "resource": {
                "uuid": str(sys_uuid.uuid4()),
                "display_name": "Former User",
                "email": "former@example.invalid",
                "avatar_urn": None,
                "active": True,
                "provider_external_id": "42",
            }
        },
    }


def test_identity_upsert_materializes_user_without_stream_binding(monkeypatch):
    identity = _identity()
    event = _identity_event()
    session = Session(None)
    created = []

    class FakeWorkspaceUser:
        objects = types.SimpleNamespace(get_one_or_none=lambda **_kwargs: None)

        def __init__(self, **values):
            self.__dict__.update(values)
            created.append(self)

        def insert(self, session=None):
            self.insert_session = session

    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUser",
        FakeWorkspaceUser,
    )

    target_uuid = provider_event_apply.apply_event(event, session, identity)

    assert target_uuid == sys_uuid.UUID(event["payload"]["resource"]["uuid"])
    assert len(created) == 1
    assert created[0].source == "zulip"
    assert created[0].provider_uuid == identity.bridge_instance_uuid
    assert created[0].external_account_uuid == sys_uuid.UUID(
        event["external_account_uuid"]
    )
    assert created[0].provider_external_id == "42"
    assert created[0].first_name == "Former User"
    assert created[0].status == "offline"
    assert created[0].insert_session is session
    assert not any(
        'FROM "m_external_chats_v2" AS chat' in statement
        for statement, _params in session.statements
    )


def test_identity_upsert_preserves_verified_iam_user(monkeypatch):
    identity = _identity()
    account_uuid = sys_uuid.uuid4()
    iam_user_uuid = sys_uuid.uuid4()
    session = Session(
        {
            "workspace_user_uuid": iam_user_uuid,
            "link_kind": "verified_account_owner",
        }
    )
    existing = types.SimpleNamespace(source="iam")
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUser",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: existing)
        ),
    )

    assert (
        provider_event_apply._upsert_provider_identity(
            session,
            identity,
            account_uuid,
            iam_user_uuid,
            "42",
            {
                "display_name": "Provider display name",
                "email": "provider@example.invalid",
                "active": True,
            },
        )
        == iam_user_uuid
    )
    assert not hasattr(existing, "first_name")


def test_identity_upsert_uses_verified_link_for_stale_event_uuid(monkeypatch):
    identity = _identity()
    linked_user_uuid = sys_uuid.uuid4()
    stale_user_uuid = sys_uuid.uuid4()
    session = Session(
        {
            "workspace_user_uuid": linked_user_uuid,
            "link_kind": "verified_account_owner",
        }
    )
    existing = types.SimpleNamespace(source="iam")
    lookups = []
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUser",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_one_or_none=lambda **kwargs: lookups.append(kwargs) or existing
            )
        ),
    )

    assert (
        provider_event_apply._upsert_provider_identity(
            session,
            identity,
            sys_uuid.uuid4(),
            stale_user_uuid,
            "42",
            {
                "display_name": "Provider display name",
                "email": None,
                "active": True,
            },
        )
        == linked_user_uuid
    )
    assert lookups[0]["filters"]["uuid"].value == linked_user_uuid
    assert not hasattr(existing, "first_name")


def test_identity_upsert_rejects_unlinked_uuid_owned_by_another_identity(
    monkeypatch,
):
    identity = _identity()
    session = Session(None)
    existing = types.SimpleNamespace(
        source="zulip",
        provider_uuid=sys_uuid.uuid4(),
        external_account_uuid=sys_uuid.uuid4(),
        provider_external_id="another-user",
    )
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUser",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: existing)
        ),
    )

    with pytest.raises(
        ValueError,
        match="UUID belongs to another identity",
    ):
        provider_event_apply._upsert_provider_identity(
            session,
            identity,
            sys_uuid.uuid4(),
            sys_uuid.uuid4(),
            "42",
            {
                "display_name": "Provider display name",
                "email": None,
                "active": True,
            },
        )


def test_topic_upsert_repairs_missing_projection_owner_binding(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _topic_event(stream_uuid)
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
            "display_name": "Provider stream",
            "source": {
                "chat_type": "channel",
                "description": "",
                "topics": [],
            },
            "capabilities": {},
            "account_settings": {"server_url": "https://zulip.example.test"},
        }
    )
    ensure_calls = []
    monkeypatch.setattr(
        provider_event_apply.external_projection,
        "ensure_external_chat_stream",
        lambda *args, **kwargs: ensure_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)
    topic_calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_workspace_stream_topic_with_flags",
        lambda *args, **kwargs: topic_calls.append((args, kwargs)),
    )
    compact_calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_compact_workspace_stream_topic_events",
        lambda *args, **kwargs: compact_calls.append((args, kwargs)),
    )

    target_uuid = provider_event_apply.apply_event(event, session, identity)

    project_id = sys_uuid.UUID(event["project_id"])
    assert target_uuid == sys_uuid.UUID(event["payload"]["resource"]["uuid"])
    assert ensure_calls[0][1]["project_id"] == project_id
    assert ensure_calls[0][1]["owner_user_uuid"] == owner_uuid
    assert ensure_calls[0][1]["projection_stream_uuid"] == stream_uuid
    assert ensure_calls[0][1]["external_account_uuid"] == sys_uuid.UUID(
        event["external_account_uuid"]
    )
    assert topic_calls[0][0] == ()
    assert topic_calls[0][1]["project_id"] == project_id
    assert topic_calls[0][1]["session"] is session
    assert topic_calls[0][1]["uuid"] == sys_uuid.UUID(
        event["payload"]["resource"]["uuid"]
    )
    assert topic_calls[0][1]["stream_uuid"] == stream_uuid
    assert (
        topic_calls[0][1]["source"]["source_scope"] == (event["external_account_uuid"])
    )
    assert compact_calls == [
        (
            (project_id, stream_uuid, target_uuid),
            {"created": True, "session": session},
        )
    ]


def test_backfill_topic_upsert_suppresses_stream_and_topic_events(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _topic_event(stream_uuid)
    event["payload"]["resource"]["provider_metadata"] = {"delivery_class": "backfill"}
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
            "display_name": "Provider stream",
            "source": {"chat_type": "channel", "description": "", "topics": []},
            "capabilities": {},
            "account_settings": {"server_url": "https://zulip.example.test"},
        }
    )
    ensured = []
    monkeypatch.setattr(
        provider_event_apply.external_projection,
        "ensure_external_chat_stream",
        lambda *args, **kwargs: ensured.append((args, kwargs)),
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_workspace_stream_topic_with_flags",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_compact_workspace_stream_topic_events",
        lambda *_args, **_kwargs: pytest.fail("backfill topic must stay quiet"),
    )

    provider_event_apply.apply_event(event, session, identity)

    assert ensured[0][1]["emit_events"] is False


def test_missing_external_chat_stream_is_materialized(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    bridge_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    member_uuid = sys_uuid.uuid4()
    session = object()
    created = []
    created_users = []
    bound = []
    deleted = []
    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceStream",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: None)
        ),
    )

    class FakeWorkspaceUser:
        objects = types.SimpleNamespace(
            get_all=lambda **_kwargs: [types.SimpleNamespace(uuid=owner_uuid)]
        )

        def __init__(self, **values):
            self.__dict__.update(values)
            created_users.append(self)

        def insert(self, session=None):
            self.insert_session = session

    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceUser",
        FakeWorkspaceUser,
    )
    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceStreamBinding",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_all=lambda **_kwargs: [])
        ),
    )
    monkeypatch.setattr(
        external_projection.helpers,
        "get_or_create_workspace_user_stream",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )
    monkeypatch.setattr(
        external_projection.helpers,
        "get_or_create_workspace_stream_bindings",
        lambda *args, **kwargs: bound.append((args, kwargs)),
    )
    monkeypatch.setattr(
        external_projection.helpers,
        "delete_workspace_stream_binding",
        lambda *args, **kwargs: deleted.append((args, kwargs)),
    )
    monkeypatch.setattr(
        external_projection.helpers,
        "get_revoked_workspace_external_chat_members",
        lambda *args, **kwargs: set(),
    )

    external_projection.ensure_external_chat_stream(
        session,
        project_id=project_uuid,
        owner_user_uuid=owner_uuid,
        projection_stream_uuid=stream_uuid,
        bridge_instance_uuid=bridge_uuid,
        external_account_uuid=account_uuid,
        provider_kind="zulip",
        provider_chat_id="channel:7",
        display_name="Engineering",
        source={
            "chat_type": "channel",
            "description": "Team",
            "topics": [],
            "participants": [
                {
                    "identity_uuid": str(owner_uuid),
                    "role": "owner",
                },
                {
                    "identity_uuid": str(member_uuid),
                    "role": "member",
                    "provider_user_id": "8",
                    "display_name": "External Member",
                    "avatar_urn": None,
                },
            ],
        },
        capabilities={"messenger.message.send": {"available": True}},
        account_settings={"server_url": "https://zulip.example.test"},
        emit_events=False,
    )

    args, values = created[0]
    assert args == (project_uuid, owner_uuid)
    assert values["uuid"] == stream_uuid
    assert values["name"] == "Engineering"
    assert values["create_default_topic"] is False
    assert values["source_name"] == "zulip"
    assert values["source"].stream_id == 7
    assert values["source"].source_scope == str(account_uuid)
    assert values["provider_uuid"] == bridge_uuid
    assert values["external_account_uuid"] == account_uuid
    assert values["emit_events"] is False
    assert len(created_users) == 1
    assert created_users[0].uuid == member_uuid
    assert created_users[0].source == "zulip"
    assert created_users[0].provider_uuid == bridge_uuid
    assert created_users[0].external_account_uuid == account_uuid
    assert created_users[0].provider_external_id == "8"
    assert created_users[0].insert_session is session
    assert bound == [
        (
            (),
            {
                "project_id": project_uuid,
                "stream_uuid": stream_uuid,
                "who_uuid": owner_uuid,
                "role_user_uuids": {
                    "owner": [owner_uuid],
                    "member": [member_uuid],
                },
                "session": session,
                "emit_events": False,
            },
        )
    ]
    assert deleted == []


def test_existing_external_chat_stream_reconciles_provider_managed_bindings(
    monkeypatch,
):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    stream = types.SimpleNamespace(user_uuid=owner_uuid)
    member_uuid = sys_uuid.uuid4()
    linked_iam_member_uuid = sys_uuid.uuid4()
    stale_member_uuid = sys_uuid.uuid4()
    native_member_uuid = sys_uuid.uuid4()
    session = object()
    bound = []
    deleted = []
    stale_binding_uuid = sys_uuid.uuid4()
    linked_iam_binding_uuid = sys_uuid.uuid4()
    native_binding_uuid = sys_uuid.uuid4()
    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceStream",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: stream)
        ),
    )
    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceUser",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_all=lambda **kwargs: (
                    [
                        types.SimpleNamespace(
                            uuid=stale_member_uuid,
                            source="zulip",
                        )
                    ]
                    if "source" in kwargs["filters"]
                    else [
                        types.SimpleNamespace(uuid=owner_uuid, source="iam"),
                        types.SimpleNamespace(uuid=member_uuid, source="zulip"),
                        types.SimpleNamespace(
                            uuid=linked_iam_member_uuid,
                            source="iam",
                        ),
                    ]
                )
            )
        ),
    )
    monkeypatch.setattr(
        external_projection.models,
        "WorkspaceStreamBinding",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_all=lambda **_kwargs: [
                    types.SimpleNamespace(
                        uuid=sys_uuid.uuid4(),
                        user_uuid=owner_uuid,
                    ),
                    types.SimpleNamespace(
                        uuid=sys_uuid.uuid4(),
                        user_uuid=member_uuid,
                    ),
                    types.SimpleNamespace(
                        uuid=stale_binding_uuid,
                        user_uuid=stale_member_uuid,
                    ),
                    types.SimpleNamespace(
                        uuid=linked_iam_binding_uuid,
                        user_uuid=linked_iam_member_uuid,
                    ),
                    types.SimpleNamespace(
                        uuid=native_binding_uuid,
                        user_uuid=native_member_uuid,
                    ),
                ]
            )
        ),
    )
    monkeypatch.setattr(
        external_projection.helpers,
        "get_or_create_workspace_stream_bindings",
        lambda *args, **kwargs: bound.append((args, kwargs)),
    )
    monkeypatch.setattr(
        external_projection.helpers,
        "delete_workspace_stream_binding",
        lambda *args, **kwargs: deleted.append((args, kwargs)),
    )
    monkeypatch.setattr(
        external_projection.helpers,
        "get_revoked_workspace_external_chat_members",
        lambda *args, **kwargs: set(),
    )

    external_projection.ensure_external_chat_stream(
        session,
        project_id=project_uuid,
        owner_user_uuid=owner_uuid,
        projection_stream_uuid=stream_uuid,
        bridge_instance_uuid=sys_uuid.uuid4(),
        external_account_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
        provider_chat_id="channel:7",
        display_name="Engineering",
        source={
            "chat_type": "channel",
            "description": "Team",
            "participants": [
                {
                    "identity_uuid": str(owner_uuid),
                    "role": "owner",
                },
                {
                    "identity_uuid": str(member_uuid),
                    "role": "member",
                },
                {
                    "identity_uuid": str(linked_iam_member_uuid),
                    "role": "member",
                },
            ],
        },
        capabilities={},
        account_settings={"server_url": "https://zulip.example.test"},
    )

    assert bound == [
        (
            (),
            {
                "project_id": project_uuid,
                "stream_uuid": stream_uuid,
                "who_uuid": owner_uuid,
                "role_user_uuids": {
                    "owner": [owner_uuid],
                    "member": [member_uuid, linked_iam_member_uuid],
                },
                "session": session,
                "emit_events": True,
            },
        )
    ]
    assert deleted == [
        (
            (project_uuid, stale_binding_uuid),
            {"session": session},
        )
    ]


def test_message_upsert_is_scoped_to_selected_projection_and_adds_provider_metadata(
    monkeypatch,
):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    assignment = {
        "owner_user_uuid": owner_uuid,
        "projection_stream_uuid": stream_uuid,
        "provider_chat_id": "zulip-channel-7",
        "source": {"chat_type": "channel"},
        "account_settings": {"server_url": "https://zulip.example.test"},
    }
    session = Session(assignment)
    ensured = []
    monkeypatch.setattr(
        provider_event_apply,
        "_ensure_projection_owner_stream",
        lambda *args, **kwargs: ensured.append((args, kwargs)),
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)
    identities = []
    canonical_author_uuid = sys_uuid.uuid4()

    def upsert_identity(*args):
        identities.append(args)
        return canonical_author_uuid

    monkeypatch.setattr(
        provider_event_apply,
        "_upsert_provider_identity",
        upsert_identity,
    )
    created = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_workspace_user_message",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )

    author_identity = {
        "provider_external_id": "former-user-42",
        "display_name": "Former User",
        "email": None,
        "avatar_urn": None,
        "active": True,
    }
    event["payload"]["resource"]["author_identity"] = author_identity
    event["payload"]["resource"].pop("source_name")
    event["payload"]["resource"].pop("source")

    target_uuid = provider_event_apply.apply_event(event, session, identity)

    assert target_uuid == sys_uuid.UUID(event["payload"]["resource"]["uuid"])
    assert ensured[0][0][0:4] == (
        session,
        sys_uuid.UUID(event["project_id"]),
        assignment,
        identity,
    )
    assert identities == [
        (
            session,
            identity,
            sys_uuid.UUID(event["external_account_uuid"]),
            sys_uuid.UUID(event["payload"]["resource"]["user_uuid"]),
            "former-user-42",
            author_identity,
        )
    ]
    assert '"selected"' in session.statements[0][0]
    assert '"projection_stream_uuid" IS NOT NULL' in session.statements[0][0]
    values = created[0][1]
    assert values["provider_uuid"] == identity.bridge_instance_uuid
    assert values["external_account_uuid"] == sys_uuid.UUID(
        event["external_account_uuid"]
    )
    assert (
        values["provider_metadata"]["provider_event_uuid"]
        == (event["provider_event_uuid"])
    )
    assert values["provider_metadata"]["provider_sequence"] == "42"
    assert values["provider_metadata"]["kind"] == "zulip"
    assert (
        values["provider_metadata"]["account_uuid"] == (event["external_account_uuid"])
    )
    assert values["provider_metadata"]["external_id"] == "zulip-message-42"
    assert values["provider_metadata"]["capabilities"] == {}
    assert values["uuid"] == sys_uuid.UUID(event["payload"]["resource"]["uuid"])
    assert values["stream_uuid"] == stream_uuid
    assert values["topic_uuid"] == sys_uuid.UUID(
        event["payload"]["resource"]["topic_uuid"]
    )
    assert values["source_name"] == "zulip"
    assert values["source"].source_scope == event["external_account_uuid"]
    assert values["source"].server_url == "https://zulip.example.test"
    assert values["created_at"] == datetime.datetime(
        2026, 7, 18, 12, tzinfo=datetime.timezone.utc
    )
    assert isinstance(values["payload"], message_payloads.MarkdownPayload)
    assert values["payload"].content == "hello"
    assert values["compact_events"] is True
    assert values["scoped_recipient_uuids"] == [owner_uuid]
    assert created[0][0][1] == canonical_author_uuid


def test_live_message_author_identity_is_cached_per_provider_realm(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    canonical_author_uuid = sys_uuid.uuid4()
    assignment = {
        "owner_user_uuid": owner_uuid,
        "projection_stream_uuid": stream_uuid,
        "provider_chat_id": "zulip-channel-7",
        "source": {"chat_type": "channel"},
        "provider_realm_uuid": sys_uuid.uuid4(),
    }
    session = Session([assignment, assignment])
    session._workspace_provider_event_batch_cache = {}
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)
    monkeypatch.setattr(
        provider_event_apply,
        "_missing_provider_message_is_tombstoned",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        provider_event_apply,
        "_ensure_projection_owner_stream",
        lambda *_args, **_kwargs: None,
    )
    identities = []
    monkeypatch.setattr(
        provider_event_apply,
        "_upsert_provider_identity",
        lambda *args: identities.append(args) or canonical_author_uuid,
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_workspace_user_message",
        lambda *_args, **_kwargs: None,
    )

    for event in (_message_event(stream_uuid), _message_event(stream_uuid)):
        event["payload"]["resource"]["author_identity"] = {
            "provider_external_id": "provider-user-42",
            "display_name": "Provider User",
            "email": None,
            "avatar_urn": None,
            "active": True,
        }
        provider_event_apply.apply_event(event, session, identity)

    assert len(identities) == 1


def test_provider_message_accepts_former_author_without_stream_binding(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    author_uuid = sys_uuid.uuid4()
    monkeypatch.setattr(
        models,
        "WorkspaceStreamBinding",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: None)
        ),
    )
    monkeypatch.setattr(
        models,
        "WorkspaceStreamTopic",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_one_or_none=lambda **_kwargs: types.SimpleNamespace(uuid=topic_uuid)
            )
        ),
    )

    message = models.WorkspaceMessage(
        uuid=sys_uuid.uuid4(),
        project_id=project_uuid,
        user_uuid=author_uuid,
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        payload=message_payloads.MarkdownPayload(content="historical message"),
        provider_uuid=sys_uuid.uuid4(),
        external_account_uuid=sys_uuid.uuid4(),
        provider_external_id="zulip-message-42",
    )

    assert message.user_uuid == author_uuid


def test_provider_message_keeps_native_account_owner_identity(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    event["payload"]["resource"]["user_uuid"] = str(owner_uuid)
    event["payload"]["resource"]["author_identity"] = {
        "provider_external_id": "owner-provider-id",
        "display_name": "Account Owner",
        "email": None,
        "avatar_urn": None,
        "active": True,
    }
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)
    monkeypatch.setattr(
        provider_event_apply,
        "_ensure_projection_owner_stream",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        provider_event_apply,
        "_upsert_provider_identity",
        lambda *_args: pytest.fail("native owner identity must not be rebound"),
    )
    created = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_workspace_user_message",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )

    provider_event_apply.apply_event(event, session, identity)

    assert created[0][0][1] == owner_uuid


def test_provider_backfill_reaction_echo_is_quiet(monkeypatch):
    identity = _identity()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    provider_reaction_uuid = sys_uuid.uuid4()
    placeholder_actor_uuid = sys_uuid.uuid4()
    canonical_actor_uuid = sys_uuid.uuid4()
    native_reaction_uuid = sys_uuid.uuid4()
    external_account_uuid = sys_uuid.uuid4()
    message = types.SimpleNamespace(stream_uuid=stream_uuid)
    native_reaction = types.SimpleNamespace(
        uuid=native_reaction_uuid,
        user_uuid=canonical_actor_uuid,
    )
    original_message_model = models.WorkspaceMessage
    reaction_model = types.SimpleNamespace(
        objects=types.SimpleNamespace(
            get_one_or_none=lambda **_kwargs: native_reaction,
        )
    )
    monkeypatch.setattr(models, "WorkspaceMessageReactions", reaction_model)
    monkeypatch.setattr(
        provider_event_apply,
        "_existing",
        lambda model, *_args: message if model is original_message_model else None,
    )
    monkeypatch.setattr(
        provider_event_apply,
        "_upsert_provider_identity",
        lambda *_args: canonical_actor_uuid,
    )
    updates = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "update_workspace_message_reaction",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    event = {
        "external_account_uuid": str(external_account_uuid),
        "kind": "reaction.upsert",
    }
    resource = {
        "uuid": str(provider_reaction_uuid),
        "message_uuid": str(message_uuid),
        "user_uuid": str(placeholder_actor_uuid),
        "emoji_name": "thumbs_up",
        "provider_external_id": "zulip-reaction-42",
        "provider_metadata": {"delivery_class": "backfill"},
        "user_identity": {
            "provider_external_id": "zulip-user-7",
            "display_name": "Provider User",
            "email": None,
            "avatar_urn": None,
            "active": True,
        },
    }

    resolved_uuid = provider_event_apply._reaction_event(
        Session([]),
        event,
        project_uuid,
        {"projection_stream_uuid": stream_uuid},
        resource,
        identity,
    )

    assert resolved_uuid == native_reaction_uuid
    assert resource["user_uuid"] == canonical_actor_uuid
    assert updates[0][0][:3] == (
        project_uuid,
        canonical_actor_uuid,
        native_reaction_uuid,
    )
    assert updates[0][0][3]["message_uuid"] == message_uuid
    assert updates[0][1]["session"].statements == []
    assert updates[0][1]["emit_events"] is False
    assert updates[0][1]["compact_events"] is True
    assert updates[0][1]["enforce_visibility"] is False


def test_provider_backfill_reaction_create_is_quiet(monkeypatch):
    identity = _identity()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    actor_uuid = sys_uuid.uuid4()
    external_account_uuid = sys_uuid.uuid4()
    message = types.SimpleNamespace(stream_uuid=stream_uuid)
    original_message_model = models.WorkspaceMessage
    monkeypatch.setattr(
        models,
        "WorkspaceMessageReactions",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: None),
        ),
    )
    monkeypatch.setattr(
        provider_event_apply,
        "_existing",
        lambda model, *_args: message if model is original_message_model else None,
    )
    created = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_workspace_message_reaction",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )
    event = {
        "external_account_uuid": str(external_account_uuid),
        "kind": "reaction.upsert",
    }
    resource = {
        "uuid": str(reaction_uuid),
        "message_uuid": str(message_uuid),
        "user_uuid": str(actor_uuid),
        "emoji_name": "thumbs_up",
        "provider_external_id": "zulip-reaction-43",
        "provider_metadata": {"delivery_class": "backfill"},
    }

    resolved_uuid = provider_event_apply._reaction_event(
        Session([]),
        event,
        project_uuid,
        {"projection_stream_uuid": stream_uuid},
        resource,
        identity,
    )

    assert resolved_uuid == reaction_uuid
    assert created[0][0][:2] == (project_uuid, actor_uuid)
    assert created[0][1]["emit_events"] is False
    assert created[0][1]["compact_events"] is True
    assert created[0][1]["enforce_visibility"] is False


def test_provider_message_snapshot_applies_owner_read_state(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    event["payload"]["resource"]["read"] = False
    message_uuid = sys_uuid.UUID(event["payload"]["resource"]["uuid"])
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)
    monkeypatch.setattr(
        provider_event_apply,
        "_ensure_projection_owner_stream",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_workspace_user_message",
        lambda *_args, **_kwargs: None,
    )
    updates = []
    monkeypatch.setattr(
        provider_event_apply,
        "_sync_provider_read_state",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    provider_event_apply.apply_event(event, session, identity)

    assert updates == [
        (
            (
                session,
                sys_uuid.UUID(event["project_id"]),
                owner_uuid,
                stream_uuid,
                sys_uuid.UUID(event["payload"]["resource"]["topic_uuid"]),
                [message_uuid],
                False,
            ),
            {},
        )
    ]


def test_provider_backfill_message_suppresses_per_message_ui_events(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    event["payload"]["resource"]["read"] = True
    event["payload"]["resource"]["provider_metadata"]["delivery_class"] = "backfill"
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)
    ensured = []
    monkeypatch.setattr(
        provider_event_apply,
        "_ensure_projection_owner_stream",
        lambda *_args, **kwargs: ensured.append(kwargs),
    )
    creates = []
    updates = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_workspace_user_message",
        lambda *args, **kwargs: creates.append((args, kwargs)),
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "sync_workspace_user_message_flags",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    provider_event_apply.apply_event(event, session, identity)

    assert creates[0][1]["emit_events"] is False
    assert updates[0][1]["emit_events"] is False
    assert updates[0][1].get("allow_author_unread", False) is False
    assert ensured[0]["emit_events"] is False


def test_native_message_still_requires_author_stream_binding(monkeypatch):
    monkeypatch.setattr(
        models,
        "WorkspaceStreamBinding",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: None)
        ),
    )

    with pytest.raises(ra_exc.ValidationErrorException):
        models.WorkspaceMessage(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
            stream_uuid=sys_uuid.uuid4(),
            topic_uuid=sys_uuid.uuid4(),
            payload=message_payloads.MarkdownPayload(content="native message"),
        )


def test_provider_reconciliation_loads_native_message_without_current_binding(
    monkeypatch,
):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    monkeypatch.setattr(
        models,
        "WorkspaceStreamBinding",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_one_or_none=lambda **_kwargs: None)
        ),
    )
    monkeypatch.setattr(
        models,
        "WorkspaceStreamTopic",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_one_or_none=lambda **_kwargs: types.SimpleNamespace(uuid=topic_uuid)
            )
        ),
    )

    token = models._PROVIDER_MESSAGE_VALIDATION_CACHE.set(set())
    try:
        message = models.WorkspaceMessage(
            uuid=sys_uuid.uuid4(),
            project_id=project_uuid,
            user_uuid=sys_uuid.uuid4(),
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            payload=message_payloads.MarkdownPayload(content="native echo"),
        )
    finally:
        models._PROVIDER_MESSAGE_VALIDATION_CACHE.reset(token)

    assert message.source_name == models.SourceName.NATIVE.value


def test_message_upsert_scopes_three_ui_events_to_account_owner(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    member_uuids = [sys_uuid.uuid4() for _index in range(300)]
    event = _message_event(stream_uuid)
    event_resource = event["payload"]["resource"]
    message_uuid = sys_uuid.UUID(event_resource["uuid"])
    user_topics = [
        types.SimpleNamespace(
            uuid=sys_uuid.UUID(event_resource["topic_uuid"]),
            user_uuid=owner_uuid,
        )
    ]
    user_streams = [types.SimpleNamespace(uuid=stream_uuid, user_uuid=owner_uuid)]
    session = Session(
        [
            {
                "owner_user_uuid": owner_uuid,
                "projection_stream_uuid": stream_uuid,
                "provider_chat_id": "zulip-channel-7",
            },
            None,
            None,
            None,
            user_topics,
            user_streams,
        ]
    )
    session._workspace_provider_event_batch_cache = {}
    created_flags = []

    class FakeWorkspaceMessage:
        __tablename__ = "m_workspace_messages"
        created = None

        def __init__(self, **values):
            self.__dict__.update(values)
            self.updated_at = values.get("updated_at", self.created_at)
            self.delivery_metadata = None
            self.delivery_status = None
            self.delivery_error = None
            self.delivery_updated_at = None
            type(self).created = self

        def insert(self, session=None):
            assert session is not None

        def get_recipients(self, session=None):
            pytest.fail("provider projection must use its scoped account owner")

    FakeWorkspaceMessage.objects = types.SimpleNamespace(
        get_one_or_none=lambda **_kwargs: FakeWorkspaceMessage.created,
    )

    class FakeWorkspaceUserMessageFlags:
        def __init__(self, **values):
            self.values = values

        def insert(self, session=None):
            assert session is not None
            created_flags.append(self.values)

    class FakeWorkspaceUserMessage:
        objects = types.SimpleNamespace(
            get_all=lambda **_kwargs: [
                types.SimpleNamespace(
                    uuid=message_uuid,
                    project_id=sys_uuid.UUID(event["project_id"]),
                    user_uuid=member_uuid,
                    stream_uuid=stream_uuid,
                    topic_uuid=sys_uuid.UUID(event_resource["topic_uuid"]),
                    payload=event_resource["payload"],
                    source_name="zulip",
                    source={"kind": "zulip"},
                    read=member_uuid == sys_uuid.UUID(event_resource["user_uuid"]),
                    pinned=False,
                    starred=False,
                )
                for member_uuid in member_uuids
            ],
        )

    class FakeWorkspaceUserStream:
        objects = types.SimpleNamespace(
            get_all=lambda **_kwargs: [
                types.SimpleNamespace(uuid=stream_uuid, user_uuid=member_uuid)
                for member_uuid in member_uuids
            ]
        )

    class FakeWorkspaceUserTopic:
        objects = types.SimpleNamespace(
            get_all=lambda **_kwargs: [
                types.SimpleNamespace(
                    uuid=sys_uuid.UUID(event_resource["topic_uuid"]),
                    user_uuid=member_uuid,
                )
                for member_uuid in member_uuids
            ]
        )

    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)
    monkeypatch.setattr(
        provider_event_apply,
        "_missing_provider_message_is_tombstoned",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        provider_event_apply.read_state,
        "lock_projects",
        lambda session, _project_ids: session.execute("SELECT 1"),
    )
    monkeypatch.setattr(
        provider_event_apply,
        "_ensure_projection_owner_stream",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceMessage",
        FakeWorkspaceMessage,
    )
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUserMessageFlags",
        FakeWorkspaceUserMessageFlags,
    )
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUserMessage",
        FakeWorkspaceUserMessage,
    )
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUserStream",
        FakeWorkspaceUserStream,
    )
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUserTopic",
        FakeWorkspaceUserTopic,
    )
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "_stream_from_event_payload",
        lambda value, session=None: {"uuid": str(value.uuid)},
    )
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "_topic_from_event_payload",
        lambda value, session=None: {"uuid": str(value.uuid)},
    )
    target_uuid = provider_event_apply.apply_event(event, session, identity)

    assert target_uuid == message_uuid
    flag_inserts = [
        params
        for statement, params in session.statements
        if 'INSERT INTO "m_workspace_user_message_flags"' in statement
    ]
    assert len(flag_inserts) == 1
    assert flag_inserts[0][3] == [owner_uuid]
    prepared = session._workspace_provider_event_batch_cache[
        ("prepared_broadcast_events",)
    ]
    assert [item["kind"] for item in prepared] == [
        "message.created",
        "topic.updated",
        "stream.updated",
    ]
    assert [item["recipients"] for item in prepared] == [[owner_uuid]] * 3
    message_payload = prepared[0]["payload"]
    assert str(message_uuid) in str(message_payload)
    assert message_payload["source"]["kind"] == "zulip"
    assert event["external_account_uuid"] in str(message_payload["source"])
    assert "zulip-message-42" in str(message_payload)
    assert all(
        'INSERT INTO "m_workspace_events"' not in statement
        for statement, _params in session.statements
    )


def test_provider_event_cannot_escape_selected_stream(monkeypatch):
    identity = _identity()
    selected_stream_uuid = sys_uuid.uuid4()
    event = _message_event(sys_uuid.uuid4())
    session = Session(
        {
            "owner_user_uuid": sys_uuid.uuid4(),
            "projection_stream_uuid": selected_stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)

    with pytest.raises(ValueError, match="selected stream"):
        provider_event_apply.apply_event(event, session, identity)


def test_provider_read_state_updates_exact_owner_messages(monkeypatch):
    identity = _identity()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    message_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    event = {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(sys_uuid.uuid4()),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(project_uuid),
        "kind": "read_state.set",
        "payload": {
            "resource": {
                "uuid": str(stream_uuid),
                "provider_external_id": "zulip-channel-7",
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "reader_uuid": str(owner_uuid),
                "message_uuids": [str(value) for value in message_uuids],
                "read": True,
            }
        },
    }
    session = Session(
        [
            {
                "owner_user_uuid": owner_uuid,
                "projection_stream_uuid": stream_uuid,
                "provider_chat_id": "zulip-channel-7",
            },
            None,
            [
                {
                    "uuid": message_uuid,
                    "author_uuid": sys_uuid.uuid4(),
                    "stream_uuid": stream_uuid,
                    "topic_uuid": topic_uuid,
                    "read": False,
                }
                for message_uuid in message_uuids
            ],
            [{"uuid": message_uuid} for message_uuid in message_uuids],
        ]
    )
    read_events = []
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "create_messages_read_event",
        lambda *args, **kwargs: read_events.append((args, kwargs)),
    )
    compact_events = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "_create_compact_messages_unread_updated_events",
        lambda *args, **kwargs: compact_events.append((args, kwargs)),
    )

    assert provider_event_apply.apply_event(event, session, identity) == stream_uuid
    assert read_events == [
        (
            (project_uuid, owner_uuid, message_uuids),
            {"session": session},
        )
    ]
    assert compact_events == [
        (
            (project_uuid, [owner_uuid], stream_uuid, topic_uuid),
            {"session": session},
        )
    ]
    lock_statement, lock_params = session.statements[1]
    assert "pg_advisory_xact_lock" in lock_statement
    assert lock_params == (project_uuid,)


def test_provider_unread_state_emits_exact_owner_message_snapshots(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    message_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    stored_rows = [
        {
            "uuid": message_uuid,
            "author_uuid": sys_uuid.uuid4(),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "read": True,
        }
        for message_uuid in message_uuids
    ]
    snapshots = [types.SimpleNamespace(uuid=value) for value in message_uuids]
    session = Session([None, stored_rows, [{"uuid": value} for value in message_uuids]])
    snapshot_events = []
    aggregate_events = []
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUserMessage",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_all=lambda **kwargs: snapshots)
        ),
    )
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "create_message_updated_event",
        lambda *args, **kwargs: snapshot_events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "_create_compact_messages_unread_updated_events",
        lambda *args, **kwargs: aggregate_events.append((args, kwargs)),
    )

    provider_event_apply._sync_provider_read_state(
        session,
        project_uuid,
        owner_uuid,
        stream_uuid,
        topic_uuid,
        message_uuids,
        False,
    )

    assert snapshot_events == [
        ((), {"message": snapshot, "session": session}) for snapshot in snapshots
    ]
    assert aggregate_events == [
        (
            (project_uuid, [owner_uuid], stream_uuid, topic_uuid),
            {"session": session},
        )
    ]


def test_provider_unread_state_keeps_owner_authored_message_read(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    stored_rows = [
        {
            "uuid": message_uuid,
            "author_uuid": owner_uuid,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "read": False,
        }
    ]
    session = Session([None, stored_rows, [{"uuid": message_uuid}]])
    read_events = []
    unread_events = []
    aggregate_events = []
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "create_messages_read_event",
        lambda *args, **kwargs: read_events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "create_message_updated_event",
        lambda *args, **kwargs: unread_events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "_create_compact_messages_unread_updated_events",
        lambda *args, **kwargs: aggregate_events.append((args, kwargs)),
    )

    provider_event_apply._sync_provider_read_state(
        session,
        project_uuid,
        owner_uuid,
        stream_uuid,
        topic_uuid,
        [message_uuid],
        False,
    )

    assert read_events == [
        (
            (project_uuid, owner_uuid, [message_uuid]),
            {"session": session},
        )
    ]
    assert unread_events == []
    assert aggregate_events == [
        (
            (project_uuid, [owner_uuid], stream_uuid, topic_uuid),
            {"session": session},
        )
    ]


def test_provider_read_state_uses_actual_topics_within_selected_stream(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    reported_topic_uuid = sys_uuid.uuid4()
    actual_topic_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    owner_uuid = sys_uuid.uuid4()
    message_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    stored_rows = [
        {
            "uuid": message_uuid,
            "author_uuid": sys_uuid.uuid4(),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "read": False,
        }
        for message_uuid, topic_uuid in zip(message_uuids, actual_topic_uuids)
    ]
    session = Session([None, stored_rows, [{"uuid": value} for value in message_uuids]])
    compact_events = []
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "create_messages_read_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "_create_compact_messages_unread_updated_events",
        lambda *args, **kwargs: compact_events.append((args, kwargs)),
    )

    provider_event_apply._sync_provider_read_state(
        session,
        project_uuid,
        owner_uuid,
        stream_uuid,
        reported_topic_uuid,
        message_uuids,
        True,
    )

    assert compact_events == [
        (
            (project_uuid, [owner_uuid], stream_uuid, topic_uuid),
            {"session": session},
        )
        for topic_uuid in sorted(actual_topic_uuids, key=str)
    ]


def test_provider_read_state_rejects_message_from_another_stream():
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    session = Session(
        [
            None,
            [
                {
                    "uuid": message_uuid,
                    "author_uuid": sys_uuid.uuid4(),
                    "stream_uuid": sys_uuid.uuid4(),
                    "topic_uuid": sys_uuid.uuid4(),
                    "read": False,
                }
            ],
        ]
    )

    with pytest.raises(
        ValueError,
        match="Provider read state message is outside the selected chat",
    ):
        provider_event_apply._sync_provider_read_state(
            session,
            project_uuid,
            sys_uuid.uuid4(),
            stream_uuid,
            sys_uuid.uuid4(),
            [message_uuid],
            True,
        )


def test_provider_read_state_rejects_non_owner_before_mutation(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    event = {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(sys_uuid.uuid4()),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(sys_uuid.uuid4()),
        "kind": "read_state.set",
        "payload": {
            "resource": {
                "uuid": str(stream_uuid),
                "provider_external_id": "zulip-channel-7",
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(sys_uuid.uuid4()),
                "message_uuids": [str(sys_uuid.uuid4())],
                "read": True,
            }
        },
    }
    session = Session(
        {
            "owner_user_uuid": sys_uuid.uuid4(),
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    mutations = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "sync_workspace_user_message_flags",
        lambda *args, **kwargs: mutations.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match="account owner"):
        provider_event_apply.apply_event(event, session, identity)

    assert mutations == []


def test_provider_read_state_defers_messages_not_yet_imported(monkeypatch):
    identity = _identity()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    imported_uuid = sys_uuid.uuid4()
    pending_uuid = sys_uuid.uuid4()
    event = {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(sys_uuid.uuid4()),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(project_uuid),
        "kind": "read_state.set",
        "payload": {
            "resource": {
                "uuid": str(stream_uuid),
                "provider_external_id": "zulip-channel-7",
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "reader_uuid": str(owner_uuid),
                "message_uuids": [str(imported_uuid), str(pending_uuid)],
                "read": True,
            }
        },
    }
    session = Session(
        [
            {
                "owner_user_uuid": owner_uuid,
                "projection_stream_uuid": stream_uuid,
                "provider_chat_id": "zulip-channel-7",
            },
            None,
            [
                {
                    "uuid": imported_uuid,
                    "author_uuid": sys_uuid.uuid4(),
                    "stream_uuid": stream_uuid,
                    "topic_uuid": topic_uuid,
                    "read": False,
                }
            ],
            [{"uuid": imported_uuid}],
        ]
    )
    read_events = []
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "create_messages_read_event",
        lambda *args, **kwargs: read_events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "_create_compact_messages_unread_updated_events",
        lambda *args, **kwargs: None,
    )

    assert provider_event_apply.apply_event(event, session, identity) == stream_uuid
    assert read_events == [
        ((project_uuid, owner_uuid, [imported_uuid]), {"session": session})
    ]


def test_message_update_preserves_created_at_and_uses_compact_broadcast(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    resource = event["payload"]["resource"]
    message_uuid = sys_uuid.UUID(resource["uuid"])
    updated_values = []
    existing = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.UUID(resource["user_uuid"]),
        stream_uuid=stream_uuid,
        topic_uuid=sys_uuid.UUID(resource["topic_uuid"]),
        created_at=datetime.datetime(2026, 7, 23, 12),
        update_dm=lambda values: updated_values.append(values),
        update=lambda session=None: None,
    )
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: existing)
    ensured_recipients = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "ensure_workspace_message_recipients",
        lambda *args, **kwargs: ensured_recipients.append((args, kwargs)),
    )
    compact_calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_compact_workspace_message_updated_events",
        lambda *args, **kwargs: compact_calls.append((args, kwargs)),
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid
    assert len(updated_values) == 1
    assert isinstance(
        updated_values[0]["payload"],
        message_payloads.MarkdownPayload,
    )
    assert updated_values[0]["payload"].content == "hello"
    assert "created_at" not in updated_values[0]
    assert not [
        item for item in session.statements if 'SET "created_at" = %s' in item[0]
    ]
    assert compact_calls == [
        (
            (sys_uuid.UUID(event["project_id"]), existing),
            {"session": session},
        )
    ]
    assert ensured_recipients == [
        (
            (sys_uuid.UUID(event["project_id"]), existing, [owner_uuid], session),
            {"emit_events": True},
        )
    ]


def test_message_update_moves_existing_provider_message_to_reported_topic(
    monkeypatch,
):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    old_topic_uuid = sys_uuid.uuid4()
    new_topic_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    resource = event["payload"]["resource"]
    resource["topic_uuid"] = str(new_topic_uuid)
    resource.pop("payload")
    message_uuid = sys_uuid.UUID(resource["uuid"])
    updated_values = []
    existing = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.UUID(resource["user_uuid"]),
        stream_uuid=stream_uuid,
        topic_uuid=old_topic_uuid,
        update_dm=lambda values: updated_values.append(values),
        update=lambda session=None: None,
    )
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: existing)
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "ensure_workspace_message_recipients",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUserMessage",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_all=lambda **_kwargs: [
                    types.SimpleNamespace(user_uuid=owner_uuid, read=False)
                ]
            )
        ),
    )
    compact_calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_compact_workspace_message_updated_events",
        lambda *args, **kwargs: compact_calls.append((args, kwargs)),
    )
    summary_invalidations = []
    monkeypatch.setattr(
        provider_event_apply.external_projection,
        "_invalidate_moved_topic_summaries",
        lambda *args, **kwargs: summary_invalidations.append((args, kwargs)),
    )
    unread_calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "_create_compact_messages_unread_updated_events",
        lambda *args, **kwargs: unread_calls.append((args, kwargs)),
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid
    assert len(updated_values) == 1
    assert updated_values[0]["topic_uuid"] == new_topic_uuid
    assert "payload" not in updated_values[0]
    assert compact_calls == [
        (
            (sys_uuid.UUID(event["project_id"]), existing),
            {"session": session},
        )
    ]
    assert summary_invalidations == [((session, [old_topic_uuid, new_topic_uuid]), {})]
    assert unread_calls == [
        (
            (
                sys_uuid.UUID(event["project_id"]),
                [owner_uuid],
                stream_uuid,
                topic_uuid,
            ),
            {"session": session, "recipients_are_scoped": True},
        )
        for topic_uuid in (old_topic_uuid, new_topic_uuid)
    ]


def test_message_update_moves_existing_provider_message_to_reported_stream(
    monkeypatch,
):
    identity = _identity()
    old_stream_uuid = sys_uuid.uuid4()
    new_stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    old_topic_uuid = sys_uuid.uuid4()
    new_topic_uuid = sys_uuid.uuid4()
    event = _message_event(new_stream_uuid)
    resource = event["payload"]["resource"]
    resource["topic_uuid"] = str(new_topic_uuid)
    resource.pop("payload")
    message_uuid = sys_uuid.UUID(resource["uuid"])
    updated_values = []
    existing = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.UUID(resource["user_uuid"]),
        stream_uuid=old_stream_uuid,
        topic_uuid=old_topic_uuid,
        update_dm=lambda values: updated_values.append(values),
        update=lambda session=None: None,
    )
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": new_stream_uuid,
            "provider_chat_id": "zulip-channel-8",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: existing)
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "ensure_workspace_message_recipients",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceUserMessage",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_all=lambda **_kwargs: [
                    types.SimpleNamespace(user_uuid=owner_uuid, read=False)
                ]
            )
        ),
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_compact_workspace_message_updated_events",
        lambda *_args, **_kwargs: None,
    )
    summary_invalidations = []
    monkeypatch.setattr(
        provider_event_apply.external_projection,
        "_invalidate_moved_topic_summaries",
        lambda *args, **kwargs: summary_invalidations.append((args, kwargs)),
    )
    unread_calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "_create_compact_messages_unread_updated_events",
        lambda *args, **kwargs: unread_calls.append((args, kwargs)),
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid
    assert updated_values[0]["stream_uuid"] == new_stream_uuid
    assert updated_values[0]["topic_uuid"] == new_topic_uuid
    assert summary_invalidations == [((session, [old_topic_uuid, new_topic_uuid]), {})]
    assert unread_calls == [
        (
            (
                sys_uuid.UUID(event["project_id"]),
                [owner_uuid],
                stream_uuid,
                topic_uuid,
            ),
            {"session": session, "recipients_are_scoped": True},
        )
        for stream_uuid, topic_uuid in (
            (old_stream_uuid, old_topic_uuid),
            (new_stream_uuid, new_topic_uuid),
        )
    ]


def test_message_update_atomically_moves_provider_projection_between_projects(
    monkeypatch,
):
    identity = _identity()
    source_project_uuid = sys_uuid.uuid4()
    destination_project_uuid = sys_uuid.uuid4()
    old_stream_uuid = sys_uuid.uuid4()
    new_stream_uuid = sys_uuid.uuid4()
    old_topic_uuid = sys_uuid.uuid4()
    new_topic_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(new_stream_uuid)
    event["project_id"] = str(destination_project_uuid)
    resource = event["payload"]["resource"]
    resource["topic_uuid"] = str(new_topic_uuid)
    resource.pop("payload")
    message_uuid = sys_uuid.UUID(resource["uuid"])
    updated_values = []

    existing = types.SimpleNamespace(
        uuid=message_uuid,
        project_id=source_project_uuid,
        user_uuid=sys_uuid.UUID(resource["user_uuid"]),
        stream_uuid=old_stream_uuid,
        topic_uuid=old_topic_uuid,
        source_name="zulip",
        source={"kind": "zulip"},
        external_account_uuid=sys_uuid.UUID(event["external_account_uuid"]),
        provider_uuid=identity.bridge_instance_uuid,
    )

    def update_dm(values):
        updated_values.append(values)
        for name, value in values.items():
            setattr(existing, name, value)

    existing.update_dm = update_dm
    existing.update = lambda session=None: None
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": new_stream_uuid,
            "provider_chat_id": "zulip-channel-8",
        }
    )
    destination_lookups = 0

    def existing_lookup(_model, project_id, _message_uuid, _session):
        nonlocal destination_lookups
        if project_id == destination_project_uuid:
            destination_lookups += 1
            return None if destination_lookups == 1 else existing
        return existing

    monkeypatch.setattr(provider_event_apply, "_existing", existing_lookup)
    projection_calls = []
    monkeypatch.setattr(
        provider_event_apply,
        "_ensure_projection_owner_stream",
        lambda *args, **kwargs: projection_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        provider_event_apply,
        "_message_recipients",
        lambda *_args: [owner_uuid],
    )
    monkeypatch.setattr(
        provider_event_apply,
        "_message_unread_recipients",
        lambda *_args: [owner_uuid],
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "ensure_workspace_message_recipients",
        lambda *_args, **_kwargs: None,
    )
    deleted_events = []
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "create_message_deleted_events",
        lambda **kwargs: deleted_events.append(kwargs),
    )
    created_events = []
    monkeypatch.setattr(
        provider_event_apply.helpers.messenger_events,
        "create_message_events",
        lambda **kwargs: created_events.append(kwargs),
    )
    summary_invalidations = []
    monkeypatch.setattr(
        provider_event_apply.external_projection,
        "_invalidate_moved_topic_summaries",
        lambda *args, **kwargs: summary_invalidations.append((args, kwargs)),
    )
    unread_calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "_create_compact_messages_unread_updated_events",
        lambda *args, **kwargs: unread_calls.append((args, kwargs)),
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid

    assert updated_values[0]["stream_uuid"] == new_stream_uuid
    assert updated_values[0]["topic_uuid"] == new_topic_uuid
    assert len(projection_calls) == 1
    dependent_updates = [
        (statement, params)
        for statement, params in session.statements
        if statement.lstrip().startswith("UPDATE m_workspace_")
        and "m_workspace_read_state_projects_v1" not in statement
    ]
    assert [params for _statement, params in dependent_updates] == [
        (
            destination_project_uuid,
            new_stream_uuid,
            new_topic_uuid,
            message_uuid,
            source_project_uuid,
        ),
        (destination_project_uuid, message_uuid, source_project_uuid),
        (destination_project_uuid, message_uuid, source_project_uuid),
    ]
    assert deleted_events[0]["project_id"] == source_project_uuid
    assert deleted_events[0]["stream_uuid"] == old_stream_uuid
    assert created_events[0]["project_id"] == destination_project_uuid
    assert created_events[0]["recipients"] == [owner_uuid]
    assert summary_invalidations == [((session, [old_topic_uuid, new_topic_uuid]), {})]
    assert [call[0][0] for call in unread_calls] == [
        source_project_uuid,
        destination_project_uuid,
    ]


def test_cross_project_payload_keeps_destination_scoped_file_urn(monkeypatch):
    account_uuid = sys_uuid.uuid4()
    source_project_uuid = sys_uuid.uuid4()
    source_stream_uuid = sys_uuid.uuid4()
    destination_project_uuid = sys_uuid.uuid4()
    destination_stream_uuid = sys_uuid.uuid4()
    destination_file_uuid = sys_uuid.uuid4()
    destination_file = types.SimpleNamespace(
        uuid=destination_file_uuid,
        project_id=destination_project_uuid,
        stream_uuid=destination_stream_uuid,
        acl_mode="stream",
        external_account_uuid=account_uuid,
    )
    monkeypatch.setattr(
        provider_event_apply.models,
        "WorkspaceFile",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(
                get_one_or_none=lambda **_kwargs: destination_file
            )
        ),
    )
    file_urn = f"urn:image:{destination_file_uuid}?preview=1"

    payload = provider_event_apply._reproject_message_payload_files(
        Session({}),
        message_payloads.MarkdownPayload(content=f"![preview]({file_urn})"),
        source_project_uuid,
        source_stream_uuid,
        destination_project_uuid,
        destination_stream_uuid,
        sys_uuid.uuid4(),
        account_uuid,
        False,
    )

    assert payload.content == f"![preview]({file_urn})"


def test_provider_message_update_preserves_native_source(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    resource = event["payload"]["resource"]
    resource["user_uuid"] = str(owner_uuid)
    message_uuid = sys_uuid.UUID(resource["uuid"])
    updated_values = []
    existing = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.uuid4(),
        stream_uuid=stream_uuid,
        topic_uuid=sys_uuid.UUID(resource["topic_uuid"]),
        source_name=models.SourceName.NATIVE.value,
        payload=message_payloads.MarkdownPayload(content="native message"),
        provider_external_id=None,
        provider_metadata={},
        update_dm=lambda values: updated_values.append(values),
        update=lambda session=None: None,
    )
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: existing)
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_compact_workspace_message_updated_events",
        lambda *args, **kwargs: None,
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid
    assert len(updated_values) == 1
    assert "source_name" not in updated_values[0]
    assert "source" not in updated_values[0]
    assert updated_values[0]["provider_external_id"] == "zulip-message-42"


def test_idempotent_message_replay_skips_unchanged_broadcast(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    resource = event["payload"]["resource"]
    message_uuid = sys_uuid.UUID(resource["uuid"])
    updated_values = []
    persisted = []
    existing = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.UUID(resource["user_uuid"]),
        stream_uuid=stream_uuid,
        topic_uuid=sys_uuid.UUID(resource["topic_uuid"]),
        created_at=datetime.datetime(2026, 7, 23, 12),
        payload=message_payloads.MarkdownPayload(content="hello"),
        provider_external_id="zulip-message-42",
        provider_metadata={
            "original_url": "https://example.test/42",
            "kind": identity.provider_kind,
            "account_uuid": event["external_account_uuid"],
            "external_id": "zulip-message-42",
            "provider_event_uuid": str(sys_uuid.uuid4()),
            "provider_sequence": "41",
            "capabilities": {},
        },
        update_dm=lambda values: updated_values.append(values),
        update=lambda session=None: persisted.append(session),
    )
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: existing)
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_compact_workspace_message_updated_events",
        lambda *_args, **_kwargs: pytest.fail(
            "unchanged replay must not broadcast an update"
        ),
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid
    assert updated_values[0]["provider_metadata"]["provider_sequence"] == "42"
    assert persisted == [session]


def test_message_replay_accepts_plain_stored_markdown_payload(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    resource = event["payload"]["resource"]
    message_uuid = sys_uuid.UUID(resource["uuid"])
    updated_values = []
    persisted = []
    existing = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.UUID(resource["user_uuid"]),
        stream_uuid=stream_uuid,
        topic_uuid=sys_uuid.UUID(resource["topic_uuid"]),
        created_at=datetime.datetime(2026, 7, 23, 12),
        payload={"kind": "markdown", "content": "hello"},
        provider_external_id="zulip-message-42",
        provider_metadata={
            "original_url": "https://example.test/42",
            "kind": identity.provider_kind,
            "account_uuid": event["external_account_uuid"],
            "external_id": "zulip-message-42",
            "provider_event_uuid": str(sys_uuid.uuid4()),
            "provider_sequence": "41",
            "capabilities": {},
        },
        update_dm=lambda values: updated_values.append(values),
        update=lambda session=None: persisted.append(session),
    )
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: existing)
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_compact_workspace_message_updated_events",
        lambda *_args, **_kwargs: pytest.fail(
            "plain stored payload must not broadcast an update"
        ),
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid
    assert updated_values[0]["provider_metadata"]["provider_sequence"] == "42"
    assert persisted == [session]


def test_older_history_message_cannot_overwrite_newer_live_projection(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    event["provider_sequence"] = "40"
    resource = event["payload"]["resource"]
    message_uuid = sys_uuid.UUID(resource["uuid"])
    existing = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.UUID(resource["user_uuid"]),
        stream_uuid=stream_uuid,
        payload=message_payloads.MarkdownPayload(content="newer live content"),
        provider_external_id="zulip-message-42",
        provider_metadata={"provider_sequence": "41"},
        update_dm=lambda **_kwargs: pytest.fail("stale history must not mutate"),
        update=lambda **_kwargs: pytest.fail("stale history must not persist"),
    )
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: existing)
    monkeypatch.setattr(
        provider_event_apply,
        "_sync_provider_read_state",
        lambda *_args, **_kwargs: pytest.fail("stale history must not change flags"),
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid


def test_message_delete_uses_compact_broadcast_path(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    event["kind"] = "message.delete"
    message_uuid = sys_uuid.UUID(event["payload"]["resource"]["uuid"])
    author_uuid = sys_uuid.UUID(event["payload"]["resource"]["user_uuid"])
    existing = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=author_uuid,
        stream_uuid=stream_uuid,
    )
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: existing)
    compact_calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "delete_workspace_user_message",
        lambda *args, **kwargs: compact_calls.append((args, kwargs)),
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid
    assert compact_calls == [
        (
            (sys_uuid.UUID(event["project_id"]), author_uuid, message_uuid),
            {
                "session": session,
                "enforce_visibility": False,
                "compact_events": True,
            },
        )
    ]


def test_provider_stream_delete_preserves_shared_native_direct_chat(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    session = object()
    monkeypatch.setattr(
        provider_event_apply,
        "_existing",
        lambda *_args: types.SimpleNamespace(uuid=stream_uuid),
    )
    monkeypatch.setattr(
        provider_event_apply.external_projection,
        "is_native_direct_projection",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "delete_workspace_user_stream",
        lambda *_args, **_kwargs: pytest.fail(
            "provider deletion must preserve the native direct chat"
        ),
    )

    assert (
        provider_event_apply._stream_event(
            session,
            {"kind": "stream.delete"},
            project_uuid,
            {
                "owner_user_uuid": owner_uuid,
                "projection_stream_uuid": stream_uuid,
            },
            {"uuid": str(stream_uuid)},
        )
        == stream_uuid
    )


def test_unknown_provider_event_kind_is_rejected_before_database_access():
    event = _message_event(sys_uuid.uuid4())
    event["kind"] = "calendar.upsert"
    session = Session(None)

    with pytest.raises(ValueError, match="not supported"):
        provider_event_apply.apply_event(event, session, _identity())

    assert session.statements == []
