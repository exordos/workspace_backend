# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json

import webob
from restalchemy.api import applications
from restalchemy.api import contexts

from workspace.messenger_api.api import app as messenger_app
from workspace.workspace_api.api import app as workspace_app


OPENAPI_VERSION = "3.0.3"


def _assert_message_pagination_contract(operation):
    parameters = {
        (parameter["in"], parameter["name"]): parameter
        for parameter in operation["parameters"]
    }
    assert parameters[("query", "page_marker")]["schema"] == {
        "type": "string",
        "format": "uuid",
    }
    assert parameters[("query", "sort_key")]["schema"]["enum"] == ["created_at"]
    assert parameters[("query", "sort_dir")]["schema"]["enum"] == [
        "asc",
        "desc",
    ]
    headers = operation["responses"][200]["headers"]
    assert headers["X-Pagination-Marker"]["schema"]["format"] == "uuid"


def _assert_multipart_object(operation, required):
    content = operation["requestBody"]["content"]
    assert set(content) == {"multipart/form-data"}
    schema = content["multipart/form-data"]["schema"]
    assert schema["type"] == "object"
    assert schema["required"] == required
    assert schema["properties"]["file"] == {
        "type": "string",
        "format": "binary",
    }


def _assert_file_upload_contract(operation):
    content = operation["requestBody"]["content"]
    assert set(content) == {"application/json", "multipart/form-data"}
    json_schema = content["application/json"]["schema"]
    assert json_schema["required"] == [
        "stream_uuid",
        "name",
        "content_type",
        "size_bytes",
        "hash",
    ]
    assert "storage_type" not in json_schema["properties"]
    multipart_schema = content["multipart/form-data"]["schema"]
    assert multipart_schema["required"] == ["file"]
    assert multipart_schema["oneOf"] == [
        {
            "required": ["stream_uuid"],
            "not": {"required": ["acl"]},
        },
        {
            "required": ["acl"],
            "not": {"required": ["stream_uuid"]},
        },
    ]
    assert "storage_type" not in multipart_schema["properties"]


def _assert_collection_pagination_contract(operation, marker_schema):
    parameters = {
        (parameter["in"], parameter["name"]): parameter
        for parameter in operation["parameters"]
    }
    assert parameters[("query", "page_limit")]["schema"] == {
        "type": "integer",
        "minimum": 0,
    }
    assert parameters[("query", "page_marker")]["schema"] == marker_schema
    headers = operation["responses"][200]["headers"]
    assert headers["X-Pagination-Marker"]["schema"] == marker_schema


def _assert_draft_contract(paths, collection_path):
    for operation in paths[collection_path].values():
        assert "emits no Workspace events" in operation["description"]
    payload_schema = paths[collection_path]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["payload"]
    assert payload_schema["required"] == ["kind", "content"]
    assert payload_schema["properties"]["kind"]["enum"] == ["markdown"]
    assert payload_schema["properties"]["content"]["maxLength"] == 10000
    error_schema = paths[collection_path]["post"]["responses"][409]["content"][
        "application/json"
    ]["schema"]
    assert error_schema["required"] == ["message"]

    resource_path = f"{collection_path}{{WorkspaceDraftUuid}}"
    for operation in paths[resource_path].values():
        assert "emits no Workspace events" in operation["description"]
    update_payload_schema = paths[resource_path]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["payload"]
    assert update_payload_schema == payload_schema
    for method in ("put", "delete"):
        response = paths[resource_path][method]["responses"][428]
        assert response["content"]["application/json"]["schema"] == error_schema


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


def _assert_all_local_references_resolve(specification):
    pending = [specification]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            reference = value.get("$ref")
            if reference is not None:
                assert reference.startswith("#/")
                target = specification
                for token in reference[2:].split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    assert token in target, f"Unresolved OpenAPI reference: {reference}"
                    target = target[token]
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def test_generated_openapi_references_are_self_contained():
    for app_module in (messenger_app, workspace_app):
        specification = _build_openapi(app_module)
        schemas = specification["components"]["schemas"]
        assert schemas["WorkspaceUser_AvatarUpload"] == schemas["WorkspaceUser_Get"]
        _assert_all_local_references_resolve(specification)


