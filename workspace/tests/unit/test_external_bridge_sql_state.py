# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime
from types import SimpleNamespace
from unittest import mock
import uuid as sys_uuid

from workspace.external_bridge_control import sql_state


def test_assignment_preserves_provider_topic_ids_after_display_name_collision():
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    provider_topic_ids = ["42:old-a", "42:old-b"]
    chat = SimpleNamespace(
        source={
            "chat_type": "channel",
            "topics": [
                {
                    "topic_uuid": str(topic_uuid),
                    "provider_topic_id": provider_topic_id,
                    "name": provider_topic_id,
                    "is_default": False,
                }
                for topic_uuid, provider_topic_id in zip(
                    topic_uuids,
                    provider_topic_ids,
                    strict=True,
                )
            ],
        },
        projection_stream_uuid=stream_uuid,
        project_id=project_uuid,
        display_name="Engineering",
        provider_chat_id="channel:42",
        provider="zulip",
        uuid=sys_uuid.uuid4(),
        revision=3,
        external_account_uuid=sys_uuid.uuid4(),
        history_depth=100,
    )
    stream_result = mock.Mock()
    stream_result.fetchone.return_value = {
        "name": "Engineering",
        "description": "",
        "private": False,
        "private_index": None,
    }
    topics_result = mock.Mock()
    topics_result.fetchall.return_value = [
        {"uuid": topic_uuid, "name": "canonical"}
        for topic_uuid in topic_uuids
    ]
    session = SimpleNamespace(
        execute=mock.Mock(side_effect=[stream_result, topics_result])
    )

    assignment = sql_state.external_chat_assignment_desired(chat, session=session)
    topics = assignment["workspace_projection"]["topics"]

    assert [item["name"] for item in topics] == ["canonical", "canonical"]
    assert [item["provider_topic_id"] for item in topics] == provider_topic_ids


def test_membership_write_is_available_only_for_live_channels():
    assert "messenger.membership.write" in sql_state.state.KNOWN_CAPABILITIES
    descriptor = {
        "available": True,
        "revision": 1,
        "limits": {},
    }
    account_capabilities = {"messenger.membership.write": descriptor}
    chat_capabilities = {"messenger.membership.write": descriptor}

    channel = sql_state._effective_chat_capabilities(
        account_capabilities,
        chat_capabilities,
        {
            "selected": True,
            "status": "live",
            "source": {"chat_type": "channel"},
        },
    )
    direct = sql_state._effective_chat_capabilities(
        account_capabilities,
        chat_capabilities,
        {
            "selected": True,
            "status": "live",
            "source": {"chat_type": "direct"},
        },
    )

    assert channel["messenger.membership.write"]["available"] is True
    assert direct["messenger.membership.write"]["available"] is False
    assert direct["messenger.membership.write"]["unavailable_reason"]["code"] == (
        "chat_type_unsupported"
    )


def test_heartbeat_retention_is_bounded_and_uses_cutoff():
    now = datetime.datetime(2026, 7, 27, tzinfo=datetime.timezone.utc)
    retention = datetime.timedelta(hours=24)
    calls = []

    class Result:
        @staticmethod
        def fetchone():
            return {"count": 23}

    session = SimpleNamespace(
        execute=lambda statement, params: calls.append((statement, params)) or Result()
    )

    assert (
        sql_state.prune_expired_heartbeats(
            session,
            now,
            retention=retention,
            batch_size=100,
        )
        == 23
    )
    statement, params = calls[0]
    assert 'DELETE FROM "m_external_bridge_heartbeats_v1"' in statement
    assert "ORDER BY" in statement
    assert "LIMIT %s" in statement
    assert params == (now - retention, 100)


