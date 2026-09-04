#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.

import contextlib
import pathlib
import uuid as sys_uuid

import pytest

from workspace.messenger_api import sticker_import
from workspace.messenger_api import sticker_storage
from workspace.messenger_api.dm import stickers


USER_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000000")
SHA_ONE = "a" * 64
SHA_TWO = "b" * 64


def _archive_item(
    temporary_directory: pathlib.Path,
    client_id: str,
    sha256: str,
    data: bytes,
    *,
    title: str = "  Ёжик  ",
) -> sticker_import.StickerImportItem:
    path = temporary_directory / (client_id + ".gif")
    path.write_bytes(data)
    manifest = stickers.StickerManifestItem(
        client_id=sys_uuid.UUID(client_id),
        file="media/%s.gif" % client_id,
        sha256=sha256,
        format="gif",
        title=title,
        alt_text="  Test  ",
        emoji=["👍", "👍"],
        tags=[" ЁЖ ", "еж"],
    )
    return sticker_import.StickerImportItem(
        manifest=manifest,
        path=path,
        size_bytes=len(data),
        sha256=sha256,
    )


class _Session:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.rollback_count = 0

    def execute(self, statement: str, params: tuple[object, ...]) -> None:
        self.executed.append((statement, params))

    def rollback(self) -> None:
        self.rollback_count += 1


class _UncertainSession(_Session):
    def commit(self) -> None:
        raise OSError("commit outcome is unknown")


class _Storage:
    def __init__(
        self,
        *,
        fail_on_save: int | None = None,
        fail_on_delete: bool = False,
    ) -> None:
        self.saved: list[str] = []
        self.deleted: list[str] = []
        self.fail_on_save = fail_on_save
        self.fail_on_delete = fail_on_delete

    def save(
        self,
        sticker_uuid: sys_uuid.UUID,
        format: str,
        source: object,
    ) -> sticker_storage.StickerStorageInfo:
        del format
        if self.fail_on_save == len(self.saved) + 1:
            raise RuntimeError("backend details must not escape")
        assert hasattr(source, "read")
        assert source.read() != b""
        object_id = sticker_storage.get_sticker_object_id(sticker_uuid, "gif")
        self.saved.append(object_id)
        return sticker_storage.StickerStorageInfo("test", "test", object_id)

    def delete(self, storage_object_id: str) -> None:
        if self.fail_on_delete:
            raise RuntimeError("cleanup backend details must not escape")
        self.deleted.append(storage_object_id)


class _Repository:
    def __init__(self, existing: dict[str, sys_uuid.UUID] | None = None) -> None:
        self.existing = dict(existing or {})
        self.inserted: list[stickers.Sticker] = []

    def find_duplicates(
        self,
        session: object,
        sha256_values: list[str],
    ) -> dict[str, sys_uuid.UUID]:
        del session
        return {
            value: self.existing[value]
            for value in sha256_values
            if value in self.existing
        }

    def insert_batch(
        self,
        session: object,
        values: list[stickers.Sticker],
    ) -> dict[str, sys_uuid.UUID]:
        del session
        self.inserted.extend(values)
        self.existing.update({value.sha256: value.uuid for value in values})
        return dict(self.existing)


class _FailingRepository(_Repository):
    def find_duplicates(
        self,
        session: object,
        sha256_values: list[str],
    ) -> dict[str, sys_uuid.UUID]:
        del session, sha256_values
        raise RuntimeError("SQL details must not escape")


class _FailingInsertRepository(_Repository):
    def insert_batch(
        self,
        session: object,
        values: list[stickers.Sticker],
    ) -> dict[str, sys_uuid.UUID]:
        del session, values
        raise RuntimeError("SQL details must not escape")


@contextlib.contextmanager
def _validated(archive: sticker_import.StickerImportArchive):
    yield archive


