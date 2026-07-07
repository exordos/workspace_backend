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
import uuid as sys_uuid

from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.storage.sql import orm

from workspace.common import file_storage_opts
from workspace.common.clients import iam as iam_client
from workspace.common.clients import zulip as zulip_client
from workspace.messenger_api.dm import base


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

ZULIP_PROCESSED_ENTITY_TYPES = (
    "stream",
    "private_stream",
    "topic",
    "message",
)


class ZulipProcessedEntity(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_zulip_processed_entities"

    server_url = properties.property(
        types.String(min_length=1, max_length=2048),
        required=True,
    )
    entity_type = properties.property(
        types.Enum(ZULIP_PROCESSED_ENTITY_TYPES),
        required=True,
    )
    entity_id = properties.property(
        types.String(min_length=1, max_length=256),
        required=True,
    )
    workspace_uuid = properties.property(
        types.UUID(),
        required=True,
    )


class ZulipEventQueueState(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_zulip_event_queue_states"

    external_account_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    server_url = properties.property(
        types.Url(),
        required=True,
    )
    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    queue_id = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=256)),
        default=None,
    )
    last_event_id = properties.property(
        types.Integer(),
        default=-1,
    )
    last_message_id = properties.property(
        types.Integer(min_value=0),
        default=0,
    )
    is_synced = properties.property(
        types.Boolean(),
        default=False,
    )


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
    ZULIP = "zulip"


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


class ExternalAccountType(str, enum.Enum):
    ZULIP = "zulip"
    IAM = "iam"


class ExternalAccountStatus(str, enum.Enum):
    NEW = "new"
    ACTIVE = "active"


DEFAULT_ZULIP_TIMEZONE = "Europe/Moscow"


class ZulipExternalAccountCredentialsKind(types_dynamic.AbstractKindModel):
    KIND = ExternalAccountType.ZULIP.value

    login = properties.property(
        types.String(min_length=1, max_length=256),
        required=True,
    )
    token = properties.property(
        types.String(min_length=1, max_length=4096),
        required=True,
    )


class ZulipExternalAccountUserInfoKind(types_dynamic.AbstractKindModel):
    KIND = ExternalAccountType.ZULIP.value

    email = properties.property(
        types.String(min_length=1, max_length=256),
        required=True,
    )
    user_id = properties.property(
        types.Integer(min_value=0),
        required=True,
    )
    avatar_version = properties.property(
        types.Integer(min_value=0),
        required=True,
    )
    is_admin = properties.property(
        types.Boolean(),
        required=True,
    )
    is_owner = properties.property(
        types.Boolean(),
        required=True,
    )
    is_guest = properties.property(
        types.Boolean(),
        required=True,
    )
    role = properties.property(
        types.Integer(min_value=0),
        required=True,
    )
    is_bot = properties.property(
        types.Boolean(),
        required=True,
    )
    full_name = properties.property(
        types.String(min_length=1, max_length=256),
        required=True,
    )
    timezone = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=128)),
        default=DEFAULT_ZULIP_TIMEZONE,
    )
    is_active = properties.property(
        types.Boolean(),
        required=True,
    )
    date_joined = properties.property(
        types.String(min_length=1, max_length=64),
        required=True,
    )
    delivery_email = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=256)),
        default=None,
    )
    avatar_url = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=2048)),
        default=None,
    )


ZULIP_EXTERNAL_ACCOUNT_CREDENTIALS_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(ZulipExternalAccountCredentialsKind),
)

ZULIP_EXTERNAL_ACCOUNT_USER_INFO_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(ZulipExternalAccountUserInfoKind),
)