def test_capability_refresh_claim_is_ordered_and_skips_locked_accounts():
    account_uuid = sys_uuid.uuid4()
    calls = []

    class Result:
        @staticmethod
        def fetchone():
            return {"uuid": account_uuid}

    session = SimpleNamespace(
        execute=lambda statement, params: calls.append((statement, params)) or Result()
    )

    assert (
        sql_state.claim_capability_refresh_account(
            session,
            after_uuid=account_uuid,
        )
        == account_uuid
    )
    statement, params = calls[0]
    assert "ORDER BY account.uuid" in statement
    assert "LIMIT 1" in statement
    assert "FOR UPDATE OF account SKIP LOCKED" in statement
    assert "account.uuid > %s" in statement
    assert params == (account_uuid,)


def test_stale_bridge_degradation_is_an_independent_bridge_only_update():
    now = datetime.datetime(2026, 7, 29, tzinfo=datetime.timezone.utc)
    calls = []

    class Result:
        @staticmethod
        def fetchone():
            return {"count": 2}

    session = SimpleNamespace(
        execute=lambda statement, params: calls.append((statement, params)) or Result()
    )

    assert sql_state.degrade_stale_bridge_instances(session, now=now) == 2
    statement, params = calls[0]
    assert "UPDATE m_external_bridge_instances_v2" in statement
    assert "m_external_accounts_v2" not in statement
    assert "last_heartbeat_at < %s" in statement
    assert params == (now, now - datetime.timedelta(seconds=30))


def test_projected_capability_events_do_not_fan_out_to_message_history(
    monkeypatch,
):
    stream = object()
    topic = object()
    stream_get_all = mock.Mock(return_value=[stream])
    topic_get_all = mock.Mock(return_value=[topic])
    message_get_all = mock.Mock(
        side_effect=AssertionError("historical messages must not be loaded")
    )
    stream_updated = mock.Mock()
    topic_updated = mock.Mock()
    message_updated = mock.Mock()
    monkeypatch.setattr(
        sql_state.models,
        "WorkspaceUserStream",
        SimpleNamespace(objects=SimpleNamespace(get_all=stream_get_all)),
    )
    monkeypatch.setattr(
        sql_state.models,
        "WorkspaceUserTopic",
        SimpleNamespace(objects=SimpleNamespace(get_all=topic_get_all)),
    )
    monkeypatch.setattr(
        sql_state.models,
        "WorkspaceUserMessage",
        SimpleNamespace(objects=SimpleNamespace(get_all=message_get_all)),
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_stream_updated_event",
        stream_updated,
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_topic_updated_event",
        topic_updated,
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_message_updated_event",
        message_updated,
    )
    session = object()

    sql_state._emit_projected_capability_events(
        session,
        {
            "project_id": sys_uuid.uuid4(),
            "projection_stream_uuid": sys_uuid.uuid4(),
        },
    )

    stream_updated.assert_called_once_with(stream, session=session)
    topic_updated.assert_called_once_with(topic, session=session)
    message_get_all.assert_not_called()
    message_updated.assert_not_called()


def test_projected_capability_updates_do_not_rewrite_message_history(monkeypatch):
    calls = []
    session = SimpleNamespace(
        execute=lambda statement, params: calls.append((statement, params))
    )
    chat = {
        "project_id": sys_uuid.uuid4(),
        "external_account_uuid": sys_uuid.uuid4(),
        "projection_stream_uuid": sys_uuid.uuid4(),
    }
    monkeypatch.setattr(
        sql_state,
        "_emit_projected_capability_events",
        lambda current_session, current_chat: calls.append(
            ("events", (current_session, current_chat))
        ),
    )

    sql_state._update_projected_capabilities(
        session,
        chat,
        {"messenger.message.read": {"available": True}},
    )

    statements = [statement for statement, _params in calls if statement != "events"]
    assert len(statements) == 2
    assert any("UPDATE m_workspace_streams" in statement for statement in statements)
    assert any(
        "UPDATE m_workspace_stream_topics" in statement for statement in statements
    )
    assert not any("m_workspace_messages" in statement for statement in statements)
