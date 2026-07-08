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
import types
import uuid as sys_uuid
from unittest import mock

import pytest
from restalchemy.common import exceptions as ra_exc
from restalchemy.api import routes as ra_routes
from restalchemy.storage.sql import orm

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes
from workspace.messenger_api.dm import models


def _zulip_account_settings(user_info=None):
    user_info = user_info or _zulip_user_info()
    return models.ZulipExternalAccountKind(
        credentials=models.ZulipExternalAccountCredentialsKind(
            login="user@example.com",
            token="zulip-token",
        ),
        user_info=user_info,
    )


def _zulip_account_settings_without_user_info():
    return models.ZulipExternalAccountKind(
        credentials=models.ZulipExternalAccountCredentialsKind(
            login="user@example.com",
            token="zulip-token",
        ),
    )


def _zulip_profile():
    return {
        "email": "user32@zulip.genesis-core.tech",
        "user_id": 32,
        "avatar_version": 2,
        "is_admin": False,
        "is_owner": False,
        "is_guest": False,
        "role": 400,
        "is_bot": False,
        "full_name": "Phoenix",
        "timezone": "Europe/Moscow",
        "is_active": True,
        "date_joined": "2026-05-14T22:36+00:00",
        "delivery_email": "cassi+phoenix@genesis-corporation.ru",
        "avatar_url": (
            "/user_avatars/2/"
            "c8fa2d4dcb2a15d1e57b80b0f904f44110ddeea7.png"
        ),
    }


def _zulip_user_info(profile=None):
    profile = profile or _zulip_profile()
    return models.ZulipExternalAccountUserInfoKind(
        email=profile["email"],
        user_id=profile["user_id"],
        avatar_version=profile["avatar_version"],
        is_admin=profile["is_admin"],
        is_owner=profile["is_owner"],
        is_guest=profile["is_guest"],
        role=profile["role"],
        is_bot=profile["is_bot"],
        full_name=profile["full_name"],
        timezone=profile["timezone"],
        is_active=profile["is_active"],
        date_joined=profile["date_joined"],
        delivery_email=profile["delivery_email"],
        avatar_url=profile["avatar_url"],
    )


def _iam_account_settings():
    return models.IamExternalAccountKind(
        credentials=models.IamExternalAccountCredentialsKind(
            username="admin",
            access_token="iam-token",
        ),
    )


def _iam_user():
    return {
        "uuid": "00000000-0000-0000-0000-000000000000",
        "username": "admin",
        "description": "This is Admin",
        "created_at": "2025-08-12T10:13:49.963391Z",
        "updated_at": "2025-08-20T07:23:41.032313Z",
        "status": "ACTIVE",
        "type": "user",
        "first_name": "Admin",
        "last_name": "Admin",
        "surname": "",
        "phone": "",
        "email": "admin@genesis-core.tech",
        "email_verified": False,
        "otp_enabled": False,
    }


def test_external_account_stores_zulip_credentials_kind():
    account = models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        account_settings=_zulip_account_settings(),
    )

    data = account._get_prepared_data()

    assert "project_id" in data
    assert data["account_type"] == "zulip"
    assert data["status"] == "new"
    assert "external_user_id" not in data
    assert data["account_settings"] == {
        "kind": "zulip",
        "credentials": {
            "kind": "zulip",
            "login": "user@example.com",
            "token": "zulip-token",
        },
        "user_info": {
            "kind": "zulip",
            "email": "user32@zulip.genesis-core.tech",
            "user_id": 32,
            "avatar_version": 2,
            "is_admin": False,
            "is_owner": False,
            "is_guest": False,
            "role": 400,
            "is_bot": False,
            "full_name": "Phoenix",
            "timezone": "Europe/Moscow",
            "is_active": True,
            "date_joined": "2026-05-14T22:36+00:00",
            "delivery_email": "cassi+phoenix@genesis-corporation.ru",
            "avatar_url": (
                "/user_avatars/2/"
                "c8fa2d4dcb2a15d1e57b80b0f904f44110ddeea7.png"
            ),
        },
    }


def test_external_account_settings_parses_zulip_create_payload():
    account_settings = (
        models.EXTERNAL_ACCOUNT_SETTINGS_TYPE.from_simple_type(
            {
                "kind": "zulip",
                "credentials": {
                    "kind": "zulip",
                    "login": "infra@genesis-corporation.ru",
                    "token": "zulip-token",
                },
            },
        )
    )

    assert account_settings.credentials.login == (
        "infra@genesis-corporation.ru"
    )
    assert account_settings.credentials.token == "zulip-token"
    assert account_settings.user_info is None


