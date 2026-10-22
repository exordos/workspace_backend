# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Public user routes used during Android messenger startup."""

import uuid as sys_uuid
from unittest import mock

import pytest
import webob

from workspace.tests.integration import conftest
from workspace.workspace_api.api import app as workspace_app


@pytest.mark.parametrize("prefix", ("/v1/", "/v1/messenger/"))
def test_current_user_routes_materialize_the_authenticated_iam_identity(
    http_server, db, prefix
):
    user_uuid = sys_uuid.uuid4()
    client = conftest.ApiClient(http_server, user_uuid, sys_uuid.uuid4())

    response = client.get(f"{prefix}me/")

    assert response.status_code == 200, response.text
    profile = response.json()
    assert profile["uuid"] == str(user_uuid)
    assert profile["username"] == f"user-{user_uuid}"
    user = client.get(f"{prefix}users/{user_uuid}")
    assert user.status_code == 200, user.text
    assert user.json() == profile
    users = client.get(f"{prefix}users/", params={"username": profile["username"]})
    assert users.status_code == 200, users.text
    assert users.json() == [profile]

    other_user_uuid = sys_uuid.uuid4()
    other = client.get(f"{prefix}me/", user=other_user_uuid)
    assert other.status_code == 200, other.text
    assert other.json()["uuid"] == str(other_user_uuid)


def test_messenger_user_directory_alias_preserves_filters_and_pagination(api, db):
    for _ in range(2):
        user_uuid = sys_uuid.uuid4()
        conftest.seed_workspace_user(db, user_uuid, f"user-{user_uuid}")

    params = {"username": f"user-{user_uuid}"}
    canonical = api.get("/v1/users/", params=params)
    alias = api.get("/v1/messenger/users/", params=params)
    assert canonical.status_code == alias.status_code == 200
    assert alias.json() == canonical.json()
    assert [user["uuid"] for user in alias.json()] == [str(user_uuid)]

    params = {"page_limit": 1, "sort_key": "uuid", "sort_dir": "asc"}
    first_user_uuid = None
    for _ in range(2):
        canonical = api.get("/v1/users/", params=params)
        alias = api.get("/v1/messenger/users/", params=params)
        assert canonical.status_code == alias.status_code == 200
        assert alias.json() == canonical.json()
        assert len(alias.json()) == 1
        assert alias.json()[0]["uuid"] != first_user_uuid
        first_user_uuid = alias.json()[0]["uuid"]
        marker = canonical.headers["X-Pagination-Marker"]
        assert alias.headers["X-Pagination-Marker"] == marker
        params["page_marker"] = marker


def test_messenger_user_detail_alias_preserves_not_found(api):
    missing_user_uuid = sys_uuid.uuid4()

    canonical = api.get(f"/v1/users/{missing_user_uuid}")
    alias = api.get(f"/v1/messenger/users/{missing_user_uuid}")

    assert canonical.status_code == alias.status_code == 404
    assert alias.json() == canonical.json()


def test_directory_aliases_keep_current_user_timestamps_stable(api):
    # Force the actor onto the first page instead of depending on random UUIDs
    # and users left by earlier modules to hide repeated fixture writes.
    api.user_uuid = str(sys_uuid.UUID(int=0))
    params = {"page_limit": 1, "sort_key": "uuid", "sort_dir": "asc"}
    canonical = api.get("/v1/users/", params=params)
    alias = api.get("/v1/messenger/users/", params=params)

    assert canonical.status_code == alias.status_code == 200
    assert canonical.json()[0]["uuid"] == api.user_uuid
    assert alias.json() == canonical.json()


def test_messenger_user_presence_alias_preserves_actor_checks(api, db):
    response = api.post(
        f"/v1/messenger/users/{api.user_uuid}/actions/presence/invoke",
        json={"status": "active"},
    )
    assert response.status_code == 200, response.text
    current_user = api.get("/v1/me/")
    assert current_user.status_code == 200, current_user.text
    assert current_user.json()["status"] == "active"

    other_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, other_user_uuid, f"user-{other_user_uuid}")
    canonical = api.post(
        f"/v1/users/{other_user_uuid}/actions/presence/invoke",
        json={"status": "active"},
    )
    response = api.post(
        f"/v1/messenger/users/{other_user_uuid}/actions/presence/invoke",
        json={"status": "active"},
    )
    assert canonical.status_code == response.status_code == 404
    assert response.json() == canonical.json()


@pytest.mark.parametrize(
    "path",
    ("/v1/me/", "/v1/users/", "/v1/messenger/me/", "/v1/messenger/users/"),
)
def test_shared_and_deprecated_user_routes_reject_invalid_tokens(db, path):
    iam_driver = mock.Mock()
    application = workspace_app.build_wsgi_application(iam_driver)

    response = webob.Request.blank(
        path, headers={"Authorization": "Bearer invalid-token"}
    ).get_response(application)

    assert response.status_code == 401
    assert not iam_driver.mock_calls
