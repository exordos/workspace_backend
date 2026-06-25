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

from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.storage.sql import orm

from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import event_payloads


class ChatType(str, enum.Enum):
    STREAM = "stream"
    GROUP = "group"
    PRIVATE = "private"


class SystemFolderType(str, enum.Enum):
    ALL = "all"
    CREATED = "created"


class ReactionStatus(str, enum.Enum):
    NEW = "new"
    ACTIVE = "active"
    DELETED = "deleted"


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
    system_type = properties.property(
        types.AllowNone(
            types.Enum([folder_type.value for folder_type in SystemFolderType])
        ),
        default=SystemFolderType.CREATED.value,
    )


class UserFolder(
    models.DumpToSimpleViewMixin,
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_folders_view"

    title = properties.property(
        types.String(min_length=1, max_length=64),
        required=True,
    )
    background_color_value = properties.property(
        types.AllowNone(types.Integer(min_value=0, max_value=2**32 - 1)),
        default=None,
    )
    unread_count = properties.property(
        types.Integer(min_value=0),
        default=0,
    )
    system_type = properties.property(
        types.AllowNone(
            types.Enum([folder_type.value for folder_type in SystemFolderType])
        ),
        default=SystemFolderType.CREATED.value,
    )
    folder_items = properties.property(
        types.List(),
        default=list,
    )


class FolderItem(
    models.DumpToSimpleViewMixin,
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_folder_items"

    folder_uuid = properties.property(
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


class UserFolderItem(
    models.DumpToSimpleViewMixin,
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_folder_items_created_view"

    folder_uuid = properties.property(
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
    unread_count = properties.property(
        types.Integer(min_value=0),
        default=0,
    )


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


class WorkspaceUserStatus(str, enum.Enum):
    ACTIVE = "active"
    IDLE = "idle"
    OFFLINE = "offline"
    DO_NOT_DISTURB = "do_not_disturb"


class WorkspaceUserSource(str, enum.Enum):
    IAM = "iam"


class WorkspaceUser(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_users"

    username = properties.property(
        types.String(min_length=1, max_length=128),
        required=True,
    )
    source = properties.property(
        types.Enum([source.value for source in WorkspaceUserSource]),
        default=WorkspaceUserSource.IAM.value,
    )
    status = properties.property(
        types.Enum([status.value for status in WorkspaceUserStatus]),
        default=WorkspaceUserStatus.ACTIVE.value,
    )
    first_name = properties.property(
        types.AllowNone(types.String(max_length=128)),
        default=None,
    )
    last_name = properties.property(
        types.AllowNone(types.String(max_length=128)),
        default=None,
    )
    email = properties.property(
        types.AllowNone(types.String(max_length=256)),
        default=None,
    )
    last_ping_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=None,
    )


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

    def get_recipients(self, session=None):
        return get_stream_recipients(
            project_id=self.project_id,
            stream_uuid=self.uuid,
            session=session,
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

    def get_stream(self):
        return WorkspaceStream.objects.get_one(
            filters={"uuid": dm_filters.EQ(self.stream_uuid)}
        )


def get_stream_recipients(project_id, stream_uuid, session=None):
    bindings = WorkspaceStreamBinding.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "stream_uuid": dm_filters.EQ(stream_uuid),
        },
        order_by={"user_uuid": "asc"},
        session=session,
    )
    return [binding.user_uuid for binding in bindings]


class WorkspaceUserStream(
    base.UserScopedModelWithUUID,
    models.ModelWithRequiredNameDesc,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_user_streams"

    owner = properties.property(
        types.UUID(),
        required=True,
    )
    role = properties.property(
        types.Enum([role.value for role in WorkspaceStreamRole]),
        required=True,
    )
    unread_count = properties.property(
        types.Integer(min_value=0),
        default=0,
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

    def get_default_topic(self):
        return WorkspaceStreamTopic.objects.get_one(
            filters={
                "default_for_stream_uuid": dm_filters.EQ(self.uuid),
            }
        )

    def get_recipients(self, session=None):
        return get_stream_recipients(
            project_id=self.project_id,
            stream_uuid=self.uuid,
            session=session,
        )


class WorkspaceMessageReactions(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_message_reactions"

    message_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    emoji_name = properties.property(
        types.String(max_length=128),
        required=True,
    )
    status = properties.property(
        types.Enum([s.value for s in ReactionStatus]),
        required=True,
    )


class WorkspaceEvent(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_events"

    epoch_version = properties.property(
        types.Integer(min_value=0),
        required=False,
    )
    payload = properties.property(
        event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE,
        required=True,
    )

    @classmethod
    def get_id_property(cls):
        return {"epoch_version": cls.properties.properties["epoch_version"]}

    def _get_prepared_data(self, properties=None):
        data = super()._get_prepared_data(properties=properties)
        if "epoch_version" in data and data["epoch_version"] is None:
            data.pop("epoch_version")
        return data


class WorkspaceStreamTopic(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    models.CustomPropertiesMixin,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_stream_topics"
    __custom_properties__ = {
        "is_default": types.Boolean(),
    }

    name = properties.property(
        types.String(max_length=128),
        required=True,
    )
    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    default_for_stream_uuid = properties.property(
        types.AllowNone(types.UUID()),
        required=False,
    )

    @property
    def is_default(self):
        return self.default_for_stream_uuid is not None

    def get_recipients(self, session=None):
        return get_stream_recipients(
            project_id=self.project_id,
            stream_uuid=self.stream_uuid,
            session=session,
        )


class WorkspaceUserTopic(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_user_topics_view"

    name = properties.property(
        types.String(max_length=128),
        required=True,
    )
    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    unread_count = properties.property(
        types.Integer(min_value=0),
        default=0,
    )
    is_default = properties.property(
        types.Boolean(),
        default=False,
    )
    is_done = properties.property(
        types.Boolean(),
        default=False,
    )

    def get_flags(self):
        return WorkspaceUserTopicFlags.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(self.uuid),
                "user_uuid": dm_filters.EQ(self.user_uuid),
                "project_id": dm_filters.EQ(self.project_id),
            }
        )

    def get_recipients(self, session=None):
        return get_stream_recipients(
            project_id=self.project_id,
            stream_uuid=self.stream_uuid,
            session=session,
        )


class WorkspaceMessage(
    models.ModelWithUUID,
    base.WorkspaceMessageBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_messages"

    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )

    def validate(self):
        super().validate()
        binding = WorkspaceStreamBinding.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(self.project_id),
                "stream_uuid": dm_filters.EQ(self.stream_uuid),
                "user_uuid": dm_filters.EQ(self.user_uuid),
            },
        )
        if binding is None:
            raise ra_exc.ValidationErrorException()
        topic = WorkspaceStreamTopic.objects.get_one_or_none(
            filters={
                "uuid": dm_filters.EQ(self.topic_uuid),
                "project_id": dm_filters.EQ(self.project_id),
                "stream_uuid": dm_filters.EQ(self.stream_uuid),
            },
        )
        if topic is None:
            raise ra_exc.ValidationErrorException()

    def get_recipients(self, session=None):
        return get_stream_recipients(
            project_id=self.project_id,
            stream_uuid=self.stream_uuid,
            session=session,
        )


class WorkspaceUserMessage(
    base.WorkspaceUserMessageBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_user_messages_view"

    def get_recipients(self, session=None):
        return get_stream_recipients(
            project_id=self.project_id,
            stream_uuid=self.stream_uuid,
            session=session,
        )


class WorkspaceUserMessageFlags(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_user_message_flags"

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


class WorkspaceUserTopicFlags(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_user_topic_flags"

    is_done = properties.property(
        types.Boolean(),
        default=False,
    )


class UnreadUserMessages(
    models.ModelWithUUID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_unread_user_messages"

    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    unread_count = properties.property(
        types.Integer(min_value=0),
        required=True,
    )