def test_external_account_stores_iam_credentials_kind():
    account = models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        server_url="https://iam.example.com",
        account_type="iam",
        account_settings=_iam_account_settings(),
    )

    data = account._get_prepared_data()

    assert data["account_type"] == "iam"
    assert data["account_settings"] == {
        "kind": "iam",
        "credentials": {
            "kind": "iam",
            "username": "admin",
            "access_token": "iam-token",
        },
    }


def test_external_account_user_sync_stores_sync_state():
    project_id = sys_uuid.uuid4()
    before = datetime.datetime.now(datetime.timezone.utc)
    user_sync = models.ExternalAccountUserSync(
        project_id=project_id,
        server_url="https://zulip.example.com",
    )
    after = datetime.datetime.now(datetime.timezone.utc)

    data = user_sync._get_prepared_data()
    next_sync_at = data["next_sync_at"]
    if isinstance(next_sync_at, str):
        next_sync_at = datetime.datetime.fromisoformat(
            next_sync_at.replace("Z", "+00:00"),
        )
    if next_sync_at.tzinfo is None:
        next_sync_at = next_sync_at.replace(tzinfo=datetime.timezone.utc)

    assert data["project_id"] == str(project_id)
    assert data["account_type"] == "zulip"
    assert data["server_url"] == "https://zulip.example.com"
    assert data["external_account_uuid"] is None
    assert data["last_synced_at"] is None
    assert before <= next_sync_at <= after


def test_external_account_user_sync_stores_iam_type():
    user_sync = models.ExternalAccountUserSync(
        project_id=sys_uuid.uuid4(),
        account_type="iam",
        server_url="https://iam.example.com",
    )

    data = user_sync._get_prepared_data()

    assert data["account_type"] == "iam"
    assert data["server_url"] == "https://iam.example.com"


def test_zulip_external_account_kind_gets_users():
    account_settings = _zulip_account_settings_without_user_info()
    external_account = models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        account_settings=account_settings,
    )
    zulip_users = [
        {
            "user_id": 42,
            "email": "ada@example.com",
            "delivery_email": None,
            "full_name": "Ada Lovelace",
        },
    ]

    with mock.patch.object(
        models.zulip_client,
        "ZulipClient",
    ) as client_cls:
        client = client_cls.return_value
        client.get_users_with_api_key.return_value = zulip_users

        users = account_settings._get_zulip_users(
            external_account=external_account,
        )

    client_cls.assert_called_once_with(endpoint="https://zulip.example.com")
    client.get_users_with_api_key.assert_called_once_with(
        login="user@example.com",
        token="zulip-token",
    )
    assert users == zulip_users


def test_external_account_user_sync_delegates_to_settings_sync_users():
    account_settings = _zulip_account_settings()
    account_settings.sync_users = mock.Mock(return_value=["user"])
    account = models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        account_settings=account_settings,
    )

    users = account.user_sync()

    assert users == ["user"]
    account_settings.sync_users.assert_called_once_with(
        external_account=account,
    )


def test_external_account_sync_creates_external_account_for_zulip_user():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    workspace_user_uuid = sys_uuid.uuid4()
    provider = models.ExternalAccount(
        project_id=project_id,
        user_uuid=user_uuid,
        server_url="https://zulip.example.com",
        account_settings=_zulip_account_settings(),
    )
    provider.save = mock.Mock()
    account_settings = provider.account_settings

    with (
        mock.patch.object(
            account_settings,
            "_get_zulip_users",
            return_value=[_zulip_profile()],
        ),
        mock.patch.object(
            account_settings,
            "_get_external_accounts_by_server_url_and_user_id",
            return_value={},
        ) as get_external_accounts,
        mock.patch.object(
            account_settings,
            "_get_or_create_workspace_user",
            return_value=types.SimpleNamespace(uuid=workspace_user_uuid),
        ) as get_workspace_user,
        mock.patch.object(models.ExternalAccount, "insert") as insert_account,
    ):
        accounts = account_settings.sync_users(external_account=provider)

    assert len(accounts) == 1
    synced_account = accounts[0]
    assert synced_account.project_id == project_id
    assert synced_account.user_uuid == workspace_user_uuid
    assert synced_account.server_url == "https://zulip.example.com"
    assert synced_account.account_type == "zulip"
    assert synced_account.status == "active"
    assert synced_account.account_settings.credentials is None
    assert synced_account.account_settings.user_info.user_id == 32
    assert synced_account.account_settings.user_info.email == (
        "user32@zulip.genesis-core.tech"
    )
    assert synced_account.account_settings.user_info.full_name == "Phoenix"
    assert provider.status == "active"
    get_external_accounts.assert_called_once()
    get_workspace_user.assert_called_once()
    insert_account.assert_called_once_with()
    provider.save.assert_called_once_with()


