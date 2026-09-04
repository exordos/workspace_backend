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

import dataclasses
import hashlib
import io
import os
import pathlib
import tempfile
import typing
import uuid as sys_uuid

import botocore.exceptions as botocore_exceptions

from workspace.common import file_storage_opts
from workspace.messenger_api import file_storage


SUPPORTED_FORMATS = frozenset(("gif", "webp", "png"))
_COPY_CHUNK_SIZE = 1024 * 1024


class StickerStorageError(RuntimeError):
    """Base error for the sticker storage boundary."""


class StickerStorageNotFoundError(StickerStorageError):
    """The requested sticker object does not exist."""


class StickerStorageConflictError(StickerStorageError):
    """An object id is occupied by different content."""


class StickerStorageBackendError(StickerStorageError):
    """The storage backend could not complete an operation."""


class StickerStorageConfigurationError(StickerStorageError):
    """The storage backend is not configured."""


@typing.final
@dataclasses.dataclass(frozen=True)
class StickerStorageInfo:
    storage_type: str
    storage_id: str
    storage_object_id: str


class StickerStorage(typing.Protocol):
    def save(
        self,
        sticker_uuid: sys_uuid.UUID,
        format: str,
        source: bytes | typing.BinaryIO,
    ) -> StickerStorageInfo: ...

    def read(self, sticker_uuid: sys_uuid.UUID, format: str) -> bytes: ...

    def delete(self, sticker_uuid: sys_uuid.UUID, format: str) -> None: ...


def get_sticker_object_id(sticker_uuid: sys_uuid.UUID, format: str) -> str:
    try:
        normalized_uuid = sys_uuid.UUID(str(sticker_uuid))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("Sticker UUID is invalid") from error
    if format not in SUPPORTED_FORMATS:
        raise ValueError("Sticker format is unsupported")
    return f"stickers/{normalized_uuid}/media.{format}"


