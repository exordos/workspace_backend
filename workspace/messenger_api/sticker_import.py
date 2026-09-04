# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import collections.abc as collections_abc
import contextlib
import dataclasses
import hashlib
import io
import json
import logging
import pathlib
import re
import stat
import tempfile
import typing
import zipfile
import uuid as sys_uuid

from restalchemy.common import exceptions as ra_exc

from workspace.common import database
from workspace.messenger_api import sticker_catalog
from workspace.messenger_api import sticker_repository
from workspace.messenger_api import sticker_storage
from workspace.messenger_api.dm import stickers


MAX_COMPRESSED_BYTES = 40 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_ITEMS = 50
MAX_COMPRESSION_RATIO = 20
_COPY_CHUNK_SIZE = 1024 * 1024
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")
_ARCHIVE_SUFFIXES = frozenset(
    (
        ".7z",
        ".arj",
        ".bz2",
        ".cab",
        ".gz",
        ".iso",
        ".jar",
        ".rar",
        ".tar",
        ".xz",
        ".zip",
    )
)
LOG = logging.getLogger(__name__)


class StickerImportValidationError(ValueError):
    """A client-correctable sticker archive validation error."""


class StickerImportError(ra_exc.RestAlchemyException):
    """A non-client failure while importing a validated archive."""

    message = "Sticker archive import failed"
    code = 500


@dataclasses.dataclass(frozen=True)
class StickerImportItem:
    manifest: stickers.StickerManifestItem
    path: pathlib.Path
    size_bytes: int
    sha256: str


@dataclasses.dataclass(frozen=True)
class StickerImportArchive:
    manifest: stickers.StickerManifest
    items: tuple[StickerImportItem, ...]
    temporary_directory: pathlib.Path


@dataclasses.dataclass
class _ByteBudget:
    limit: int
    used: int = 0

    def consume(self, amount: int) -> None:
        if self.used + amount > self.limit:
            _invalid("Archive exceeds the unpacked size limit")
        self.used += amount


def _invalid(message: str, error: BaseException | None = None) -> typing.NoReturn:
    if error is None:
        raise StickerImportValidationError(message)
    raise StickerImportValidationError(message) from error


def _copy_archive(
    source: bytes | typing.BinaryIO,
    destination: pathlib.Path,
) -> None:
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    if not hasattr(source, "read"):
        _invalid("Archive source must be bytes or a binary file")
    file_source = typing.cast(typing.BinaryIO, source)
    total = 0
    try:
        with destination.open("wb") as target:
            while True:
                chunk = file_source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    _invalid("Archive source must return binary data")
                total += len(chunk)
                if total > MAX_COMPRESSED_BYTES:
                    _invalid("Archive exceeds the compressed size limit")
                target.write(chunk)
    except OSError as error:
        _invalid("Archive could not be read", error)
    except Exception as error:
        _invalid("Archive could not be read", error)


def _validate_archive_name(name: str) -> None:
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or name.startswith("//")
        or _DRIVE_PATH_RE.match(name) is not None
    ):
        _invalid("Archive contains an unsafe path")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts[:-1]):
        _invalid("Archive contains an unsafe path")
    if ".." in parts:
        _invalid("Archive contains an unsafe path")


