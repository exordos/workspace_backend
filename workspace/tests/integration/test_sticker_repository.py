"""PostgreSQL contract smoke tests for the sticker repository."""

import uuid as sys_uuid

import psycopg.rows
import pytest

from workspace.messenger_api import sticker_repository


def _row(
    sticker_uuid: sys_uuid.UUID, sha: str, *, active: bool = True, blocked: bool = False
) -> tuple[object, ...]:
    return (
        sticker_uuid,
        "Кот",
        "Кот",
        ["🐈"],
        ["кот"],
        "кот кот кот",
        "sticker",
        "png",
        1,
        sha,
        "internal-object",
        active,
        blocked,
    )


@pytest.mark.usefixtures("_database")
def test_repository_visibility_favorite_and_keyset(db) -> None:
    repository = sticker_repository.StickerRepository()
    db.row_factory = psycopg.rows.dict_row
    user_uuid = sys_uuid.uuid4()
    first = sys_uuid.uuid4()
    second = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_stickers
              (uuid,title,alt_text,emoji,tags,search_text,category,format,
               size_bytes,sha256,media_object_id,active,blocked)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            _row(first, "1" * 64),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_stickers
              (uuid,title,alt_text,emoji,tags,search_text,category,format,
               size_bytes,sha256,media_object_id,active,blocked)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            _row(second, "2" * 64),
        )
    try:
        page = repository.list_stickers(db, user_uuid, q="кот", page_limit=1)
        assert len(page.items) == 1
        assert page.next_marker
        next_page = repository.list_stickers(
            db,
            user_uuid,
            q="кот",
            page_limit=1,
            page_marker=page.next_marker,
        )
        assert {item.uuid for item in page.items}.isdisjoint(
            {item.uuid for item in next_page.items}
        )
        repository.star(db, user_uuid, first)
        repository.star(db, user_uuid, first)
        favorite_page = repository.list_stickers(db, user_uuid, favorite=True)
        assert favorite_page.items[0].is_favorite
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_stickers WHERE uuid IN (%s,%s)",
                (first, second),
            )
