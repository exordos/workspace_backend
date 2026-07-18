# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import types
import uuid as sys_uuid

import pytest

from workspace.external_bridge_control import provider_event_apply


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Session:
    def __init__(self, rows):
        if not isinstance(rows, list):
            rows = [rows]
        self.rows = iter(rows)
        self.statements = []

    def execute(self, statement, params):
        self.statements.append((statement, params))
        return Result(next(self.rows))


def _identity():
    return types.SimpleNamespace(
        bridge_instance_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
    )


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
                "source_name": "zulip",
                "source": {"kind": "zulip"},
                "provider_external_id": "zulip-message-42",
                "provider_metadata": {"original_url": "https://example.test/42"},
            }
        },
    }


def test_message_upsert_is_scoped_to_selected_projection_and_adds_provider_metadata(
    monkeypatch,
):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    session = Session(
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    monkeypatch.setattr(provider_event_apply, "_existing", lambda *_args: None)
    created = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "create_workspace_user_message",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )

    target_uuid = provider_event_apply.apply_event(event, session, identity)

    assert target_uuid == sys_uuid.UUID(event["payload"]["resource"]["uuid"])
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
    assert values["compact_events"] is True


def test_message_upsert_compacts_300_recipients_to_three_ui_events(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    member_uuids = [sys_uuid.uuid4() for _index in range(300)]
    event = _message_event(stream_uuid)
    event_resource = event["payload"]["resource"]
    message_uuid = sys_uuid.UUID(event_resource["uuid"])
    session = Session(
        [
            {
                "owner_user_uuid": owner_uuid,
                "projection_stream_uuid": stream_uuid,
                "provider_chat_id": "zulip-channel-7",
            },
            *[
                item
                for epoch_version in (81, 82, 83)
                for item in (None, None, {"epoch_version": epoch_version}, None)
            ],
        ]
    )
    created_flags = []

    class FakeWorkspaceMessage:
        created = None

        def __init__(self, **values):
            self.__dict__.update(values)
            self.delivery_metadata = None
            self.delivery_status = None
            self.delivery_error = None
            self.delivery_updated_at = None
            type(self).created = self

        def insert(self, session=None):
            assert session is not None

        def get_recipients(self, session=None):
            assert session is not None
            return member_uuids

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
    assert [flag["user_uuid"] for flag in created_flags] == member_uuids
    broadcast_inserts = [
        item
        for item in session.statements
        if "INSERT INTO m_workspace_broadcast_message_events_v1" in item[0]
    ]
    assert len(broadcast_inserts) == 3
    payloads = [params[7] for _statement, params in broadcast_inserts]
    assert {
        kind
        for kind in ("message.created", "topic.updated", "stream.updated")
        if any(f'"kind": "{kind}"' in payload for payload in payloads)
    } == {"message.created", "topic.updated", "stream.updated"}
    event_payload = next(
        payload for payload in payloads if '"kind": "message.created"' in payload
    )
    assert '"kind": "message.created"' in event_payload
    assert str(message_uuid) in event_payload
    assert '"kind": "zulip"' in event_payload
    assert f'"account_uuid": "{event["external_account_uuid"]}"' in (event_payload)
    assert '"external_id": "zulip-message-42"' in event_payload
    audience_members = [
        params[1]
        for statement, params in session.statements
        if "INSERT INTO m_workspace_event_audience_members_v1" in statement
    ]
    assert audience_members == [sorted(member_uuids, key=str)] * 3
    assert (
        sum(
            "m_workspace_broadcast_message_events_v1" in statement
            and statement.lstrip().startswith("INSERT INTO")
            for statement, _params in session.statements
        )
        == 3
    )
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
        {
            "owner_user_uuid": owner_uuid,
            "projection_stream_uuid": stream_uuid,
            "provider_chat_id": "zulip-channel-7",
        }
    )
    messages = {
        message_uuid: types.SimpleNamespace(
            uuid=message_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
        )
        for message_uuid in message_uuids
    }
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "get_workspace_user_message",
        lambda project_id, user_uuid, message_uuid: messages[message_uuid],
    )
    updates = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "sync_workspace_user_message_flags",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    assert provider_event_apply.apply_event(event, session, identity) == stream_uuid
    assert [args[2] for args, _kwargs in updates] == message_uuids
    assert all(args[0:2] == (project_uuid, owner_uuid) for args, _kwargs in updates)
    assert all(args[3] == {"read": True} for args, _kwargs in updates)
    assert all(kwargs == {"session": session} for _args, kwargs in updates)


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


def test_message_update_uses_compact_broadcast_path(monkeypatch):
    identity = _identity()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    event = _message_event(stream_uuid)
    resource = event["payload"]["resource"]
    message_uuid = sys_uuid.UUID(resource["uuid"])
    existing = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.UUID(resource["user_uuid"]),
        stream_uuid=stream_uuid,
        update_dm=lambda values: None,
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
    compact_calls = []
    monkeypatch.setattr(
        provider_event_apply.helpers,
        "_create_workspace_message_updated_events",
        lambda *args, **kwargs: compact_calls.append((args, kwargs)),
    )

    assert provider_event_apply.apply_event(event, session, identity) == message_uuid
    assert compact_calls == [
        (
            (sys_uuid.UUID(event["project_id"]), message_uuid),
            {"session": session, "compact_events": True},
        )
    ]


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


def test_unknown_provider_event_kind_is_rejected_before_database_access():
    event = _message_event(sys_uuid.uuid4())
    event["kind"] = "calendar.upsert"
    session = Session(None)

    with pytest.raises(ValueError, match="not supported"):
        provider_event_apply.apply_event(event, session, _identity())

    assert session.statements == []
