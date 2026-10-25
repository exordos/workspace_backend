# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid
from unittest import mock

import pytest

from workspace.messenger_api.api import routes
from workspace.workspace_api.api import routes as workspace_routes
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import external_models


@pytest.mark.parametrize(
    "avatar",
    (
        "urn:gravatar:098f6bcd4621d373cade4e832627b4f6",
        f"urn:image:{sys_uuid.uuid4()}",
        "urn:url:https://cdn.example.com/avatar.png",
    ),
)
def test_workspace_user_avatar_accepts_supported_urns(avatar):
    user = models.WorkspaceUser(username="user", avatar=avatar)
    assert user.avatar == avatar


def test_workspace_gravatar_avatar_normalizes_email():
    assert models.build_workspace_user_gravatar_avatar(" User@Example.COM ") == (
        models.build_workspace_user_gravatar_avatar("user@example.com")
    )


def test_external_accounts_use_the_new_provider_neutral_messenger_route():
    assert (
        workspace_routes.MessengerRoute.external_accounts is routes.ExternalAccountRoute
    )
    assert routes.ExternalAccountRoute.__controller__.__name__ == (
        "ExternalAccountController"
    )


@pytest.mark.parametrize(
    "server_url,host",
    [
        ("https://ZULIP.example.com:9981/path/", "zulip.example.com"),
        ("http://localhost:21080", "localhost"),
        ("https://192.168.100.28/", "192.168.100.28"),
    ],
)
def test_external_account_host_is_read_from_provider(server_url, host):
    account = external_models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        owner_user_uuid=sys_uuid.uuid4(),
        provider="zulip",
        settings={"server_url": server_url},
    )
    provider = external_models.Provider(hostname=host)
    account.provider_uuid = provider.uuid
    with mock.patch.object(
        external_models.Provider,
        "objects",
        mock.Mock(get_one=mock.Mock(return_value=provider)),
    ) as objects:
        assert account.get_host() == host
    assert objects.get_one.call_args.kwargs["filters"]["uuid"].value == provider.uuid
