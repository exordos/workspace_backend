# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

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
