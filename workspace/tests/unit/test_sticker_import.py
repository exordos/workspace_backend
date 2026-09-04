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

import hashlib
import io
import json
import pathlib
import stat
import tempfile
import uuid as sys_uuid
import zipfile
from unittest import mock

import pytest

from workspace.messenger_api import sticker_import


CLIENT_ID = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")


def _item(data=b"not actually an image", format="png", **overrides):
    item = {
        "client_id": str(CLIENT_ID),
        "file": f"media/{CLIENT_ID}.{format}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "format": format,
        "title": "Example",
        "alt_text": "Example sticker",
        "emoji": [],
        "tags": ["example"],
    }
    item.update(overrides)
    return item


def _archive(items=None, files=None, manifest=None, compression=zipfile.ZIP_DEFLATED):
    items = [_item()] if items is None else items
    files = (
        {item["file"]: b"not actually an image" for item in items}
        if files is None
        else files
    )
    manifest = {"schema_version": 1, "items": items} if manifest is None else manifest
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("manifest.json", json.dumps(manifest).encode("utf-8"))
        for name, value in files.items():
            if isinstance(value, zipfile.ZipInfo):
                archive.writestr(value, b"link")
            else:
                archive.writestr(name, value)
    return output.getvalue()


def test_valid_archive_extracts_arbitrary_declared_image_bytes_and_cleans_up():
    data = b"this is not a decoded PNG"
    item = _item(data=data)

    with sticker_import.validate_archive(
        _archive([item], {item["file"]: data})
    ) as result:
        assert result.manifest.schema_version == 1
        assert result.items[0].manifest.file == item["file"]
        assert result.items[0].path.read_bytes() == data
        temporary_directory = result.temporary_directory
        assert temporary_directory.exists()

    assert not temporary_directory.exists()


@pytest.mark.parametrize(
    "manifest",
    [
        {"schema_version": 1, "items": [], "unknown": True},
        {"schema_version": 2, "items": []},
        {"schema_version": 1, "items": [{}]},
        {"schema_version": 1, "items": [{"client_id": str(CLIENT_ID)}]},
    ],
)
def test_manifest_shape_and_unknown_fields_are_rejected(manifest):
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(_archive(manifest=manifest)):
            pass


@pytest.mark.parametrize(
    "payload",
    [b"{", b"\xff\xfe", json.dumps([]).encode("utf-8")],
)
def test_malformed_manifest_is_rejected(payload):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", payload)

    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(output.getvalue()):
            pass


@pytest.mark.parametrize(
    "name",
    [
        "/media/file.png",
        "C:/media/file.png",
        "../file.png",
        "media\\file.png",
        "media//file.png",
    ],
)
def test_unsafe_archive_paths_are_rejected(name):
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(_archive(files={name: b"x"})):
            pass


def test_missing_extra_and_duplicate_media_paths_are_rejected():
    item = _item()
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(_archive([item], files={})):
            pass
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(
            _archive([item], files={item["file"]: b"x", "media/extra.png": b"x"}),
        ):
            pass

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "manifest.json", json.dumps({"schema_version": 1, "items": []})
        )
        archive.writestr("media/extra.png", b"one")
        archive.writestr("media/extra.png", b"two")
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(output.getvalue()):
            pass


def test_symlink_nonregular_encrypted_and_nested_archive_entries_are_rejected():
    symlink = zipfile.ZipInfo("media/link.png")
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(
            _archive(files={"media/link.png": symlink})
        ):
            pass

    encrypted = zipfile.ZipInfo("media/encrypted.png")
    encrypted.flag_bits |= 0x1
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(
            _archive(files={"media/encrypted.png": encrypted})
        ):
            pass

    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(
            _archive(files={"media/nested.zip": b"zip"})
        ):
            pass


def test_limits_are_checked_before_extraction(monkeypatch):
    item = _item(data=b"x" * 20)
    monkeypatch.setattr(sticker_import, "MAX_MEDIA_BYTES", 10)
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(
            _archive([item], {item["file"]: b"x" * 20})
        ):
            pass

    item = _item(data=b"x" * 60)
    monkeypatch.setattr(sticker_import, "MAX_MEDIA_BYTES", 100)
    monkeypatch.setattr(sticker_import, "MAX_UNPACKED_BYTES", 100)
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(
            _archive([item], {item["file"]: b"x" * 60})
        ):
            pass


def test_compressed_transport_and_ratio_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(sticker_import, "MAX_COMPRESSED_BYTES", 10)
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(b"01234567890"):
            pass

    data = b"x" * 100_000
    item = _item(data=data)
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(_archive([item], {item["file"]: data})):
            pass


def test_checksum_and_extension_agreement_are_checked_without_content_validation():
    item = _item(data=b"arbitrary bytes", format="png")
    with sticker_import.validate_archive(
        _archive([item], {item["file"]: b"arbitrary bytes"}),
    ):
        pass

    bad_checksum = dict(item, sha256="0" * 64)
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(
            _archive([bad_checksum], {item["file"]: b"arbitrary bytes"}),
        ):
            pass

    bad_extension = dict(item, file=f"media/{CLIENT_ID}.gif")
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(
            _archive([bad_extension], {bad_extension["file"]: b"arbitrary bytes"}),
        ):
            pass


def test_item_count_and_crc_errors_are_rejected():
    items = []
    files = {}
    for index in range(51):
        client_id = sys_uuid.UUID(int=index + 2)
        data = str(index).encode()
        item = _item(data=data)
        item["client_id"] = str(client_id)
        item["file"] = f"media/{client_id}.png"
        items.append(item)
        files[item["file"]] = data
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(_archive(items, files)):
            pass

    item = _item()
    original_open = sticker_import.zipfile.ZipFile.open

    def broken_open(archive, info, *args, **kwargs):
        if info.filename.startswith("media/"):
            raise zipfile.BadZipFile("bad CRC")
        return original_open(archive, info, *args, **kwargs)

    payload = _archive([item])
    with mock.patch.object(sticker_import.zipfile.ZipFile, "open", broken_open):
        with pytest.raises(sticker_import.StickerImportValidationError):
            with sticker_import.validate_archive(payload):
                pass


def test_temporary_directory_is_cleaned_after_validation_error(tmp_path, monkeypatch):
    original = tempfile.TemporaryDirectory
    created = []

    def recording_directory(*args, **kwargs):
        kwargs["dir"] = tmp_path
        directory = original(*args, **kwargs)
        created.append(directory)
        return directory

    monkeypatch.setattr(
        sticker_import.tempfile, "TemporaryDirectory", recording_directory
    )
    item = _item(sha256="0" * 64)
    with pytest.raises(sticker_import.StickerImportValidationError):
        with sticker_import.validate_archive(_archive([item])):
            pass

    assert created
    assert not pathlib.Path(created[0].name).exists()
