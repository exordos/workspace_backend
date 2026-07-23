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
import hashlib
import re
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from workspace.common import file_storage_opts
from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import message_payloads


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
    def folder_uuid(self) -> sys_uuid.UUID:
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
    ZULIP = "zulip"


class WorkspaceUserLastPingAtType(types.UTCDateTimeZ):
    def to_simple_type(self, value: datetime.datetime) -> str:
        return value.isoformat()


WORKSPACE_USER_AVATAR_MAX_LENGTH = 2048
WORKSPACE_USER_GRAVATAR_PREFIX = "urn:gravatar:"
WORKSPACE_USER_IMAGE_AVATAR_PREFIX = "urn:image:"
WORKSPACE_USER_URL_AVATAR_PREFIX = "urn:url:"
WORKSPACE_USER_GRAVATAR_HASH_RE = re.compile(
    r"(?:[0-9a-f]{32}|[0-9a-f]{64})",
    re.IGNORECASE,
)


def build_workspace_user_gravatar_avatar(email: str) -> str:
    normalized_email = email.strip().lower().encode()
    email_hash = hashlib.md5(
        normalized_email,
        usedforsecurity=False,
    ).hexdigest()
    return "%s%s" % (WORKSPACE_USER_GRAVATAR_PREFIX, email_hash)


def build_workspace_user_default_avatar(user_uuid: object) -> str:
    return build_workspace_user_gravatar_avatar(str(user_uuid))


class WorkspaceUserAvatarType(types.String):
    def __init__(self) -> None:
        super(WorkspaceUserAvatarType, self).__init__(
            min_length=1,
            max_length=WORKSPACE_USER_AVATAR_MAX_LENGTH,
        )

    def validate(self, value: str) -> bool:
        return super(WorkspaceUserAvatarType, self).validate(
            value,
        ) and self._is_workspace_user_avatar_urn(value)

    def _is_workspace_user_avatar_urn(self, value: str) -> bool:
        return (
            self._is_uuid_urn(value, WORKSPACE_USER_IMAGE_AVATAR_PREFIX)
            or self._is_url_urn(value)
            or self._is_gravatar_urn(value)
        )

    @staticmethod
    def _is_gravatar_urn(value: str) -> bool:
        if not value.startswith(WORKSPACE_USER_GRAVATAR_PREFIX):
            return False
        avatar_hash = value[len(WORKSPACE_USER_GRAVATAR_PREFIX) :]
        return WORKSPACE_USER_GRAVATAR_HASH_RE.fullmatch(avatar_hash) is not None

    @staticmethod
    def _is_uuid_urn(value: str, prefix: str) -> bool:
        if not value.startswith(prefix):
            return False
        try:
            sys_uuid.UUID(value[len(prefix) :])
        except ValueError:
            return False
        return True

    @staticmethod
    def _is_url_urn(value: str) -> bool:
        if not value.startswith(WORKSPACE_USER_URL_AVATAR_PREFIX):
            return False
        url = value[len(WORKSPACE_USER_URL_AVATAR_PREFIX) :]
        return url.startswith("http://") or url.startswith("https://")


