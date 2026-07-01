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

import os
import pathlib

from oslo_config import cfg

from workspace.common import file_storage_opts


CONF = cfg.CONF
ENV_STORAGE_PATH = file_storage_opts.ENV_STORAGE_PATH


def get_storage_path():
    env_storage_path = os.environ.get(ENV_STORAGE_PATH)
    if env_storage_path is not None:
        return env_storage_path
    return CONF[file_storage_opts.DOMAIN].storage_path


def get_workspace_file_path(file_uuid, storage_path=None):
    file_name = str(file_uuid)
    root = pathlib.Path(storage_path or get_storage_path())
    return root / file_name[:2] / file_name


def save_workspace_file(file_uuid, data, storage_path=None):
    path = get_workspace_file_path(
        file_uuid=file_uuid,
        storage_path=storage_path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    with open(temporary_path, "wb") as file:
        file.write(data)
    os.replace(temporary_path, path)
    return path


def read_workspace_file(file_uuid, storage_path=None):
    path = get_workspace_file_path(
        file_uuid=file_uuid,
        storage_path=storage_path,
    )
    return path.read_bytes()


def delete_workspace_file(file_uuid, storage_path=None):
    path = get_workspace_file_path(
        file_uuid=file_uuid,
        storage_path=storage_path,
    )
    try:
        path.unlink()
    except FileNotFoundError:
        pass
