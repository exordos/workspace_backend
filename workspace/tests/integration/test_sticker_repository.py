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
    third = sys_uuid.uuid4()
    sticker_uuids = (first, second, third)
    with db.cursor() as cursor:
        for sticker_uuid, sha in zip(sticker_uuids, ("1" * 64, "2" * 64, "3" * 64)):
            cursor.execute(
                """
                INSERT INTO m_workspace_stickers
                  (uuid,title,alt_text,emoji,tags,search_text,category,format,
                   size_bytes,sha256,media_object_id,active,blocked)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                _row(sticker_uuid, sha),
            )
    try:
        repository.star(db, user_uuid, first)
        repository.star(db, user_uuid, first)
        repository.star(db, user_uuid, second)

        for q, favorite in (
            (None, False),
            (None, True),
            ("кот", False),
            ("кот", True),
        ):
            page = repository.list_stickers(
                db, user_uuid, q=q, favorite=favorite, page_limit=1
            )
            assert len(page.items) == 1
            assert page.next_marker
            next_page = repository.list_stickers(
                db,
                user_uuid,
                q=q,
                favorite=favorite,
                page_limit=1,
                page_marker=page.next_marker,
            )
            assert {item.uuid for item in page.items}.isdisjoint(
                {item.uuid for item in next_page.items}
            )
            if favorite:
                assert all(item.is_favorite for item in page.items + next_page.items)

        hidden = repository.update(db, first, {"active": False})
        assert hidden is not None
        assert hidden.sticker.active is False
        blocked = repository.update(db, second, {"active": False, "blocked": True})
        assert blocked is not None
        assert blocked.sticker.blocked is True
        resolved = repository.resolve_batch(db, user_uuid, sticker_uuids)
        assert first in {item.uuid for item in resolved}
        assert second not in {item.uuid for item in resolved}
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_stickers WHERE uuid IN (%s,%s,%s)",
                sticker_uuids,
            )
