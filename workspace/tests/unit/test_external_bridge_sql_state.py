# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime
from types import SimpleNamespace
from unittest import mock
import uuid as sys_uuid

import pytest

from workspace.external_bridge_control import sql_state


def test_row_value_requires_mapping_columns():
    assert sql_state._row_value({"uuid": "present"}, "uuid") == "present"
    with pytest.raises(KeyError):
        sql_state._row_value({}, "uuid")


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


def test_catalog_prelock_includes_report_existing_and_changed_chat_projects(
    monkeypatch,
):
    account_uuid = sys_uuid.uuid4()
    duplicate_account_uuid = sys_uuid.uuid4()
    changed_chat_account_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    legacy_uuid = sys_uuid.uuid4()
    linked_uuid = sys_uuid.uuid4()
    existing_participant_uuid = sys_uuid.uuid4()
    canonical_participant_uuid = sys_uuid.uuid4()
    canonical_legacy_uuid = sys_uuid.uuid4()
    realm_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    report_project_uuid = sys_uuid.uuid4()
    existing_chat_project_uuid = sys_uuid.uuid4()
    changed_chat_project_uuid = sys_uuid.uuid4()

    def result(rows):
        value = mock.Mock()
        value.fetchall.return_value = rows
        return value

    def execute(statement, _params=None):
        normalized = " ".join(statement.split())
        if normalized.startswith(("SAVEPOINT", "ROLLBACK TO", "RELEASE SAVEPOINT")):
            return result([])
        if "SELECT uuid FROM m_external_accounts_v2" in normalized:
            return result([{"uuid": duplicate_account_uuid}])
        if "SELECT uuid, external_account_uuid, provider_external_id" in normalized:
            return result(
                [
                    {
                        "uuid": legacy_uuid,
                        "external_account_uuid": account_uuid,
                        "provider_external_id": "legacy-user",
                    }
                ]
            )
        if "SELECT workspace_user_uuid" in normalized:
            return result([{"workspace_user_uuid": linked_uuid}])
        if "SELECT source, project_id" in normalized:
            return result(
                [
                    {
                        "project_id": existing_chat_project_uuid,
                        "source": {
                            "participants": [
                                {"identity_uuid": str(existing_participant_uuid)}
                            ]
                        },
                    }
                ]
            )
        if "SELECT DISTINCT external_account_uuid, project_id" in normalized:
            return result(
                [
                    {
                        "external_account_uuid": changed_chat_account_uuid,
                        "project_id": changed_chat_project_uuid,
                    }
                ]
            )
        raise AssertionError(f"Unexpected SQL: {normalized}")

    session = SimpleNamespace(execute=mock.Mock(side_effect=execute))
    account_locks = []
    merge_locks = []
    monkeypatch.setattr(
        sql_state.read_state,
        "lock_external_account_resources",
        lambda requested_session, values: account_locks.append(
            (requested_session, set(values))
        ),
    )

    def canonical_uuid(_provider, _realm_uuid, provider_user_id):
        return (
            canonical_participant_uuid
            if provider_user_id == "participant"
            else canonical_legacy_uuid
        )

    monkeypatch.setattr(
        sql_state.identity_linking,
        "canonical_provider_identity_uuid",
        canonical_uuid,
    )
    monkeypatch.setattr(
        sql_state.identity_linking,
        "lock_identity_merge_resources",
        lambda requested_session, users, projects: merge_locks.append(
            (requested_session, set(users), set(projects))
        ),
    )
    identity = SimpleNamespace(provider_kind="zulip")
    report = {
        "resource_type": "external_chat_catalog",
        "resource_uuid": str(chat_uuid),
        "catalog": {
            "external_account_uuid": str(account_uuid),
            "owner_user_uuid": str(owner_uuid),
            "project_id": str(report_project_uuid),
            "source": {
                "provider_realm_uuid": str(realm_uuid),
                "provider_owner_user_id": "owner",
            },
            "participants": [{"provider_user_id": "participant"}],
        },
    }

    assert sql_state._prelock_catalog_identity_resources(
        session,
        identity,
        [report],
    )
    assert account_locks == [
        (session, {account_uuid}),
        (
            session,
            {
                account_uuid,
                duplicate_account_uuid,
                changed_chat_account_uuid,
            },
        ),
    ]
    assert len(merge_locks) == 1
    requested_session, users, projects = merge_locks[0]
    assert requested_session is session
    assert {
        owner_uuid,
        legacy_uuid,
        linked_uuid,
        existing_participant_uuid,
        canonical_participant_uuid,
        canonical_legacy_uuid,
    } <= users
    assert projects == {
        report_project_uuid,
        existing_chat_project_uuid,
        changed_chat_project_uuid,
    }


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
    candidate_statement, candidate_params = calls[0]
    assert "ORDER BY account.uuid" in candidate_statement
    assert "LIMIT 1" in candidate_statement
    assert "account.uuid > %s" in candidate_statement
    assert candidate_params == (account_uuid,)
    claim_statement, claim_params = calls[-1]
    assert "FOR UPDATE OF account SKIP LOCKED" in claim_statement
    assert "account.uuid = %s" in claim_statement
    assert claim_params == (account_uuid,)


