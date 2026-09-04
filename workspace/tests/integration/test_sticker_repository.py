"""PostgreSQL contract tests for the sticker repository."""

import concurrent.futures
import os
import uuid as sys_uuid

import psycopg
import psycopg.rows
import pytest

from workspace.messenger_api import sticker_repository
from workspace.messenger_api import sticker_catalog
from workspace.messenger_api.dm import stickers as sticker_models


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


def _model(sticker_uuid: sys_uuid.UUID, sha: str) -> sticker_models.Sticker:
    fields = sticker_catalog.normalize_sticker_fields(
        "Новый кот", "Описание", ["🐈"], ["новый", "кот"]
    )
    return sticker_models.Sticker(
        uuid=sticker_uuid,
        **fields,
        category="sticker",
        format="png",
        width=None,
        height=None,
        size_bytes=1,
        sha256=sha,
        media_object_id="internal-object",
        active=True,
        blocked=False,
    )


@pytest.mark.usefixtures("_database")
def test_repository_visibility_favorite_and_keyset(db) -> None:
    repository = sticker_repository.StickerRepository()
    db.row_factory = psycopg.rows.dict_row
    user_uuid = sys_uuid.uuid4()
    first = sys_uuid.uuid4()
    second = sys_uuid.uuid4()
    third = sys_uuid.uuid4()
    inserted = sys_uuid.uuid4()
    sticker_uuids = (first, second, third, inserted)
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
        user_two = sys_uuid.uuid4()
        repository.star(db, user_two, third)

        filtered = repository.list_stickers(
            db,
            user_uuid,
            uuid=[third, third, first],
            category="sticker",
            format="png",
        )
        assert {item.uuid for item in filtered.items} == {first, third}
        assert {
            item.uuid
            for item in repository.list_stickers(db, user_two, favorite=True).items
        } == {third}
        assert third not in {
            item.uuid
            for item in repository.list_stickers(db, user_uuid, favorite=True).items
        }

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

        inserted_model = _model(inserted, "4" * 64)
        inserted_map = repository.insert_batch(db, [inserted_model])
        assert inserted_map["4" * 64] == inserted
        duplicate_map = repository.insert_batch(
            db, [_model(sys_uuid.uuid4(), "4" * 64)]
        )
        assert duplicate_map["4" * 64] == inserted
        assert repository.find_duplicates(db, ["4" * 64])["4" * 64] == inserted

        updated_text = repository.update(
            db, third, {"title": "Ёж", "tags": ["ЛИСА", "лиса"]}
        )
        assert updated_text is not None
        assert updated_text.sticker.search_text == "еж кот лиса"

        hidden = repository.update(db, first, {"active": False})
        assert hidden is not None
        assert hidden.sticker.active is False
        blocked = repository.update(db, second, {"active": False, "blocked": True})
        assert blocked is not None
        assert blocked.sticker.blocked is True
        resolved = repository.resolve_batch(db, user_uuid, sticker_uuids)
        assert first in {item.uuid for item in resolved}
        assert second not in {item.uuid for item in resolved}

        def star_from_new_connection(_: int) -> bool:
            connection = psycopg.connect(
                os.environ.get(
                    "WORKSPACE_TEST_DB_URL",
                    "postgresql://workspace:pass@localhost:5432/workspace_test",
                ),
                autocommit=True,
            )
            connection.row_factory = psycopg.rows.dict_row
            try:
                return repository.star(connection, user_uuid, third)
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(star_from_new_connection, (1, 2))) == [True, True]
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_stickers WHERE uuid IN (%s,%s,%s,%s)",
                sticker_uuids,
            )
