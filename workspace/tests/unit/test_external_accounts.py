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

import uuid as sys_uuid
from types import SimpleNamespace

from restalchemy.api import routes as ra_routes

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes
from workspace.messenger_api.dm import models


def test_external_account_stores_zulip_credentials_kind():
    account = models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        external_user_id="42",
        account_settings=models.ZulipExternalAccountKind(
            login="user@example.com",
            token="zulip-token",
        ),
    )

    data = account._get_prepared_data()

    assert "project_id" in data
    assert data["account_type"] == "zulip"
    assert data["external_user_id"] == "42"
    assert data["account_settings"] == {
        "kind": "zulip",
        "login": "user@example.com",
        "token": "zulip-token",
    }


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


def test_external_account_route_does_not_allow_delete():
    assert routes.ApiEndpointRoute.external_accounts is routes.ExternalAccountRoute
    assert ra_routes.CREATE in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.FILTER in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.GET in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.UPDATE in routes.ExternalAccountRoute.__allow_methods__
    assert ra_routes.DELETE not in routes.ExternalAccountRoute.__allow_methods__
