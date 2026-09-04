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
