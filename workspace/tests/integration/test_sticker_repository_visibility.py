#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.

import datetime
import uuid as sys_uuid

import psycopg

from workspace.messenger_api import sticker_repository


def test_uuid_batch_includes_hidden_unblocked_but_not_blocked(
    _database: None,
    db: psycopg.Connection,
) -> None:
    db.row_factory = psycopg.rows.dict_row
    user_uuid = sys_uuid.uuid4()
    hidden_uuid = sys_uuid.uuid4()
    blocked_uuid = sys_uuid.uuid4()
    visible_uuid = sys_uuid.uuid4()
    timestamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    rows = (
        (
            hidden_uuid,
            "Hidden sticker",
            "",
            [],
            ["hidden"],
            "hidden sticker hidden",
            False,
            False,
            "a" * 64,
        ),
        (
            blocked_uuid,
            "Blocked sticker",
            "",
            [],
            ["blocked"],
            "blocked sticker blocked",
            False,
            True,
            "b" * 64,
        ),
        (
            visible_uuid,
            "Visible sticker",
            "",
            [],
            ["visible"],
            "visible sticker visible",
            True,
            False,
            "c" * 64,
        ),
    )
    try:
        with db.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO m_workspace_stickers
                  (uuid,title,alt_text,emoji,tags,search_text,category,format,
                   size_bytes,sha256,media_object_id,active,blocked,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,'sticker','png',1,%s,%s,%s,%s,%s,%s)
                """,
                [
                    (
                        sticker_uuid,
                        title,
                        alt_text,
                        emoji,
                        tags,
                        search_text,
                        sha256,
                        "visibility/%s" % sticker_uuid,
                        active,
                        blocked,
                        timestamp,
                        timestamp,
                    )
                    for (
                        sticker_uuid,
                        title,
                        alt_text,
                        emoji,
                        tags,
                        search_text,
                        active,
                        blocked,
                        sha256,
                    ) in rows
                ],
            )
            cursor.executemany(
                """
                INSERT INTO m_workspace_sticker_favorites (user_uuid, sticker_uuid)
                VALUES (%s, %s)
                """,
                [(user_uuid, hidden_uuid), (user_uuid, visible_uuid)],
            )

        repository = sticker_repository.StickerRepository()
        hidden_record = repository.get_any(db, user_uuid, hidden_uuid)
        assert hidden_record is not None
        assert hidden_record.sticker.active is False
        assert hidden_record.is_favorite is True
        blocked_record = repository.get_any(db, user_uuid, blocked_uuid)
        assert blocked_record is not None
        assert blocked_record.sticker.blocked is True
        assert blocked_record.is_favorite is False
        visible_record = repository.get_any(db, user_uuid, visible_uuid)
        assert visible_record is not None
        assert visible_record.is_favorite is True
        assert repository.get_active(db, user_uuid, hidden_uuid) is None
        batch = repository.list_stickers(
            db,
            user_uuid,
            uuid=[hidden_uuid, blocked_uuid, visible_uuid],
        )
        assert {record.uuid for record in batch.items} == {
            hidden_uuid,
            visible_uuid,
        }
        assert blocked_uuid not in {record.uuid for record in batch.items}

        ordinary = repository.list_stickers(db, user_uuid)
        assert {record.uuid for record in ordinary.items} == {visible_uuid}

        hidden_search = repository.list_stickers(
            db,
            user_uuid,
            uuid=[hidden_uuid],
            q="hidden",
        )
        assert hidden_search.items == []

        favorite = repository.list_stickers(
            db,
            user_uuid,
            uuid=[hidden_uuid, visible_uuid],
            favorite=True,
        )
        assert [record.uuid for record in favorite.items] == [visible_uuid]
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_stickers WHERE uuid = ANY(%s::uuid[])",
                ([hidden_uuid, blocked_uuid, visible_uuid],),
            )


def test_search_ranks_mixed_yo_exact_tag_above_title_exact(
    _database: None,
    db: psycopg.Connection,
) -> None:
    db.row_factory = psycopg.rows.dict_row
    user_uuid = sys_uuid.uuid4()
    tag_uuid = sys_uuid.uuid4()
    title_uuid = sys_uuid.uuid4()
    timestamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    try:
        with db.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO m_workspace_stickers
                  (uuid,title,alt_text,emoji,tags,search_text,category,format,
                   size_bytes,sha256,media_object_id,active,blocked,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,'sticker','png',1,%s,%s,TRUE,FALSE,%s,%s)
                """,
                [
                    (
                        tag_uuid,
                        "Дерево",
                        "",
                        [],
                        ["берёза"],
                        "дерево береза",
                        "d" * 64,
                        "search/%s" % tag_uuid,
                        timestamp,
                        timestamp,
                    ),
                    (
                        title_uuid,
                        "Береза",
                        "",
                        [],
                        [],
                        "береза",
                        "e" * 64,
                        "search/%s" % title_uuid,
                        timestamp,
                        timestamp,
                    ),
                ],
            )
        repository = sticker_repository.StickerRepository()
        result = repository.list_stickers(db, user_uuid, q="береза")
        assert [record.uuid for record in result.items] == [tag_uuid, title_uuid]
        assert result.items[0].rank == 4.0
        assert result.items[1].rank == 3.0
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_stickers WHERE uuid = ANY(%s::uuid[])",
                ([tag_uuid, title_uuid],),
            )
