#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.

import datetime
import uuid as sys_uuid

import psycopg

from workspace.messenger_api import sticker_catalog
from workspace.messenger_api import sticker_repository


def test_representative_search_volume_uses_tag_and_trigram_indexes(
    _database: None,
    db: psycopg.Connection,
) -> None:
    db.row_factory = psycopg.rows.dict_row
    timestamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    test_uuids = [sys_uuid.UUID(int=500000 + index) for index in range(50_000)]
    favorite_user_uuid = sys_uuid.UUID(int=1)
    favorite_uuids = test_uuids[:100]
    favorite_created_at = [
        timestamp + datetime.timedelta(seconds=100 if index < 2 else 100 - index)
        for index in range(len(favorite_uuids))
    ]
    rows = [
        (
            test_uuid,
            "Performance %s" % index,
            "",
            [],
            ["performance-tag"] if index < 100 else ["other"],
            "performance needle %s" % index
            if index < 100
            else "performance unrelated %s" % index,
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
            cursor.executemany(
                """
                INSERT INTO m_workspace_sticker_favorites
                  (user_uuid, sticker_uuid, created_at)
                VALUES (%s, %s, %s)
                """,
                list(
                    zip(
                        [favorite_user_uuid] * len(favorite_uuids),
                        favorite_uuids,
                        favorite_created_at,
                    )
                ),
            )
            cursor.execute("ANALYZE m_workspace_stickers")
            cursor.execute("ANALYZE m_workspace_sticker_favorites")
            repository = sticker_repository.StickerRepository()
            plans: dict[bool, str] = {}
            for favorite in (False, True):
                query = repository._query(
                    sticker_catalog.build_list_query(
                        q="needle",
                        favorite=favorite,
                        page_limit=50,
                    )
                )
                statement, params = repository._list_statement(
                    query,
                    sys_uuid.UUID(int=1),
                    None,
                )
                cursor.execute("SELECT set_limit(%s::real)", (0.1,))
                cursor.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + statement,
                    params,
                )
                plans[favorite] = "\n".join(
                    row["QUERY PLAN"] for row in cursor.fetchall()
                )
                cursor.execute("SELECT set_limit(%s::real)", (0.3,))
            favorite_query = repository._query(
                sticker_catalog.build_list_query(
                    favorite=True,
                    page_limit=1,
                )
            )
            favorite_statement, favorite_params = repository._list_statement(
                favorite_query,
                favorite_user_uuid,
                None,
            )
            cursor.execute(
                "EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + favorite_statement,
                favorite_params,
            )
            favorite_plan = "\n".join(row["QUERY PLAN"] for row in cursor.fetchall())

        expected_order = [
            sticker_uuid
            for _, sticker_uuid in sorted(
                zip(favorite_created_at, favorite_uuids),
                reverse=True,
            )
        ]
        actual_order: list[sys_uuid.UUID] = []
        marker = None
        while True:
            page = repository.list_stickers(
                db,
                favorite_user_uuid,
                sticker_catalog.build_list_query(
                    favorite=True,
                    page_limit=1,
                    page_marker=marker,
                ),
            )
            actual_order.extend(record.uuid for record in page.items)
            if page.next_marker is None:
                break
            marker = page.next_marker
        assert actual_order == expected_order
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_sticker_favorites "
                "WHERE user_uuid = %s AND sticker_uuid = ANY(%s::uuid[])",
                (favorite_user_uuid, favorite_uuids),
            )
            cursor.execute(
                "DELETE FROM m_workspace_stickers WHERE uuid = ANY(%s::uuid[])",
                (test_uuids,),
            )

    for plan in plans.values():
        assert "m_workspace_stickers_tags_gin_idx" in plan
        assert "m_workspace_stickers_search_text_trgm_idx" in plan, plan
        assert "Seq Scan on m_workspace_stickers candidate" not in plan
    assert "m_workspace_sticker_favorites_user_created_idx" in favorite_plan
    assert "Seq Scan on m_workspace_stickers" not in favorite_plan
