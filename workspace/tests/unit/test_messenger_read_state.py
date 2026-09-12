# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import types
import unittest.mock
import uuid as sys_uuid

from workspace.messenger_api.api import v2_store
from workspace.messenger_api.dm import read_state


def test_counter_projection_scopes_lock_stream_before_sorted_topics():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    first_topic_uuid = sys_uuid.UUID("10000000-0000-0000-0000-000000000000")
    second_topic_uuid = sys_uuid.UUID("20000000-0000-0000-0000-000000000000")
    session = types.SimpleNamespace(execute=unittest.mock.Mock())

    read_state.lock_counter_projection_scopes(
        session,
        project_id,
        user_uuid,
        stream_uuid,
        (second_topic_uuid, first_topic_uuid, second_topic_uuid),
    )

    assert session.execute.call_count == 2
    stream_statement, stream_values = session.execute.call_args_list[0].args
    topic_statement, topic_values = session.execute.call_args_list[1].args
    assert "FROM messenger_stream_bindings" in stream_statement
    assert "FOR UPDATE" in stream_statement
    assert stream_values == (project_id, user_uuid, stream_uuid)
    assert "FROM messenger_user_topic_bindings" in topic_statement
    assert "ORDER BY topic_uuid" in topic_statement
    assert topic_values == (
        project_id,
        user_uuid,
        [first_topic_uuid, second_topic_uuid],
    )


def test_stream_counter_projection_scopes_lock_active_users_in_order():
    project_id = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    session = types.SimpleNamespace(execute=unittest.mock.Mock())

    read_state.lock_stream_counter_projection_scopes(
        session,
        project_id,
        stream_uuid,
    )

    session.execute.assert_called_once()
    statement, values = session.execute.call_args.args
    assert "FROM messenger_stream_bindings" in statement
    assert "AND active" in statement
    assert "ORDER BY user_uuid" in statement
    assert "FOR UPDATE" in statement
    assert values == (project_id, stream_uuid)


