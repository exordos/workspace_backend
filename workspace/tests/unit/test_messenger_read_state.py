# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import types
import unittest.mock
import uuid as sys_uuid

from workspace.messenger_api.dm import read_state


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