def _read_source_to_temp(
    source: bytes | typing.BinaryIO,
    directory: pathlib.Path | None = None,
) -> tuple[pathlib.Path, int, str]:
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    if not hasattr(source, "read"):
        raise TypeError("Sticker source must be bytes or a binary file")
    file_source = typing.cast(typing.BinaryIO, source)

    digest = hashlib.sha256()
    size = 0
    temporary_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=directory,
            delete=False,
        ) as temporary:
            temporary_path = pathlib.Path(temporary.name)
            while True:
                chunk = file_source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise TypeError("Sticker source must return binary data")
                temporary.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        return temporary_path, size, digest.hexdigest()
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _file_matches(path: pathlib.Path, size: int, digest: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    hasher = hashlib.sha256()
    actual_size = 0
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as file:
            while True:
                chunk = file.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                actual_size += len(chunk)
    except OSError:
        return False
    return actual_size == size and hasher.hexdigest() == digest


class LocalStickerStorage:
    storage_type = file_storage_opts.STORAGE_TYPE_FILE
    storage_id = ""

    def __init__(self, storage_path: str | None = None) -> None:
        self._root = pathlib.Path(
            storage_path or file_storage.get_storage_path(),
        ).resolve()

    def _path(self, sticker_uuid: sys_uuid.UUID, format: str) -> pathlib.Path:
        object_id = get_sticker_object_id(sticker_uuid, format)
        path = self._root / object_id
        if self._root not in path.parents:
            raise ValueError("Sticker object id is invalid")
        try:
            resolved_parent = path.parent.resolve()
            resolved_parent.relative_to(self._root)
        except (OSError, ValueError) as error:
            raise ValueError("Sticker storage path is invalid") from error
        return path

    def save(
        self,
        sticker_uuid: sys_uuid.UUID,
        format: str,
        source: bytes | typing.BinaryIO,
    ) -> StickerStorageInfo:
        object_id = get_sticker_object_id(sticker_uuid, format)
        path = self._path(sticker_uuid, format)
        path.parent.mkdir(parents=True, exist_ok=True)
        path = self._path(sticker_uuid, format)
        temporary_path, size, digest = _read_source_to_temp(source, path.parent)
        try:
            if path.exists() or path.is_symlink():
                if _file_matches(path, size, digest):
                    return self._info(object_id)
                raise StickerStorageConflictError("Sticker object already exists")
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                if _file_matches(path, size, digest):
                    return self._info(object_id)
                raise StickerStorageConflictError(
                    "Sticker object already exists",
                ) from None
            return self._info(object_id)
        except OSError as error:
            raise StickerStorageBackendError("Local sticker storage failed") from error
        finally:
            temporary_path.unlink(missing_ok=True)

    def read(self, sticker_uuid: sys_uuid.UUID, format: str) -> bytes:
        path = self._path(sticker_uuid, format)
        fd: int | None = None
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as file:
                fd = None
                return file.read()
        except FileNotFoundError as error:
            raise StickerStorageNotFoundError("Sticker object was not found") from error
        except OSError as error:
            raise StickerStorageBackendError("Local sticker storage failed") from error
        finally:
            if fd is not None:
                os.close(fd)

    def delete(self, sticker_uuid: sys_uuid.UUID, format: str) -> None:
        path = self._path(sticker_uuid, format)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise StickerStorageBackendError("Local sticker storage failed") from error

    def _info(self, object_id: str) -> StickerStorageInfo:
        return StickerStorageInfo(self.storage_type, self.storage_id, object_id)


class S3StickerStorage:
    storage_type = file_storage_opts.STORAGE_TYPE_S3

    def __init__(self, client: typing.Any | None = None) -> None:
        try:
            self._base = file_storage.S3WorkspaceFileStorage()
        except ValueError as error:
            raise StickerStorageConfigurationError(
                "S3 sticker storage is not configured",
            ) from error
        self.bucket_name = self._base.bucket_name
        self._client = client

    @property
    def storage_id(self) -> str:
        return self.bucket_name

    @property
    def client(self) -> typing.Any:
        return self._client if self._client is not None else self._base.client

    def save(
        self,
        sticker_uuid: sys_uuid.UUID,
        format: str,
        source: bytes | typing.BinaryIO,
    ) -> StickerStorageInfo:
        object_id = get_sticker_object_id(sticker_uuid, format)
        temporary_path, size, digest = _read_source_to_temp(source)
        try:
            existing = self._head(object_id)
            if existing is not None:
                if self._head_matches(existing, size, digest):
                    return self._info(object_id)
                raise StickerStorageConflictError("Sticker object already exists")
            with temporary_path.open("rb") as file:
                try:
                    self.client.put_object(
                        Body=file,
                        Bucket=self.bucket_name,
                        Key=object_id,
                        ContentLength=size,
                        Metadata={"sha256": digest},
                        IfNoneMatch="*",
                    )
                except botocore_exceptions.ClientError as error:
                    if self._error_code(error) == "PreconditionFailed":
                        existing = self._head(object_id)
                        if existing is not None and self._head_matches(
                            existing,
                            size,
                            digest,
                        ):
                            return self._info(object_id)
                        raise StickerStorageConflictError(
                            "Sticker object already exists",
                        ) from error
                    raise StickerStorageBackendError(
                        "S3 sticker storage failed",
                    ) from error
                except Exception as error:
                    raise StickerStorageBackendError(
                        "S3 sticker storage failed",
                    ) from error
            return self._info(object_id)
        finally:
            temporary_path.unlink(missing_ok=True)

    def read(self, sticker_uuid: sys_uuid.UUID, format: str) -> bytes:
        object_id = get_sticker_object_id(sticker_uuid, format)
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=object_id)
            return response["Body"].read()
        except botocore_exceptions.ClientError as error:
            self._raise_for_backend_error(error, "S3 sticker storage failed")
        except StickerStorageError:
            raise
        except Exception as error:
            raise StickerStorageBackendError("S3 sticker storage failed") from error
        raise AssertionError("unreachable")

    def delete(self, sticker_uuid: sys_uuid.UUID, format: str) -> None:
        object_id = get_sticker_object_id(sticker_uuid, format)
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=object_id)
        except botocore_exceptions.ClientError as error:
            if self._error_code(error) in {"404", "NoSuchKey", "NotFound"}:
                return
            self._raise_for_backend_error(error, "S3 sticker storage failed")
        except Exception as error:
            raise StickerStorageBackendError("S3 sticker storage failed") from error

    def _head(self, object_id: str) -> dict[str, typing.Any] | None:
        try:
            return self.client.head_object(Bucket=self.bucket_name, Key=object_id)
        except botocore_exceptions.ClientError as error:
            if self._error_code(error) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise StickerStorageBackendError("S3 sticker storage failed") from error
        except Exception as error:
            raise StickerStorageBackendError("S3 sticker storage failed") from error

    @staticmethod
    def _head_matches(
        response: dict[str, typing.Any],
        size: int,
        digest: str,
    ) -> bool:
        metadata = response.get("Metadata") or {}
        return (
            response.get("ContentLength") == size and metadata.get("sha256") == digest
        )

    @staticmethod
    def _error_code(error: botocore_exceptions.ClientError) -> str:
        return str(error.response.get("Error", {}).get("Code", ""))

    @classmethod
    def _raise_for_backend_error(
        cls,
        error: botocore_exceptions.ClientError,
        message: str,
    ) -> typing.NoReturn:
        if cls._error_code(error) in {"404", "NoSuchKey", "NotFound"}:
            raise StickerStorageNotFoundError(
                "Sticker object was not found",
            ) from error
        raise StickerStorageBackendError(message) from error

    def _info(self, object_id: str) -> StickerStorageInfo:
        return StickerStorageInfo(self.storage_type, self.storage_id, object_id)


def get_sticker_storage(
    storage_type: str | None = None,
) -> LocalStickerStorage | S3StickerStorage:
    selected_type = storage_type or file_storage.get_default_storage_type()
    if selected_type == file_storage_opts.STORAGE_TYPE_FILE:
        return LocalStickerStorage()
    if selected_type == file_storage_opts.STORAGE_TYPE_S3:
        return S3StickerStorage()
    raise StickerStorageConfigurationError("Sticker storage type is unsupported")