def test_read_scope_repair_uses_active_projection_task_index(monkeypatch):
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    session = types.SimpleNamespace(execute=unittest.mock.Mock())
    monkeypatch.setattr(
        v2_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    store = v2_store.MessengerV2Store(project_id, user_uuid)

    store._materialize_read_scope_message_states(stream_uuid=stream_uuid)

    statement, _values = session.execute.call_args.args
    assert "status NOT IN ('completed', 'dead_letter')" in statement
    assert "status <> 'completed'" not in statement


def test_v2_read_locks_provider_accounts_and_project_before_counter_scopes(
    monkeypatch,
):
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    session = object()
    calls = []
    monkeypatch.setattr(
        v2_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    store = v2_store.MessengerV2Store(project_id, user_uuid)
    monkeypatch.setattr(
        store,
        "_lock_provider_read_accounts_for_stream",
        lambda value: calls.append(("account-lock", value)) or (account_uuid,),
    )
    monkeypatch.setattr(
        v2_store.read_state,
        "lock_projects",
        lambda *args: calls.append(("project-lock", args)),
    )
    monkeypatch.setattr(
        v2_store.read_state,
        "lock_counter_projection_scopes",
        lambda *args: calls.append(("counter-lock", args)),
    )

    result = store._lock_read_counter_scopes(stream_uuid, (topic_uuid,))

    assert result == (account_uuid,)
    assert calls == [
        ("account-lock", stream_uuid),
        ("project-lock", (session, (project_id,))),
        (
            "counter-lock",
            (session, project_id, user_uuid, stream_uuid, (topic_uuid,)),
        ),
    ]


def test_clear_message_uses_exact_negative_read_counter_deltas(monkeypatch):
    project_id = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    first_user_uuid = sys_uuid.uuid4()
    second_user_uuid = sys_uuid.uuid4()
    coordinate = read_state.MessageReadCoordinate(
        uuid=message_uuid,
        topic_uuid=topic_uuid,
        ingest_sequence=read_state.READ_CHUNK_BITS * 7 + 19,
    )
    changed = types.SimpleNamespace(
        fetchall=lambda: [
            {"user_uuid": first_user_uuid},
            {"user_uuid": second_user_uuid},
        ]
    )
    session = types.SimpleNamespace(
        execute=unittest.mock.Mock(side_effect=(changed, object()))
    )
    adjust = unittest.mock.Mock()
    monkeypatch.setattr(read_state, "message_coordinate", lambda *_args: coordinate)
    monkeypatch.setattr(read_state, "_adjust_topic_read_stats", adjust)
    monkeypatch.setattr(read_state, "lock_message_structure", lambda *_args: None)
    monkeypatch.setattr(read_state, "lock_projects", lambda *_args: None)
    monkeypatch.setattr(
        read_state,
        "bump_project_structure_revisions",
        lambda *_args: None,
    )

    read_state.clear_message_for_all_users(session, project_id, message_uuid)

    update_statement, update_values = session.execute.call_args_list[0].args
    assert "RETURNING user_uuid" in update_statement
    assert update_values == (19, 7, 19)
    adjust.assert_called_once_with(
        session,
        project_id,
        [
            {
                "user_uuid": first_user_uuid,
                "topic_uuid": topic_uuid,
                "read_delta": -1,
            },
            {
                "user_uuid": second_user_uuid,
                "topic_uuid": topic_uuid,
                "read_delta": -1,
            },
        ],
    )


def test_read_counter_verification_starts_from_sparse_true_flags():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    verified = types.SimpleNamespace(
        fetchall=lambda: [
            {
                "user_uuid": user_uuid,
                "topic_uuid": topic_uuid,
                "actual_read_count": 4,
                "stored_read_count": 4,
            }
        ]
    )
    session = types.SimpleNamespace(
        execute=unittest.mock.Mock(side_effect=(verified, object()))
    )

    processed = read_state._verify_read_stats_batch(
        session,
        project_id,
        last_user_uuid=None,
        last_topic_uuid=None,
        batch_size=37,
    )

    assert processed == 1
    statement, values = session.execute.call_args_list[0].args
    assert "FROM m_workspace_user_message_flags AS flags" in statement
    assert "flags.read = TRUE" in statement
    assert "JOIN candidate_users AS candidate_user" in statement
    fanout_join = (
        "FROM candidates AS candidate\n            LEFT JOIN m_workspace_messages"
    )
    assert fanout_join not in statement
    assert values == (project_id, 37, project_id)


def test_legacy_gap_flag_lookup_keeps_message_user_pairs():
    project_id = sys_uuid.uuid4()
    first_message_uuid = sys_uuid.uuid4()
    second_message_uuid = sys_uuid.uuid4()
    first_user_uuid = sys_uuid.uuid4()
    second_user_uuid = sys_uuid.uuid4()
    session = unittest.mock.Mock()
    session.execute.return_value.fetchall.return_value = [
        {"uuid": first_message_uuid, "user_uuid": first_user_uuid}
    ]

    existing = read_state._existing_legacy_flag_coordinates(
        session,
        project_id,
        [
            {"uuid": first_message_uuid, "user_uuid": first_user_uuid},
            {"uuid": second_message_uuid, "user_uuid": second_user_uuid},
        ],
    )

    assert existing == {(first_message_uuid, first_user_uuid)}
    statement, values = session.execute.call_args.args
    assert "FROM unnest(%s::uuid[], %s::uuid[])" in statement
    assert "flags.uuid = coordinate.uuid" in statement
    assert "flags.user_uuid = coordinate.user_uuid" in statement
    assert "ANY(" not in statement
    assert values == (
        [first_message_uuid, second_message_uuid],
        [first_user_uuid, second_user_uuid],
        project_id,
    )


def test_legacy_gap_empty_recipient_page_advances_bounded_message_cursor():
    project_id = sys_uuid.uuid4()
    session = types.SimpleNamespace(
        execute=unittest.mock.Mock(
            side_effect=(
                object(),
                types.SimpleNamespace(
                    fetchone=lambda: {"mode": read_state.PROJECT_MODE_DUAL}
                ),
                object(),
                types.SimpleNamespace(
                    fetchall=lambda: [
                        {"ingest_sequence": 11},
                        {"ingest_sequence": 12},
                    ]
                ),
                types.SimpleNamespace(fetchall=lambda: []),
                object(),
            )
        )
    )

    processed = read_state._compact_legacy_gaps_batch(
        session,
        project_id,
        last_user_uuid=None,
        last_ingest_sequence=10,
        target_ingest_sequence=99,
        repair_kind="full_pending",
        batch_size=2,
    )

    assert processed == 2
    candidate_statement, candidate_values = session.execute.call_args_list[3].args
    assert "SELECT ingest_sequence" in candidate_statement
    assert "LIMIT %s" in candidate_statement
    assert candidate_values == (project_id, 10, 99, 2)
    recipient_statement, recipient_values = session.execute.call_args_list[4].args
    assert "JOIN LATERAL" in recipient_statement
    assert recipient_values == (project_id, 10, 12, 2)
    progress_statement, progress_values = session.execute.call_args_list[5].args
    assert "last_user_uuid = %s" in progress_statement
    assert progress_values == (None, 12, 2, project_id)
