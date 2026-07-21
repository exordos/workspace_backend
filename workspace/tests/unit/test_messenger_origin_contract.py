#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import datetime
import inspect
import uuid as sys_uuid

from restalchemy.api import routes as ra_routes
import webob

from workspace.messenger_api.api import app
from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import middlewares
from workspace.messenger_api.api import routes
from workspace.messenger_api.dm import models
from workspace.messenger_api import events


ROUTE_MANIFEST = {
    "folders": (
        routes.FolderRoute,
        controllers.FolderController,
        {
            ra_routes.CREATE,
            ra_routes.FILTER,
            ra_routes.GET,
            ra_routes.UPDATE,
            ra_routes.DELETE,
        },
    ),
    "folder_items": (
        routes.FolderItemRoute,
        controllers.FolderItemController,
        {ra_routes.CREATE, ra_routes.FILTER, ra_routes.GET, ra_routes.DELETE},
    ),
    "streams": (
        routes.WorkspaceStreamRoute,
        controllers.WorkspaceStreamController,
        {
            ra_routes.CREATE,
            ra_routes.FILTER,
            ra_routes.GET,
            ra_routes.UPDATE,
            ra_routes.DELETE,
        },
    ),
    "stream_bindings": (
        routes.WorkspaceStreamBindingRoute,
        controllers.WorkspaceStreamBindingController,
        {ra_routes.FILTER, ra_routes.GET, ra_routes.UPDATE, ra_routes.DELETE},
    ),
    "stream_topics": (
        routes.WorkspaceStreamTopicRoute,
        controllers.WorkspaceStreamTopicController,
        {
            ra_routes.CREATE,
            ra_routes.FILTER,
            ra_routes.GET,
            ra_routes.UPDATE,
            ra_routes.DELETE,
        },
    ),
    "messages": (
        routes.WorkspaceMessageRoute,
        controllers.WorkspaceMessageController,
        {
            ra_routes.CREATE,
            ra_routes.FILTER,
            ra_routes.GET,
            ra_routes.UPDATE,
            ra_routes.DELETE,
        },
    ),
    "drafts": (
        routes.WorkspaceDraftRoute,
        controllers.WorkspaceDraftController,
        {
            ra_routes.CREATE,
            ra_routes.FILTER,
            ra_routes.GET,
            ra_routes.UPDATE,
            ra_routes.DELETE,
        },
    ),
    "external_accounts": (
        routes.ExternalAccountRoute,
        controllers.ExternalAccountController,
        {
            ra_routes.CREATE,
            ra_routes.FILTER,
            ra_routes.GET,
            ra_routes.UPDATE,
            ra_routes.DELETE,
        },
    ),
    "external_chats": (
        routes.ExternalChatRoute,
        controllers.ExternalChatController,
        {ra_routes.FILTER, ra_routes.GET},
    ),
    "external_operations": (
        routes.ExternalOperationRoute,
        controllers.ExternalOperationController,
        {ra_routes.FILTER, ra_routes.GET, ra_routes.DELETE},
    ),
    "external_bridge_instances": (
        routes.ExternalBridgeInstanceRoute,
        controllers.ExternalBridgeInstanceController,
        {ra_routes.FILTER, ra_routes.GET},
    ),
    "external_provider_policies": (
        routes.ExternalProviderPolicyRoute,
        controllers.ExternalProviderPolicyController,
        {ra_routes.GET, ra_routes.UPDATE},
    ),
    "external_provider_health": (
        routes.ExternalProviderHealthRoute,
        controllers.ExternalProviderHealthController,
        {ra_routes.GET},
    ),
    "message_reactions": (
        routes.WorkspaceMessageReactionRoute,
        controllers.WorkspaceMessageReactionController,
        {
            ra_routes.CREATE,
            ra_routes.FILTER,
            ra_routes.GET,
            ra_routes.UPDATE,
            ra_routes.DELETE,
        },
    ),
    "files": (
        routes.WorkspaceFileRoute,
        controllers.WorkspaceFileController,
        {
            ra_routes.CREATE,
            ra_routes.FILTER,
            ra_routes.GET,
            ra_routes.UPDATE,
            ra_routes.DELETE,
        },
    ),
    "users": (
        routes.WorkspaceUserRoute,
        controllers.WorkspaceUserController,
        {ra_routes.FILTER, ra_routes.GET},
    ),
    "me": (
        routes.MeRoute,
        controllers.MeController,
        {ra_routes.FILTER},
    ),
}