def test_messenger_openapi_keeps_internal_v1_paths_and_add_users_action():
    specification = _build_openapi(messenger_app)
    paths = specification["paths"]

    assert "/v1/messages/" in paths
    assert "/v1/streams/" in paths
    assert "/v1/messenger/messages/" not in paths
    assert "/v1/events/" not in paths
    assert "/v1/epoch/" not in paths
    _assert_message_pagination_contract(paths["/v1/messages/"]["get"])

    add_users_path = "/v1/streams/{WorkspaceUserStreamUuid}/actions/add_users/invoke"
    assert set(paths[add_users_path]) == {"post"}
    assert paths[add_users_path]["post"]["operationId"].startswith("Add_users_")

    avatar_upload_path = "/v1/users/{WorkspaceUserUuid}/actions/avatar_upload/invoke"
    avatar_reset_path = "/v1/users/{WorkspaceUserUuid}/actions/avatar_reset/invoke"
    assert set(paths[avatar_upload_path]) == {"post"}
    assert set(paths[avatar_reset_path]) == {"post"}
    me_operation = paths["/v1/me/"]["get"]
    assert me_operation["parameters"] == []
    assert me_operation["responses"][200]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/WorkspaceUser_Get"}
    _assert_multipart_object(paths[avatar_upload_path]["post"], ["file"])
    _assert_file_upload_contract(paths["/v1/files/"]["post"])
    _assert_collection_pagination_contract(
        paths["/v1/folders/"]["get"],
        {"type": "string", "format": "uuid"},
    )
    _assert_draft_contract(paths, "/v1/drafts/")


def test_workspace_openapi_exposes_messenger_and_rest_events():
    specification = _build_openapi(workspace_app)
    paths = specification["paths"]

    assert "/v1/messenger/messages/" in paths
    assert not any(path.startswith("/v1/mail/") for path in paths)
    assert not any(path.startswith("/v1/calendar/") for path in paths)
    assert "/v1/providers/" not in paths
    assert set(paths["/v1/events/"]) == {"get"}
    assert "/v1/events/ws" not in paths
    assert "/v1/events/ws/" not in paths
    event_operation = paths["/v1/events/"]["get"]
    assert any(
        parameter["name"] == "epoch_generation"
        for parameter in event_operation["parameters"]
    )
    assert event_operation["responses"][410]["content"]["application/json"][
        "schema"
    ]["properties"]["error"]["enum"] == ["epoch_pruned"]
    epoch_schema = paths["/v1/epoch/"]["get"]["responses"][200]["content"][
        "application/json"
    ]["schema"]
    assert "epoch_generation" in epoch_schema["required"]
    assert not any("/commands/" in path for path in paths)
    assert not any("/blobs/" in path for path in paths)
    me_operation = paths["/v1/me/"]["get"]
    assert me_operation["parameters"] == []
    assert me_operation["responses"][200]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/WorkspaceUser_Get"}
    _assert_message_pagination_contract(paths["/v1/messenger/messages/"]["get"])
    _assert_file_upload_contract(paths["/v1/messenger/files/"]["post"])
    _assert_collection_pagination_contract(
        event_operation,
        {"type": "integer", "minimum": 0},
    )
    _assert_draft_contract(paths, "/v1/messenger/drafts/")
    avatar_upload_path = (
        "/v1/users/{WorkspaceUserUuid}/actions/avatar_upload/invoke"
    )
    _assert_multipart_object(paths[avatar_upload_path]["post"], ["file"])

    schemas = specification["components"]["schemas"]
    raw_provider_fields = {
        "provider_uuid",
        "external_account_uuid",
        "provider_external_id",
    }
    assert raw_provider_fields.isdisjoint(schemas["WorkspaceUser_Get"]["properties"])
    assert raw_provider_fields.isdisjoint(
        schemas["WorkspaceFile_Filter"]["properties"],
    )
    for schema_name in (
        "WorkspaceUserStream_Filter",
        "WorkspaceUserTopic_Filter",
        "WorkspaceUserMessage_Filter",
        "WorkspaceMessageReactions_Filter",
    ):
        projection_properties = schemas[schema_name]["properties"]
        assert raw_provider_fields.isdisjoint(projection_properties)
        assert {"provider", "delivery"} <= set(projection_properties)
    assert schemas["WorkspaceEvent_Filter"]["properties"]["object_type"]["enum"] == [
        "file",
        "folder",
        "folder_item",
        "message",
        "message_reaction",
        "stream",
        "stream_binding",
        "topic",
        "user",
    ]
