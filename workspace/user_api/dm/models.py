#    Copyright 2025 Genesis Corporation.
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

import enum

from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class ChatType(str, enum.Enum):
    STREAM = "stream"
    GROUP = "group"
    PRIVATE = "private"


class SystemFolderType(str, enum.Enum):
    ALL = "all"
    CREATED = "created"


class Folder(
    models.DumpToSimpleViewMixin,
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "folders"

    title = properties.property(
        types.String(min_length=1, max_length=64),
        required=True,
    )
    user_id = properties.property(
        types.Integer(min_value=0, max_value=2**31 - 1),
        required=True,
    )
    background_color_value = properties.property(
        types.AllowNone(types.Integer(min_value=0, max_value=2**32 - 1)),
        default=None,
    )
    unread_messages = properties.property(
        types.TypedList(types.Integer(min_value=0, max_value=2**31 - 1)),
        default=list,
    )
    system_type = properties.property(
        types.AllowNone(
            types.Enum([folder_type.value for folder_type in SystemFolderType])
        ),
        default=SystemFolderType.CREATED.value,
    )


class FolderItem(
    models.DumpToSimpleViewMixin,
    models.CustomPropertiesMixin,
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "folder_items"
    __custom_properties__ = {
        "folder_uuid": types.UUID(),
    }

    folder = relationships.relationship(
        Folder,
        prefetch=True,
        required=True,
    )
    user_id = properties.property(
        types.Integer(min_value=0, max_value=2**31 - 1),
        required=True,
    )
    chat_id = properties.property(
        types.Integer(min_value=0, max_value=2**31 - 1),
        required=True,
    )
    order_index = properties.property(
        types.AllowNone(types.Integer(max_value=2**31 - 1)),
        default=None,
    )
    pinned_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=None,
    )
    chat_type = properties.property(
        types.Enum([t.value for t in ChatType]),
        required=True,
    )

    @property
    def folder_uuid(self):
        return self.folder.uuid

    @folder_uuid.setter
    def folder_uuid(self, value):
        if value is None:
            raise ValueError("folder_uuid must not be None")

        folder_id = types.UUID().from_simple_type(value)
        self.folder = Folder.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(folder_id),
                "user_id": dm_filters.EQ(self.user_id),
            },
        )


class FolderItemRAFix(FolderItem):
    pass


class Service(
    models.ModelWithUUID,
    models.ModelWithRequiredNameDesc,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "catalog_services"

    service_url = properties.property(
        types.Url(),
        required=True,
    )
    icon = properties.property(
        types.AllowNone(types.Url()),
        default=None,
    )