def test_import_uses_first_manifest_sha_item_and_returns_manifest_order(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    high = "f0000000-0000-0000-0000-000000000000"
    low = "10000000-0000-0000-0000-000000000000"
    first = _archive_item(tmp_path, high, SHA_ONE, b"arbitrary")
    second = _archive_item(tmp_path, low, SHA_ONE, b"arbitrary")
    archive = sticker_import.StickerImportArchive(
        manifest=stickers.StickerManifest(schema_version=1, items=[]),
        items=(first, second),
        temporary_directory=tmp_path,
    )
    monkeypatch.setattr(
        sticker_import, "validate_archive", lambda source: _validated(archive)
    )
    storage = _Storage()
    repository = _Repository()
    result = sticker_import.import_archive(
        b"ignored",
        _Session(),
        USER_UUID,
        repository,
        storage,
    )

    assert result.created == 1
    assert result.duplicates == 1
    assert [item.status for item in result["items"]] == ["created", "duplicate"]
    assert result["items"][0].sticker_uuid == result["items"][1].sticker_uuid
    assert result["items"][0].file == first.manifest.file
    assert len(storage.saved) == 1
    assert repository.inserted[0].title == "Ёжик"
    assert repository.inserted[0].search_text == "ежик test еж еж"


def test_import_storage_failure_rolls_back_and_cleans_owned_objects(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000001",
        SHA_ONE,
        b"one",
    )
    second = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000002",
        SHA_TWO,
        b"two",
    )
    archive = sticker_import.StickerImportArchive(
        manifest=stickers.StickerManifest(schema_version=1, items=[]),
        items=(first, second),
        temporary_directory=tmp_path,
    )
    monkeypatch.setattr(
        sticker_import, "validate_archive", lambda source: _validated(archive)
    )
    session = _Session()
    storage = _Storage(fail_on_save=2)

    with pytest.raises(sticker_import.StickerImportError):
        sticker_import.import_archive(
            b"ignored",
            session,
            USER_UUID,
            _Repository(),
            storage,
        )

    assert session.rollback_count == 2
    assert storage.deleted == storage.saved


def test_import_validation_failure_does_not_touch_session_or_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid(source: object):
        del source
        raise sticker_import.StickerImportValidationError("invalid archive")

    monkeypatch.setattr(sticker_import, "validate_archive", invalid)
    session = _Session()
    storage = _Storage()
    with pytest.raises(sticker_import.StickerImportValidationError):
        sticker_import.import_archive(
            b"invalid",
            session,
            USER_UUID,
            _Repository(),
            storage,
        )
    assert session.rollback_count == 0
    assert storage.saved == []


def test_success_does_not_delete_before_caller_commit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000003",
        SHA_ONE,
        b"one",
    )
    archive = sticker_import.StickerImportArchive(
        manifest=stickers.StickerManifest(schema_version=1, items=[]),
        items=(item,),
        temporary_directory=tmp_path,
    )
    monkeypatch.setattr(
        sticker_import, "validate_archive", lambda source: _validated(archive)
    )
    storage = _Storage()
    result = sticker_import.import_archive(
        b"ignored",
        _Session(),
        USER_UUID,
        _Repository(),
        storage,
    )
    assert result.created == 1
    assert storage.deleted == []


def test_known_sql_failure_rolls_back_without_storage_writes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000004",
        SHA_ONE,
        b"one",
    )
    archive = sticker_import.StickerImportArchive(
        manifest=stickers.StickerManifest(schema_version=1, items=[]),
        items=(item,),
        temporary_directory=tmp_path,
    )
    monkeypatch.setattr(
        sticker_import, "validate_archive", lambda source: _validated(archive)
    )
    session = _Session()
    storage = _Storage()
    with pytest.raises(sticker_import.StickerImportError):
        sticker_import.import_archive(
            b"ignored",
            session,
            USER_UUID,
            _FailingRepository(),
            storage,
        )
    assert session.rollback_count == 1
    assert storage.saved == []


