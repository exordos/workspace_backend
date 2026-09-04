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

import io
import hashlib
import pathlib
import tempfile
import types
import uuid as sys_uuid
from unittest import mock

import botocore.exceptions as botocore_exceptions
import pytest

from workspace.messenger_api import file_storage
from workspace.messenger_api import sticker_storage


STICKER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")


def _s3_storage(client):
    base = types.SimpleNamespace(bucket_name="sticker-catalog", client=client)
    with mock.patch.object(
        file_storage,
        "S3WorkspaceFileStorage",
        return_value=base,
    ):
        return sticker_storage.S3StickerStorage(client=client)


def _client_error(code):
    return botocore_exceptions.ClientError(
        {"Error": {"Code": code, "Message": "test"}},
        "storage operation",
    )


def test_object_id_is_deterministic_and_validates_inputs():
    assert sticker_storage.get_sticker_object_id(STICKER_UUID, "webp") == (
        "stickers/00000000-0000-0000-0000-000000000001/media.webp"
    )

    with pytest.raises(ValueError):
        sticker_storage.get_sticker_object_id(STICKER_UUID, "jpg")
    with pytest.raises(ValueError):
        sticker_storage.get_sticker_object_id("not-a-uuid", "png")


def test_local_storage_saves_reads_deletes_and_has_no_sidecar(tmp_path):
    storage = sticker_storage.LocalStickerStorage(str(tmp_path))
    data = io.BytesIO(b"sticker bytes")

    info = storage.save(STICKER_UUID, "png", data)

    assert info.storage_object_id == (
        "stickers/00000000-0000-0000-0000-000000000001/media.png"
    )
    path = tmp_path / info.storage_object_id
    assert path.read_bytes() == b"sticker bytes"
    assert storage.read(STICKER_UUID, "png") == b"sticker bytes"
    assert sorted(pathlib.Path(tmp_path).rglob("*")) == [
        tmp_path / "stickers",
        tmp_path / "stickers/00000000-0000-0000-0000-000000000001",
        path,
    ]

    storage.delete(STICKER_UUID, "png")
    storage.delete(STICKER_UUID, "png")
    with pytest.raises(sticker_storage.StickerStorageNotFoundError):
        storage.read(STICKER_UUID, "png")


def test_local_storage_is_idempotent_for_same_content_and_rejects_overwrite(
    tmp_path,
):
    storage = sticker_storage.LocalStickerStorage(str(tmp_path))
    first = storage.save(STICKER_UUID, "gif", b"same")

    replay = storage.save(STICKER_UUID, "gif", io.BytesIO(b"same"))
    assert replay == first

    with pytest.raises(sticker_storage.StickerStorageConflictError):
        storage.save(STICKER_UUID, "gif", b"different")
    assert storage.read(STICKER_UUID, "gif") == b"same"


def test_local_storage_rejects_parent_symlink_without_touching_outside(tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage_root / "stickers").symlink_to(outside, target_is_directory=True)
    storage = sticker_storage.LocalStickerStorage(str(storage_root))

    with pytest.raises(ValueError):
        storage.save(STICKER_UUID, "png", b"not outside")

    assert list(outside.iterdir()) == []


def test_local_storage_creates_temporary_file_in_target_parent(tmp_path, monkeypatch):
    storage = sticker_storage.LocalStickerStorage(str(tmp_path))
    target_parent = tmp_path / "stickers" / str(STICKER_UUID)
    original = tempfile.NamedTemporaryFile
    calls = []

    def record_tempfile(*args, **kwargs):
        calls.append(kwargs["dir"])
        return original(*args, **kwargs)

    monkeypatch.setattr(sticker_storage.tempfile, "NamedTemporaryFile", record_tempfile)
    storage.save(STICKER_UUID, "png", b"same")

    assert calls == [target_parent]


def test_local_storage_does_not_read_or_accept_existing_target_symlink(tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    storage = sticker_storage.LocalStickerStorage(str(tmp_path))
    info = storage.save(STICKER_UUID, "gif", b"stored")
    target = tmp_path / info.storage_object_id
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(sticker_storage.StickerStorageConflictError):
        storage.save(STICKER_UUID, "gif", b"stored")
    with pytest.raises(sticker_storage.StickerStorageBackendError):
        storage.read(STICKER_UUID, "gif")
    assert outside.read_bytes() == b"outside"


def test_s3_storage_uses_guarded_put_and_translates_operations():
    client = mock.Mock()
    client.head_object.side_effect = _client_error("404")
    client.get_object.return_value = {"Body": io.BytesIO(b"s3 bytes")}
    storage = _s3_storage(client)

    info = storage.save(STICKER_UUID, "webp", b"s3 bytes")
    assert info.storage_object_id.endswith("/media.webp")
    assert storage.read(STICKER_UUID, "webp") == b"s3 bytes"
    storage.delete(STICKER_UUID, "webp")

    put_call = client.put_object.call_args.kwargs
    assert put_call["Bucket"] == "sticker-catalog"
    assert put_call["Key"] == info.storage_object_id
    assert put_call["ContentLength"] == len(b"s3 bytes")
    assert put_call["Metadata"]["sha256"]
    assert put_call["IfNoneMatch"] == "*"
    assert client.get_object.call_args.kwargs["Key"] == info.storage_object_id
    assert client.delete_object.call_args.kwargs["Key"] == info.storage_object_id


def test_s3_storage_replays_known_content_without_put_and_rejects_conflict():
    client = mock.Mock()
    digest = hashlib.sha256(b"same").hexdigest()
    client.head_object.return_value = {
        "ContentLength": 4,
        "Metadata": {"sha256": digest},
    }
    storage = _s3_storage(client)

    storage.save(STICKER_UUID, "png", b"same")
    client.put_object.assert_not_called()
    with pytest.raises(sticker_storage.StickerStorageConflictError):
        storage.save(STICKER_UUID, "png", b"other")


def test_s3_storage_translates_not_found_and_backend_errors():
    client = mock.Mock()
    client.get_object.side_effect = _client_error("NoSuchKey")
    client.delete_object.side_effect = [
        _client_error("NotFound"),
        _client_error("AccessDenied"),
    ]
    storage = _s3_storage(client)

    with pytest.raises(sticker_storage.StickerStorageNotFoundError):
        storage.read(STICKER_UUID, "gif")
    storage.delete(STICKER_UUID, "gif")
    with pytest.raises(sticker_storage.StickerStorageBackendError):
        storage.delete(STICKER_UUID, "gif")
