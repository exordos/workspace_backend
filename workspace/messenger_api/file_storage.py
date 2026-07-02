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

import dataclasses
import os
import pathlib

from oslo_config import cfg

from workspace.common import file_storage_opts


CONF = cfg.CONF
ENV_STORAGE_PATH = file_storage_opts.ENV_STORAGE_PATH


@dataclasses.dataclass(frozen=True)
class WorkspaceFileStorageInfo:
    storage_type: str
    storage_id: str
    storage_object_id: str


def get_default_storage_type():
    return CONF[file_storage_opts.DOMAIN].default_type


def get_storage_path():
    env_storage_path = os.environ.get(ENV_STORAGE_PATH)
    if env_storage_path is not None:
        return env_storage_path
    return CONF[file_storage_opts.DOMAIN].storage_path


def get_workspace_file_object_id(file_uuid):
    file_name = str(file_uuid)
    return f"{file_name[:2]}/{file_name}"


def _get_local_file_path(storage_object_id, storage_path=None):
    root = pathlib.Path(storage_path or get_storage_path()).resolve()
    path = (root / storage_object_id).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"Invalid storage object id: {storage_object_id}")
    return path


def get_workspace_file_path(file_uuid, storage_path=None, storage_object_id=None):
    return _get_local_file_path(
        storage_object_id or get_workspace_file_object_id(file_uuid),
        storage_path=storage_path,
    )


class LocalWorkspaceFileStorage:
    storage_type = file_storage_opts.STORAGE_TYPE_FILE
    storage_id = ""

    def save(self, file_uuid, data, storage_object_id=None):
        object_id = storage_object_id or get_workspace_file_object_id(file_uuid)
        path = get_workspace_file_path(
            file_uuid=file_uuid,
            storage_object_id=object_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f"{path.name}.tmp")
        with open(temporary_path, "wb") as file:
            file.write(data)
        os.replace(temporary_path, path)
        return WorkspaceFileStorageInfo(
            storage_type=self.storage_type,
            storage_id=self.storage_id,
            storage_object_id=object_id,
        )

    def read(self, file_uuid, storage_object_id=None):
        path = get_workspace_file_path(
            file_uuid=file_uuid,
            storage_object_id=storage_object_id,
        )
        return path.read_bytes()

    def delete(self, file_uuid, storage_object_id=None):
        path = get_workspace_file_path(
            file_uuid=file_uuid,
            storage_object_id=storage_object_id,
        )
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class S3WorkspaceFileStorage:
    storage_type = file_storage_opts.STORAGE_TYPE_S3

    def __init__(self):
        self._conf = CONF[file_storage_opts.S3_DOMAIN]
        self.bucket_name = self._conf.bucket_name
        self._client = None
        if not self.bucket_name:
            raise ValueError("S3 bucket_name is required")

    @property
    def storage_id(self):
        return self.bucket_name

    @property
    def client(self):
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def _create_client(self):
        import boto3

        kwargs = {}
        if self._conf.access_key_id is not None:
            kwargs["aws_access_key_id"] = self._conf.access_key_id
        if self._conf.secret_access_key is not None:
            kwargs["aws_secret_access_key"] = self._conf.secret_access_key
        if self._conf.endpoint_url is not None:
            kwargs["endpoint_url"] = self._conf.endpoint_url
        if self._conf.region_name is not None:
            kwargs["region_name"] = self._conf.region_name
        return boto3.client("s3", **kwargs)

    def save(self, file_uuid, data, storage_object_id=None):
        object_id = storage_object_id or get_workspace_file_object_id(file_uuid)
        self.client.put_object(
            Body=data,
            Bucket=self.bucket_name,
            Key=object_id,
        )
        return WorkspaceFileStorageInfo(
            storage_type=self.storage_type,
            storage_id=self.storage_id,
            storage_object_id=object_id,
        )

    def read(self, file_uuid, storage_object_id=None):
        object_id = storage_object_id or get_workspace_file_object_id(file_uuid)
        response = self.client.get_object(
            Bucket=self.bucket_name,
            Key=object_id,
        )
        return response["Body"].read()

    def delete(self, file_uuid, storage_object_id=None):
        object_id = storage_object_id or get_workspace_file_object_id(file_uuid)
        self.client.delete_object(
            Bucket=self.bucket_name,
            Key=object_id,
        )


def get_workspace_file_storage(storage_type=None):
    storage_type = storage_type or get_default_storage_type()
    if storage_type == file_storage_opts.STORAGE_TYPE_FILE:
        return LocalWorkspaceFileStorage()
    if storage_type == file_storage_opts.STORAGE_TYPE_S3:
        return S3WorkspaceFileStorage()
    raise ValueError(f"Unsupported workspace file storage type: {storage_type}")


def get_workspace_file_storage_info(file_uuid, storage_type=None,
                                    storage_object_id=None):
    storage = get_workspace_file_storage(storage_type=storage_type)
    return WorkspaceFileStorageInfo(
        storage_type=storage.storage_type,
        storage_id=storage.storage_id,
        storage_object_id=(
            storage_object_id or get_workspace_file_object_id(file_uuid)
        ),
    )


def save_workspace_file(file_uuid, data, storage_type=None,
                        storage_object_id=None):
    storage = get_workspace_file_storage(storage_type=storage_type)
    return storage.save(
        file_uuid=file_uuid,
        data=data,
        storage_object_id=storage_object_id,
    )


def read_workspace_file(file_uuid, storage_type=None, storage_object_id=None):
    storage = get_workspace_file_storage(storage_type=storage_type)
    return storage.read(
        file_uuid=file_uuid,
        storage_object_id=storage_object_id,
    )


def delete_workspace_file(file_uuid, storage_type=None, storage_object_id=None):
    storage = get_workspace_file_storage(storage_type=storage_type)
    storage.delete(
        file_uuid=file_uuid,
        storage_object_id=storage_object_id,
    )