class ZulipExternalAccountKind(types_dynamic.AbstractKindModel):
    KIND = ExternalAccountType.ZULIP.value

    credentials = properties.property(
        types.AllowNone(ZULIP_EXTERNAL_ACCOUNT_CREDENTIALS_TYPE),
        default=None,
    )
    user_info = properties.property(
        types.AllowNone(ZULIP_EXTERNAL_ACCOUNT_USER_INFO_TYPE),
        default=None,
    )

    def sync_users(self, external_account):
        users = self._get_zulip_users(external_account=external_account)
        external_accounts_by_user = (
            self._get_external_accounts_by_server_url_and_user_id(
                external_account=external_account,
            )
        )
        synced_accounts = []
        for user in users:
            user_info = self._get_zulip_user_info(user=user)
            user_key = (external_account.server_url, user_info.user_id)
            synced_account = external_accounts_by_user.get(
                user_key,
            )
            if synced_account is None:
                synced_account = self._create_zulip_external_account(
                    external_account=external_account,
                    user=user,
                    user_info=user_info,
                )
                external_accounts_by_user[user_key] = synced_account
            else:
                self._update_zulip_external_account(
                    external_account=synced_account,
                    user_info=user_info,
                )
            synced_accounts.append(synced_account)

        external_account.update_dm(
            values={"status": ExternalAccountStatus.ACTIVE.value},
        )
        external_account.save()
        return synced_accounts

    def _get_zulip_users(self, external_account):
        credentials = self.credentials
        client = zulip_client.ZulipClient(endpoint=external_account.server_url)
        users = client.get_users_with_api_key(
            login=credentials.login,
            token=credentials.token,
        )
        return users

    def _get_external_accounts_by_server_url_and_user_id(self, external_account):
        accounts = type(external_account).objects.get_all(
            filters={
                "project_id": dm_filters.EQ(external_account.project_id),
                "account_type": dm_filters.EQ(external_account.account_type),
                "server_url": dm_filters.EQ(external_account.server_url),
            },
            order_by={"created_at": "asc", "uuid": "asc"},
        )
        return {
            (
                account.server_url,
                account.account_settings.user_info.user_id,
            ): account
            for account in accounts
            if (
                account.server_url == external_account.server_url
                and account.account_settings.user_info is not None
            )
        }

    def _create_zulip_external_account(
        self,
        external_account,
        user,
        user_info,
    ):
        workspace_user = self._get_or_create_workspace_user(user=user)
        synced_account = type(external_account)(
            project_id=external_account.project_id,
            user_uuid=workspace_user.uuid,
            server_url=external_account.server_url,
            account_type=external_account.account_type,
            status=ExternalAccountStatus.ACTIVE.value,
            account_settings=ZulipExternalAccountKind(
                credentials=None,
                user_info=user_info,
            ),
        )
        synced_account.insert()
        return synced_account

    def _update_zulip_external_account(self, external_account, user_info):
        external_account.account_settings.user_info = user_info
        external_account.update_dm(
            values={"status": ExternalAccountStatus.ACTIVE.value},
        )
        external_account.save()
        return external_account

    def _get_or_create_workspace_user(self, user):
        email = self._empty_to_none(user["delivery_email"])
        if email is not None:
            workspace_users = WorkspaceUser.objects.get_all(
                filters={"email": dm_filters.EQ(email)},
            )
            for workspace_user in workspace_users:
                return workspace_user

        workspace_user = WorkspaceUser(
            uuid=sys_uuid.uuid4(),
            username=user["full_name"],
            source=WorkspaceUserSource.ZULIP.value,
            email=email,
        )
        workspace_user.insert()
        return workspace_user

    @staticmethod
    def _empty_to_none(value):
        if value == "":
            return None
        return value

    @staticmethod
    def _empty_to_default(value, default):
        if value == "":
            return default
        return value

    def _get_zulip_user_info(self, user):
        return ZulipExternalAccountUserInfoKind(
            email=user["email"],
            user_id=user["user_id"],
            avatar_version=user["avatar_version"],
            is_admin=user["is_admin"],
            is_owner=user["is_owner"],
            is_guest=user["is_guest"],
            role=user["role"],
            is_bot=user["is_bot"],
            full_name=user["full_name"],
            timezone=self._empty_to_default(
                user["timezone"],
                DEFAULT_ZULIP_TIMEZONE,
            ),
            is_active=user["is_active"],
            date_joined=user["date_joined"],
            delivery_email=self._empty_to_none(user["delivery_email"]),
            avatar_url=self._empty_to_none(user["avatar_url"]),
        )


class IamExternalAccountCredentialsKind(types_dynamic.AbstractKindModel):
    KIND = ExternalAccountType.IAM.value

    username = properties.property(
        types.String(min_length=1, max_length=256),
        required=True,
    )
    access_token = properties.property(
        types.String(min_length=1, max_length=4096),
        required=True,
    )


IAM_EXTERNAL_ACCOUNT_CREDENTIALS_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(IamExternalAccountCredentialsKind),
)


