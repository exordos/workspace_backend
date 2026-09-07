"""PostgreSQL contract tests for the sticker repository."""

import concurrent.futures
import datetime
import os
import uuid as sys_uuid

import psycopg
import psycopg.rows
import pytest

from workspace.messenger_api import sticker_repository
from workspace.messenger_api import sticker_catalog
from workspace.messenger_api.dm import stickers as sticker_models


def _row(
    sticker_uuid: sys_uuid.UUID,
    sha: str,
    *,
    title: str = "Кот",
    alt_text: str = "Кот",
    tags: list[str] | None = None,
    search_text: str | None = None,
    active: bool = True,
    blocked: bool = False,
    timestamp: datetime.datetime | None = None,
    updated_timestamp: datetime.datetime | None = None,
) -> tuple[object, ...]:
    timestamp = timestamp or datetime.datetime.now(datetime.timezone.utc)
    updated_timestamp = updated_timestamp or timestamp
    tags = tags if tags is not None else ["кот"]
    return (
        sticker_uuid,
        title,
        alt_text,
        ["🐈"],
        tags,
        search_text if search_text is not None else " ".join([title, alt_text, *tags]),
        "sticker",
        "png",
        1,
        sha,
        "internal-object",
        active,
        blocked,
        timestamp,
        updated_timestamp,
    )