def test_external_account_sync_updates_external_account_by_server_url_and_user_id():
    project_id = sys_uuid.uuid4()
    workspace_user_uuid = sys_uuid.uuid4()
    provider = models.ExternalAccount(
        project_id=project_id,
        user_uuid=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        account_settings=_zulip_account_settings(),
    )
    provider.save = mock.Mock()
    account_settings = provider.account_settings
    existing_user_info = models.ZulipExternalAccountUserInfoKind(
        email="old@example.com",
        user_id=32,
        avatar_version=1,
        is_admin=True,
        is_owner=True,
        is_guest=True,
        role=100,
        is_bot=True,
        full_name="Old Name",
        timezone="UTC",
        is_active=False,
        date_joined="2026-01-01T00:00+00:00",
        delivery_email=None,
        avatar_url=None,
    )
    existing_account = models.ExternalAccount(
        project_id=project_id,
        user_uuid=workspace_user_uuid,
        server_url="https://zulip.example.com",
        account_settings=models.ZulipExternalAccountKind(
            credentials=None,
            user_info=existing_user_info,
        ),
    )
    existing_account.save = mock.Mock()
    with (
        mock.patch.object(
            account_settings,
            "_get_zulip_users",
            return_value=[_zulip_profile()],
        ),
        mock.patch.object(
            account_settings,
            "_get_external_accounts_by_server_url_and_user_id",
            return_value={
                ("https://zulip.example.com", 32): existing_account,
            },
        ) as get_external_accounts,
        mock.patch.object(
            account_settings,
            "_update_zulip_external_account",
            return_value=existing_account,
        ) as update_external_account,
    ):
        accounts = account_settings.sync_users(external_account=provider)

    assert accounts == [existing_account]
    assert provider.status == "active"
    get_external_accounts.assert_called_once()
    update_external_account.assert_called_once()
    call_kwargs = update_external_account.call_args.kwargs
    assert call_kwargs["external_account"] is existing_account
    assert call_kwargs["user_info"].user_id == 32
    provider.save.assert_called_once_with()


def test_external_account_gets_external_accounts_by_server_url_and_zulip_user_id():
    project_id = sys_uuid.uuid4()
    user_info = models.ZulipExternalAccountUserInfoKind(
        email="user32@zulip.genesis-core.tech",
        user_id=32,
        avatar_version=2,
        is_admin=False,
        is_owner=False,
        is_guest=False,
        role=400,
        is_bot=False,
        full_name="Phoenix",
        timezone="Europe/Moscow",
        is_active=True,
        date_joined="2026-05-14T22:36+00:00",
        delivery_email="cassi+phoenix@genesis-corporation.ru",
        avatar_url=(
            "/user_avatars/2/"
            "c8fa2d4dcb2a15d1e57b80b0f904f44110ddeea7.png"
        ),
    )
    account = models.ExternalAccount(
        project_id=project_id,
        user_uuid=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        account_settings=models.ZulipExternalAccountKind(
            credentials=None,
            user_info=user_info,
        ),
    )
    other_server_account = models.ExternalAccount(
        project_id=project_id,
        user_uuid=sys_uuid.uuid4(),
        server_url="https://other-zulip.example.com",
        account_settings=models.ZulipExternalAccountKind(
            credentials=None,
            user_info=models.ZulipExternalAccountUserInfoKind(
                email="user32@other-zulip.example.com",
                user_id=32,
                avatar_version=2,
                is_admin=False,
                is_owner=False,
                is_guest=False,
                role=400,
                is_bot=False,
                full_name="Phoenix",
                timezone="Europe/Moscow",
                is_active=True,
                date_joined="2026-05-14T22:36+00:00",
                delivery_email=None,
                avatar_url=None,
            ),
        ),
    )
    provider = models.ExternalAccount(
        project_id=project_id,
        user_uuid=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        account_settings=_zulip_account_settings(),
    )

    with mock.patch.object(
        orm.ObjectCollection,
        "get_all",
        return_value=[account, other_server_account],
    ) as get_all:
        accounts = (
            provider.account_settings
            ._get_external_accounts_by_server_url_and_user_id(
                external_account=provider,
            )
        )

    assert accounts == {("https://zulip.example.com", 32): account}
    get_all.assert_called_once()
    assert get_all.call_args.kwargs["order_by"] == {
        "created_at": "asc",
        "uuid": "asc",
    }
    filters = get_all.call_args.kwargs["filters"]
    assert filters["project_id"].value == project_id
    assert filters["account_type"].value == "zulip"
    assert filters["server_url"].value == "https://zulip.example.com"