class IamExternalAccountKind(types_dynamic.AbstractKindModel):
    KIND = ExternalAccountType.IAM.value

    credentials = properties.property(
        IAM_EXTERNAL_ACCOUNT_CREDENTIALS_TYPE,
        required=True,
    )

    def sync_users(self, external_account):
        users = self._get_iam_users(external_account=external_account)
        synced_users = []
        for user in users:
            synced_users.append(self._sync_iam_user(user=user))

        external_account.update_dm(
            values={"status": ExternalAccountStatus.ACTIVE.value},
        )
        external_account.save()
        return synced_users

    def _get_iam_users(self, external_account):
        client = iam_client.IamClient(endpoint=external_account.server_url)
        return client.get_users(token=self.credentials.access_token)

    def _sync_iam_user(self, user):
        user_uuid = sys_uuid.UUID(user["uuid"])
        status = WorkspaceUserStatus.ACTIVE.value
        if user["status"] != "ACTIVE":
            status = WorkspaceUserStatus.OFFLINE.value

        values = {
            "username": user["username"],
            "source": WorkspaceUserSource.IAM.value,
            "status": status,
            "first_name": self._empty_to_none(user.get("first_name")),
            "last_name": self._empty_to_none(user.get("last_name")),
            "email": self._empty_to_none(user["email"]),
        }
        workspace_user = WorkspaceUser.objects.get_one_or_none(
            filters={"uuid": dm_filters.EQ(user_uuid)},
        )
        if workspace_user is not None:
            workspace_user.update_dm(values=values)
            workspace_user.save()
            return workspace_user

        workspace_user = WorkspaceUser(
            uuid=user_uuid,
            **values,
        )
        workspace_user.insert()
        return workspace_user

    @staticmethod
    def _empty_to_none(value):
        if value == "":
            return None
        return value


EXTERNAL_ACCOUNT_SETTINGS_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(ZulipExternalAccountKind),
    types_dynamic.KindModelType(IamExternalAccountKind),
)


class ExternalAccount(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_external_accounts"

    user_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    server_url = properties.property(
        types.Url(),
        required=True,
    )
    account_type = properties.property(
        types.Enum([account_type.value for account_type in ExternalAccountType]),
        default=ExternalAccountType.ZULIP.value,
    )
    status = properties.property(
        types.Enum([status.value for status in ExternalAccountStatus]),
        default=ExternalAccountStatus.NEW.value,
    )
    account_settings = properties.property(
        EXTERNAL_ACCOUNT_SETTINGS_TYPE,
        required=True,
    )

    def user_sync(self):
        return self.account_settings.sync_users(external_account=self)


class ExternalAccountUserSync(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_external_account_user_syncs"
    SYNC_INTERVAL_MINUTES = 5

    account_type = properties.property(
        types.Enum([account_type.value for account_type in ExternalAccountType]),
        default=ExternalAccountType.ZULIP.value,
    )
    server_url = properties.property(
        types.Url(),
        required=True,
    )
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    last_synced_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=None,
    )
    next_sync_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    def get_external_account(self):
        if self.external_account_uuid is None:
            return self._select_external_account()
        return ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(self.external_account_uuid),
                "project_id": dm_filters.EQ(self.project_id),
            },
        )

    def _select_external_account(self):
        accounts = ExternalAccount.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(self.project_id),
                "account_type": dm_filters.EQ(self.account_type),
                "server_url": dm_filters.EQ(self.server_url),
            },
            order_by={"created_at": "asc", "uuid": "asc"},
        )
        for account in accounts:
            self.update_dm(
                values={"external_account_uuid": account.uuid},
            )
            self.save()
            return account
        return None

    def _update_next_sync_at(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.update_dm(
            values={
                "last_synced_at": now,
                "next_sync_at": (
                    now
                    + datetime.timedelta(
                        minutes=self.SYNC_INTERVAL_MINUTES,
                    )
                ),
            },
        )
        self.save()

    def sync(self):
        external_account = self.get_external_account()
        if external_account is None:
            self._update_next_sync_at()
            return
        users = external_account.user_sync()
        self._update_next_sync_at()
        return users


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


WORKSPACE_EVENT_SCHEMA_VERSION = 1
WORKSPACE_EVENT_OBJECT_TYPES = (
    "message",
    "stream",
    "stream_binding",
    "topic",
    "user",
    "folder",
    "folder_item",
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
    base.WorkspaceSourceBase,
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
