# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import copy
import types
import uuid as sys_uuid
from unittest import mock

import pytest
import webob
from restalchemy.api import applications

from workspace.common.clients import iam
from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import middlewares
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import user_profile
from workspace.workspace_api.api import app


@pytest.fixture
def profile_api():
    user_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    client = mock.Mock()
    current = {
        "uuid": str(user_uuid),
        "username": "cassi-profile",
        "first_name": "Cassandra",
        "last_name": "Volkova",
        "surname": "",
        "phone": "+123456789",
        "description": "Engineer",
        "email": "profile@example.invalid",
        "custom_props": {"kind": "basic", "role": "Engineer", "other": {}},
        "otp_secret": "must-never-be-returned",
        "email_verified": True,
    }
    projection = {
        "uuid": user_uuid,
        "avatar": "urn:image:" + str(sys_uuid.uuid4()),
        "status": "idle",
    }
    calls = []

    class Store:
        def sync_iam_identity(self, values):
            calls.append(values)
            projection.update(
                {name: value for name, value in values.items() if name != "user_uuid"}
            )

        def get_resource(self, resource, target):
            assert resource == "users"
            assert target == user_uuid
            return projection.copy()

    def update_user(target, token, values):
        assert target == user_uuid
        assert token == "test-token"
        current.update(values)
        return copy.deepcopy(current)

    client.get_user.side_effect = lambda *args, **kwargs: copy.deepcopy(current)
    client.update_user.side_effect = update_user
    api_store.configure_store_factory(
        lambda project, actor: contextlib.nullcontext(Store())
    )
    application = applications.OpenApiApplication(
        route_class=app.get_api_application(),
        openapi_engine=app.get_openapi_engine(),
    )
    context = types.SimpleNamespace(
        user_uuid=user_uuid,
        project_id=project_uuid,
        iam_context=types.SimpleNamespace(
            token_info=types.SimpleNamespace(token="test-token")
        ),
    )

    def request(method="GET", path="/v1/me/", body=None):
        req = webob.Request.blank(path, method=method)
        if body is not None:
            req.json_body = body
        req.application = application
        req.context = context
        try:
            return application.process_request(req)
        except Exception as exc:
            return middlewares.ErrorsHandlerMiddleware(
                application
            )._construct_error_response(req, exc)

    with mock.patch.object(
        controllers.MeController, "_iam_client", return_value=client
    ):
        try:
            yield types.SimpleNamespace(
                request=request,
                current=current,
                projection=projection,
                calls=calls,
                client=client,
                user_uuid=user_uuid,
                context=context,
            )
        finally:
            api_store.reset_store_factory()


def test_get_profile_reads_authoritative_iam_and_keeps_workspace_fields(profile_api):
    response = profile_api.request()
    assert response.status_code == 200
    assert response.json["description"] == "Engineer"
    assert response.json["custom_props"] == profile_api.current["custom_props"]
    assert response.json["email"] == "profile@example.invalid"
    assert response.json["avatar"] == profile_api.projection["avatar"]
    assert response.json["status"] == "idle"
    assert "otp_secret" not in response.json
    assert "email_verified" not in response.json
    profile_api.client.get_user.assert_called_once_with(
        profile_api.user_uuid, token="test-token"
    )


def test_update_profile_returns_saved_fields_and_get_reads_them(profile_api):
    values = {
        "username": "cassi-profile-updated",
        "first_name": None,
        "last_name": "New",
        "surname": "Middle",
        "phone": "+1987654321",
        "description": "Updated",
        "custom_props": {"kind": "basic", "other": {"team": "Workspace"}},
    }
    avatar = profile_api.projection["avatar"]
    response = profile_api.request("PUT", body=values)
    assert response.status_code == 200
    assert {name: response.json[name] for name in values} == values
    assert response.json["email"] == "profile@example.invalid"
    assert response.json["avatar"] == avatar
    assert profile_api.request().json == response.json
    profile_api.client.update_user.assert_called_once_with(
        profile_api.user_uuid,
        token="test-token",
        values=values,
    )


def test_update_profile_is_partial_and_custom_props_is_whole_object(profile_api):
    response = profile_api.request("PUT", body={"description": "One field"})
    assert response.status_code == 200
    assert response.json["first_name"] == "Cassandra"
    assert response.json["phone"] == "+123456789"
    response = profile_api.request(
        "PUT", body={"custom_props": {"kind": "basic", "other": {}}}
    )
    assert response.status_code == 200
    assert "role" not in response.json["custom_props"]


@pytest.mark.parametrize(
    "field",
    [
        "email",
        "uuid",
        "source",
        "status",
        "avatar",
        "project_id",
        "user_uuid",
        "provider_uuid",
        "password",
        "otp_secret",
        "email_verified",
        "unknown",
    ],
)
def test_update_rejects_readonly_and_unknown_fields_before_iam(profile_api, field):
    response = profile_api.request("PUT", body={field: str(sys_uuid.uuid4())})
    assert response.status_code == (
        403
        if field in {"email", "uuid", "source", "status", "avatar", "provider_uuid"}
        else 400
    )
    profile_api.client.update_user.assert_not_called()
    assert profile_api.calls == []


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"username": ""},
        {"username": "x" * 129},
        {"first_name": 123},
        {"surname": "x" * 129},
        {"phone": "1" * 16},
        {"description": "x" * 256},
        {"custom_props": "invalid"},
    ],
)
def test_update_rejects_invalid_profile_values(profile_api, values):
    response = profile_api.request("PUT", body=values)
    assert response.status_code == 400
    profile_api.client.update_user.assert_not_called()


@pytest.mark.parametrize("values", [[], ["username"], "invalid", 42, False])
def test_update_requires_a_json_object(profile_api, values):
    response = profile_api.request("PUT", body=values)
    assert response.status_code == 400
    profile_api.client.update_user.assert_not_called()


def test_me_does_not_accept_a_target_uuid(profile_api):
    response = profile_api.request(
        "PUT", path=f"/v1/me/{sys_uuid.uuid4()}", body={"username": "other"}
    )
    assert response.status_code >= 400
    profile_api.client.update_user.assert_not_called()
    request = types.SimpleNamespace(context=profile_api.context)
    with pytest.raises(controllers.messenger_exc.ExternalResourceForbiddenError):
        controllers.MeController(request).update(sys_uuid.uuid4(), username="other")
    profile_api.client.update_user.assert_not_called()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 502])
def test_iam_failure_preserves_error_status_and_does_not_write_projection(
    profile_api, status
):
    profile_api.client.update_user.side_effect = iam.IamProfileRequestError(status)
    response = profile_api.request("PUT", body={"custom_props": {"kind": "basic"}})
    assert response.status_code == status
    assert response.json["message"] == "IAM profile request failed."
    assert profile_api.calls == []


def test_custom_props_is_omitted_when_iam_does_not_allow_read(profile_api):
    del profile_api.current["custom_props"]
    response = profile_api.request()
    assert response.status_code == 200
    assert "custom_props" not in response.json


def test_profile_update_openapi_matches_partial_input():
    from workspace.tests.unit import test_openapi_generation_contract as contract

    specification = contract._build_openapi(app)
    paths = specification["paths"]
    assert set(paths["/v1/me/"]) == {"get", "put"}
    assert not any(path.startswith("/v1/me/{") for path in paths)
    schema = contract._component_schema(specification, "WorkspaceUserProfile_Update")
    assert set(schema["properties"]) == set(user_profile.EDITABLE_FIELDS)
    assert schema["required"] == []
    assert schema["minProperties"] == 1
    assert schema["additionalProperties"] is False
