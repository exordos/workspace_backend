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

import enum

from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
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
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_folders"

    title = properties.property(
        types.String(min_length=1, max_length=64),
        required=True,
    )
    user_uuid = properties.property(
        types.UUID(),
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
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_folder_items"
    __custom_properties__ = {
        "folder_uuid": types.UUID(),
    }

    folder = relationships.relationship(
        Folder,
        prefetch=True,
        required=True,
    )
    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    stream_uuid = properties.property(
        types.UUID(),
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
                "user_uuid": dm_filters.EQ(self.user_uuid),
                "project_id": dm_filters.EQ(self.project_id),
            },
        )


class FolderItemRAFix(FolderItem):
    pass


class ZulipSource(types_dynamic.AbstractKindModel):
    KIND = "zulip"

    stream_id = properties.property(
        types.Integer(min_value=0, max_value=2**31 - 1),
        required=True,
    )


class NativeSource(types_dynamic.AbstractKindModel):
    KIND = "native"


class SourceName(str, enum.Enum):
    ZULIP = ZulipSource.KIND
    NATIVE = NativeSource.KIND


class WorkspaceStreamRole(str, enum.Enum):
    GUEST = "guest"
    MEMBER = "member"
    MODERATOR = "moderator"
    ADMINISTRATOR = "administrator"
    OWNER = "owner"


class WorkspaceStreamBindingStatus(str, enum.Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    ACTIVE = "active"


class WorkspaceStream(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithRequiredNameDesc,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_streams"

    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    source_name = properties.property(
        types.Enum([source.value for source in SourceName]),
        required=True,
    )
    source = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(ZulipSource),
            types_dynamic.KindModelType(NativeSource),
        ),
        required=True,
    )
    invite_only = properties.property(
        types.Boolean(),
        default=False,
    )
    announce = properties.property(
        types.Boolean(),
        default=False,
    )
    private = properties.property(
        types.Boolean(),
        default=False,
    )


class WorkspaceStreamBinding(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_stream_bindings"

    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    who_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    role = properties.property(
        types.Enum([role.value for role in WorkspaceStreamRole]),
        default=WorkspaceStreamRole.MEMBER.value,
    )
    status = properties.property(
        types.Enum([status.value for status in WorkspaceStreamBindingStatus]),
        default=WorkspaceStreamBindingStatus.NEW.value,
    )

    def get_stream(self):
        return WorkspaceStream.objects.get_one(
            filters={"uuid": ra_filters.EQ(self.stream_uuid)}
        )


class WorkspaceUserStream(
    models.ModelWithUUID,
    models.ModelWithRequiredNameDesc,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_user_streams"

    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    last_synced_at = properties.property(
        types.UTCDateTimeZ(),
        required=True,
    )
    source_name = properties.property(
        types.Enum([source.value for source in SourceName]),
        required=True,
    )
    source = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(ZulipSource),
            types_dynamic.KindModelType(NativeSource),
        ),
        required=True,
    )
    invite_only = properties.property(
        types.Boolean(),
        default=False,
    )
    announce = properties.property(
        types.Boolean(),
        default=False,
    )
    private = properties.property(
        types.Boolean(),
        default=False,
    )

    def __init__(self, init_stream=False, **kwargs):
        if init_stream:
            self._create_stream_and_bindings(kwargs=kwargs)
        super().__init__(**kwargs)
    
    def _create_stream_and_bindings(self, kwargs):
        # create stream
        stream = models.WorkspaceStream(
            **kwargs
        )
        stream.insert()

        # create binding
        binding = models.WorkspaceStreamBinding(
            project_id=stream.project_id,
            stream_uuid=stream.uuid,
            user_uuid=stream.user_uuid,
            who_uuid=stream.user_uuid,
            role=models.WorkspaceStreamRole.OWNER.value,
        )
        binding.insert()

    def get_stream(self):
        return WorkspaceStream.objects.get_one(
            filters={"uuid": dm_filters.EQ(self.uuid)}
        )

    def sync(self):
        self.last_synced_at = self.get_stream().updated_at
        self.update()


class StreamBindingToSync(
    models.ModelWithUUID,
    orm.SQLStorableMixin,
    
):
    __tablename__ = "m_stream_binding_to_sync"
    
    stream = relationships.relationship(
        WorkspaceStream,
        prefetch=True,
        required=True,
    )
    user_stream = relationships.relationship(
        WorkspaceUserStream,
        prefetch=True,
        required=False,
    )
    binding = relationships.relationship(
        WorkspaceStreamBinding,
        prefetch=True,
        required=True,
    )


class MarkdownPayload(types_dynamic.AbstractKindModel):
    KIND = "markdown"

    content = properties.property(
        types.String(max_length=10000),
        required=True,
    )


class WorkspaceMessage(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_messages"

    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    payload = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(MarkdownPayload),
        ),
        required=True,
    )
    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )


class WorkspaceUserMessage(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_user_messages"

    payload = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(MarkdownPayload),
        ),
        required=True,
    )
    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    last_synced_at = properties.property(
        types.UTCDateTimeZ(),
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

    def __init__(self, init_message=False, **kwargs):
        if init_message:
            self._create_message(kwargs=kwargs)
        super().__init__(**kwargs)
    
    def _create_message(self, kwargs):
        # create message
        message = WorkspaceMessage(
            **kwargs
        )
        message.insert()


class MessageToSync(
    models.ModelWithUUID,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_message_to_sync"

    message = relationships.relationship(
        WorkspaceMessage,
        prefetch=True,
        required=True,
    )
    user_stream = relationships.relationship(
        WorkspaceUserStream,
        prefetch=True,
        required=True,
    )
    user_message = relationships.relationship(
        WorkspaceUserMessage,
        prefetch=True,
        required=False,
    )