# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
from unittest import mock

import webob
from restalchemy.api import applications
from restalchemy.api import contexts

from workspace.messenger_api.api import app as messenger_app
from workspace.messenger_api.dm import base as messenger_dm_base
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


def _assert_reaction_activity_contract(operation):
    parameters = {
        (parameter["in"], parameter["name"]): parameter
        for parameter in operation["parameters"]
    }
    assert set(parameters) == {
        ("query", "page_limit"),
        ("query", "page_marker"),
    }
    assert parameters[("query", "page_limit")]["schema"] == {
        "type": "integer",
        "minimum": 0,
    }
    assert parameters[("query", "page_marker")]["schema"] == {
        "type": "string",
        "format": "uuid",
    }
    headers = operation["responses"][200]["headers"]
    assert headers["X-Pagination-Limit"]["schema"] == {"type": "integer"}
    assert headers["X-Pagination-Marker"]["schema"] == {
        "type": "string",
        "format": "uuid",
    }


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
    assert payload_schema["properties"]["content"]["maxLength"] == 40000
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


def _assert_topic_summary_contract(paths, topic_path):
    assert f"{topic_path}/actions/set_summary/invoke" not in paths

    prompt = paths[f"{topic_path}/actions/set_summary_prompt/invoke"]["post"]
    assert "owner or administrator" in prompt["description"]
    prompt_schema = prompt["requestBody"]["content"]["application/json"]["schema"]
    assert prompt_schema["required"] == []
    assert prompt_schema["minProperties"] == 1
    assert prompt_schema["additionalProperties"] is False
    assert prompt_schema["properties"]["summary_system_prompt"]["maxLength"] == 16384
    assert prompt_schema["properties"]["summary_reasoning_effort"] == {
        "type": "string",
        "enum": ["minimal", "low", "medium", "high"],
        "nullable": True,
    }
    assert prompt_schema["properties"]["summary_enabled"] == {
        "type": "boolean",
    }
    assert 403 in prompt["responses"]


def _assert_topic_summary_management_contract(specification, root):
    paths = specification["paths"]
    collection_path = f"{root}topic_summary_endpoints/"
    collection = paths[collection_path]
    assert set(collection) == {"get", "post"}
    create_schema = collection["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert create_schema["required"] == [
        "uuid",
        "name",
        "base_url",
        "model",
        "api_key",
    ]
    assert create_schema["properties"]["api_key"]["writeOnly"] is True
    assert create_schema["properties"]["supports_vision"]["default"] is False
    assert create_schema["properties"]["max_output_tokens"]["maximum"] == 32768

    endpoint_path = next(
        path
        for path in paths
        if path.startswith(f"{collection_path}{{")
    )
    assert set(paths[endpoint_path]) == {"get", "put", "delete"}
    update_schema = paths[endpoint_path]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert update_schema["minProperties"] == 1
    assert update_schema["properties"]["api_key"]["writeOnly"] is True
    for schema_name in (
        "WorkspaceLLMEndpoint_Filter",
        "WorkspaceLLMEndpoint_Get",
        "WorkspaceLLMEndpoint_Create",
        "WorkspaceLLMEndpoint_Update",
    ):
        response_properties = specification["components"]["schemas"][schema_name][
            "properties"
        ]
        assert "api_key" not in response_properties
        assert "claim_token" not in response_properties
        assert "revision" not in response_properties
        assert "supports_vision" in response_properties

    settings_path = next(
        path
        for path in paths
        if path.startswith(f"{root}topic_summary_settings/{{")
    )
    for operation in paths[settings_path].values():
        assert operation["parameters"][0]["in"] == "path"
        assert operation["parameters"][0]["schema"]["format"] == "uuid"
    settings_schema = paths[settings_path]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert settings_schema["required"] == [
        "global_enabled",
        "project_enabled",
    ]


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


def test_generated_openapi_color_defaults_are_deterministic():
    with mock.patch.object(
        messenger_dm_base.random,
        "randint",
        side_effect=AssertionError("OpenAPI generation must not select a color"),
    ):
        first_specification = _build_openapi(messenger_app)
        second_specification = _build_openapi(messenger_app)

    assert first_specification == second_specification
    color_schemas = {
        name: schema["properties"]["color"]
        for name, schema in first_specification["components"]["schemas"].items()
        if "color" in schema.get("properties", {})
    }
    assert color_schemas
    assert all("default" not in schema for schema in color_schemas.values())