def _is_regular_file(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode == 0 or stat.S_IFMT(mode) == 0:
        return not info.is_dir()
    return stat.S_ISREG(mode)


def _preflight_entries(
    archive: zipfile.ZipFile,
) -> tuple[zipfile.ZipInfo, dict[str, zipfile.ZipInfo], int]:
    infos = archive.infolist()
    names: set[str] = set()
    manifest_info: zipfile.ZipInfo | None = None
    media_entries: dict[str, zipfile.ZipInfo] = {}
    unpacked_size = 0
    compressed_size = 0
    for info in infos:
        name = info.filename
        _validate_archive_name(name)
        if name in names:
            _invalid("Archive contains duplicate paths")
        names.add(name)
        if info.flag_bits & 0x1:
            _invalid("Archive contains encrypted entries")
        if not _is_regular_file(info) and name != "media/":
            _invalid("Archive contains a non-regular entry")
        if name.endswith("/") and name != "media/":
            _invalid("Archive contains an unexpected directory")
        unpacked_size += info.file_size
        compressed_size += info.compress_size
        if unpacked_size > MAX_UNPACKED_BYTES:
            _invalid("Archive exceeds the unpacked size limit")
        if name == "manifest.json":
            if manifest_info is not None:
                _invalid("Archive must contain one manifest.json")
            manifest_info = info
        elif name == "media/":
            continue
        elif name.startswith("media/"):
            if pathlib.PurePosixPath(name).suffix.lower() in _ARCHIVE_SUFFIXES:
                _invalid("Archive contains a nested archive")
            if info.file_size <= 0:
                _invalid("Media file must not be empty")
            if info.file_size > MAX_MEDIA_BYTES:
                _invalid("Media file exceeds the size limit")
            media_entries[name] = info
            if len(media_entries) > MAX_ITEMS:
                _invalid("Archive contains too many items")
        else:
            _invalid("Archive contains an unexpected path")
    if manifest_info is None:
        _invalid("Archive must contain one manifest.json")
    if compressed_size and unpacked_size > compressed_size * MAX_COMPRESSION_RATIO:
        _invalid("Archive exceeds the compression ratio limit")
    return manifest_info, media_entries, unpacked_size


def _read_manifest(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    budget: _ByteBudget,
) -> tuple[stickers.StickerManifest, int]:
    try:
        content = io.BytesIO()
        actual_size = 0
        with archive.open(info, "r") as source:
            while True:
                chunk = source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    _invalid("Manifest must contain binary data")
                budget.consume(len(chunk))
                actual_size += len(chunk)
                content.write(chunk)
        if actual_size != info.file_size:
            _invalid("Manifest size does not match the archive entry")
        value = json.loads(content.getvalue().decode("utf-8"))
        manifest = stickers.StickerManifest.from_simple_type(value)
        manifest.validate()
        for item in manifest["items"]:
            item.validate()
        return manifest, actual_size
    except StickerImportValidationError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        _invalid("Manifest is invalid", error)
    except (RuntimeError, OSError, zipfile.BadZipFile) as error:
        _invalid("Manifest could not be read", error)


def _extract_media(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: pathlib.Path,
    expected_size: int,
    budget: _ByteBudget,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    try:
        with archive.open(info, "r") as source, destination.open("wb") as target:
            while True:
                chunk = source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                budget.consume(len(chunk))
                if total > MAX_MEDIA_BYTES or total > expected_size:
                    _invalid("Media file exceeds the declared size")
                digest.update(chunk)
                target.write(chunk)
    except StickerImportValidationError:
        raise
    except (RuntimeError, OSError, zipfile.BadZipFile) as error:
        _invalid("Media file could not be read", error)
    if total != expected_size:
        _invalid("Media file size does not match the archive entry")
    return total, digest.hexdigest()


def _validate_zip(
    archive_path: pathlib.Path,
    temporary_directory: pathlib.Path,
) -> StickerImportArchive:
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            manifest_info, media_entries, unpacked_size = _preflight_entries(archive)
            budget = _ByteBudget(MAX_UNPACKED_BYTES)
            manifest, _manifest_size = _read_manifest(archive, manifest_info, budget)
            items = manifest["items"]
            if len(items) > MAX_ITEMS:
                _invalid("Archive contains too many items")
            expected_paths = {item.file for item in items}
            if set(media_entries) != expected_paths:
                _invalid("Manifest and media files do not match")
            extracted: list[StickerImportItem] = []
            extracted_size = unpacked_size - manifest_info.file_size
            if extracted_size > MAX_UNPACKED_BYTES:
                _invalid("Archive exceeds the unpacked size limit")
            for item in items:
                info = media_entries[item.file]
                destination = (
                    temporary_directory
                    / "media"
                    / pathlib.PurePosixPath(
                        item.file,
                    ).name
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                size_bytes, digest = _extract_media(
                    archive,
                    info,
                    destination,
                    info.file_size,
                    budget,
                )
                if size_bytes <= 0:
                    _invalid("Media file must not be empty")
                if digest != item.sha256:
                    _invalid("Media checksum does not match the manifest")
                extracted.append(
                    StickerImportItem(
                        manifest=item,
                        path=destination,
                        size_bytes=size_bytes,
                        sha256=digest,
                    )
                )
            return StickerImportArchive(
                manifest=manifest,
                items=tuple(extracted),
                temporary_directory=temporary_directory,
            )
    except StickerImportValidationError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError) as error:
        _invalid("Archive is invalid", error)


@contextlib.contextmanager
def validate_archive(
    source: bytes | typing.BinaryIO,
) -> collections_abc.Iterator[StickerImportArchive]:
    """Validate one archive and expose extracted files only in this context."""
    with tempfile.TemporaryDirectory(prefix="workspace-sticker-import-") as raw_dir:
        temporary_directory = pathlib.Path(raw_dir)
        archive_path = temporary_directory / "archive.zip"
        _copy_archive(source, archive_path)
        result = _validate_zip(archive_path, temporary_directory)
        yield result


def _lock_sha256_values(session: typing.Any, values: typing.Iterable[str]) -> None:
    """Serialize imports for each SHA without taking a process-local lock."""

    for value in values:
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (value,),
        )


def _build_sticker(
    item: StickerImportItem,
    sticker_uuid: sys_uuid.UUID,
    media_object_id: str,
) -> stickers.Sticker:
    try:
        normalized = sticker_catalog.normalize_sticker_fields(
            item.manifest.title,
            item.manifest.alt_text,
            item.manifest.emoji,
            item.manifest.tags,
        )
        return stickers.Sticker(
            uuid=sticker_uuid,
            title=normalized["title"],
            alt_text=normalized["alt_text"],
            emoji=normalized["emoji"],
            tags=normalized["tags"],
            search_text=normalized["search_text"],
            category=item.manifest.category,
            format=item.manifest.format,
            width=item.manifest.width,
            height=item.manifest.height,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            media_object_id=media_object_id,
        )
    except (TypeError, ValueError) as error:
        raise StickerImportValidationError("Sticker metadata is invalid") from error


def _cleanup_failed_import(
    session: typing.Any,
    storage: sticker_storage.StickerStorage,
    owned_objects: collections_abc.Iterable[str],
) -> None:
    try:
        database.rollback_current_session(session)
    except Exception as error:  # pragma: no cover - defensive backend boundary
        LOG.warning(
            "Sticker import rollback failed (%s)",
            type(error).__name__,
        )
    for object_id in owned_objects:
        try:
            storage.delete(object_id)
        except Exception as error:  # pragma: no cover - exercised by integration
            LOG.warning(
                "Sticker import object cleanup failed (%s)",
                type(error).__name__,
            )


def _import_validated_archive(
    archive: StickerImportArchive,
    session: typing.Any,
    repository: typing.Any,
    storage: sticker_storage.StickerStorage,
) -> stickers.StickerImportResult:
    owned_objects: list[str] = []
    try:
        by_sha: dict[str, list[StickerImportItem]] = {}
        for item in archive.items:
            by_sha.setdefault(item.sha256, []).append(item)

        # D-13 makes the first manifest item the deterministic winner for a SHA.
        representatives = {sha: group[0] for sha, group in by_sha.items()}
        sha_values = sorted(by_sha)
        existing = repository.find_duplicates(session, sha_values)
        missing = [value for value in sha_values if value not in existing]

        if missing:
            _lock_sha256_values(session, missing)
            existing.update(repository.find_duplicates(session, missing))
        missing = [value for value in sha_values if value not in existing]

        candidates: list[stickers.Sticker] = []
        candidate_by_sha: dict[str, stickers.Sticker] = {}
        for sha in missing:
            item = representatives[sha]
            sticker_uuid = sys_uuid.uuid4()
            object_id = sticker_storage.get_sticker_object_id(
                sticker_uuid,
                item.manifest.format,
            )
            with item.path.open("rb") as source:
                saved = storage.save(sticker_uuid, item.manifest.format, source)
            if saved.storage_object_id != object_id:
                raise StickerImportError()
            owned_objects.append(saved.storage_object_id)
            candidate = _build_sticker(item, sticker_uuid, saved.storage_object_id)
            candidates.append(candidate)
            candidate_by_sha[sha] = candidate

        if candidates:
            inserted = repository.insert_batch(session, candidates)
            existing.update(inserted)
        else:
            inserted = {}

        result_items: list[stickers.StickerImportItemResult] = []
        created_count = 0
        duplicate_count = 0
        for item in archive.items:
            sha = item.sha256
            sticker_uuid = existing.get(sha)
            if sticker_uuid is None:
                raise StickerImportError()
            created = (
                sha in candidate_by_sha
                and inserted.get(sha) == candidate_by_sha[sha].uuid
                and item is representatives[sha]
            )
            status = "created" if created else "duplicate"
            if created:
                created_count += 1
            else:
                duplicate_count += 1
            result_items.append(
                stickers.StickerImportItemResult(
                    client_id=item.manifest.client_id,
                    file=item.manifest.file,
                    status=status,
                    sticker_uuid=sticker_uuid,
                )
            )
        return stickers.StickerImportResult(
            created=created_count,
            duplicates=duplicate_count,
            items=result_items,
        )
    except Exception as error:
        _cleanup_failed_import(session, storage, owned_objects)
        if isinstance(
            error, (StickerImportValidationError, ra_exc.RestAlchemyException)
        ):
            raise
        raise StickerImportError() from error


def import_archive(
    source: bytes | typing.BinaryIO,
    session: typing.Any,
    user_uuid: sys_uuid.UUID,
    repository: typing.Any | None = None,
    storage: sticker_storage.StickerStorage | None = None,
) -> stickers.StickerImportResult:
    """Import a fully validated archive in the caller's transaction.

    The service intentionally does not commit or open a session.  A successful
    return leaves the storage objects in place for the caller's final commit;
    only known failures trigger rollback and compensation.
    """

    del user_uuid
    active_repository = (
        repository if repository is not None else sticker_repository.StickerRepository()
    )
    active_storage = (
        storage if storage is not None else sticker_storage.get_sticker_storage()
    )
    with validate_archive(source) as archive:
        return _import_validated_archive(
            archive,
            session,
            active_repository,
            active_storage,
        )
