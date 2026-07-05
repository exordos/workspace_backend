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
import uuid as sys_uuid
from types import SimpleNamespace
from unittest import mock

from restalchemy.api import routes as ra_routes

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes
from workspace.messenger_api.dm import models


def test_external_account_stores_zulip_credentials_kind():
    account = models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        account_settings=models.ZulipExternalAccountKind(
            login="user@example.com",
            server_url="https://zulip.example.com",
            token="zulip-token",
        ),
    )

    data = account._get_prepared_data()

    assert "project_id" in data
    assert data["account_type"] == "zulip"
    assert data["status"] == "new"
    assert "external_user_id" not in data
    assert data["account_settings"] == {
        "kind": "zulip",
        "login": "user@example.com",
        "server_url": "https://zulip.example.com",
        "token": "zulip-token",
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
    assert data["is_synced"] is False
    assert data["last_synced_at"] is None
    assert before <= next_sync_at <= after


def test_external_account_controller_uses_workspace_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    controller = controllers.ExternalAccountController(
        SimpleNamespace(
            context=SimpleNamespace(
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
        SimpleNamespace(
            context=SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )
    account_settings = models.ZulipExternalAccountKind(
        login="user@example.com",
        server_url="https://zulip.example.com",
        token="zulip-token",
    )
    created_sync = {}

    class FakeExternalAccountUserSync:
        objects = SimpleNamespace(
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
        client.get_current_user_with_api_key.return_value = {"user_id": 42}

        account = controller.create(
            account_settings=account_settings,
        )

    client_cls.assert_called_once_with(endpoint="https://zulip.example.com")
    client.get_current_user_with_api_key.assert_called_once_with(
        login="user@example.com",
        token="zulip-token",
    )
    assert account.project_id == project_id
    assert account.user_uuid == user_uuid
    assert account.status == "new"
    assert account.account_settings is account_settings
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
        SimpleNamespace(
            context=SimpleNamespace(
                project_id=project_id,
                user_uuid=user_uuid,
            ),
        ),
    )
    account_settings = models.ZulipExternalAccountKind(
        login="user@example.com",
        server_url="https://zulip.example.com",
        token="zulip-token",
    )

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
        objects = SimpleNamespace(
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
        client.get_current_user_with_api_key.return_value = {"user_id": 42}

        account = controller.create(
            account_settings=account_settings,
        )

    assert account.project_id == project_id
    assert account.user_uuid == user_uuid
    insert.assert_called_once_with()
    assert existing_sync.update_values == {
        "external_account_uuid": account.uuid,
    }
    assert existing_sync.updated is True
    FakeExternalAccountUserSync.objects.get_one_or_none.assert_called_once()


def test_external_account_route_allows_delete():
    assert routes.ApiEndpointRoute.external_accounts is routes.ExternalAccountRoute
    assert ra_routes.CREATE in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.FILTER in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.GET in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.UPDATE in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.DELETE in routes.ExternalAccountRoute.__allow_methods__
