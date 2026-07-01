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
import enum

from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import event_payloads


class ChatType(str, enum.Enum):
    STREAM = "stream"
    GROUP = "group"
    PRIVATE = "private"


class SystemFolderType(str, enum.Enum):
    ALL = base.FOLDER_SYSTEM_TYPE_ALL
    CREATED = base.FOLDER_SYSTEM_TYPE_CREATED


ZulipSource = base.ZulipSource
NativeSource = base.NativeSource
SourceName = base.SourceName
WorkspaceStreamRole = base.WorkspaceStreamRole
WorkspaceStreamNotificationMode = base.WorkspaceStreamNotificationMode
WorkspaceTopicNotificationMode = base.WorkspaceTopicNotificationMode


class Folder(
    base.WorkspaceFolderBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_folders"


class UserFolder(
    base.WorkspaceUserFolderBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_folders_view"


class FolderItem(
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


class SystemFolderItemBase(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    folder = properties.property(
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

    @property
    def folder_uuid(self):
        return self.folder


class AllFolderItem(SystemFolderItemBase):
    __tablename__ = "m_folder_all_items_view"


class PersonalFolderItem(SystemFolderItemBase):
    __tablename__ = "m_folder_private_items_view"


class ChannelFolderItem(SystemFolderItemBase):
    __tablename__ = "m_folder_channel_items_view"


class WorkspaceUserStatus(str, enum.Enum):
    ACTIVE = "active"
    IDLE = "idle"
    OFFLINE = "offline"
    DO_NOT_DISTURB = "do_not_disturb"


class WorkspaceUserSource(str, enum.Enum):
    IAM = "iam"


class WorkspaceUserLastPingAtType(types.UTCDateTimeZ):
    def to_simple_type(self, value):
        return value.isoformat()


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
    status_emoji = properties.property(
        types.AllowNone(types.String(max_length=64)),
        default=None,
    )
    status_text = properties.property(
        types.AllowNone(types.String(max_length=256)),
        default=None,
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
        WorkspaceUserLastPingAtType(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class WorkspaceFile(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithRequiredNameDesc,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_files"

    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    content_type = properties.property(
        types.String(min_length=1, max_length=255),
        required=True,
    )
    size_bytes = properties.property(
        types.Integer(min_value=0),
        required=True,
    )
    hash = properties.property(
        types.String(min_length=1, max_length=255),
        required=True,
    )


class WorkspaceFileAccess(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_file_accesses"

    file_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )


class WorkspaceStream(base.WorkspaceStreamBase, orm.SQLStorableMixin):
    __tablename__ = "m_workspace_streams"

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
    notification_mode = properties.property(
        types.Enum([mode.value for mode in WorkspaceStreamNotificationMode]),
        default=WorkspaceStreamNotificationMode.ALL_MESSAGES.value,
    )

    def get_stream(self):
        return WorkspaceStream.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(self.stream_uuid),
                "project_id": dm_filters.EQ(self.project_id),
            },
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


class WorkspaceUserStream(base.WorkspaceUserStreamBase, orm.SQLStorableMixin):
    __tablename__ = "m_workspace_user_streams"

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

    def insert(self, session=None):
        engine = self._get_engine()
        data = self._get_prepared_data()
        data.pop("epoch_version", None)
        columns = tuple(data)
        statement = (
            f"INSERT INTO {engine.escape(self.get_table().name)} "
            f"({', '.join(engine.escape(column) for column in columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))}) "
            f"RETURNING {engine.escape('epoch_version')}"
        )
        with engine.session_manager(session=session) as s:
            row = s.execute(statement, tuple(data[column] for column in columns))
            self.epoch_version = row.fetchone()["epoch_version"]
            self._saved = True
        return self.epoch_version


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
    color = properties.property(
        types.Integer(min_value=0, max_value=base.COLOR_MAX_VALUE),
        default=base.random_color,
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
    color = properties.property(
        types.Integer(min_value=0, max_value=base.COLOR_MAX_VALUE),
        default=base.random_color,
    )
    last_message_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
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
    notification_mode = properties.property(
        types.Enum([mode.value for mode in WorkspaceTopicNotificationMode]),
        default=WorkspaceTopicNotificationMode.DEFAULT.value,
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
    notification_mode = properties.property(
        types.Enum([mode.value for mode in WorkspaceTopicNotificationMode]),
        default=WorkspaceTopicNotificationMode.DEFAULT.value,
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
