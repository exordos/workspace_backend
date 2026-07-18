# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

import pytest

from workspace.messenger_api.api import routes
from workspace.messenger_api.dm import models


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
    assert routes.ApiEndpointRoute.external_accounts is routes.ExternalAccountRoute
    assert routes.ExternalAccountRoute.__controller__.__name__ == (
        "ExternalAccountController"
    )