class WorkspaceUser(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_users"

    provider_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    provider_external_id = properties.property(
        types.AllowNone(types.String(max_length=2048)),
        default=None,
    )

    def pour(self, **kwargs: typing.Any) -> None:
        if "uuid" not in kwargs:
            kwargs["uuid"] = sys_uuid.uuid4()
        if "avatar" not in kwargs or kwargs["avatar"] is None:
            email = kwargs.get("email")
            if email:
                kwargs["avatar"] = build_workspace_user_gravatar_avatar(email)
            else:
                kwargs["avatar"] = build_workspace_user_default_avatar(
                    kwargs["uuid"],
                )
        super(WorkspaceUser, self).pour(**kwargs)

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
    avatar = properties.property(
        WorkspaceUserAvatarType(),
        required=True,
    )
    last_ping_at = properties.property(
        WorkspaceUserLastPingAtType(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    @classmethod
    def sync_iam_identity(
        cls,
        user_uuid: sys_uuid.UUID,
        username: str,
        first_name: str | None,
        last_name: str | None,
        email: str | None,
        status: str = WorkspaceUserStatus.ACTIVE.value,
    ) -> "WorkspaceUser":
        values = {
            "username": username,
            "source": WorkspaceUserSource.IAM.value,
            "first_name": first_name or None,
            "last_name": last_name or None,
            "email": email or None,
        }
        workspace_user = cls.objects.get_one_or_none(
            filters={"uuid": dm_filters.EQ(user_uuid)},
        )
        if workspace_user is None:
            workspace_user = cls.objects.get_one_or_none(
                filters={"username": dm_filters.EQ(username)},
            )
        if workspace_user is None:
            workspace_user = cls(
                uuid=user_uuid,
                status=status,
                **values,
            )
            workspace_user.insert()
            return workspace_user

        changed_values = {
            name: value
            for name, value in values.items()
            if getattr(workspace_user, name) != value
        }
        if changed_values:
            workspace_user.update_dm(values=changed_values)
            workspace_user.save()
        return workspace_user


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
        types.AllowNone(types.UUID()),
        default=None,
    )
    acl_mode = properties.property(
        types.Enum(["owner", "stream", "public"]),
        default="stream",
    )
    provider_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
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
    storage_type = properties.property(
        types.Enum(file_storage_opts.STORAGE_TYPES),
        default=file_storage_opts.STORAGE_TYPE_FILE,
    )
    storage_id = properties.property(
        types.String(max_length=255),
        default="",
    )
    storage_object_id = properties.property(
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


class WorkspaceVisibleFile(WorkspaceFile):
    __tablename__ = "m_workspace_visible_files_v1"

    # Public files have no materialized viewer row.  The canonical read path
    # combines ``viewer_user_uuid = current user`` with ``acl_mode = public``.
    viewer_user_uuid = properties.property(types.AllowNone(types.UUID()), default=None)


class WorkspaceStream(base.WorkspaceStreamBase, orm.SQLStorableMixin):
    __tablename__ = "m_workspace_streams"

    provider_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None
    )
    provider_external_id = properties.property(
        types.AllowNone(types.String(max_length=2048)), default=None
    )
    delivery_status = properties.property(
        types.AllowNone(types.Enum(["pending", "delivered", "failed"])),
        default=None,
    )
    delivery_error = properties.property(types.AllowNone(types.String()), default=None)
    delivery_updated_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    provider_metadata = properties.property(types.AllowNone(types.Dict()), default=None)
    delivery_metadata = properties.property(types.AllowNone(types.Dict()), default=None)

    def get_recipients(self, session: typing.Any = None) -> list[sys_uuid.UUID]:
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

    def get_stream(self) -> WorkspaceStream:
        return WorkspaceStream.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(self.stream_uuid),
                "project_id": dm_filters.EQ(self.project_id),
            },
        )


def get_stream_recipients(
    project_id: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
    session: typing.Any = None,
) -> list[sys_uuid.UUID]:
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

    def get_default_topic(self) -> "WorkspaceStreamTopic | None":
        if self.default_topic_uuid is None:
            return None
        return WorkspaceStreamTopic.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(self.default_topic_uuid),
                "project_id": dm_filters.EQ(self.project_id),
                "stream_uuid": dm_filters.EQ(self.uuid),
            }
        )

    def get_recipients(self, session: typing.Any = None) -> list[sys_uuid.UUID]:
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

    provider_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None
    )
    provider_external_id = properties.property(
        types.AllowNone(types.String(max_length=2048)), default=None
    )
    delivery_status = properties.property(
        types.AllowNone(types.Enum(["pending", "delivered", "failed"])),
        default=None,
    )
    delivery_error = properties.property(types.AllowNone(types.String()), default=None)
    delivery_updated_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    provider_metadata = properties.property(types.AllowNone(types.Dict()), default=None)
    delivery_metadata = properties.property(types.AllowNone(types.Dict()), default=None)

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


class WorkspaceVisibleMessageReaction(WorkspaceMessageReactions):
    __tablename__ = "m_workspace_visible_message_reactions_v1"

    viewer_user_uuid = properties.property(types.UUID(), required=True)


WORKSPACE_EVENT_SCHEMA_VERSION = 1
WORKSPACE_EVENT_OBJECT_TYPES = (
    "message",
    "message_reaction",
    "stream",
    "stream_binding",
    "topic",
    "user",
    "folder",
    "folder_item",
    "file",
    "external_account",
    "external_chat",
    "external_operation",
)
WORKSPACE_EVENT_ACTIONS = (
    "created",
    "updated",
    "deleted",
    "read",
)


