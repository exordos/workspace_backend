# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json

import webob
from restalchemy.api import applications
from restalchemy.api import contexts

from workspace.messenger_api.api import app as messenger_app
from workspace.provider_api.api import app as provider_app
from workspace.workspace_api.api import app as workspace_app


OPENAPI_VERSION = "3.0.3"


def _build_openapi(app_module):
    application = applications.OpenApiApplication(
        route_class=app_module.get_api_application(),
        openapi_engine=app_module.get_openapi_engine(),
    )
    request = webob.Request.blank(f"/specifications/{OPENAPI_VERSION}")
    request.application = application
    request.api_context = contexts.RequestContext(request)

    specification = application.openapi_engine.build_openapi_specification(
        OPENAPI_VERSION,
        request,
    )

    assert isinstance(request.api_context, contexts.RequestContext)
    assert specification["openapi"] == OPENAPI_VERSION
    json.dumps(specification)
    return specification


def test_messenger_openapi_keeps_internal_v1_paths_and_add_users_action():
    specification = _build_openapi(messenger_app)
    paths = specification["paths"]

    assert "/v1/messages/" in paths
    assert "/v1/streams/" in paths
    assert "/v1/messenger/messages/" not in paths
    assert "/v1/events/" not in paths
    assert "/v1/epoch/" not in paths

    add_users_path = "/v1/streams/{WorkspaceUserStreamUuid}/actions/add_users/invoke"
    assert set(paths[add_users_path]) == {"post"}
    assert paths[add_users_path]["post"]["operationId"].startswith("Add_users_")


def test_workspace_openapi_exposes_ui_mail_calendar_and_rest_events():
    specification = _build_openapi(workspace_app)
    paths = specification["paths"]

    assert "/v1/messenger/messages/" in paths
    assert "/v1/mail/folders/" in paths
    assert "/v1/mail/messages/" in paths
    assert "/v1/mail/messages/{MailMessageUuid}/actions/send/invoke" in paths
    assert "/v1/calendar/calendars/" in paths
    assert "/v1/calendar/events/" in paths
    assert "/v1/calendar/events/{CalendarEventUuid}/actions/move/invoke" in paths

    assert set(paths["/v1/events/"]) == {"get"}
    assert "/v1/events/ws" not in paths
    assert "/v1/events/ws/" not in paths
    assert not any("/commands/" in path for path in paths)
    assert not any("/blobs/" in path for path in paths)


def test_provider_openapi_exposes_service_provider_domains_and_commands():
    specification = _build_openapi(provider_app)
    paths = specification["paths"]
    provider_prefix = "/v1/providers/{provider_uuid}"

    expected_paths = {
        "/v1/providers/",
        provider_prefix,
        f"{provider_prefix}/external_accounts/",
        (
            f"{provider_prefix}/external_accounts/"
            "{external_account_uuid}/actions/status/invoke"
        ),
        f"{provider_prefix}/mail/folders/{{entity_uuid}}",
        f"{provider_prefix}/mail/messages/{{entity_uuid}}",
        f"{provider_prefix}/mail/commands/",
        (f"{provider_prefix}/mail/commands/{{command_uuid}}/actions/result/invoke"),
        f"{provider_prefix}/calendar/calendars/{{entity_uuid}}",
        f"{provider_prefix}/calendar/events/{{entity_uuid}}",
        f"{provider_prefix}/calendar/commands/",
        (f"{provider_prefix}/calendar/commands/{{command_uuid}}/actions/result/invoke"),
        f"{provider_prefix}/messenger/users/{{entity_uuid}}",
        f"{provider_prefix}/messenger/streams/{{entity_uuid}}",
        f"{provider_prefix}/messenger/topics/{{entity_uuid}}",
        f"{provider_prefix}/messenger/messages/{{entity_uuid}}",
        (f"{provider_prefix}/messenger/messages/{{entity_uuid}}/actions/flags/invoke"),
        f"{provider_prefix}/messenger/reactions/{{entity_uuid}}",
        f"{provider_prefix}/messenger/commands/",
        (
            f"{provider_prefix}/messenger/commands/"
            "{command_uuid}/actions/result/invoke"
        ),
    }
    assert expected_paths <= set(paths)

    assert set(paths[f"{provider_prefix}/mail/messages/{{entity_uuid}}"]) == {
        "get",
        "put",
        "delete",
    }
    assert set(paths[f"{provider_prefix}/calendar/events/{{entity_uuid}}"]) == {
        "get",
        "put",
        "delete",
    }
    assert set(paths[f"{provider_prefix}/messenger/messages/{{entity_uuid}}"]) == {
        "get",
        "put",
        "delete",
    }
    assert set(
        paths[
            f"{provider_prefix}/messenger/messages/{{entity_uuid}}/actions/flags/invoke"
        ]
    ) == {"post"}
    assert "/v1/events/" not in paths
    assert "/v1/epoch/" not in paths
