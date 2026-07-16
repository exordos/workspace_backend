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
import datetime
import json
import os
import pathlib
import uuid as sys_uuid

from oslo_config import cfg

from workspace.common import file_storage_opts


CONF = cfg.CONF
ENV_STORAGE_PATH = file_storage_opts.ENV_STORAGE_PATH


@dataclasses.dataclass(frozen=True)
class WorkspaceFileStorageInfo:
    storage_type: str
    storage_id: str
    storage_object_id: str


@dataclasses.dataclass(frozen=True)
class WorkspaceFileMetadata:
    uuid: sys_uuid.UUID
    project_id: sys_uuid.UUID
    stream_uuid: sys_uuid.UUID | None
    owner_uuid: sys_uuid.UUID
    name: str
    description: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime.datetime
    acl_mode: str = "stream_members"

    def to_json(self):
        created_at = self.created_at
        if created_at.tzinfo is None:
            raise ValueError("File metadata timestamp must be timezone-aware")
        if self.acl_mode == "stream_members":
            if self.stream_uuid is None:
                raise ValueError("Stream file metadata requires a stream UUID")
            acl = {
                "mode": self.acl_mode,
                "stream_uuid": str(self.stream_uuid),
            }
        elif self.acl_mode == "public":
            if self.stream_uuid is not None:
                raise ValueError("Public file metadata must not have a stream UUID")
            acl = {"mode": self.acl_mode}
        else:
            raise ValueError("Unsupported file metadata ACL mode")
        payload = {
            "acl": acl,
            "content_type": self.content_type,
            "created_at": created_at.astimezone(
                datetime.timezone.utc,
            ).isoformat(),
            "description": self.description,
            "name": self.name,
            "owner_uuid": str(self.owner_uuid),
            "project_id": str(self.project_id),
            "schema_version": 1,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "uuid": str(self.uuid),
        }
        if self.stream_uuid is not None:
            payload["stream_uuid"] = str(self.stream_uuid)
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, value):
        data = json.loads(value.decode("utf-8"))
        if data["schema_version"] != 1:
            raise ValueError("Unsupported file metadata schema version")
        acl_mode = data["acl"]["mode"]
        if acl_mode not in ("stream_members", "public"):
            raise ValueError("Unsupported file metadata ACL mode")
        stream_uuid = data.get("stream_uuid")
        if acl_mode == "stream_members" and (
            stream_uuid is None or data["acl"].get("stream_uuid") != stream_uuid
        ):
            raise ValueError("File metadata ACL stream does not match file stream")
        if acl_mode == "public" and (
            stream_uuid is not None or "stream_uuid" in data["acl"]
        ):
            raise ValueError("Public file metadata must not have a stream UUID")
        return cls(
            uuid=sys_uuid.UUID(data["uuid"]),
            project_id=sys_uuid.UUID(data["project_id"]),
            stream_uuid=(None if stream_uuid is None else sys_uuid.UUID(stream_uuid)),
            owner_uuid=sys_uuid.UUID(data["owner_uuid"]),
            name=data["name"],
            description=data["description"],
            content_type=data["content_type"],
            size_bytes=data["size_bytes"],
            sha256=data["sha256"],
            created_at=datetime.datetime.fromisoformat(data["created_at"]),
            acl_mode=acl_mode,
        )


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


def get_workspace_file_metadata_object_id(file_uuid):
    file_name = str(file_uuid)
    return f"metadata/{file_name[:2]}/{file_name}.json"


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

    def save_metadata(self, file_uuid, metadata):
        path = _get_local_file_path(get_workspace_file_metadata_object_id(file_uuid))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f"{path.name}.tmp")
        temporary_path.write_bytes(metadata.to_json())
        os.replace(temporary_path, path)

    def read_metadata(self, file_uuid):
        path = _get_local_file_path(get_workspace_file_metadata_object_id(file_uuid))
        return WorkspaceFileMetadata.from_json(path.read_bytes())

    def delete_metadata(self, file_uuid):
        path = _get_local_file_path(get_workspace_file_metadata_object_id(file_uuid))
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

    def save_metadata(self, file_uuid, metadata):
        self.client.put_object(
            Body=metadata.to_json(),
            Bucket=self.bucket_name,
            ContentType="application/json",
            Key=get_workspace_file_metadata_object_id(file_uuid),
        )

    def read_metadata(self, file_uuid):
        response = self.client.get_object(
            Bucket=self.bucket_name,
            Key=get_workspace_file_metadata_object_id(file_uuid),
        )
        return WorkspaceFileMetadata.from_json(response["Body"].read())

    def delete_metadata(self, file_uuid):
        self.client.delete_object(
            Bucket=self.bucket_name,
            Key=get_workspace_file_metadata_object_id(file_uuid),
        )


def get_workspace_file_storage(storage_type=None):
    storage_type = storage_type or get_default_storage_type()
    if storage_type == file_storage_opts.STORAGE_TYPE_FILE:
        return LocalWorkspaceFileStorage()
    if storage_type == file_storage_opts.STORAGE_TYPE_S3:
        return S3WorkspaceFileStorage()
    raise ValueError(f"Unsupported workspace file storage type: {storage_type}")


def get_workspace_file_storage_info(
    file_uuid, storage_type=None, storage_object_id=None
):
    storage = get_workspace_file_storage(storage_type=storage_type)
    return WorkspaceFileStorageInfo(
        storage_type=storage.storage_type,
        storage_id=storage.storage_id,
        storage_object_id=(
            storage_object_id or get_workspace_file_object_id(file_uuid)
        ),
    )


def save_workspace_file(file_uuid, data, storage_type=None, storage_object_id=None):
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


def save_workspace_file_metadata(metadata, storage_type=None):
    storage = get_workspace_file_storage(storage_type=storage_type)
    storage.save_metadata(metadata.uuid, metadata)


def read_workspace_file_metadata(file_uuid, storage_type=None):
    storage = get_workspace_file_storage(storage_type=storage_type)
    return storage.read_metadata(file_uuid)


def delete_workspace_file_metadata(file_uuid, storage_type=None):
    storage = get_workspace_file_storage(storage_type=storage_type)
    storage.delete_metadata(file_uuid)
