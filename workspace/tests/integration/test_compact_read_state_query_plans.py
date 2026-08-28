# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json

import pytest


pytestmark = pytest.mark.usefixtures("_database")


def _walk_plan(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _walk_plan(child)


def test_reverse_chunk_update_uses_chunk_user_index(db):
    index_name = "m_workspace_user_read_chunks_chunk_user_idx"
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE indexname = %s
              AND schemaname = current_schema()
            """,
            (index_name,),
        )
        index_definition = cursor.fetchone()[0].replace('"', "")
        assert "(chunk_number, user_uuid)" in index_definition

        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute(
            """
            EXPLAIN (FORMAT JSON)
            UPDATE m_workspace_user_read_chunks_v1
            SET read_bits = set_bit(read_bits, %s, 0)
            WHERE chunk_number = %s
              AND get_bit(read_bits, %s) = 1
            """,
            (0, 0, 0),
        )
        plan = cursor.fetchone()[0][0]["Plan"]
        cursor.execute("SET LOCAL enable_seqscan = on")

    assert any(node.get("Index Name") == index_name for node in _walk_plan(plan))


def _explain(db, statement, values):
    with db.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("SET LOCAL enable_sort = off")
        cursor.execute(f"EXPLAIN (FORMAT JSON) {statement}", values)
        plan = cursor.fetchone()[0][0]["Plan"]
        cursor.execute("SET LOCAL enable_seqscan = on")
        cursor.execute("SET LOCAL enable_sort = on")
    return plan


def test_compact_stream_snapshot_uses_created_at_page_index(db):
    index_name = "m_workspace_messages_stream_read_page_idx"
    plan = _explain(
        db,
        """
        SELECT uuid, created_at, ingest_sequence
        FROM m_workspace_messages
        WHERE project_id = %s AND stream_uuid = %s
        ORDER BY created_at, uuid
        LIMIT 500
        """,
        (
            "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000002",
        ),
    )

    assert any(node.get("Index Name") == index_name for node in _walk_plan(plan))


def test_compact_topic_snapshot_has_covering_created_at_page_index(db):
    index_name = "m_workspace_messages_topic_read_page_idx"
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema() AND indexname = %s
            """,
            (index_name,),
        )
        index_definition = cursor.fetchone()[0].replace('"', "")

    assert "(project_id, stream_uuid, topic_uuid, created_at, uuid)" in (
        index_definition
    )
    assert "INCLUDE (ingest_sequence)" in index_definition
    assert " WHERE " not in index_definition


def test_compact_stream_mask_scan_uses_ingest_sequence_index(db):
    index_name = "m_workspace_messages_stream_ingest_sequence_idx"
    plan = _explain(
        db,
        """
        SELECT topic_uuid, ingest_sequence
        FROM m_workspace_messages
        WHERE project_id = %s AND stream_uuid = %s
        ORDER BY ingest_sequence
        """,
        (
            "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000002",
        ),
    )

    assert any(node.get("Index Name") == index_name for node in _walk_plan(plan))


def test_membership_backfill_uses_full_project_message_user_index(db):
    index_name = "m_workspace_flags_project_message_user_idx"
    plan = _explain(
        db,
        """
        SELECT uuid, user_uuid
        FROM m_workspace_user_message_flags
        WHERE project_id = %s
        ORDER BY uuid, user_uuid
        LIMIT 500
        """,
        ("10000000-0000-4000-8000-000000000001",),
    )

    assert any(node.get("Index Name") == index_name for node in _walk_plan(plan))


def test_legacy_gap_current_tail_uses_ordered_recipient_indexes(db):
    plan = _explain(
        db,
        """
        SELECT eligible.user_uuid
        FROM (
            (
                SELECT binding.user_uuid
                FROM m_workspace_stream_bindings AS binding
                WHERE binding.project_id = %s
                  AND binding.stream_uuid = %s
                  AND binding.user_uuid > %s
                ORDER BY binding.user_uuid
            )
            UNION ALL
            (
                SELECT membership.user_uuid
                FROM m_workspace_read_memberships_v1 AS membership
                WHERE membership.project_id = %s
                  AND membership.stream_uuid = %s
                  AND membership.user_uuid > %s
                  AND %s <= membership.last_detached_sequence
                  AND NOT EXISTS (
                        SELECT 1
                        FROM m_workspace_stream_bindings AS current_binding
                        WHERE current_binding.project_id = membership.project_id
                          AND current_binding.stream_uuid = membership.stream_uuid
                          AND current_binding.user_uuid = membership.user_uuid
                      )
                ORDER BY membership.user_uuid
            )
        ) AS eligible
        ORDER BY eligible.user_uuid
        LIMIT 500
        """,
        (
            "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000003",
            "10000000-0000-4000-8000-000000000002",
            "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000003",
            "10000000-0000-4000-8000-000000000002",
            281474976710656,
        ),
    )
    nodes = list(_walk_plan(plan))

    assert any(
        node.get("Index Name") == "m_workspace_stream_bindings_unique_idx"
        for node in nodes
    )
    assert any(
        node.get("Index Name") == "m_workspace_read_memberships_stream_user_idx"
        for node in nodes
    )
    recipient_bounds = " ".join(
        node.get("Index Cond", "") for node in nodes if node.get("Index Cond")
    )
    assert "user_uuid >" in recipient_bounds
    assert not any(node.get("Node Type") == "Sort" for node in nodes), json.dumps(
        plan, indent=2
    )