def _model(
    sticker_uuid: sys_uuid.UUID,
    sha: str,
    *,
    title: str = "Новый кот",
    alt_text: str = "Описание",
    tags: list[str] | None = None,
) -> sticker_models.Sticker:
    fields = sticker_catalog.normalize_sticker_fields(
        title,
        alt_text,
        ["🐈"],
        tags if tags is not None else ["новый", "кот"],
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
    partial = sys_uuid.uuid4()
    typo = sys_uuid.uuid4()
    equal_old = sys_uuid.uuid4()
    equal_new = sys_uuid.uuid4()
    inserted = sys_uuid.uuid4()
    english = sys_uuid.uuid4()
    race_one = sys_uuid.uuid4()
    race_two = sys_uuid.uuid4()
    initial_uuids = (first, second, third, partial, typo, equal_old, equal_new)
    sticker_uuids = (
        first,
        second,
        third,
        partial,
        typo,
        equal_old,
        equal_new,
        inserted,
        english,
    )
    cleanup_uuids = sticker_uuids + (race_one, race_two)
    timestamp = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    seed_rows = (
        _row(first, "1" * 64, tags=["кот"], timestamp=timestamp),
        _row(
            second,
            "2" * 64,
            title="кот",
            tags=[],
            search_text="кот",
            timestamp=timestamp,
        ),
        _row(
            third,
            "3" * 64,
            title="котенок",
            tags=[],
            search_text="котенок",
            timestamp=timestamp,
        ),
        _row(
            partial,
            "5" * 64,
            title="животное",
            tags=[],
            search_text="животное кот",
            timestamp=timestamp,
        ),
        _row(
            typo,
            "6" * 64,
            title="животное",
            tags=[],
            search_text="кит",
            timestamp=timestamp,
        ),
        _row(
            equal_old,
            "9" * 64,
            title="животное",
            tags=[],
            search_text="животное кот",
            timestamp=timestamp,
            updated_timestamp=timestamp + datetime.timedelta(seconds=2),
        ),
        _row(
            equal_new,
            "a" * 64,
            title="животное",
            tags=[],
            search_text="животное кот",
            timestamp=timestamp,
            updated_timestamp=timestamp + datetime.timedelta(seconds=2),
        ),
    )
    with db.cursor() as cursor:
        for values in seed_rows:
            cursor.execute(
                """
                INSERT INTO m_workspace_stickers
                  (uuid,title,alt_text,emoji,tags,search_text,category,format,
                   size_bytes,sha256,media_object_id,active,blocked,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                values,
            )
    try:
        repository.star(db, user_uuid, first)
        repository.star(db, user_uuid, first)
        repository.star(db, user_uuid, second)
        repository.star(db, user_uuid, third)
        repository.star(db, user_uuid, partial)
        repository.star(db, user_uuid, equal_old)
        repository.star(db, user_uuid, equal_new)
        user_two = sys_uuid.uuid4()
        repository.star(db, user_two, typo)
        with db.cursor() as cursor:
            favorite_times = {
                first: timestamp + datetime.timedelta(seconds=1),
                second: timestamp + datetime.timedelta(seconds=2),
                third: timestamp + datetime.timedelta(seconds=3),
                partial: timestamp + datetime.timedelta(seconds=4),
                equal_old: timestamp + datetime.timedelta(seconds=5),
                equal_new: timestamp + datetime.timedelta(seconds=5),
            }
            for sticker_uuid, favorite_time in favorite_times.items():
                cursor.execute(
                    """
                    UPDATE m_workspace_sticker_favorites
                       SET created_at = %s
                     WHERE user_uuid = %s AND sticker_uuid = %s
                    """,
                    (favorite_time, user_uuid, sticker_uuid),
                )

        filtered = repository.list_stickers(
            db,
            user_uuid,
            sticker_catalog.build_list_query(
                uuids=[third, third, first],
                category="sticker",
                format="png",
            ),
        )
        assert {item.uuid for item in filtered.items} == {first, third}
        assert {
            item.uuid
            for item in repository.list_stickers(
                db,
                user_two,
                sticker_catalog.build_list_query(favorite=True),
            ).items
        } == {typo}
        assert typo not in {
            item.uuid
            for item in repository.list_stickers(
                db,
                user_uuid,
                sticker_catalog.build_list_query(favorite=True),
            ).items
        }

        equal_rank_uuid_order = sorted((equal_old, equal_new), key=str, reverse=True)
        expected_orders = {
            (None, False): sorted(initial_uuids, key=str, reverse=True),
            (None, True): [*equal_rank_uuid_order, partial, third, second, first],
            ("кот", False): [
                first,
                second,
                third,
                *equal_rank_uuid_order,
                partial,
                typo,
            ],
            ("кот", True): [first, second, third, *equal_rank_uuid_order, partial],
        }
        for q, favorite in (
            (None, False),
            (None, True),
            ("кот", False),
            ("кот", True),
        ):
            actual = []
            marker = None
            while True:
                page = repository.list_stickers(
                    db,
                    user_uuid,
                    sticker_catalog.build_list_query(
                        q=q,
                        favorite=favorite,
                        page_limit=1,
                        page_marker=marker,
                    ),
                )
                actual.extend(item.uuid for item in page.items)
                if page.next_marker is None:
                    break
                marker = page.next_marker
            assert actual == expected_orders[(q, favorite)]
            assert len(actual) == len(set(actual))
            if favorite:
                assert all(
                    item.is_favorite
                    for item in repository.list_stickers(
                        db,
                        user_uuid,
                        sticker_catalog.build_list_query(q=q, favorite=True),
                    ).items
                )

        inserted_model = _model(inserted, "4" * 64)
        inserted_map = repository.insert_batch(db, [inserted_model])
        assert inserted_map["4" * 64] == inserted
        duplicate_map = repository.insert_batch(
            db, [_model(sys_uuid.uuid4(), "4" * 64)]
        )
        assert duplicate_map["4" * 64] == inserted
        assert repository.find_duplicates(db, ["4" * 64])["4" * 64] == inserted
        repository.insert_batch(
            db,
            [
                _model(
                    english,
                    "8" * 64,
                    title="Cat",
                    alt_text="Friendly cat",
                    tags=["pet"],
                )
            ],
        )
        english_page = repository.list_stickers(
            db,
            user_uuid,
            sticker_catalog.build_list_query(q="CAT"),
        )
        assert [item.uuid for item in english_page.items] == [english]

        updated_text = repository.update(
            db, third, {"title": "Ёж", "tags": ["ЛИСА", "лиса"]}
        )
        assert updated_text is not None
        assert updated_text.sticker.search_text == "еж кот лиса"
        assert [
            item.uuid
            for item in repository.list_stickers(
                db,
                user_uuid,
                sticker_catalog.build_list_query(q="ёж"),
            ).items
        ] == [third]
        assert [
            item.uuid
            for item in repository.list_stickers(
                db,
                user_uuid,
                sticker_catalog.build_list_query(q="еж"),
            ).items
        ] == [third]

        hidden = repository.update(db, first, {"active": False})
        assert hidden is not None
        assert hidden.sticker.active is False
        assert repository.get_active(db, user_uuid, first) is None
        with pytest.raises(sticker_repository.StickerNotVisibleError):
            repository.star(db, user_uuid, first)
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM m_workspace_sticker_favorites
                 WHERE user_uuid = %s AND sticker_uuid = %s
                """,
                (user_uuid, first),
            )
            assert cursor.fetchone()["count"] == 1
        blocked = repository.update(db, second, {"active": False, "blocked": True})
        assert blocked is not None
        assert blocked.sticker.blocked is True
        with pytest.raises(sticker_repository.StickerRepositoryValidationError):
            repository.update(db, third, {"active": True, "blocked": True})
        assert repository.get_active(db, user_uuid, second) is None
        normal_after_hide = repository.list_stickers(
            db, user_uuid, sticker_catalog.build_list_query()
        )
        assert first not in {item.uuid for item in normal_after_hide.items}
        assert second not in {item.uuid for item in normal_after_hide.items}
        with pytest.raises(sticker_repository.StickerNotVisibleError):
            repository.star(db, user_uuid, second)
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM m_workspace_sticker_favorites
                 WHERE user_uuid = %s AND sticker_uuid = %s
                """,
                (user_uuid, second),
            )
            assert cursor.fetchone()["count"] == 1
        resolved = repository.resolve_batch(db, user_uuid, sticker_uuids)
        assert first in {item.uuid for item in resolved}
        assert second not in {item.uuid for item in resolved}

        restored_first = repository.update(db, first, {"active": True})
        restored_second = repository.update(
            db, second, {"active": True, "blocked": False}
        )
        assert restored_first is not None and restored_first.sticker.active is True
        assert restored_second is not None and restored_second.sticker.blocked is False
        assert repository.get_active(db, user_uuid, first) is not None
        restored_favorites = repository.list_stickers(
            db,
            user_uuid,
            sticker_catalog.build_list_query(favorite=True),
        )
        assert {item.uuid for item in restored_favorites.items} >= {first, second}

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
                return repository.star(connection, user_uuid, typo)
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(star_from_new_connection, (1, 2))) == [True, True]
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM m_workspace_sticker_favorites
                 WHERE user_uuid = %s AND sticker_uuid = %s
                """,
                (user_uuid, typo),
            )
            assert cursor.fetchone()["count"] == 1

        race_models = (_model(race_one, "7" * 64), _model(race_two, "7" * 64))

        def insert_from_new_connection(index: int) -> dict[str, sys_uuid.UUID]:
            connection = psycopg.connect(
                os.environ.get(
                    "WORKSPACE_TEST_DB_URL",
                    "postgresql://workspace:pass@localhost:5432/workspace_test",
                ),
                autocommit=True,
            )
            connection.row_factory = psycopg.rows.dict_row
            try:
                return repository.insert_batch(connection, [race_models[index]])
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            race_results = list(executor.map(insert_from_new_connection, (0, 1)))
        assert race_results[0]["7" * 64] == race_results[1]["7" * 64]
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM m_workspace_stickers WHERE sha256 = %s",
                ("7" * 64,),
            )
            assert cursor.fetchone()["count"] == 1
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_stickers WHERE uuid = ANY(%s::uuid[])",
                (list(cleanup_uuids),),
            )
