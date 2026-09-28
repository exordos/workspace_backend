# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

from workspace.workspace_v3 import projections


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.fetched = False

    def fetchall(self):
        self.fetched = True
        return self._rows


def test_mapping_uses_selected_column_names_regardless_of_input_order():
    row_uuid = sys_uuid.uuid4()
    project_id = sys_uuid.uuid4()

    assert projections._mapping(
        {"project_id": project_id, "uuid": row_uuid},
        ("uuid", "project_id"),
    ) == {"uuid": row_uuid, "project_id": project_id}


def test_folder_membership_delete_rows_are_fetched_before_next_query():
    project_id = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    deleted = _Result()

    class Session:
        def execute(self, query, _params=()):
            if "FROM workspace_v3.stream_bindings AS binding" in query:
                return _Result(
                    [
                        {
                            "project_id": project_id,
                            "stream_uuid": stream_uuid,
                            "user_uuid": user_uuid,
                            "private": False,
                        }
                    ]
                )
            if "INSERT INTO workspace_v3.folders" in query:
                return _Result()
            if "DELETE FROM workspace_v3.folder_items" in query:
                return deleted
            if "INSERT INTO workspace_v3.folder_items" in query:
                assert deleted.fetched
                return _Result()
            return _Result()

    projections._sync_folder_memberships(
        Session(),
        [(project_id, stream_uuid, user_uuid)],
    )

    assert deleted.fetched


def test_claim_does_not_gate_counters_on_global_provider_activity():
    queries = []

    class Session:
        def execute(self, query, params=()):
            queries.append((query, params))
            return _Result()

    assert projections.claim_projection_tasks(Session(), "worker") == []

    claim_query, claim_params = queries[1]
    assert "FROM workspace_v3.provider_consumers AS provider" not in claim_query
    assert "task.created_at > clock_timestamp()" not in claim_query
    assert claim_params[1] == projections.DEFAULT_BATCH_SIZE


def test_topic_counter_query_caps_message_scans_and_materializes_latest_message():
    queries = []

    class Session:
        def execute(self, query, params=()):
            queries.append((query, params))
            return _Result()

    projections._update_topic_counters(
        Session(),
        [(sys_uuid.uuid4(), sys_uuid.uuid4(), sys_uuid.uuid4())],
    )

    query, params = queries[0]
    assert "raw_counter_snapshots AS MATERIALIZED" in query
    assert "latest_messages AS MATERIALIZED" in query
    assert query.count("LIMIT %s") == 2
    assert params[-2:] == (
        projections.MAX_UNREAD_COUNT,
        projections.MAX_UNREAD_COUNT,
    )


def test_stream_counter_query_sums_topic_bindings_without_message_flags():
    queries = []

    class Session:
        def execute(self, query, params=()):
            queries.append((query, params))
            return _Result()

    projections._update_stream_counters(
        Session(),
        [(sys_uuid.uuid4(), sys_uuid.uuid4(), sys_uuid.uuid4())],
    )

    query, params = queries[0]
    assert "LEFT JOIN workspace_v3.topic_bindings AS binding" in query
    assert "workspace_v3.message_flags" not in query
    assert query.count("LEAST(") == 2
    assert params[-2:] == (
        projections.MAX_UNREAD_COUNT,
        projections.MAX_UNREAD_COUNT,
    )


def test_folder_counter_query_sums_capped_stream_bindings():
    queries = []

    class Session:
        def execute(self, query, params=()):
            queries.append((query, params))
            return _Result()

    projections._update_folder_counters(
        Session(),
        {(sys_uuid.uuid4(), sys_uuid.uuid4(), sys_uuid.uuid4())},
    )

    query, params = queries[0]
    assert "LEFT JOIN workspace_v3.stream_bindings AS binding" in query
    assert query.count("LEAST(") == 2
    assert params[-2:] == (
        projections.MAX_UNREAD_COUNT,
        projections.MAX_UNREAD_COUNT,
    )