class WorkspaceEvent(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_events"

    schema_version = properties.property(
        types.Integer(min_value=1),
        default=WORKSPACE_EVENT_SCHEMA_VERSION,
    )
    epoch_version = properties.property(
        types.Integer(min_value=0),
        required=False,
    )
    object_type = properties.property(
        types.Enum(WORKSPACE_EVENT_OBJECT_TYPES),
        required=True,
    )
    action = properties.property(
        types.Enum(WORKSPACE_EVENT_ACTIONS),
        required=True,
    )
    payload = properties.property(
        types.Dict(),
        required=True,
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return {"epoch_version": cls.properties.properties["epoch_version"]}

    def _get_prepared_data(
        self, properties: typing.Any = None
    ) -> dict[str, typing.Any]:
        data = super()._get_prepared_data(properties=properties)
        if "epoch_version" in data and data["epoch_version"] is None:
            data.pop("epoch_version")
        return data

    def insert(self, session: typing.Any = None) -> int:
        engine = self._get_engine()
        session = session or contexts.Context().get_session()
        data = self._get_prepared_data()
        data.pop("epoch_version", None)
        # A single transaction can emit events for many users in one project.
        # Serialize those writers before they touch per-user cursor rows so
        # concurrent bulk projections cannot acquire the same cursor locks in
        # different orders and deadlock.
        session.execute(
            """
            SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))
            """,
            (self.project_id,),
        )
        columns = tuple(data)
        statement = (
            f"INSERT INTO {engine.escape(self.get_table().name)} "
            f"({', '.join(engine.escape(column) for column in columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))}) "
            f"RETURNING {engine.escape('epoch_version')}"
        )
        row = session.execute(statement, tuple(data[column] for column in columns))
        self.epoch_version = row.fetchone()["epoch_version"]
        session.execute(
            """
            INSERT INTO "m_workspace_event_cursors" (
                "project_id", "user_uuid", "current_epoch_version"
            ) VALUES (%s, %s, %s)
            ON CONFLICT ("project_id", "user_uuid") DO UPDATE
            SET
                "current_epoch_version" = GREATEST(
                    "m_workspace_event_cursors"."current_epoch_version",
                    EXCLUDED."current_epoch_version"
                ),
                "updated_at" = NOW()
            """,
            (self.project_id, self.user_uuid, self.epoch_version),
        )
        self._saved = True
        return self.epoch_version


class WorkspaceVisibleEvent(WorkspaceEvent):
    __tablename__ = "m_workspace_visible_events"


class WorkspaceProject(
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_projects_view"

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return {"project_id": cls.properties.properties["project_id"]}


class WorkspaceStreamTopic(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    base.WorkspaceSourceBase,
    models.CustomPropertiesMixin,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_stream_topics"

    provider_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None
    )
    provider_external_id = properties.property(
        types.AllowNone(types.String(max_length=2048)), default=None
    )
    delivery_status = properties.property(
        types.AllowNone(types.Enum(["pending", "delivered", "failed"])),
        default=None,
    )
    delivery_error = properties.property(types.AllowNone(types.String()), default=None)
    delivery_updated_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    provider_metadata = properties.property(types.AllowNone(types.Dict()), default=None)
    delivery_metadata = properties.property(types.AllowNone(types.Dict()), default=None)

    name = properties.property(
        types.String(max_length=128),
        required=True,
    )
    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    color = properties.property(
        base.Color(),
        default=base.random_color,
    )

    def get_recipients(self, session: typing.Any = None) -> list[sys_uuid.UUID]:
        return get_stream_recipients(
            project_id=self.project_id,
            stream_uuid=self.stream_uuid,
            session=session,
        )


class WorkspaceDraft(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_drafts"

    project_id = properties.property(
        types.UUID(),
        required=True,
        read_only=True,
    )
    user_uuid = properties.property(
        types.UUID(),
        required=True,
        read_only=True,
    )
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
    revision = properties.property(
        types.Integer(min_value=1),
        default=1,
        read_only=True,
    )


class WorkspaceUserTopic(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    base.WorkspaceSourceBase,
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
        base.Color(),
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

    def get_flags(self) -> "WorkspaceUserTopicFlags":
        return WorkspaceUserTopicFlags.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(self.uuid),
                "user_uuid": dm_filters.EQ(self.user_uuid),
                "project_id": dm_filters.EQ(self.project_id),
            }
        )

    def get_recipients(self, session: typing.Any = None) -> list[sys_uuid.UUID]:
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

    provider_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None
    )
    provider_external_id = properties.property(
        types.AllowNone(types.String(max_length=2048)), default=None
    )
    delivery_status = properties.property(
        types.AllowNone(types.Enum(["pending", "delivered", "failed"])),
        default=None,
    )
    delivery_error = properties.property(types.AllowNone(types.String()), default=None)
    delivery_updated_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    provider_metadata = properties.property(types.AllowNone(types.Dict()), default=None)
    delivery_metadata = properties.property(types.AllowNone(types.Dict()), default=None)

    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )

    def validate(self) -> None:
        super().validate()
        binding = WorkspaceStreamBinding.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(self.project_id),
                "stream_uuid": dm_filters.EQ(self.stream_uuid),
                "user_uuid": dm_filters.EQ(self.user_uuid),
            },
        )
        if binding is None and (
            self.provider_uuid is None or self.external_account_uuid is None
        ):
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

    def get_recipients(self, session: typing.Any = None) -> list[sys_uuid.UUID]:
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

    def get_recipients(self, session: typing.Any = None) -> list[sys_uuid.UUID]:
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
