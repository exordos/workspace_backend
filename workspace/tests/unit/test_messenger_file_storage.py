#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import datetime
import dataclasses
import io
import types
import uuid as sys_uuid
from unittest import mock

from workspace.common import file_storage_opts
from workspace.messenger_api import file_storage


def _metadata(file_uuid):
    return file_storage.WorkspaceFileMetadata(
        uuid=file_uuid,
        project_id=sys_uuid.uuid4(),
        stream_uuid=sys_uuid.uuid4(),
        owner_uuid=sys_uuid.uuid4(),
        name="diagram.png",
        description="Диаграмма",
        content_type="image/png",
        size_bytes=7,
        sha256="a" * 64,
        created_at=datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc),
    )


def _public_metadata(file_uuid):
    return file_storage.WorkspaceFileMetadata(
        uuid=file_uuid,
        project_id=sys_uuid.uuid4(),
        stream_uuid=None,
        owner_uuid=sys_uuid.uuid4(),
        name="avatar.png",
        description="Workspace user avatar",
        content_type="image/png",
        size_bytes=8,
        sha256="b" * 64,
        created_at=datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc),
        acl_mode="public",
    )


def _patch_conf(monkeypatch, tmp_path, default_type="file"):
    conf = {
        file_storage_opts.DOMAIN: types.SimpleNamespace(
            default_type=default_type,
            storage_path=str(tmp_path),
        ),
        file_storage_opts.S3_DOMAIN: types.SimpleNamespace(
            endpoint_url="http://s3.local",
            bucket_name="workspace-files",
            access_key_id="access",
            secret_access_key="secret",
            region_name="us-east-1",
        ),
    }
    monkeypatch.setattr(file_storage, "CONF", conf)


def test_local_storage_saves_reads_and_deletes_file(tmp_path, monkeypatch):
    _patch_conf(monkeypatch, tmp_path)
    file_uuid = sys_uuid.uuid4()
    data = b"local data"

    storage_info = file_storage.save_workspace_file(
        file_uuid=file_uuid,
        data=data,
    )

    assert storage_info.storage_type == file_storage_opts.STORAGE_TYPE_FILE
    assert storage_info.storage_id == ""
    assert storage_info.storage_object_id == (
        file_storage.get_workspace_file_object_id(file_uuid)
    )
    assert (
        file_storage.read_workspace_file(
            file_uuid=file_uuid,
            storage_type=storage_info.storage_type,
            storage_object_id=storage_info.storage_object_id,
        )
        == data
    )

    path = file_storage.get_workspace_file_path(file_uuid=file_uuid)
    assert path.read_bytes() == data

    file_storage.delete_workspace_file(
        file_uuid=file_uuid,
        storage_type=storage_info.storage_type,
        storage_object_id=storage_info.storage_object_id,
    )
    assert not path.exists()


def test_local_storage_keeps_file_metadata_and_acl(tmp_path, monkeypatch):
    _patch_conf(monkeypatch, tmp_path)
    file_uuid = sys_uuid.uuid4()
    metadata = _metadata(file_uuid)

    file_storage.save_workspace_file_metadata(metadata)

    assert file_storage.read_workspace_file_metadata(file_uuid) == metadata
    metadata_path = tmp_path / file_storage.get_workspace_file_metadata_object_id(
        file_uuid,
    )
    assert b'"mode":"stream_members"' in metadata_path.read_bytes()

    file_storage.delete_workspace_file_metadata(file_uuid)
    assert not metadata_path.exists()


def test_local_storage_lists_stable_object_ids_without_temporary_files(
    tmp_path,
    monkeypatch,
):
    _patch_conf(monkeypatch, tmp_path)
    storage = file_storage.LocalWorkspaceFileStorage()
    first_uuid = sys_uuid.uuid4()
    second_uuid = sys_uuid.uuid4()
    first = storage.save(first_uuid, b"first").storage_object_id
    second = storage.save(second_uuid, b"second").storage_object_id
    temporary = tmp_path / f"{first}.tmp"
    temporary.write_bytes(b"partial")

    assert storage.list_object_ids() == sorted((first, second))


def test_local_storage_reads_external_provider_sidecar_v2(tmp_path, monkeypatch):
    _patch_conf(monkeypatch, tmp_path)
    file_uuid = sys_uuid.uuid4()
    metadata = dataclasses.replace(
        _metadata(file_uuid),
        origin={
            "kind": "external_provider",
            "provider_kind": "zulip",
            "external_account_uuid": str(sys_uuid.uuid4()),
            "external_chat_uuid": str(sys_uuid.uuid4()),
            "operation_uuid": str(sys_uuid.uuid4()),
        },
    )

    file_storage.save_workspace_file_metadata(metadata)

    assert file_storage.read_workspace_file_metadata(file_uuid) == metadata
    payload = (
        tmp_path / file_storage.get_workspace_file_metadata_object_id(file_uuid)
    ).read_bytes()
    assert b'"schema_version":2' in payload
    assert b'"kind":"external_provider"' in payload


