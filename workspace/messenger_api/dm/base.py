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

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types

from workspace.messenger_api.dm import message_payloads


FOLDER_SYSTEM_TYPE_ALL = "all"
FOLDER_SYSTEM_TYPE_CREATED = "created"
FOLDER_SYSTEM_TYPES = (FOLDER_SYSTEM_TYPE_ALL, FOLDER_SYSTEM_TYPE_CREATED)


class UserScopedModelWithUUID(models.ModelWithUUID):
    user_uuid = properties.property(
        types.UUID(),
        required=True,
        id_property=True,
    )

    @classmethod
    def get_id_property(cls):
        return {"uuid": cls.properties.properties["uuid"]}


class WorkspaceMessageFieldsBase(models.Model):
    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    topic_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    payload = properties.property(
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE,
        required=True,
    )


class WorkspaceMessageBase(
    models.ModelWithProject,
    models.ModelWithTimestamp,
    WorkspaceMessageFieldsBase,
):
    pass


class WorkspaceUserMessageBase(
    UserScopedModelWithUUID,
    WorkspaceMessageBase,
):
    author_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    read = properties.property(
        types.Boolean(),
        default=False,
    )
    pinned = properties.property(
        types.Boolean(),
        default=False,
    )
    starred = properties.property(
        types.Boolean(),
        default=False,
    )
    is_own = properties.property(
        types.Boolean(),
        default=False,
    )


class WorkspaceFolderBase(
    UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
):
    title = properties.property(
        types.String(min_length=1, max_length=64),
        required=True,
    )
    background_color_value = properties.property(
        types.AllowNone(types.Integer(min_value=0, max_value=2**32 - 1)),
        default=None,
    )
    system_type = properties.property(
        types.AllowNone(types.Enum(FOLDER_SYSTEM_TYPES)),
        default=FOLDER_SYSTEM_TYPE_CREATED,
    )


class WorkspaceUserFolderBase(
    WorkspaceFolderBase,
):
    unread_count = properties.property(
        types.Integer(min_value=0),
        default=0,
    )
    folder_items = properties.property(
        types.List(),
        default=list,
    )