def test_external_account_updates_zulip_external_account():
    project_id = sys_uuid.uuid4()
    workspace_user_uuid = sys_uuid.uuid4()
    provider = models.ExternalAccount(
        project_id=project_id,
        user_uuid=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        account_settings=_zulip_account_settings(),
    )
    external_account = models.ExternalAccount(
        project_id=project_id,
        user_uuid=workspace_user_uuid,
        server_url="https://zulip.example.com",
        account_settings=models.ZulipExternalAccountKind(
            credentials=None,
            user_info=_zulip_user_info(),
        ),
    )
    external_account.save = mock.Mock()
    user_info = provider.account_settings._get_zulip_user_info(
        user=_zulip_profile(),
    )

    updated_account = provider.account_settings._update_zulip_external_account(
        external_account=external_account,
        user_info=user_info,
    )

    assert updated_account is external_account
    assert external_account.account_settings.user_info is user_info
    assert external_account.status == "active"
    external_account.save.assert_called_once_with()


def test_iam_external_account_kind_syncs_users_and_marks_account_active():
    account = models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        server_url="https://iam.example.com",
        account_type="iam",
        account_settings=_iam_account_settings(),
    )
    account.save = mock.Mock()
    workspace_user = models.WorkspaceUser(
        uuid=sys_uuid.UUID("00000000-0000-0000-0000-000000000000"),
        username="admin",
    )

    with (
        mock.patch.object(
            account.account_settings,
            "_get_iam_users",
            return_value=[_iam_user()],
        ) as get_iam_users,
        mock.patch.object(
            account.account_settings,
            "_sync_iam_user",
            return_value=workspace_user,
        ) as sync_iam_user,
    ):
        users = account.account_settings.sync_users(external_account=account)

    assert users == [workspace_user]
    assert account.status == "active"
    get_iam_users.assert_called_once_with(external_account=account)
    sync_iam_user.assert_called_once_with(user=_iam_user())
    account.save.assert_called_once_with()


def test_iam_external_account_kind_creates_workspace_user():
    account_settings = _iam_account_settings()

    with (
        mock.patch.object(
            orm.ObjectCollection,
            "get_one_or_none",
            return_value=None,
        ) as get_one_or_none,
        mock.patch.object(models.WorkspaceUser, "insert") as insert,
    ):
        workspace_user = account_settings._sync_iam_user(user=_iam_user())

    assert workspace_user.uuid == sys_uuid.UUID(
        "00000000-0000-0000-0000-000000000000",
    )
    assert workspace_user.username == "admin"
    assert workspace_user.source == "iam"
    assert workspace_user.status == "active"
    assert workspace_user.first_name == "Admin"
    assert workspace_user.last_name == "Admin"
    assert workspace_user.email == "admin@genesis-core.tech"
    filters = get_one_or_none.call_args.kwargs["filters"]
    assert filters["uuid"].value == sys_uuid.UUID(
        "00000000-0000-0000-0000-000000000000",
    )
    insert.assert_called_once_with()


def test_external_account_normalizes_empty_zulip_user_fields():
    account_settings = _zulip_account_settings()
    user = _zulip_profile()
    user["timezone"] = ""
    user["delivery_email"] = ""
    user["avatar_url"] = ""

    user_info = account_settings._get_zulip_user_info(user=user)

    assert user_info.timezone == "Europe/Moscow"
    assert user_info.delivery_email is None
    assert user_info.avatar_url is None