def test_generated_openapi_message_payload_uses_markdown_content_limit():
    specification = _build_openapi(messenger_app)
    payload_schema = specification["components"]["schemas"][
        "WorkspaceUserMessage_Create"
    ]["properties"]["payload"]

    assert payload_schema["oneOf"][0]["properties"]["content"]["maxLength"] == 40000


def test_generated_openapi_exposes_persisted_complete_reaction_user_lists():
    specification = _build_openapi(workspace_app)

    for schema_name in (
        "WorkspaceUserMessage_Filter",
        "WorkspaceUserMessage_Get",
        "WorkspaceUserMessage_Create",
        "WorkspaceUserMessage_Update",
    ):
        reaction_users_schema = specification["components"]["schemas"][schema_name][
            "properties"
        ]["reaction_users"]
        assert reaction_users_schema["readOnly"] is True
        assert reaction_users_schema["default"] == {}
        assert reaction_users_schema["example"] == {
            "heart": [
                "11111111-1111-1111-1111-111111111111",
                "33333333-3333-3333-3333-333333333333",
            ]
        }
        assert reaction_users_schema["additionalProperties"] == {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "format": "uuid"},
        }
        assert "never partial" in reaction_users_schema["description"]


def test_messenger_openapi_keeps_internal_v1_paths_and_add_users_action():
    specification = _build_openapi(messenger_app)
    paths = specification["paths"]

    assert "/v1/messages/" in paths
    assert "/v1/activity/reactions/" in paths
    assert "/v1/streams/" in paths
    assert "/v1/messenger/messages/" not in paths
    assert "/v1/events/" not in paths
    assert "/v1/epoch/" not in paths
    _assert_message_pagination_contract(paths["/v1/messages/"]["get"])
    _assert_reaction_activity_contract(paths["/v1/activity/reactions/"]["get"])

    add_users_path = "/v1/streams/{WorkspaceUserStreamUuid}/actions/add_users/invoke"
    assert set(paths[add_users_path]) == {"post"}
    assert paths[add_users_path]["post"]["operationId"].startswith("Add_users_")

    avatar_upload_path = "/v1/users/{WorkspaceUserUuid}/actions/avatar_upload/invoke"
    avatar_reset_path = "/v1/users/{WorkspaceUserUuid}/actions/avatar_reset/invoke"
    assert set(paths[avatar_upload_path]) == {"post"}
    assert set(paths[avatar_reset_path]) == {"post"}
    me_operation = paths["/v1/me/"]["get"]
    assert me_operation["parameters"] == []
    assert me_operation["responses"][200]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WorkspaceUser_Get"
    }
    _assert_multipart_object(paths[avatar_upload_path]["post"], ["file"])
    _assert_file_upload_contract(paths["/v1/files/"]["post"])
    _assert_collection_pagination_contract(
        paths["/v1/folders/"]["get"],
        {"type": "string", "format": "uuid"},
    )
    _assert_draft_contract(paths, "/v1/drafts/")
    _assert_topic_summary_contract(
        paths,
        "/v1/stream_topics/{WorkspaceUserTopicUuid}",
    )
    _assert_topic_summary_management_contract(specification, "/v1/")


