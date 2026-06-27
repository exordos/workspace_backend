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


class MessageCreatedEventPayload(
    types_dynamic.AbstractKindModel,
    base.WorkspaceUserMessageBase,
):
    KIND = "message.created"

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


class TopicEventPayloadBase(
    types_dynamic.AbstractKindModel,
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
):
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


class TopicDeletedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "topic.deleted"

    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )


class StreamDeletedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithUUID,
):
    KIND = "stream.deleted"


class StreamBindingsCreatedEventPayload(
    types_dynamic.AbstractKindModel,
    models.ModelWithProject,
):
    KIND = "stream_bindings.created"

    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    stream_bindings = properties.property(
        types.List(),
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


WORKSPACE_EVENT_PAYLOAD_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(MessageCreatedEventPayload),
    types_dynamic.KindModelType(FolderCreatedEventPayload),
    types_dynamic.KindModelType(FolderUpdatedEventPayload),
    types_dynamic.KindModelType(StreamCreatedEventPayload),
    types_dynamic.KindModelType(StreamUpdatedEventPayload),
    types_dynamic.KindModelType(TopicCreatedEventPayload),
    types_dynamic.KindModelType(TopicUpdatedEventPayload),
    types_dynamic.KindModelType(TopicDeletedEventPayload),
    types_dynamic.KindModelType(StreamDeletedEventPayload),
    types_dynamic.KindModelType(StreamBindingsCreatedEventPayload),
    types_dynamic.KindModelType(FolderDeletedEventPayload),
    types_dynamic.KindModelType(FolderItemDeletedEventPayload),
)