def test_external_account_uses_workspace_user_by_email_for_new_zulip_account():
    account_settings = _zulip_account_settings()
    workspace_user = models.WorkspaceUser(
        uuid=sys_uuid.uuid4(),
        username="cassi+phoenix@genesis-corporation.ru",
        email="cassi+phoenix@genesis-corporation.ru",
    )

    with mock.patch.object(
        orm.ObjectCollection,
        "get_all",
        return_value=[workspace_user],
    ) as get_all:
        found_user = account_settings._get_or_create_workspace_user(
            user=_zulip_profile(),
        )

    assert found_user is workspace_user
    filters = get_all.call_args.kwargs["filters"]
    assert filters["email"].value == "cassi+phoenix@genesis-corporation.ru"


def test_external_account_creates_workspace_user_by_email_for_new_zulip_account():
    account_settings = _zulip_account_settings()

    with (
        mock.patch.object(
            orm.ObjectCollection,
            "get_all",
            return_value=[],
        ) as get_all,
        mock.patch.object(models.WorkspaceUser, "insert") as insert,
    ):
        workspace_user = account_settings._get_or_create_workspace_user(
            user=_zulip_profile(),
        )

    assert workspace_user.username == "Phoenix"
    assert workspace_user.email == "cassi+phoenix@genesis-corporation.ru"
    assert workspace_user.source == "zulip"
    filters = get_all.call_args.kwargs["filters"]
    assert filters["email"].value == "cassi+phoenix@genesis-corporation.ru"
    insert.assert_called_once_with()


def test_external_account_creates_workspace_user_without_empty_email():
    account_settings = _zulip_account_settings()
    user = _zulip_profile()
    user["delivery_email"] = ""

    with mock.patch.object(models.WorkspaceUser, "insert") as insert:
        workspace_user = account_settings._get_or_create_workspace_user(
            user=user,
        )

    assert workspace_user.username == "Phoenix"
    assert workspace_user.email is None
    insert.assert_called_once_with()


def test_external_account_user_sync_updates_schedule_after_sync():
    account = types.SimpleNamespace(user_sync=mock.Mock(return_value=["user"]))
    user_sync = models.ExternalAccountUserSync(
        project_id=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        external_account_uuid=sys_uuid.uuid4(),
    )
    save = mock.Mock()
    user_sync.save = save

    before = datetime.datetime.now(datetime.timezone.utc)
    with mock.patch.object(
        user_sync,
        "get_external_account",
        mock.Mock(return_value=account),
    ):
        users = user_sync.sync()
    after = datetime.datetime.now(datetime.timezone.utc)

    assert users == ["user"]
    account.user_sync.assert_called_once_with()
    assert before <= user_sync.last_synced_at <= after
    assert user_sync.next_sync_at == (
        user_sync.last_synced_at
        + datetime.timedelta(
            minutes=models.ExternalAccountUserSync.SYNC_INTERVAL_MINUTES,
        )
    )
    save.assert_called_once_with()


def test_external_account_user_sync_updates_schedule_without_external_account():
    user_sync = models.ExternalAccountUserSync(
        project_id=sys_uuid.uuid4(),
        account_type="iam",
        server_url="https://iam.example.com",
    )
    save = mock.Mock()
    user_sync.save = save

    before = datetime.datetime.now(datetime.timezone.utc)
    with mock.patch.object(
        user_sync,
        "get_external_account",
        mock.Mock(return_value=None),
    ):
        users = user_sync.sync()
    after = datetime.datetime.now(datetime.timezone.utc)

    assert users is None
    assert before <= user_sync.last_synced_at <= after
    assert user_sync.next_sync_at == (
        user_sync.last_synced_at
        + datetime.timedelta(
            minutes=models.ExternalAccountUserSync.SYNC_INTERVAL_MINUTES,
        )
    )
    save.assert_called_once_with()


def test_external_account_controller_uses_workspace_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )

    filters = controller.get_autofilters()
    values = controller.get_autovalues()

    assert filters["project_id"].value == project_id
    assert filters["user_uuid"].value == user_uuid
    assert values["project_id"] == project_id
    assert values["user_uuid"] == user_uuid


