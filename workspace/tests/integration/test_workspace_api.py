# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.


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