def test_legacy_gap_candidate_message_page_is_independently_bounded(db):
    plan = _explain(
        db,
        """
        SELECT ingest_sequence
        FROM m_workspace_messages
        WHERE project_id = %s
          AND ingest_sequence > %s
          AND ingest_sequence <= %s
        ORDER BY ingest_sequence
        LIMIT 500
        """,
        (
            "10000000-0000-4000-8000-000000000001",
            281474976710656,
            281474976711656,
        ),
    )
    nodes = list(_walk_plan(plan))

    assert any(node.get("Node Type") == "Limit" for node in nodes)
    message_nodes = [
        node
        for node in nodes
        if node.get("Index Name") == "m_workspace_messages_project_ingest_sequence_idx"
    ]
    assert message_nodes
    message_bounds = " ".join(
        node.get("Index Cond", "") for node in message_nodes if node.get("Index Cond")
    )
    assert "ingest_sequence >" in message_bounds
    assert "ingest_sequence <=" in message_bounds
    assert not any(node.get("Node Type") == "Sort" for node in nodes), json.dumps(
        plan, indent=2
    )


def test_legacy_gap_later_page_has_strict_indexed_sequence_bound(db):
    plan = _explain(
        db,
        """
        SELECT message.uuid, recipient.user_uuid, message.ingest_sequence
        FROM m_workspace_messages AS message
        JOIN LATERAL (
            SELECT eligible.user_uuid
            FROM (
                (
                    SELECT binding.user_uuid
                    FROM m_workspace_stream_bindings AS binding
                    WHERE binding.project_id = message.project_id
                      AND binding.stream_uuid = message.stream_uuid
                    ORDER BY binding.user_uuid
                )
                UNION ALL
                (
                    SELECT membership.user_uuid
                    FROM m_workspace_read_memberships_v1 AS membership
                    WHERE membership.project_id = message.project_id
                      AND membership.stream_uuid = message.stream_uuid
                      AND message.ingest_sequence
                            <= membership.last_detached_sequence
                      AND NOT EXISTS (
                            SELECT 1
                            FROM m_workspace_stream_bindings AS current_binding
                            WHERE current_binding.project_id =
                                    membership.project_id
                              AND current_binding.stream_uuid =
                                    membership.stream_uuid
                              AND current_binding.user_uuid = membership.user_uuid
                          )
                    ORDER BY membership.user_uuid
                )
            ) AS eligible
            ORDER BY eligible.user_uuid
        ) AS recipient ON TRUE
        WHERE message.project_id = %s
          AND message.ingest_sequence > %s
          AND message.ingest_sequence <= %s
        ORDER BY message.ingest_sequence, recipient.user_uuid
        LIMIT 500
        """,
        (
            "10000000-0000-4000-8000-000000000001",
            281474976710656,
            281474976711656,
        ),
    )
    nodes = list(_walk_plan(plan))

    message_nodes = [
        node
        for node in nodes
        if node.get("Index Name") == "m_workspace_messages_project_ingest_sequence_idx"
    ]
    assert message_nodes
    assert any(
        "ingest_sequence >" in node.get("Index Cond", "") for node in message_nodes
    )
    assert any(
        node.get("Index Name") == "m_workspace_stream_bindings_unique_idx"
        for node in nodes
    )
    assert any(
        node.get("Index Name") == "m_workspace_read_memberships_stream_user_idx"
        for node in nodes
    )
    assert not any(node.get("Node Type") == "Sort" for node in nodes), json.dumps(
        plan, indent=2
    )


def test_legacy_gap_flag_lookup_probes_only_paired_coordinates(db):
    plan = _explain(
        db,
        """
        SELECT flags.uuid, flags.user_uuid
        FROM unnest(%s::uuid[], %s::uuid[])
            AS coordinate(uuid, user_uuid)
        JOIN m_workspace_user_message_flags AS flags
          ON flags.project_id = %s
         AND flags.uuid = coordinate.uuid
         AND flags.user_uuid = coordinate.user_uuid
        """,
        (
            [
                "10000000-0000-4000-8000-000000000002",
                "10000000-0000-4000-8000-000000000003",
            ],
            [
                "10000000-0000-4000-8000-000000000004",
                "10000000-0000-4000-8000-000000000005",
            ],
            "10000000-0000-4000-8000-000000000001",
        ),
    )
    nodes = list(_walk_plan(plan))

    assert any(
        node.get("Index Name") == "m_workspace_flags_project_message_user_idx"
        for node in nodes
    ), json.dumps(plan, indent=2)
    assert not any(
        node.get("Node Type") == "Seq Scan"
        and node.get("Relation Name") == "m_workspace_user_message_flags"
        for node in nodes
    ), json.dumps(plan, indent=2)
    assert any(
        node.get("Node Type") == "Function Scan" and node.get("Plan Rows") == 2
        for node in nodes
    ), json.dumps(plan, indent=2)