def test_public_file_metadata_has_no_stream_uuid(tmp_path, monkeypatch):
    _patch_conf(monkeypatch, tmp_path)
    file_uuid = sys_uuid.uuid4()
    metadata = _public_metadata(file_uuid)

    file_storage.save_workspace_file_metadata(metadata)

    assert file_storage.read_workspace_file_metadata(file_uuid) == metadata
    metadata_path = tmp_path / file_storage.get_workspace_file_metadata_object_id(
        file_uuid,
    )
    payload = metadata_path.read_bytes()
    assert b'"mode":"public"' in payload
    assert b'"owner_uuid"' in payload
    assert b'"stream_uuid"' not in payload


def test_default_storage_type_selects_s3(tmp_path, monkeypatch):
    _patch_conf(monkeypatch, tmp_path, default_type="s3")
    file_uuid = sys_uuid.uuid4()
    storage = mock.Mock()
    storage.save.return_value = file_storage.WorkspaceFileStorageInfo(
        storage_type="s3",
        storage_id="workspace-files",
        storage_object_id="key",
    )

    with mock.patch.object(
        file_storage,
        "S3WorkspaceFileStorage",
        return_value=storage,
    ):
        storage_info = file_storage.save_workspace_file(
            file_uuid=file_uuid,
            data=b"s3 data",
        )

    storage.save.assert_called_once_with(
        file_uuid=file_uuid,
        data=b"s3 data",
        storage_object_id=None,
    )
    assert storage_info.storage_type == "s3"


def test_s3_storage_uses_configured_bucket_and_key(tmp_path, monkeypatch):
    _patch_conf(monkeypatch, tmp_path)
    file_uuid = sys_uuid.uuid4()
    client = mock.Mock()
    client.get_object.return_value = {"Body": io.BytesIO(b"s3 data")}

    storage = file_storage.S3WorkspaceFileStorage()
    storage._client = client

    storage_info = storage.save(file_uuid=file_uuid, data=b"s3 data")
    data = storage.read(
        file_uuid=file_uuid,
        storage_object_id=storage_info.storage_object_id,
    )
    storage.delete(
        file_uuid=file_uuid,
        storage_object_id=storage_info.storage_object_id,
    )

    assert storage_info.storage_type == "s3"
    assert storage_info.storage_id == "workspace-files"
    assert data == b"s3 data"
    client.put_object.assert_called_once_with(
        Body=b"s3 data",
        Bucket="workspace-files",
        Key=storage_info.storage_object_id,
    )
    client.get_object.assert_called_once_with(
        Bucket="workspace-files",
        Key=storage_info.storage_object_id,
    )
    client.delete_object.assert_called_once_with(
        Bucket="workspace-files",
        Key=storage_info.storage_object_id,
    )


def test_s3_storage_keeps_file_metadata_and_acl(tmp_path, monkeypatch):
    _patch_conf(monkeypatch, tmp_path)
    file_uuid = sys_uuid.uuid4()
    metadata = _metadata(file_uuid)
    client = mock.Mock()
    client.get_object.return_value = {"Body": io.BytesIO(metadata.to_json())}
    storage = file_storage.S3WorkspaceFileStorage()
    storage._client = client

    storage.save_metadata(file_uuid, metadata)
    assert storage.read_metadata(file_uuid) == metadata
    storage.delete_metadata(file_uuid)

    object_id = file_storage.get_workspace_file_metadata_object_id(file_uuid)
    client.put_object.assert_called_once_with(
        Body=metadata.to_json(),
        Bucket="workspace-files",
        ContentType="application/json",
        Key=object_id,
    )
    client.get_object.assert_called_once_with(
        Bucket="workspace-files",
        Key=object_id,
    )
    client.delete_object.assert_called_once_with(
        Bucket="workspace-files",
        Key=object_id,
    )


def test_s3_storage_lists_every_paginated_object(tmp_path, monkeypatch):
    _patch_conf(monkeypatch, tmp_path)
    client = mock.Mock()
    client.list_objects_v2.side_effect = [
        {
            "Contents": [{"Key": "z"}],
            "IsTruncated": True,
            "NextContinuationToken": "next",
        },
        {"Contents": [{"Key": "a"}], "IsTruncated": False},
    ]
    storage = file_storage.S3WorkspaceFileStorage()
    storage._client = client

    assert storage.list_object_ids() == ["a", "z"]
    assert client.list_objects_v2.call_args_list == [
        mock.call(Bucket="workspace-files"),
        mock.call(Bucket="workspace-files", ContinuationToken="next"),
    ]
