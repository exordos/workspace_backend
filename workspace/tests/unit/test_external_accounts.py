# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api.api import controllers
from workspace.messenger_api.dm import models


def _zulip_credentials():
    return models.ZulipExternalAccountCredentialsKind(
        login="user@example.com",
        token="provider-token",
    )


def test_external_account_stores_zulip_credentials_without_backend_sync():
    account = models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        account_settings=models.ZulipExternalAccountKind(
            credentials=_zulip_credentials(),
        ),
    )

    data = account._get_prepared_data()

    assert data["account_type"] == "zulip"
    assert data["account_settings"] == {
        "kind": "zulip",
        "credentials": {
            "kind": "zulip",
            "login": "user@example.com",
            "token": "provider-token",
        },
        "user_info": None,
    }
    assert not hasattr(account, "user_sync")
    assert not hasattr(account.account_settings, "sync_users")


def test_external_account_settings_parses_zulip_create_payload():
    settings = models.EXTERNAL_ACCOUNT_SETTINGS_TYPE.from_simple_type(
        {
            "kind": "zulip",
            "credentials": {
                "kind": "zulip",
                "login": "user@example.com",
                "token": "provider-token",
            },
        }
    )

    assert settings.KIND == "zulip"
    assert settings.credentials.login == "user@example.com"
    assert settings.credentials.token == "provider-token"


def test_external_account_stores_iam_credentials_without_network_sync():
    settings = models.IamExternalAccountKind(
        credentials=models.IamExternalAccountCredentialsKind(
            username="service-user",
            access_token="access-token",
        ),
    )
    account = models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        server_url="https://iam.example.com",
        account_type="iam",
        account_settings=settings,
    )

    assert account.account_type == "iam"
    assert not hasattr(settings, "sync_users")


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


def test_external_account_controller_rejects_provider_user_info():
    settings = models.ZulipExternalAccountKind(
        credentials=_zulip_credentials(),
        user_info=models.ZulipExternalAccountUserInfoKind(
            email="remote@example.com",
            user_id=42,
            avatar_version=1,
            is_admin=False,
            is_owner=False,
            is_guest=False,
            role=400,
            is_bot=False,
            full_name="Remote User",
            timezone="UTC",
            is_active=True,
            date_joined="2026-01-01T00:00:00Z",
        ),
    )

    with pytest.raises(ra_exc.ValidationErrorException):
        controllers.ExternalAccountController._reject_user_info_from_api(
            settings,
        )
