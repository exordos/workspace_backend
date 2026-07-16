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

from restalchemy.dm import properties
from restalchemy.dm import models
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import models as messenger_models


class MessageEventTimestampType(types.UTCDateTimeZ):
    def from_simple_type(self, value):
        try:
            return super().from_simple_type(value)
        except ValueError:
            if not isinstance(value, str):
                raise
            parsed = datetime.datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.astimezone(datetime.timezone.utc)


MESSAGE_EVENT_TIMESTAMP_TYPE = MessageEventTimestampType()


class ProviderDeliveryEventPayloadBase(models.Model):
    provider = properties.property(
        types.AllowNone(types.Dict()),
        default=None,
    )
    delivery = properties.property(
        types.AllowNone(types.Dict()),
        default=None,
    )


class MessageEventPayloadBase(
    types_dynamic.AbstractKindModel,
    base.WorkspaceUserMessageBase,
    ProviderDeliveryEventPayloadBase,
):
    created_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class MessageCreatedEventPayload(MessageEventPayloadBase):
    KIND = "message.created"


class MessageUpdatedEventPayload(MessageEventPayloadBase):
    KIND = "message.updated"


class MessageReadEventPayload(MessageEventPayloadBase):
    KIND = "message.read"


class MessagesReadEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithProject,
):
    KIND = "messages.read"

    message_uuids = properties.property(
        types.List(),
        required=True,
    )


class MessageDeletedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "message.deleted"

    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    topic_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    author_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    source_name = properties.property(
        types.Enum([source.value for source in base.SourceName]),
        required=True,
    )
    source = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(base.ZulipSource),
            types_dynamic.KindModelType(base.NativeSource),
        ),
        required=True,
    )


class MessageReactionEventPayloadBase(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
    models.ModelWithProject,
    ProviderDeliveryEventPayloadBase,
):
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
    source_name = properties.property(
        types.Enum([source.value for source in base.SourceName]),
        required=True,
    )
    source = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(base.ZulipSource),
            types_dynamic.KindModelType(base.NativeSource),
        ),
        required=True,
    )
    old_message_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    old_emoji_name = properties.property(
        types.AllowNone(types.String(max_length=128)),
        default=None,
    )
    old_source_name = properties.property(
        types.AllowNone(
            types.Enum([source.value for source in base.SourceName]),
        ),
        default=None,
    )
    old_source = properties.property(
        types.AllowNone(
            types_dynamic.KindModelSelectorType(
                types_dynamic.KindModelType(base.ZulipSource),
                types_dynamic.KindModelType(base.NativeSource),
            ),
        ),
        default=None,
    )


class MessageReactionCreatedEventPayload(MessageReactionEventPayloadBase):
    KIND = "message_reaction.created"


class MessageReactionUpdatedEventPayload(MessageReactionEventPayloadBase):
    KIND = "message_reaction.updated"


class MessageReactionDeletedEventPayload(MessageReactionEventPayloadBase):
    KIND = "message_reaction.deleted"


class FolderEventPayloadBase(
    types_dynamic.AbstractKindModel,
    base.WorkspaceUserFolderBase,
):
    created_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class FolderCreatedEventPayload(FolderEventPayloadBase):
    KIND = "folder.created"


class FolderUpdatedEventPayload(FolderEventPayloadBase):
    KIND = "folder.updated"


class StreamEventPayloadBase(
    types_dynamic.AbstractKindModel,
    base.WorkspaceUserStreamBase,
    ProviderDeliveryEventPayloadBase,
):
    created_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class StreamCreatedEventPayload(StreamEventPayloadBase):
    KIND = "stream.created"


class StreamUpdatedEventPayload(StreamEventPayloadBase):
    KIND = "stream.updated"


class StreamReadEventPayload(StreamEventPayloadBase):
    KIND = "stream.read"


class TopicEventPayloadBase(
    types_dynamic.AbstractKindModel,
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    ProviderDeliveryEventPayloadBase,
):
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
        types.Enum([mode.value for mode in base.WorkspaceTopicNotificationMode]),
        default=base.WorkspaceTopicNotificationMode.DEFAULT.value,
    )
    created_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class TopicCreatedEventPayload(TopicEventPayloadBase):
    KIND = "topic.created"


class TopicUpdatedEventPayload(TopicEventPayloadBase):
    KIND = "topic.updated"


class TopicReadEventPayload(TopicEventPayloadBase):
    KIND = "topic.read"


class TopicDeletedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "topic.deleted"

    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    source_name = properties.property(
        types.AllowNone(types.Enum([source.value for source in base.SourceName])),
        default=None,
    )
    source = properties.property(
        types.AllowNone(
            types_dynamic.KindModelSelectorType(
                types_dynamic.KindModelType(base.ZulipSource),
                types_dynamic.KindModelType(base.NativeSource),
            )
        ),
        default=None,
    )


class StreamDeletedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "stream.deleted"

    source_name = properties.property(
        types.AllowNone(types.Enum([source.value for source in base.SourceName])),
        default=None,
    )
    source = properties.property(
        types.AllowNone(
            types_dynamic.KindModelSelectorType(
                types_dynamic.KindModelType(base.ZulipSource),
                types_dynamic.KindModelType(base.NativeSource),
            )
        ),
        default=None,
    )


class StreamBindingsCreatedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "stream_bindings.created"

    items = properties.property(
        types.List(),
        required=True,
    )


class StreamBindingUpdatedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
):
    KIND = "stream_binding.updated"

    stream_uuid = properties.property(types.UUID(), required=True)
    user_uuid = properties.property(types.UUID(), required=True)
    who_uuid = properties.property(types.UUID(), required=True)
    role = properties.property(types.String(min_length=1), required=True)
    notification_mode = properties.property(
        types.String(min_length=1),
        required=True,
    )


class StreamBindingDeletedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "stream_binding.deleted"

    stream_uuid = properties.property(types.UUID(), required=True)
    user_uuid = properties.property(types.UUID(), required=True)


class UserUpdatedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
    models.ModelWithTimestamp,
):
    KIND = "user.updated"

    created_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    username = properties.property(
        types.String(min_length=1, max_length=128),
        required=True,
    )
    source = properties.property(
        types.Enum(
            [
                messenger_models.WorkspaceUserSource.IAM.value,
                messenger_models.WorkspaceUserSource.ZULIP.value,
            ]
        ),
        required=True,
    )
    status = properties.property(
        types.Enum(["active", "idle", "offline", "do_not_disturb"]),
        required=True,
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
    avatar = properties.property(
        messenger_models.WorkspaceUserAvatarType(),
        required=True,
    )
    last_ping_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        required=True,
    )


class FolderDeletedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "folder.deleted"


class FolderItemDeletedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "folder_item.deleted"


class FileEventPayloadBase(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithRequiredNameDesc,
    models.ModelWithTimestamp,
):
    user_uuid = properties.property(types.UUID(), required=True)
    stream_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    content_type = properties.property(
        types.String(min_length=1, max_length=255),
        required=True,
    )
    size_bytes = properties.property(types.Integer(min_value=0), required=True)
    hash = properties.property(
        types.String(min_length=1, max_length=255),
        required=True,
    )


class FileCreatedEventPayload(FileEventPayloadBase):
    KIND = "file.created"


class FileUpdatedEventPayload(FileEventPayloadBase):
    KIND = "file.updated"


class FileDeletedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "file.deleted"

    stream_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )


WORKSPACE_EVENT_PAYLOAD_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(MessageCreatedEventPayload),
    types_dynamic.KindModelType(MessageUpdatedEventPayload),
    types_dynamic.KindModelType(MessageReadEventPayload),
    types_dynamic.KindModelType(MessagesReadEventPayload),
    types_dynamic.KindModelType(MessageDeletedEventPayload),
    types_dynamic.KindModelType(MessageReactionCreatedEventPayload),
    types_dynamic.KindModelType(MessageReactionUpdatedEventPayload),
    types_dynamic.KindModelType(MessageReactionDeletedEventPayload),
    types_dynamic.KindModelType(FolderCreatedEventPayload),
    types_dynamic.KindModelType(FolderUpdatedEventPayload),
    types_dynamic.KindModelType(StreamCreatedEventPayload),
    types_dynamic.KindModelType(StreamUpdatedEventPayload),
    types_dynamic.KindModelType(StreamReadEventPayload),
    types_dynamic.KindModelType(TopicCreatedEventPayload),
    types_dynamic.KindModelType(TopicUpdatedEventPayload),
    types_dynamic.KindModelType(TopicReadEventPayload),
    types_dynamic.KindModelType(TopicDeletedEventPayload),
    types_dynamic.KindModelType(StreamDeletedEventPayload),
    types_dynamic.KindModelType(StreamBindingsCreatedEventPayload),
    types_dynamic.KindModelType(StreamBindingUpdatedEventPayload),
    types_dynamic.KindModelType(StreamBindingDeletedEventPayload),
    types_dynamic.KindModelType(UserUpdatedEventPayload),
    types_dynamic.KindModelType(FolderDeletedEventPayload),
    types_dynamic.KindModelType(FolderItemDeletedEventPayload),
    types_dynamic.KindModelType(FileCreatedEventPayload),
    types_dynamic.KindModelType(FileUpdatedEventPayload),
    types_dynamic.KindModelType(FileDeletedEventPayload),
)
