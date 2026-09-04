#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.

import datetime
import uuid as sys_uuid

import psycopg


def test_representative_search_volume_uses_tag_and_trigram_indexes(
    _database: None,
    db: psycopg.Connection,
) -> None:
    db.row_factory = psycopg.rows.dict_row
    timestamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    test_uuids = [sys_uuid.UUID(int=500000 + index) for index in range(256)]
    rows = [
        (
            test_uuid,
            "Performance %s" % index,
            "",
            [],
            ["performance-tag"],
            "performance needle %s" % index,
            "sticker",
            "png",
            1,
            "%064x" % index,
            "performance/%s" % index,
            True,
            False,
            timestamp,
            timestamp,
        )
        for index, test_uuid in enumerate(test_uuids)
    ]
    try:
        with db.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO m_workspace_stickers
                  (uuid,title,alt_text,emoji,tags,search_text,category,format,
                   size_bytes,sha256,media_object_id,active,blocked,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                rows,
            )
            cursor.execute("SET enable_seqscan = off")
            cursor.execute(
                """
                EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
                SELECT uuid
                  FROM m_workspace_stickers
                 WHERE tags @> ARRAY[%s]::text[]
                    OR tags @> ARRAY[%s]::text[]
                """,
                ("performance-tag", "performance-tag"),
            )
            tag_plan = "\n".join(row["QUERY PLAN"] for row in cursor.fetchall())
            cursor.execute(
                """
                EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
                SELECT uuid
                  FROM m_workspace_stickers
                 WHERE search_text LIKE '%%' || %s || '%%'
                """,
                ("needle",),
            )
            text_plan = "\n".join(row["QUERY PLAN"] for row in cursor.fetchall())
            cursor.execute("RESET enable_seqscan")
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_stickers WHERE uuid = ANY(%s::uuid[])",
                (test_uuids,),
            )

    assert "m_workspace_stickers_tags_gin_idx" in tag_plan
    assert "m_workspace_stickers_search_text_trgm_idx" in text_plan
