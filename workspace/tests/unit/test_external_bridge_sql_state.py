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
        {"uuid": topic_uuid, "name": "canonical"} for topic_uuid in topic_uuids
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
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    stream = SimpleNamespace(uuid=stream_uuid, user_uuid=user_uuid)
    topic = SimpleNamespace(uuid=topic_uuid, user_uuid=user_uuid)
    stream_get_all = mock.Mock(return_value=[stream])
    topic_get_all = mock.Mock(return_value=[topic])
    message_get_all = mock.Mock(
        side_effect=AssertionError("historical messages must not be loaded")
    )
    stream_event = object()
    topic_event = object()
    stream_updated = mock.Mock(return_value=stream_event)
    topic_values = []

    def capture_topics(_project, values, **_kwargs):
        topic_values.extend(values)
        return topic_event

    topic_updated = mock.Mock(side_effect=capture_topics)
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
        "prepare_stream_updated_broadcast",
        stream_updated,
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "prepare_topic_updated_broadcast",
        topic_updated,
    )
    emitted = mock.Mock()
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_prepared_resource_broadcast_events",
        emitted,
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_message_updated_event",
        message_updated,
    )
    session = object()

    result = sql_state._emit_projected_capability_events(
        session,
        {
            "project_id": project_uuid,
            "projection_stream_uuid": stream_uuid,
        },
    )

    stream_updated.assert_called_once_with(
        project_uuid,
        [stream],
        session=session,
    )
    assert topic_updated.call_count == 1
    topic_call = topic_updated.call_args
    assert topic_call.args[0] == project_uuid
    assert topic_values == [topic]
    assert topic_call.kwargs == {"session": session}
    emitted.assert_called_once_with([stream_event, topic_event], session=session)
    assert result == (1, 1, 2)
    message_get_all.assert_not_called()
    message_updated.assert_not_called()


def test_large_projected_capability_stream_prepares_before_broadcast_lock(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuids = sorted((sys_uuid.uuid4() for _index in range(3)), key=str)
    user_uuids = [sys_uuid.uuid4() for _index in range(4_000)]
    streams = [
        SimpleNamespace(uuid=stream_uuid, user_uuid=user_uuid)
        for user_uuid in user_uuids
    ]
    topics = [
        SimpleNamespace(uuid=topic_uuid, user_uuid=user_uuid)
        for topic_uuid in reversed(topic_uuids)
        for user_uuid in reversed(user_uuids)
    ]
    monkeypatch.setattr(
        sql_state.models,
        "WorkspaceUserStream",
        SimpleNamespace(objects=SimpleNamespace(get_all=lambda **_kwargs: streams)),
    )
    monkeypatch.setattr(
        sql_state.models,
        "WorkspaceUserTopic",
        SimpleNamespace(objects=SimpleNamespace(get_all=lambda **_kwargs: topics)),
    )
    calls = []

    def record_streams(project_id, values, **kwargs):
        event = ("stream", project_id, list(values), kwargs)
        calls.append(("prepare", event))
        return event

    def record_topics(project_id, values, **kwargs):
        event = ("topic", project_id, list(values), kwargs)
        calls.append(("prepare", event))
        return event

    def emit(events, **kwargs):
        calls.append(("emit", list(events), kwargs))

    monkeypatch.setattr(
        sql_state.messenger_events,
        "prepare_stream_updated_broadcast",
        record_streams,
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "prepare_topic_updated_broadcast",
        record_topics,
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_prepared_resource_broadcast_events",
        emit,
    )

    result = sql_state._emit_projected_capability_events(
        object(),
        {
            "project_id": project_uuid,
            "projection_stream_uuid": stream_uuid,
        },
    )

    assert result == (4_000, 12_000, 4)
    prepared = [call[1] for call in calls if call[0] == "prepare"]
    assert [kind for kind, _project, _values, _kwargs in prepared] == [
        "stream",
        "topic",
        "topic",
        "topic",
    ]
    assert [values[0].uuid for _kind, _project, values, _kwargs in prepared[1:]] == (
        topic_uuids
    )
    assert all(len(values) == 4_000 for _kind, _project, values, _kwargs in prepared)
    assert calls[-1][0] == "emit"
    assert calls[-1][1] == prepared
    assert all(kwargs == {"session": mock.ANY} for *_values, kwargs in prepared)


def test_initial_sync_completion_emits_latest_message_per_topic(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    first = SimpleNamespace(
        uuid=sys_uuid.uuid4(),
        created_at=datetime.datetime(2026, 8, 17, 10, tzinfo=datetime.timezone.utc),
    )
    second = SimpleNamespace(
        uuid=sys_uuid.uuid4(),
        created_at=datetime.datetime(2026, 8, 17, 11, tzinfo=datetime.timezone.utc),
    )
    latest_result = mock.Mock()
    latest_result.fetchall.return_value = [{"uuid": second.uuid}, {"uuid": first.uuid}]
    session = SimpleNamespace(execute=mock.Mock(return_value=latest_result))
    projected_events = mock.Mock()
    created_events = mock.Mock()
    message_get_all = mock.Mock(return_value=[second, first])
    monkeypatch.setattr(
        sql_state,
        "_emit_projected_capability_events",
        projected_events,
    )
    monkeypatch.setattr(
        sql_state.models,
        "WorkspaceMessage",
        SimpleNamespace(objects=SimpleNamespace(get_all=message_get_all)),
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_message_events",
        created_events,
    )
    chat = SimpleNamespace(
        project_id=project_uuid,
        projection_stream_uuid=stream_uuid,
        owner_user_uuid=owner_uuid,
    )

    sql_state._emit_initial_sync_projection_events(session, chat)

    projected_events.assert_called_once_with(
        session,
        {"project_id": project_uuid, "projection_stream_uuid": stream_uuid},
    )
    statement, parameters = session.execute.call_args.args
    assert "DISTINCT ON (message.topic_uuid)" in statement
    assert parameters == (project_uuid, stream_uuid)
    assert message_get_all.call_args.kwargs["session"] is session
    assert created_events.call_args_list == [
        mock.call(project_uuid, first, [owner_uuid], session=session),
        mock.call(project_uuid, second, [owner_uuid], session=session),
    ]


def test_capability_projection_claim_is_bounded_and_skips_locked_accounts():
    calls = []
    account_uuid = sys_uuid.uuid4()

    class Result:
        @staticmethod
        def fetchone():
            return {"uuid": account_uuid}

    session = SimpleNamespace(
        execute=lambda statement, params: calls.append((statement, params)) or Result()
    )

    assert (
        sql_state.claim_capability_projection_refresh_account(
            session,
            after_uuid=account_uuid,
        )
        == account_uuid
    )
    statement, params = calls[0]
    assert "m_workspace_stream_topics" in statement
    assert "IS DISTINCT FROM chat.capabilities" in statement
    assert "FOR UPDATE OF account SKIP LOCKED" in statement
    assert "account.uuid > %s" in statement
    assert params == (account_uuid,)