ACTION_MANIFEST = {
    (routes.ExternalAccountRoute, "reconnect"): (
        controllers.ExternalAccountController,
        True,
    ),
    (routes.ExternalAccountRoute, "disconnect"): (
        controllers.ExternalAccountController,
        True,
    ),
    (routes.ExternalChatRoute, "select"): (
        controllers.ExternalChatController,
        True,
    ),
    (routes.ExternalChatRoute, "deselect"): (
        controllers.ExternalChatController,
        True,
    ),
    (routes.ExternalChatRoute, "move"): (
        controllers.ExternalChatController,
        True,
    ),
    (routes.ExternalOperationRoute, "retry"): (
        controllers.ExternalOperationController,
        True,
    ),
    (routes.ExternalBridgeInstanceRoute, "suspend"): (
        controllers.ExternalBridgeInstanceController,
        True,
    ),
    (routes.ExternalBridgeInstanceRoute, "resume"): (
        controllers.ExternalBridgeInstanceController,
        True,
    ),
    (routes.ExternalBridgeInstanceRoute, "revoke"): (
        controllers.ExternalBridgeInstanceController,
        True,
    ),
    (routes.ExternalProviderPolicyRoute, "suspend"): (
        controllers.ExternalProviderPolicyController,
        True,
    ),
    (routes.ExternalProviderPolicyRoute, "resume"): (
        controllers.ExternalProviderPolicyController,
        True,
    ),
    (routes.FolderItemRoute, "pin"): (
        controllers.FolderItemController,
        True,
    ),
    (routes.FolderItemRoute, "unpin"): (
        controllers.FolderItemController,
        True,
    ),
    (routes.WorkspaceStreamRoute, "add_users"): (
        controllers.WorkspaceStreamBindingController,
        True,
    ),
    (routes.WorkspaceStreamRoute, "archive"): (
        controllers.WorkspaceStreamController,
        True,
    ),
    (routes.WorkspaceStreamRoute, "unarchive"): (
        controllers.WorkspaceStreamController,
        True,
    ),
    (routes.WorkspaceStreamRoute, "notifications"): (
        controllers.WorkspaceStreamController,
        True,
    ),
    (routes.WorkspaceStreamRoute, "read"): (
        controllers.WorkspaceStreamController,
        True,
    ),
    (routes.WorkspaceStreamTopicRoute, "toggle_done"): (
        controllers.WorkspaceStreamTopicController,
        True,
    ),
    (routes.WorkspaceStreamTopicRoute, "notifications"): (
        controllers.WorkspaceStreamTopicController,
        True,
    ),
    (routes.WorkspaceStreamTopicRoute, "set_default"): (
        controllers.WorkspaceStreamTopicController,
        True,
    ),
    (routes.WorkspaceStreamTopicRoute, "read"): (
        controllers.WorkspaceStreamTopicController,
        True,
    ),
    (routes.WorkspaceMessageRoute, "read"): (
        controllers.WorkspaceMessageController,
        True,
    ),
    (routes.WorkspaceMessageRoute, "read_up_to"): (
        controllers.WorkspaceMessageController,
        True,
    ),
    (routes.WorkspaceFileRoute, "download"): (
        controllers.WorkspaceFileController,
        False,
    ),
    (routes.WorkspaceUserRoute, "presence"): (
        controllers.WorkspaceUserController,
        True,
    ),
    (routes.WorkspaceUserRoute, "avatar_upload"): (
        controllers.WorkspaceUserController,
        True,
    ),
    (routes.WorkspaceUserRoute, "avatar_reset"): (
        controllers.WorkspaceUserController,
        True,
    ),
}


def test_messenger_v1_route_and_method_manifest_is_preserved():
    assert app.MessengerApiApp.v1 is routes.ApiEndpointRoute
    assert set(routes.ApiEndpointRoute.__allow_methods__) == {ra_routes.FILTER}

    root_routes = {
        name
        for name, value in routes.ApiEndpointRoute.__dict__.items()
        if inspect.isclass(value) and issubclass(value, ra_routes.Route)
    }
    assert root_routes == set(ROUTE_MANIFEST)

    for name, (route_class, controller_class, methods) in ROUTE_MANIFEST.items():
        assert getattr(routes.ApiEndpointRoute, name) is route_class
        assert route_class.__controller__ is controller_class
        assert set(route_class.__allow_methods__) == methods


def test_messenger_action_manifest_is_preserved():
    for (route_class, name), (controller_class, invoke) in ACTION_MANIFEST.items():
        action_class = getattr(route_class, name)
        assert issubclass(action_class, ra_routes.Action)
        assert action_class.__controller__ is controller_class
        assert action_class.is_invoke() is invoke


def test_server_settings_origin_path_and_fields_are_preserved():
    assert middlewares.SERVER_SETTINGS_PATH == "/v1/server_settings"
    request = webob.Request.blank(
        middlewares.SERVER_SETTINGS_PATH,
        base_url="http://127.0.0.1:3000",
    )

    settings = middlewares.build_server_settings(request)

    assert settings["realm_icon"] == "urn:url:https://127.0.0.1:3000/logo-512x512.png"

    assert set(settings) == {
        "result",
        "msg",
        "authentication_methods",
        "push_notifications_enabled",
        "email_auth_enabled",
        "require_email_format_usernames",
        "realm_url",
        "realm_name",
        "realm_icon",
        "realm_description",
        "realm_web_public_access_enabled",
        "meet_url",
        "external_authentication_methods",
        "realm_uri",
    }
    assert set(settings["authentication_methods"]) == {
        "password",
        "dev",
        "email",
        "ldap",
        "remoteuser",
        "github",
        "azuread",
        "gitlab",
        "google",
        "apple",
        "saml",
        "openid connect",
    }