def test_external_account_controller_create_fetches_zulip_profile():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )
    account_settings = _zulip_account_settings_without_user_info()
    created_sync = {}

    class FakeExternalAccountUserSync:
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=None),
        )

        def __init__(self, **kwargs):
            self.values = kwargs

        def insert(self):
            created_sync.update(self.values)

    with (
        mock.patch.object(
            controllers.zulip_client,
            "ZulipClient",
        ) as client_cls,
        mock.patch.object(models.ExternalAccount, "insert") as insert,
        mock.patch.object(
            models,
            "ExternalAccountUserSync",
            FakeExternalAccountUserSync,
        ),
    ):
        client = client_cls.return_value
        client.get_current_user_with_api_key.return_value = _zulip_profile()

        account = controller.create(
            server_url="https://zulip.example.com",
            account_settings=account_settings,
        )

    client_cls.assert_called_once_with(endpoint="https://zulip.example.com")
    client.get_current_user_with_api_key.assert_called_once_with(
        login="user@example.com",
        token="zulip-token",
    )
    assert account.project_id == project_id
    assert account.user_uuid == user_uuid
    assert account.server_url == "https://zulip.example.com"
    assert account.status == "new"
    assert account.account_settings is account_settings
    assert account.account_settings.user_info.email == (
        "user32@zulip.genesis-core.tech"
    )
    assert account.account_settings.user_info.user_id == 32
    assert account.account_settings.user_info.avatar_version == 2
    assert account.account_settings.user_info.is_admin is False
    assert account.account_settings.user_info.is_owner is False
    assert account.account_settings.user_info.is_guest is False
    assert account.account_settings.user_info.role == 400
    assert account.account_settings.user_info.is_bot is False
    assert account.account_settings.user_info.full_name == "Phoenix"
    assert account.account_settings.user_info.timezone == "Europe/Moscow"
    assert account.account_settings.user_info.is_active is True
    assert account.account_settings.user_info.date_joined == (
        "2026-05-14T22:36+00:00"
    )
    assert account.account_settings.user_info.delivery_email == (
        "cassi+phoenix@genesis-corporation.ru"
    )
    assert account.account_settings.user_info.avatar_url == (
        "/user_avatars/2/"
        "c8fa2d4dcb2a15d1e57b80b0f904f44110ddeea7.png"
    )
    insert.assert_called_once_with()
    assert created_sync == {
        "project_id": project_id,
        "account_type": "zulip",
        "server_url": "https://zulip.example.com",
        "external_account_uuid": account.uuid,
    }
    FakeExternalAccountUserSync.objects.get_one_or_none.assert_called_once()


def test_external_account_controller_create_binds_existing_user_sync():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )
    account_settings = _zulip_account_settings_without_user_info()

    class ExistingSync:
        def __init__(self):
            self.uuid = sys_uuid.uuid4()
            self.external_account_uuid = None
            self.update_values = None
            self.updated = False

        def update_dm(self, values):
            self.update_values = values
            self.external_account_uuid = values["external_account_uuid"]

        def update(self):
            self.updated = True

    existing_sync = ExistingSync()

    class FakeExternalAccountUserSync:
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=existing_sync),
        )

        def __init__(self, **kwargs):
            raise AssertionError("Sync already exists")

    with (
        mock.patch.object(
            controllers.zulip_client,
            "ZulipClient",
        ) as client_cls,
        mock.patch.object(models.ExternalAccount, "insert") as insert,
        mock.patch.object(
            models,
            "ExternalAccountUserSync",
            FakeExternalAccountUserSync,
        ),
    ):
        client = client_cls.return_value
        client.get_current_user_with_api_key.return_value = _zulip_profile()

        account = controller.create(
            server_url="https://zulip.example.com",
            account_settings=account_settings,
        )

    assert account.project_id == project_id
    assert account.user_uuid == user_uuid
    assert account.server_url == "https://zulip.example.com"
    insert.assert_called_once_with()
    assert existing_sync.update_values == {
        "external_account_uuid": account.uuid,
    }
    assert existing_sync.updated is True
    FakeExternalAccountUserSync.objects.get_one_or_none.assert_called_once()


def test_external_account_controller_create_rejects_zulip_user_info():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )
    account_settings = _zulip_account_settings()

    with mock.patch.object(
        controllers.zulip_client,
        "ZulipClient",
    ) as client_cls:
        with mock.patch.object(models.ExternalAccount, "insert") as insert:
            with pytest.raises(ra_exc.ValidationErrorException):
                controller.create(
                    server_url="https://zulip.example.com",
                    account_settings=account_settings,
                )

    client_cls.assert_not_called()
    insert.assert_not_called()


