# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import pytest

from workspace.messenger_api.api import controllers


@pytest.fixture(autouse=True)
def iam_profiles(monkeypatch):
    profiles = {}

    class Client:
        def get_user(self, user_uuid, token):
            assert token == "integration-test-token"
            profile = profiles.setdefault(
                str(user_uuid),
                {
                    "username": f"user-{user_uuid}",
                    "first_name": "",
                    "last_name": "",
                    "email": "",
                    "surname": "",
                    "phone": None,
                    "description": "",
                },
            )
            return profile.copy()

        def update_user(self, user_uuid, token, values):
            self.get_user(user_uuid, token)
            profiles[str(user_uuid)].update(values)
            return self.get_user(user_uuid, token)

    monkeypatch.setattr(controllers.MeController, "_iam_client", lambda self: Client())


def test_only_workspace_v1_contract_is_exposed(workspace_api):
    api = workspace_api
    assert api.get("/v1/").status_code == 200
    assert api.get("/v1/messenger/").status_code == 200
    assert api.get("/v1/mail/").status_code >= 400
    assert api.get("/v1/calendar/").status_code >= 400
    assert api.get("/v1/providers/").status_code >= 400
    assert api.get("/v1/events/").status_code == 200
    assert api.get("/v1/epoch/").status_code == 200
    assert api.get("/v1/messages/").status_code >= 400
    assert api.get("/v1/external_accounts/").status_code >= 400
    assert api.get("/v1/messenger/events/").status_code >= 400


def test_me_returns_current_iam_user(workspace_api):
    response = workspace_api.get("/v1/me/")

    assert response.status_code == 200
    assert response.json()["uuid"] == workspace_api.user_uuid
    assert response.json()["username"] == f"user-{workspace_api.user_uuid}"


def test_me_updates_iam_and_refreshes_real_workspace_projection(workspace_api):
    before = workspace_api.get("/v1/me/").json()
    values = {
        "username": "cassi-profile-integration",
        "surname": "Middle",
        "phone": "+123456789",
    }
    response = workspace_api.put("/v1/me/", json=values)
    assert response.status_code == 200
    profile = response.json()
    assert {name: profile[name] for name in values} == values
    assert profile["avatar"] == before["avatar"]
    assert profile["email"] == before["email"]
    fresh = workspace_api.get("/v1/me/").json()
    assert {name: fresh[name] for name in values} == values
    assert fresh["avatar"] == profile["avatar"]
    assert fresh["email"] == profile["email"]


def test_me_cannot_update_email(workspace_api):
    response = workspace_api.put("/v1/me/", json={"email": "changed@example.invalid"})
    assert response.status_code == 403
