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
import random
import typing

from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from workspace.messenger_api.dm import message_payloads


FOLDER_SYSTEM_TYPE_ALL = "all"
FOLDER_SYSTEM_TYPE_CREATED = "created"
FOLDER_SYSTEM_TYPES = (FOLDER_SYSTEM_TYPE_ALL, FOLDER_SYSTEM_TYPE_CREATED)
COLOR_MAX_VALUE = 0xFFFFFF


def random_color() -> int:
    return random.randint(0, COLOR_MAX_VALUE)


class StrictBoolean(types.Boolean):
    def from_simple_type(self, value: typing.Any) -> bool:
        if not self.validate(value):
            raise ValueError("Expected a JSON boolean")
        return value


class Color(types.Integer):
    def __init__(self) -> None:
        super().__init__(min_value=0, max_value=COLOR_MAX_VALUE)

    def to_openapi_spec(
        self,
        prop_kwargs: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        prop_kwargs = dict(prop_kwargs)
        prop_kwargs.pop("default", None)
        return super().to_openapi_spec(prop_kwargs)


class NativeSource(types_dynamic.AbstractKindModel):
    KIND = "native"


class SourceName(str, enum.Enum):
    NATIVE = NativeSource.KIND


def native_source() -> NativeSource:
    return NativeSource()


class WorkspaceSourceBase(models.Model):
    source_name = properties.property(
        types.Enum([source.value for source in SourceName]),
        default=SourceName.NATIVE.value,
    )
    source = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(NativeSource),
        ),
        default=native_source,
    )


class UserScopedModelWithUUID(models.ModelWithUUID):
    user_uuid = properties.property(
        types.UUID(),
        required=True,
        id_property=True,
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return {"uuid": cls.properties.properties["uuid"]}


class WorkspaceMessageFieldsBase(WorkspaceSourceBase):
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

    def validate_key_announcement_update(self, kind: str) -> None:
        if message_payloads.KeyAnnouncedPayload.KIND in (self.payload.KIND, kind):
            raise ra_exc.ValidationErrorException()


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
    mentioned = properties.property(
        types.Boolean(),
        default=False,
        read_only=True,
    )
    reactions = properties.property(
        types.Dict(),
        default=dict,
    )
    reaction_users = properties.property(
        types.Dict(),
        default=dict,
        read_only=True,
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
        read_only=True,
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


class WorkspaceStreamRole(str, enum.Enum):
    GUEST = "guest"
    MEMBER = "member"
    MODERATOR = "moderator"
    ADMINISTRATOR = "administrator"
    OWNER = "owner"


class WorkspaceStreamNotificationMode(str, enum.Enum):
    MENTIONS_ONLY = "mentions_only"
    MUTED = "muted"
    ALL_MESSAGES = "all_messages"


class WorkspaceTopicNotificationMode(str, enum.Enum):
    MUTE = "mute"
    DEFAULT = "default"
    UNMUTE = "unmute"
    FOLLOW = "follow"


class WorkspaceStreamBase(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithRequiredNameDesc,
    models.ModelWithTimestamp,
):
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
    is_archived = properties.property(
        types.Boolean(),
        default=False,
    )
    direct_user_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    private_index = properties.property(
        types.AllowNone(types.String(max_length=73)),
        default=None,
    )
    color = properties.property(
        Color(),
        default=random_color,
    )
    default_topic_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )


class WorkspaceStreamEncryptionCreation(models.Model):
    source_name = properties.property(
        types.Enum([source.value for source in SourceName]),
        default=SourceName.NATIVE.value,
    )
    encryption = properties.property(StrictBoolean(), default=False)
    current_encryption_key_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None
    )

    def validate(self) -> None:
        super().validate()
        if self.encryption != (self.current_encryption_key_uuid is not None):
            raise ra_exc.ValidationErrorException()
        if self.encryption and self.source_name == "zulip":
            raise ra_exc.ValidationErrorException()


class WorkspaceUserStreamBase(
    UserScopedModelWithUUID,
    models.ModelWithRequiredNameDesc,
    models.ModelWithProject,
    models.ModelWithTimestamp,
):
    owner = properties.property(
        types.UUID(),
        required=True,
    )
    role = properties.property(
        types.Enum([role.value for role in WorkspaceStreamRole]),
        required=True,
    )
    encryption = properties.property(StrictBoolean(), default=False)
    current_encryption_key_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )

    notification_mode = properties.property(
        types.Enum([mode.value for mode in WorkspaceStreamNotificationMode]),
        default=WorkspaceStreamNotificationMode.ALL_MESSAGES.value,
    )
    unread_count = properties.property(
        types.Integer(min_value=0),
        default=0,
    )
    active_unread_count = properties.property(
        types.Integer(min_value=0),
        default=0,
    )
    passive_unread_count = properties.property(
        types.Integer(min_value=0),
        default=0,
    )
    source_name = properties.property(
        types.Enum([source.value for source in SourceName]),
        required=True,
    )
    source = properties.property(
        types_dynamic.KindModelSelectorType(
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
    is_archived = properties.property(
        types.Boolean(),
        default=False,
    )
    direct_user_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    private_index = properties.property(
        types.AllowNone(types.String(max_length=73)),
        default=None,
    )
    color = properties.property(
        Color(),
        default=random_color,
    )
    last_message_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    default_topic_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
        read_only=True,
    )

    @staticmethod
    def validate_encryption_update(values: typing.Mapping[str, typing.Any]) -> None:
        if {"encryption", "current_encryption_key_uuid"}.intersection(values):
            raise ra_exc.ValidationErrorException()

    @staticmethod
    def validate_message_kind(source_name: str, kind: str) -> None:
        if source_name == "zulip" and kind == message_payloads.E2EEPayload.KIND:
            raise ra_exc.ValidationErrorException()

    def validate_message_creation(self, kind: str) -> None:
        self.validate_message_kind(self.source_name, kind)
        if (
            kind == message_payloads.KeyAnnouncedPayload.KIND
            and self.role != WorkspaceStreamRole.OWNER.value
        ):
            raise ra_exc.ValidationErrorException()