def test_workspace_openapi_exposes_messenger_and_rest_events():
    specification = _build_openapi(workspace_app)
    paths = specification["paths"]

    assert "/v1/messenger/messages/" in paths
    assert "/v1/messenger/activity/reactions/" in paths
    assert not any(path.startswith("/v1/mail/") for path in paths)
    assert not any(path.startswith("/v1/calendar/") for path in paths)
    assert "/v1/providers/" not in paths
    push_device_path = "/v1/push_devices/{PushDeviceUuid}"
    assert set(paths[push_device_path]) == {"put", "delete"}
    push_device_put = paths[push_device_path]["put"]
    request_reference = push_device_put["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert request_reference == "#/components/schemas/PushDevice_Update"
    push_device_request = specification["components"]["schemas"][
        "PushDevice_Update"
    ]
    assert push_device_request["required"] == [
        "transport",
        "platform",
        "registration_token",
        "encryption",
    ]
    request_encryption = push_device_request["properties"]["encryption"]["oneOf"][0]
    assert request_encryption["properties"]["kind"]["enum"] == ["HPKE"]
    assert request_encryption["properties"]["algorithm"]["enum"] == [
        "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
    ]
    assert "public_key" in request_encryption["properties"]
    for status in (200, 201):
        response_schema = push_device_put["responses"][status]["content"][
            "application/json"
        ]["schema"]
        assert {"user_uuid", "project_id"} <= set(
            response_schema["properties"],
        )
        assert "registration_token" in response_schema["properties"]
        response_encryption = response_schema["properties"]["encryption"]["oneOf"][0][
            "properties"
        ]
        assert "public_key" in response_encryption
    assert set(paths[push_device_path]["delete"]["responses"]) == {
        204,
        "default",
    }
    assert set(paths["/v1/events/"]) == {"get"}
    assert "/v1/events/ws" not in paths
    assert "/v1/events/ws/" not in paths
    event_operation = paths["/v1/events/"]["get"]
    assert any(
        parameter["name"] == "epoch_generation"
        for parameter in event_operation["parameters"]
    )
    assert event_operation["responses"][410]["content"]["application/json"]["schema"][
        "properties"
    ]["error"]["enum"] == ["epoch_pruned"]
    epoch_schema = paths["/v1/epoch/"]["get"]["responses"][200]["content"][
        "application/json"
    ]["schema"]
    assert "epoch_generation" in epoch_schema["required"]
    assert not any("/commands/" in path for path in paths)
    assert not any("/blobs/" in path for path in paths)
    me_operation = paths["/v1/me/"]["get"]
    assert me_operation["parameters"] == []
    assert me_operation["responses"][200]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WorkspaceUser_Get"
    }
    _assert_message_pagination_contract(paths["/v1/messenger/messages/"]["get"])
    _assert_reaction_activity_contract(
        paths["/v1/messenger/activity/reactions/"]["get"],
    )
    _assert_file_upload_contract(paths["/v1/messenger/files/"]["post"])
    _assert_collection_pagination_contract(
        event_operation,
        {"type": "integer", "minimum": 0},
    )
    _assert_draft_contract(paths, "/v1/messenger/drafts/")
    _assert_topic_summary_contract(
        paths,
        "/v1/messenger/stream_topics/{WorkspaceUserTopicUuid}",
    )
    _assert_topic_summary_management_contract(specification, "/v1/messenger/")
    avatar_upload_path = "/v1/users/{WorkspaceUserUuid}/actions/avatar_upload/invoke"
    _assert_multipart_object(paths[avatar_upload_path]["post"], ["file"])

    schemas = specification["components"]["schemas"]
    raw_provider_fields = {
        "provider_uuid",
        "external_account_uuid",
        "provider_external_id",
    }
    user_properties = schemas["WorkspaceUser_Get"]["properties"]
    assert raw_provider_fields.isdisjoint(user_properties)
    assert user_properties["identity_kind"]["enum"] == ["external"]
    assert user_properties["display_name"]["readOnly"] is True
    assert set(user_properties["provider"]["properties"]) == {
        "kind",
        "account_uuid",
    }
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
        assert "identity_kind" not in projection_properties
        assert "external_id" in projection_properties["provider"]["properties"]
    topic_properties = schemas["WorkspaceUserTopic_Get"]["properties"]
    assert topic_properties["summary"]["maxLength"] == 4096
    assert topic_properties["summary_last_message_uuid"]["format"] == "uuid"
    assert topic_properties["summary_has_new_messages"]["readOnly"] is True
    assert topic_properties["summary_enabled"]["readOnly"] is True
    assert topic_properties["summary_system_prompt"]["maxLength"] == 16384
    assert schemas["WorkspaceEvent_Filter"]["properties"]["object_type"]["enum"] == [
        "external_account",
        "external_chat",
        "external_operation",
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