def test_assignment_repair_locks_chats_and_requires_distinct_verified_users():
    calls = []
    result = mock.Mock()
    result.fetchall.return_value = []
    session = SimpleNamespace(
        execute=lambda statement, params: calls.append((statement, params)) or result
    )

    assert (
        sql_state.repair_external_chat_assignments(
            session,
            sys_uuid.uuid4(),
            sys_uuid.uuid4(),
            "zulip",
        )
        == 0
    )
    statement, _params = calls[0]
    normalized = " ".join(statement.split())
    assert "COUNT(DISTINCT workspace_user.uuid)" in statement
    assert "JOIN m_external_accounts_v2 AS account" in statement
    assert (
        "chat.history_depth IS DISTINCT FROM COALESCE( "
        "account.settings->>'history_depth', chat.history_depth )" in normalized
    )
    assert "FOR UPDATE OF chat SKIP LOCKED" in statement


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
        "get_stream_recipients",
        mock.Mock(return_value=[user_uuid]),
    )
    monkeypatch.setattr(
        sql_state.messenger_dm_helpers,
        "get_compact_workspace_stream_users",
        mock.Mock(return_value=[user_uuid]),
    )
    monkeypatch.setattr(
        sql_state.messenger_dm_helpers,
        "get_compact_workspace_user_stream_snapshots",
        mock.Mock(return_value=[stream]),
    )
    monkeypatch.setattr(
        sql_state.messenger_dm_helpers,
        "get_compact_workspace_user_topic_snapshots_batch",
        mock.Mock(return_value=[topic]),
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
    topic_result = SimpleNamespace(
        fetchall=mock.Mock(return_value=[{"uuid": topic_uuid}])
    )
    session = SimpleNamespace(execute=mock.Mock(return_value=topic_result))

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
    topic_snapshots = mock.Mock(
        side_effect=lambda _project_id, selected_topics, _users, **_kwargs: [
            topic for topic in topics if topic.uuid in selected_topics
        ]
    )
    monkeypatch.setattr(
        sql_state.models,
        "get_stream_recipients",
        mock.Mock(return_value=user_uuids),
    )
    monkeypatch.setattr(
        sql_state.messenger_dm_helpers,
        "get_compact_workspace_stream_users",
        mock.Mock(return_value=user_uuids),
    )
    monkeypatch.setattr(
        sql_state.messenger_dm_helpers,
        "get_compact_workspace_user_stream_snapshots",
        mock.Mock(return_value=streams),
    )
    monkeypatch.setattr(
        sql_state.messenger_dm_helpers,
        "get_compact_workspace_user_topic_snapshots_batch",
        topic_snapshots,
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

    topic_result = SimpleNamespace(
        fetchall=mock.Mock(
            return_value=[{"uuid": topic_uuid} for topic_uuid in topic_uuids]
        )
    )
    session = SimpleNamespace(execute=mock.Mock(return_value=topic_result))
    result = sql_state._emit_projected_capability_events(
        session,
        {
            "project_id": project_uuid,
            "projection_stream_uuid": stream_uuid,
        },
    )

    assert result == (4_000, 12_000, 4)
    topic_snapshots.assert_called_once_with(
        project_uuid,
        topic_uuids,
        user_uuids,
        session=session,
    )
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
    first_snapshot = {
        "uuid": first.uuid,
        "user_uuid": owner_uuid,
        "read": False,
        "pinned": True,
        "starred": False,
        "reactions": {"eyes": 2},
    }
    second_snapshot = {
        "uuid": second.uuid,
        "user_uuid": owner_uuid,
        "read": True,
        "pinned": False,
        "starred": True,
        "reactions": {"heart": 1},
    }
    compact_snapshots = mock.Mock(return_value=[second_snapshot, first_snapshot])
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
        sql_state.messenger_dm_helpers,
        "get_compact_workspace_user_message_snapshots",
        compact_snapshots,
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_compact_message_events",
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
    compact_snapshots.assert_called_once_with(
        project_uuid,
        [second.uuid, first.uuid],
        [owner_uuid],
        session=session,
    )
    assert created_events.call_args_list == [
        mock.call(project_uuid, [first_snapshot], session=session),
        mock.call(project_uuid, [second_snapshot], session=session),
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
    candidate_statement, candidate_params = calls[0]
    assert "m_workspace_stream_topics" in candidate_statement
    assert "IS DISTINCT FROM chat.capabilities" in candidate_statement
    assert "account.uuid > %s" in candidate_statement
    assert candidate_params == (account_uuid,)
    claim_statement, claim_params = calls[-1]
    assert "FOR UPDATE OF account SKIP LOCKED" in claim_statement
    assert "account.uuid = %s" in claim_statement
    assert claim_params == (account_uuid,)