def test_external_account_controller_create_rejects_iam_account():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )

    with mock.patch.object(
        controllers.zulip_client,
        "ZulipClient",
    ) as client_cls:
        with mock.patch.object(models.ExternalAccount, "insert") as insert:
            with mock.patch.object(
                models,
                "ExternalAccountUserSync",
            ) as user_sync_cls:
                with pytest.raises(ra_exc.ValidationErrorException):
                    controller.create(
                        server_url=(
                            "https://exordos.com/api/core/v1/iam/clients/"
                            "default"
                        ),
                        account_settings=_iam_account_settings(),
                    )

    client_cls.assert_not_called()
    insert.assert_not_called()
    user_sync_cls.assert_not_called()


def test_external_account_controller_update_rejects_zulip_user_info():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )
    account = models.ExternalAccount(
        project_id=project_id,
        user_uuid=user_uuid,
        server_url="https://zulip.example.com",
        account_settings=_zulip_account_settings(),
    )
    account.update = mock.Mock()
    account_settings = _zulip_account_settings()

    with mock.patch.object(controller, "get", return_value=account):
        with mock.patch.object(
            controllers.zulip_client,
            "ZulipClient",
        ) as client_cls:
            with pytest.raises(ra_exc.ValidationErrorException):
                controller.update(
                    uuid=account.uuid,
                    account_settings=account_settings,
                )

    client_cls.assert_not_called()
    account.update.assert_not_called()
    assert account.account_settings is not account_settings


def test_external_account_controller_update_rejects_iam_account():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )
    account = models.ExternalAccount(
        project_id=project_id,
        user_uuid=user_uuid,
        server_url="https://zulip.example.com",
        account_settings=_zulip_account_settings(),
    )
    account.update = mock.Mock()
    account_settings = _iam_account_settings()

    with mock.patch.object(controller, "get", return_value=account):
        with pytest.raises(ra_exc.ValidationErrorException):
            controller.update(
                uuid=account.uuid,
                account_settings=account_settings,
            )

    account.update.assert_not_called()
    assert account.account_settings is not account_settings


def test_external_account_controller_update_rejects_other_zulip_user():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )
    account = models.ExternalAccount(
        project_id=project_id,
        user_uuid=user_uuid,
        server_url="https://zulip.example.com",
        account_settings=_zulip_account_settings(),
    )
    account.update = mock.Mock()
    new_profile = _zulip_profile()
    new_profile["user_id"] = 64
    new_settings = _zulip_account_settings_without_user_info()

    with mock.patch.object(controller, "get", return_value=account):
        with mock.patch.object(
            controllers.zulip_client,
            "ZulipClient",
        ) as client_cls:
            client = client_cls.return_value
            client.get_current_user_with_api_key.return_value = new_profile

            with pytest.raises(ra_exc.ValidationErrorException):
                controller.update(
                    uuid=account.uuid,
                    account_settings=new_settings,
                )

    client_cls.assert_called_once_with(endpoint="https://zulip.example.com")
    client.get_current_user_with_api_key.assert_called_once_with(
        login="user@example.com",
        token="zulip-token",
    )
    account.update.assert_not_called()
    assert account.account_settings is not new_settings


def test_external_account_controller_update_accepts_same_zulip_user():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )
    account = models.ExternalAccount(
        project_id=project_id,
        user_uuid=user_uuid,
        server_url="https://zulip.example.com",
        account_settings=_zulip_account_settings(),
    )
    account.update = mock.Mock()
    new_profile = _zulip_profile()
    new_profile["full_name"] = "Phoenix Updated"
    new_settings = _zulip_account_settings_without_user_info()

    with mock.patch.object(controller, "get", return_value=account):
        with mock.patch.object(
            controllers.zulip_client,
            "ZulipClient",
        ) as client_cls:
            client = client_cls.return_value
            client.get_current_user_with_api_key.return_value = new_profile

            updated_account = controller.update(
                uuid=account.uuid,
                account_settings=new_settings,
            )

    assert updated_account is account
    client_cls.assert_called_once_with(endpoint="https://zulip.example.com")
    client.get_current_user_with_api_key.assert_called_once_with(
        login="user@example.com",
        token="zulip-token",
    )
    assert account.account_settings is new_settings
    assert account.account_settings.user_info.user_id == 32
    assert account.account_settings.user_info.full_name == "Phoenix Updated"
    account.update.assert_called_once_with()


def test_external_account_route_allows_delete():
    assert routes.ApiEndpointRoute.external_accounts is routes.ExternalAccountRoute
    assert ra_routes.CREATE in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.FILTER in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.GET in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.UPDATE in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.DELETE in routes.ExternalAccountRoute.__allow_methods__
