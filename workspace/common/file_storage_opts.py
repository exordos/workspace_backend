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

from oslo_config import cfg


DOMAIN = "messenger_files"
S3_DOMAIN = "messenger_files_s3"
ENV_STORAGE_PATH = "WORKSPACE_FILE_STORAGE_PATH"
DEFAULT_STORAGE_PATH = "/var/lib/workspace/messenger/files"
STORAGE_TYPE_FILE = "file"
STORAGE_TYPE_S3 = "s3"
STORAGE_TYPES = (STORAGE_TYPE_FILE, STORAGE_TYPE_S3)

file_storage_opts = [
    cfg.StrOpt(
        "default-type",
        default=STORAGE_TYPE_FILE,
        choices=STORAGE_TYPES,
        help="Default messenger file storage type",
    ),
    cfg.StrOpt(
        "storage-path",
        default=DEFAULT_STORAGE_PATH,
        help="Path to local messenger file storage",
    ),
]

s3_storage_opts = [
    cfg.StrOpt("endpoint-url", help="S3 endpoint URL"),
    cfg.StrOpt("bucket-name", help="Name of the S3 bucket"),
    cfg.StrOpt("access-key-id", help="AWS access key ID"),
    cfg.StrOpt("secret-access-key", help="AWS secret access key"),
    cfg.StrOpt("region-name", help="AWS region name"),
]


def register_opts(conf=cfg.CONF):
    conf.register_opts(file_storage_opts, DOMAIN)
    conf.register_opts(s3_storage_opts, S3_DOMAIN)