def test_insert_failure_after_multiple_saves_cleans_exact_owned_objects(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000005",
        SHA_ONE,
        b"one",
    )
    second = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000006",
        SHA_TWO,
        b"two",
    )
    archive = sticker_import.StickerImportArchive(
        manifest=stickers.StickerManifest(schema_version=1, items=[]),
        items=(first, second),
        temporary_directory=tmp_path,
    )
    monkeypatch.setattr(
        sticker_import, "validate_archive", lambda source: _validated(archive)
    )
    session = _Session()
    storage = _Storage()
    with pytest.raises(sticker_import.StickerImportError):
        sticker_import.import_archive(
            b"ignored",
            session,
            USER_UUID,
            _FailingInsertRepository(),
            storage,
        )
    assert session.rollback_count == 2
    assert storage.deleted == storage.saved
    assert len(storage.deleted) == 2


def test_cleanup_failure_is_logged_without_masking_original_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000007",
        SHA_ONE,
        b"one",
    )
    archive = sticker_import.StickerImportArchive(
        manifest=stickers.StickerManifest(schema_version=1, items=[]),
        items=(item,),
        temporary_directory=tmp_path,
    )
    monkeypatch.setattr(
        sticker_import, "validate_archive", lambda source: _validated(archive)
    )
    storage = _Storage(fail_on_delete=True)
    caplog.set_level("WARNING")
    with pytest.raises(sticker_import.StickerImportError):
        sticker_import.import_archive(
            b"ignored",
            _Session(),
            USER_UUID,
            _FailingInsertRepository(),
            storage,
        )
    assert "cleanup backend details" not in caplog.text
    assert "Sticker import object cleanup failed" in caplog.text


def test_uncertain_caller_commit_does_not_delete_and_retry_is_duplicate(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000008",
        SHA_ONE,
        b"one",
    )
    archive = sticker_import.StickerImportArchive(
        manifest=stickers.StickerManifest(schema_version=1, items=[]),
        items=(item,),
        temporary_directory=tmp_path,
    )
    monkeypatch.setattr(
        sticker_import, "validate_archive", lambda source: _validated(archive)
    )
    storage = _Storage()
    session = _UncertainSession()
    repository = _Repository()
    result = sticker_import.import_archive(
        b"ignored", session, USER_UUID, repository, storage
    )
    assert result.created == 1
    assert storage.deleted == []

    with pytest.raises(OSError):
        session.commit()
    retry = sticker_import.import_archive(
        b"ignored", session, USER_UUID, repository, storage
    )
    assert retry.created == 0
    assert retry.duplicates == 1
    assert retry["items"][0].status == "duplicate"
    assert len(storage.saved) == 1

    # The other possible commit outcome is a rollback.  The caller removes
    # the uncommitted repository row, while the service still does not delete
    # the object whose commit outcome was uncertain.
    repository.existing.pop(SHA_ONE)
    rolled_back_retry = sticker_import.import_archive(
        b"ignored", _Session(), USER_UUID, repository, storage
    )
    assert rolled_back_retry.created == 1
    assert rolled_back_retry["items"][0].status == "created"
    assert len(storage.saved) == 2
    assert storage.saved[0] != storage.saved[1]
    assert storage.deleted == []


def test_mixed_existing_and_new_sha_has_per_item_status(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_uuid = sys_uuid.UUID("40000000-0000-0000-0000-000000000000")
    first = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000009",
        SHA_ONE,
        b"one",
    )
    second = _archive_item(
        tmp_path,
        "10000000-0000-0000-0000-000000000010",
        SHA_TWO,
        b"two",
    )
    archive = sticker_import.StickerImportArchive(
        manifest=stickers.StickerManifest(schema_version=1, items=[]),
        items=(first, second),
        temporary_directory=tmp_path,
    )
    monkeypatch.setattr(
        sticker_import, "validate_archive", lambda source: _validated(archive)
    )
    storage = _Storage()
    result = sticker_import.import_archive(
        b"ignored",
        _Session(),
        USER_UUID,
        _Repository({SHA_ONE: existing_uuid}),
        storage,
    )
    assert [item.status for item in result["items"]] == ["duplicate", "created"]
    assert result["items"][0].sticker_uuid == existing_uuid
    assert len(storage.saved) == 1