def test_file_upload_openapi_keeps_required_multipart_contract():
    request_body = (
        controllers.WorkspaceFileController.create.openapi_schema.request_body
    )

    assert request_body["required"] is True
    assert set(request_body["content"]) == {
        "application/json",
        "multipart/form-data",
    }
    json_schema = request_body["content"]["application/json"]["schema"]
    assert json_schema["required"] == [
        "stream_uuid",
        "name",
        "content_type",
        "size_bytes",
        "hash",
    ]
    assert "storage_type" not in json_schema["properties"]
    schema = request_body["content"]["multipart/form-data"]["schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["file"]
    assert schema["oneOf"] == [
        {
            "required": ["stream_uuid"],
            "not": {"required": ["acl"]},
        },
        {
            "required": ["acl"],
            "not": {"required": ["stream_uuid"]},
        },
    ]
    properties = schema["properties"]
    assert properties == {
        "file": {"format": "binary", "type": "string"},
        "stream_uuid": {"format": "uuid", "type": "string"},
        "acl": {
            "description": (
                'JSON ACL object. The only public form is {"mode":"public"}.'
            ),
            "pattern": '^\\s*\\{\\s*"mode"\\s*:\\s*"public"\\s*\\}\\s*$',
            "type": "string",
        },
        "name": {"type": "string"},
        "description": {"type": "string"},
    }


def test_messenger_response_model_keeps_original_fields():
    original_fields = {
        models.WorkspaceUserStream: {
            "uuid",
            "user_uuid",
            "name",
            "description",
            "project_id",
            "created_at",
            "updated_at",
            "owner",
            "role",
            "notification_mode",
            "unread_count",
            "source_name",
            "source",
            "invite_only",
            "announce",
            "private",
            "is_archived",
            "direct_user_uuid",
            "color",
            "last_message_uuid",
            "default_topic_uuid",
        },
        models.WorkspaceUserTopic: {
            "uuid",
            "user_uuid",
            "project_id",
            "created_at",
            "updated_at",
            "source_name",
            "source",
            "name",
            "stream_uuid",
            "color",
            "last_message_uuid",
            "unread_count",
            "is_default",
            "is_done",
            "notification_mode",
        },
        models.WorkspaceUserMessage: {
            "uuid",
            "user_uuid",
            "project_id",
            "created_at",
            "updated_at",
            "source_name",
            "source",
            "stream_uuid",
            "topic_uuid",
            "payload",
            "author_uuid",
            "read",
            "pinned",
            "starred",
            "is_own",
            "reactions",
        },
        models.WorkspaceMessageReactions: {
            "uuid",
            "project_id",
            "created_at",
            "updated_at",
            "message_uuid",
            "user_uuid",
            "emoji_name",
        },
    }

    for model_class, fields in original_fields.items():
        assert fields <= set(model_class.properties.properties)


def test_messenger_event_keeps_original_flat_envelope_and_payload():
    created_at = datetime.datetime(
        2026,
        6,
        24,
        10,
        0,
        tzinfo=datetime.timezone.utc,
    )
    payload = {
        "kind": "message.created",
        "uuid": str(sys_uuid.uuid4()),
        "project_id": str(sys_uuid.uuid4()),
        "user_uuid": str(sys_uuid.uuid4()),
        "stream_uuid": str(sys_uuid.uuid4()),
        "topic_uuid": str(sys_uuid.uuid4()),
        "author_uuid": str(sys_uuid.uuid4()),
        "payload": {"kind": "markdown", "content": "hello"},
        "read": False,
        "pinned": False,
        "starred": False,
        "is_own": False,
        "reactions": {},
        "source_name": "native",
        "source": {"kind": "native"},
        "created_at": "2026-06-24T10:00:00.000000Z",
        "updated_at": "2026-06-24T10:00:00.000000Z",
    }
    row = {
        "schema_version": 1,
        "uuid": sys_uuid.uuid4(),
        "epoch_version": 7,
        "project_id": sys_uuid.uuid4(),
        "user_uuid": sys_uuid.uuid4(),
        "object_type": "message",
        "action": "created",
        "payload": payload,
        "created_at": created_at,
        "updated_at": created_at,
    }

    event = events.event_row_to_messenger_event(row)

    assert set(event) == {
        "schema_version",
        "uuid",
        "epoch_version",
        "project_id",
        "user_uuid",
        "object_type",
        "action",
        "payload",
        "created_at",
        "updated_at",
    }
    assert event["payload"] == payload
