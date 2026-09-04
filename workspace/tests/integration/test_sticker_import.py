#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.

import concurrent.futures
import hashlib
import io
import json
import os
import pathlib
import uuid as sys_uuid
import zipfile

import psycopg

from workspace.messenger_api import sticker_import
from workspace.messenger_api import sticker_repository
from workspace.messenger_api import sticker_storage


TEST_DB_URL = os.environ.get(
    "WORKSPACE_TEST_DB_URL",
    "postgresql://workspace:pass@localhost:5432/workspace_test",
)
USER_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000000")


def _archive(items: list[dict[str, object]], files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"schema_version": 1, "items": items}),
        )
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _item(
    client_id: sys_uuid.UUID, data: bytes, *, title: str = "Sticker"
) -> dict[str, object]:
    digest = hashlib.sha256(data).hexdigest()
    return {
        "client_id": str(client_id),
        "file": "media/%s.gif" % client_id,
        "sha256": digest,
        "format": "gif",
        "title": title,
        "alt_text": "",
        "emoji": [],
        "tags": [],
    }


def _connection() -> psycopg.Connection:
    return psycopg.connect(
        TEST_DB_URL,
        autocommit=False,
        row_factory=psycopg.rows.dict_row,
    )


def _delete_stickers(connection: psycopg.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM m_workspace_stickers")
    connection.commit()


def test_real_postgres_import_created_mixed_and_hidden_blocked_duplicates(
    _database: None,
    tmp_path: pathlib.Path,
) -> None:
    first_data = b"not an image, intentionally arbitrary"
    second_data = b"another arbitrary payload"
    first_id = sys_uuid.uuid4()
    second_id = sys_uuid.uuid4()
    archive = _archive(
        [_item(first_id, first_data), _item(second_id, second_data)],
        {
            "media/%s.gif" % first_id: first_data,
            "media/%s.gif" % second_id: second_data,
        },
    )
    connection = _connection()
    try:
        repository = sticker_repository.StickerRepository()
        storage = sticker_storage.LocalStickerStorage(str(tmp_path))
        result = sticker_import.import_archive(
            archive,
            connection,
            USER_UUID,
            repository,
            storage,
        )
        connection.commit()
        assert result.created == 2
        assert result.duplicates == 0
        assert [item.status for item in result["items"]] == ["created", "created"]

        duplicate = sticker_import.import_archive(
            archive,
            connection,
            USER_UUID,
            repository,
            storage,
        )
        connection.commit()
        assert duplicate.created == 0
        assert duplicate.duplicates == 2
        assert [item.status for item in duplicate["items"]] == [
            "duplicate",
            "duplicate",
        ]

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE m_workspace_stickers SET active = FALSE WHERE sha256 = %s",
                (hashlib.sha256(first_data).hexdigest(),),
            )
            cursor.execute(
                "UPDATE m_workspace_stickers SET active = FALSE, blocked = TRUE "
                "WHERE sha256 = %s",
                (hashlib.sha256(second_data).hexdigest(),),
            )
        connection.commit()
        hidden_duplicate = sticker_import.import_archive(
            archive,
            connection,
            USER_UUID,
            repository,
            storage,
        )
        connection.commit()
        assert hidden_duplicate.created == 0
        assert hidden_duplicate.duplicates == 2
    finally:
        _delete_stickers(connection)
        connection.close()


def test_real_postgres_concurrent_same_sha_has_one_winner(
    _database: None,
    tmp_path: pathlib.Path,
) -> None:
    data = b"same arbitrary bytes"
    client_id = sys_uuid.uuid4()
    archive = _archive(
        [_item(client_id, data)],
        {"media/%s.gif" % client_id: data},
    )

    def run_import() -> str:
        connection = _connection()
        try:
            result = sticker_import.import_archive(
                archive,
                connection,
                USER_UUID,
                sticker_repository.StickerRepository(),
                sticker_storage.LocalStickerStorage(str(tmp_path)),
            )
            connection.commit()
            return result["items"][0].status
        finally:
            connection.close()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _: run_import(), range(2)))
        assert sorted(statuses) == ["created", "duplicate"]
        connection = _connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM m_workspace_stickers WHERE sha256 = %s",
                    (hashlib.sha256(data).hexdigest(),),
                )
                row = cursor.fetchone()
                assert row["count"] == 1
        finally:
            connection.close()
    finally:
        connection = _connection()
        try:
            _delete_stickers(connection)
        finally:
            connection.close()


def test_real_postgres_mixed_existing_and_new_sha_writes_only_new_storage_object(
    _database: None,
    tmp_path: pathlib.Path,
) -> None:
    existing_data = b"pre-existing arbitrary bytes"
    new_data = b"new arbitrary bytes"
    existing_id = sys_uuid.uuid4()
    new_id = sys_uuid.uuid4()
    existing_archive = _archive(
        [_item(existing_id, existing_data)],
        {"media/%s.gif" % existing_id: existing_data},
    )
    mixed_archive = _archive(
        [_item(existing_id, existing_data), _item(new_id, new_data)],
        {
            "media/%s.gif" % existing_id: existing_data,
            "media/%s.gif" % new_id: new_data,
        },
    )
    connection = _connection()
    try:
        repository = sticker_repository.StickerRepository()
        existing_storage = sticker_storage.LocalStickerStorage(
            str(tmp_path / "existing")
        )
        first = sticker_import.import_archive(
            existing_archive,
            connection,
            USER_UUID,
            repository,
            existing_storage,
        )
        connection.commit()
        existing_uuid = first["items"][0].sticker_uuid

        new_storage = sticker_storage.LocalStickerStorage(str(tmp_path / "new"))
        result = sticker_import.import_archive(
            mixed_archive,
            connection,
            USER_UUID,
            repository,
            new_storage,
        )
        connection.commit()
        assert result.created == 1
        assert result.duplicates == 1
        assert [item.status for item in result["items"]] == [
            "duplicate",
            "created",
        ]
        assert result["items"][0].sticker_uuid == existing_uuid
        assert result["items"][1].sticker_uuid != existing_uuid
        stored_files = [
            path for path in (tmp_path / "new").rglob("*") if path.is_file()
        ]
        assert len(stored_files) == 1
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) AS count FROM m_workspace_stickers")
            assert cursor.fetchone()["count"] == 2
    finally:
        _delete_stickers(connection)
        connection.close()
