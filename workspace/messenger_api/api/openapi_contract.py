# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import copy
import typing


PAGINATION_LIMIT_PARAMETER = {
    "name": "page_limit",
    "in": "query",
    "description": "Maximum resources returned in this page.",
    "schema": {"type": "integer", "minimum": 0},
}

MESSAGE_PAGINATION_PARAMETERS = (
    {**PAGINATION_LIMIT_PARAMETER, "description": "Maximum messages returned."},
    {
        "name": "page_marker",
        "in": "query",
        "description": (
            "UUID of the last message from the previous page in the same IAM "
            "and filter scope."
        ),
        "schema": {"type": "string", "format": "uuid"},
    },
    {
        "name": "sort_key",
        "in": "query",
        "description": "Messages are keyset-paginated by created_at and uuid.",
        "schema": {"type": "string", "enum": ["created_at"]},
    },
    {
        "name": "sort_dir",
        "in": "query",
        "schema": {"type": "string", "enum": ["asc", "desc"]},
    },
)

MESSAGE_PAGINATION_HEADERS = {
    "X-Pagination-Limit": {
        "description": "Requested page limit.",
        "schema": {"type": "integer"},
    },
    "X-Pagination-Marker": {
        "description": (
            "UUID continuation marker. Present only when another message exists."
        ),
        "schema": {"type": "string", "format": "uuid"},
    },
}

DRAFT_PAGINATION_PARAMETERS = (
    {**PAGINATION_LIMIT_PARAMETER, "description": "Maximum drafts returned."},
    {
        "name": "page_marker",
        "in": "query",
        "description": (
            "UUID of the last draft from the previous page in the same owner "
            "and filter scope."
        ),
        "schema": {"type": "string", "format": "uuid"},
    },
    {
        "name": "sort_key",
        "in": "query",
        "description": "Drafts are keyset-paginated by updated_at and uuid.",
        "schema": {"type": "string", "enum": ["updated_at"]},
    },
    {
        "name": "sort_dir",
        "in": "query",
        "schema": {"type": "string", "enum": ["asc", "desc"]},
    },
    {
        "name": "stream_uuid",
        "in": "query",
        "schema": {"type": "string", "format": "uuid"},
    },
    {
        "name": "topic_uuid",
        "in": "query",
        "schema": {"type": "string", "format": "uuid"},
    },
)

DRAFT_ETAG_HEADER = {
    "ETag": {
        "description": "Strong entity tag containing the current draft revision.",
        "schema": {"type": "string", "pattern": '^"[1-9][0-9]*"$'},
    }
}

DRAFT_IF_MATCH_PARAMETER = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "description": "Strong ETag returned by the latest draft response.",
    "schema": {"type": "string", "pattern": '^"[1-9][0-9]*"$'},
}

DRAFT_PAYLOAD_SCHEMA = {
    "type": "object",
    "required": ["kind", "content"],
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["markdown"]},
        "content": {"type": "string", "minLength": 1, "maxLength": 10000},
    },
}

DRAFT_ERROR_SCHEMA = {
    "type": "object",
    "required": ["message"],
    "additionalProperties": False,
    "properties": {"message": {"type": "string"}},
}

DRAFT_SIDE_EFFECTS_DESCRIPTION = (
    "Drafts are PostgreSQL-only client state. This operation emits no Workspace "
    "events, websocket or desktop notifications, or messages. Other clients "
    "observe changes after reload or an explicit API "
    "refetch."
)

PUBLIC_CAPABILITIES_SCHEMA = {
    "type": "object",
    "readOnly": True,
    "additionalProperties": {
        "type": "object",
        "required": ["available", "revision", "limits"],
        "additionalProperties": False,
        "properties": {
            "available": {"type": "boolean"},
            "revision": {"type": "integer", "minimum": 1},
            "limits": {"type": "object", "additionalProperties": True},
            "unavailable_reason": {
                "type": "object",
                "nullable": True,
                "required": ["code", "message"],
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
    },
}

PROVIDER_SCHEMA = {
    "type": "object",
    "nullable": True,
    "readOnly": True,
    "required": ["kind", "account_uuid", "external_id", "capabilities"],
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["zulip"]},
        "account_uuid": {"type": "string", "format": "uuid"},
        "external_id": {"type": "string", "nullable": True},
        "capabilities": PUBLIC_CAPABILITIES_SCHEMA,
    },
}

DELIVERY_SCHEMA = {
    "type": "object",
    "nullable": True,
    "readOnly": True,
    "required": [
        "external_operation_uuid",
        "status",
        "safe_error",
        "can_retry",
        "can_discard",
        "updated_at",
        "duplicate_risk",
        "retry_requires_confirmation",
        "original_url",
        "reconciliation_reason",
    ],
    "additionalProperties": False,
    "properties": {
        "external_operation_uuid": {"type": "string", "format": "uuid"},
        "status": {
            "type": "string",
            "enum": [
                "pending",
                "delivered",
                "failed",
                "manual_reconciliation_required",
                "discarded",
            ],
        },
        "safe_error": {"type": "string", "nullable": True},
        "can_retry": {"type": "boolean"},
        "can_discard": {"type": "boolean"},
        "updated_at": {
            "type": "string",
            "format": "date-time",
            "nullable": True,
        },
        "duplicate_risk": {"type": "boolean"},
        "retry_requires_confirmation": {"type": "boolean"},
        "original_url": {"type": "string", "format": "uri", "nullable": True},
        "reconciliation_reason": {
            "type": "string",
            "nullable": True,
            "enum": [
                "provider_history_unavailable",
                "no_match_after_auto_resend",
                "unsafe_provider_state",
            ],
        },
    },
}

EVENT_CURSOR_SCHEMA = {
    "type": "object",
    "required": [
        "epoch_version",
        "epoch_generation",
        "current_epoch_version",
        "minimum_epoch_version",
    ],
    "properties": {
        "epoch_version": {"type": "integer", "minimum": 0},
        "epoch_generation": {"type": "string"},
        "current_epoch_version": {"type": "integer", "minimum": 0},
        "minimum_epoch_version": {"type": "integer", "minimum": 1},
    },
}

EVENT_CURSOR_EXPIRED_SCHEMA = {
    "type": "object",
    "required": [
        "type",
        "code",
        "error",
        "message",
        "reason",
        "epoch_generation",
        "current_epoch_version",
        "minimum_epoch_version",
    ],
    "properties": {
        "type": {"type": "string", "enum": ["EventsCursorExpiredError"]},
        "code": {"type": "integer", "enum": [410]},
        "error": {"type": "string", "enum": ["epoch_pruned"]},
        "message": {"type": "string"},
        "reason": {
            "type": "string",
            "enum": [
                "epoch_generation_required",
                "epoch_generation_changed",
                "future_epoch",
                "epoch_pruned",
            ],
        },
        "epoch_generation": {"type": "string"},
        "current_epoch_version": {"type": "integer", "minimum": 0},
        "minimum_epoch_version": {"type": "integer", "minimum": 1},
    },
}

EXTERNAL_CAPABILITIES_SCHEMA = {
    "type": "object",
    "readOnly": True,
    "additionalProperties": {
        "type": "object",
        "required": ["available", "revision", "limits"],
        "additionalProperties": False,
        "properties": {
            "available": {"type": "boolean"},
            "revision": {"type": "integer", "minimum": 1},
            "limits": {"type": "object", "additionalProperties": True},
            "unavailable_reason": {
                "type": "object",
                "nullable": True,
                "required": ["code", "message"],
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
    },
}

EXTERNAL_HISTORY_DEPTH_SCHEMA = {
    "type": "string",
    "enum": ["new", "7_days", "30_days", "90_days", "all"],
}

EXTERNAL_ACCOUNT_SETTINGS_PROPERTIES = {
    "kind": {"type": "string", "enum": ["zulip"]},
    "server_url": {"type": "string", "format": "uri", "maxLength": 2048},
    "email": {"type": "string", "format": "email", "maxLength": 320},
    "selection_mode": {"type": "string", "enum": ["explicit", "all"]},
    "history_depth": EXTERNAL_HISTORY_DEPTH_SCHEMA,
    "default_project_id": {"type": "string", "format": "uuid"},
}


def _object_schema(
    properties: dict[str, typing.Any],
    required: list[str],
) -> dict[str, typing.Any]:
    return {
        "type": "object",
        "required": required,
        "additionalProperties": False,
        "properties": copy.deepcopy(properties),
    }


def _request_body(schema: dict[str, typing.Any]) -> dict[str, typing.Any]:
    return {
        "required": True,
        "content": {"application/json": {"schema": schema}},
    }


def _external_response(
    reference: str,
    *,
    etag: bool = False,
) -> dict[str, typing.Any]:
    response = {
        "description": reference.rsplit("/", 1)[-1],
        "content": {"application/json": {"schema": {"$ref": reference}}},
    }
    if etag:
        response["headers"] = copy.deepcopy(DRAFT_ETAG_HEADER)
    return response


def add_external_bridge_public_contract(
    specification: dict[str, typing.Any],
    root: str,
    components: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    schemas = components["components"]["schemas"]
    account_settings = _object_schema(
        EXTERNAL_ACCOUNT_SETTINGS_PROPERTIES,
        [
            "kind",
            "server_url",
            "email",
            "selection_mode",
            "history_depth",
            "default_project_id",
        ],
    )
    for name in ("ExternalAccount_Filter", "ExternalAccount_Get"):
        properties = schemas[name]["properties"]
        properties["settings"] = copy.deepcopy(account_settings)
        properties["status"] = {
            "type": "string",
            "enum": [
                "connecting",
                "backfill",
                "live",
                "degraded",
                "auth_required",
                "disconnected",
                "suspended",
            ],
        }
        properties["capabilities"] = copy.deepcopy(EXTERNAL_CAPABILITIES_SCHEMA)
        properties.pop("owner_user_uuid", None)
        properties.pop("provider", None)

    create_settings = copy.deepcopy(EXTERNAL_ACCOUNT_SETTINGS_PROPERTIES)
    create_settings["api_key"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 4096,
        "writeOnly": True,
    }
    create_schema = _object_schema(
        {
            "uuid": {"type": "string", "format": "uuid"},
            "settings": _object_schema(
                create_settings,
                [
                    "kind",
                    "server_url",
                    "email",
                    "api_key",
                    "default_project_id",
                ],
            ),
        },
        ["uuid", "settings"],
    )
    update_schema = _object_schema(
        {
            "settings": _object_schema(
                {
                    key: EXTERNAL_ACCOUNT_SETTINGS_PROPERTIES[key]
                    for key in (
                        "kind",
                        "selection_mode",
                        "history_depth",
                        "default_project_id",
                    )
                },
                ["kind", "selection_mode", "history_depth", "default_project_id"],
            )
        },
        ["settings"],
    )
    reconnect_schema = _object_schema(
        {
            "settings": _object_schema(
                {
                    **{
                        key: EXTERNAL_ACCOUNT_SETTINGS_PROPERTIES[key]
                        for key in ("kind", "server_url", "email")
                    },
                    "api_key": create_settings["api_key"],
                },
                ["kind", "server_url", "email", "api_key"],
            )
        },
        ["settings"],
    )
    account_collection = specification["paths"][f"{root}external_accounts/"]
    account_collection["get"]["parameters"] = [
        parameter
        for parameter in account_collection["get"]["parameters"]
        if parameter["name"] in {"status", "page_limit", "page_marker"}
    ]
    account_collection["post"]["requestBody"] = _request_body(create_schema)
    account_collection["post"]["responses"][201] = _external_response(
        "#/components/schemas/ExternalAccount_Get",
        etag=True,
    )
    account_path = f"{root}external_accounts/{{ExternalAccountUuid}}"
    account_resource = specification["paths"][account_path]
    account_resource["get"]["responses"][200] = _external_response(
        "#/components/schemas/ExternalAccount_Get",
        etag=True,
    )
    account_resource["put"]["requestBody"] = _request_body(update_schema)
    account_resource["put"]["parameters"].append(
        copy.deepcopy(DRAFT_IF_MATCH_PARAMETER)
    )
    account_resource["put"]["responses"][200] = _external_response(
        "#/components/schemas/ExternalAccount_Get",
        etag=True,
    )
    reconnect_path = f"{account_path}/actions/reconnect/invoke"
    reconnect = specification["paths"][reconnect_path]["post"]
    reconnect["requestBody"] = _request_body(reconnect_schema)
    reconnect["parameters"].append(copy.deepcopy(DRAFT_IF_MATCH_PARAMETER))
    reconnect["responses"][200] = _external_response(
        "#/components/schemas/ExternalAccount_Get",
        etag=True,
    )
    disconnect_path = f"{account_path}/actions/disconnect/invoke"
    specification["paths"][disconnect_path]["post"]["responses"][200] = (
        _external_response("#/components/schemas/ExternalAccount_Get", etag=True)
    )

    chat_source = _object_schema(
        {
            "kind": {"type": "string", "enum": ["zulip"]},
            "chat_type": {
                "type": "string",
                "enum": ["channel", "personal", "group"],
            },
            "original_url": {
                "type": "string",
                "format": "uri",
                "nullable": True,
            },
        },
        ["kind", "chat_type"],
    )
    for name in ("ExternalChat_Filter", "ExternalChat_Get"):
        properties = schemas[name]["properties"]
        properties["source"] = copy.deepcopy(chat_source)
        properties["capabilities"] = copy.deepcopy(EXTERNAL_CAPABILITIES_SCHEMA)
        properties["history_depth"] = copy.deepcopy(EXTERNAL_HISTORY_DEPTH_SCHEMA)
        for internal in ("owner_user_uuid", "provider", "provider_chat_id"):
            properties.pop(internal, None)
    chat_collection = specification["paths"][f"{root}external_chats/"]["get"]
    chat_collection["parameters"] = [
        {
            "name": "external_account_uuid",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        },
        *[
            parameter
            for parameter in chat_collection["parameters"]
            if parameter["name"] in {"page_limit", "page_marker"}
        ],
    ]
    chat_path = f"{root}external_chats/{{ExternalChatUuid}}"
    assignment_body = _request_body(
        _object_schema(
            {"project_id": {"type": "string", "format": "uuid"}},
            ["project_id"],
        )
    )
    for action in ("select", "move"):
        operation = specification["paths"][f"{chat_path}/actions/{action}/invoke"][
            "post"
        ]
        operation["requestBody"] = copy.deepcopy(assignment_body)
        operation["responses"][200] = _external_response(
            "#/components/schemas/ExternalChat_Get",
            etag=True,
        )
        if action == "move":
            operation["parameters"].append(copy.deepcopy(DRAFT_IF_MATCH_PARAMETER))
    deselect = specification["paths"][f"{chat_path}/actions/deselect/invoke"]["post"]
    deselect["responses"][200] = _external_response(
        "#/components/schemas/ExternalChat_Get",
        etag=True,
    )

    operation_collection = specification["paths"][f"{root}external_operations/"]["get"]
    operation_statuses = [
        "queued",
        "running",
        "succeeded",
        "failed",
        "manual_reconciliation_required",
        "discarded",
    ]
    reconciliation_states = [
        "not_required",
        "delayed_check",
        "committed_match",
        "automatic_resend_queued",
        "manual_required",
    ]
    reconciliation_reasons = [
        "provider_history_unavailable",
        "no_match_after_auto_resend",
        "unsafe_provider_state",
    ]
    for name in ("ExternalOperation_Filter", "ExternalOperation_Get"):
        properties = schemas[name]["properties"]
        properties["status"] = {
            "type": "string",
            "enum": copy.deepcopy(operation_statuses),
        }
        properties["reconciliation_state"] = {
            "type": "string",
            "enum": copy.deepcopy(reconciliation_states),
            "readOnly": True,
        }
        properties["reconciliation_reason"] = {
            "type": "string",
            "enum": copy.deepcopy(reconciliation_reasons),
            "nullable": True,
            "readOnly": True,
        }
        properties["reconciliation_evidence"] = {
            "type": "object",
            "readOnly": True,
            "description": (
                "Sanitized reconciliation summary. Provider match inputs and "
                "raw search results, including equivalent-match counts, are "
                "never exposed."
            ),
            "additionalProperties": True,
        }
        properties["attempt_history"] = {
            "type": "array",
            "readOnly": True,
            "items": {
                "type": "object",
                "required": [
                    "attempt",
                    "status",
                    "safe_error",
                    "duplicate_risk",
                    "original_url",
                    "reconciliation_state",
                    "reconciliation_reason",
                ],
                "additionalProperties": False,
                "properties": {
                    "attempt": {"type": "integer", "minimum": 0},
                    "status": {"type": "string", "enum": operation_statuses},
                    "safe_error": {"type": "string", "nullable": True},
                    "duplicate_risk": {"type": "boolean"},
                    "original_url": {
                        "type": "string",
                        "format": "uri",
                        "nullable": True,
                    },
                    "reconciliation_state": {
                        "type": "string",
                        "enum": reconciliation_states,
                    },
                    "reconciliation_reason": {
                        "type": "string",
                        "enum": reconciliation_reasons,
                        "nullable": True,
                    },
                },
            },
        }
        properties.pop("owner_user_uuid", None)
    operation_collection["parameters"] = [
        {
            "name": "external_account_uuid",
            "in": "query",
            "schema": {"type": "string", "format": "uuid"},
        },
        {
            "name": "status",
            "in": "query",
            "schema": {
                "type": "string",
                "enum": copy.deepcopy(operation_statuses),
            },
        },
        *[
            parameter
            for parameter in operation_collection["parameters"]
            if parameter["name"] in {"page_limit", "page_marker"}
        ],
    ]
    operation_path = f"{root}external_operations/{{ExternalOperationUuid}}"
    retry = specification["paths"][f"{operation_path}/actions/retry/invoke"]["post"]
    retry["requestBody"] = _request_body(
        _object_schema(
            {
                "confirm_duplicate_risk": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Must be true when retry_requires_confirmation is true. "
                        "The retry may duplicate a provider-side operation."
                    ),
                }
            },
            [],
        ),
    )
    retry["responses"][200] = _external_response(
        "#/components/schemas/ExternalOperation_Get",
        etag=True,
    )
    for path in list(specification["paths"]):
        if path.startswith(f"{root}external_operations/actions"):
            del specification["paths"][path]
    preflight_path = f"{root}external_operations/actions/preflight/invoke"
    specification["paths"][preflight_path] = {
        "post": {
            "summary": "Preflight an external operation",
            "tags": ["ExternalOperation"],
            "operationId": "Preflight_external_operation",
            "requestBody": _request_body(
                _object_schema(
                    {
                        "external_account_uuid": {
                            "type": "string",
                            "format": "uuid",
                        },
                        "action": {"type": "string", "minLength": 1},
                        "target": _object_schema(
                            {
                                "type": {"type": "string", "minLength": 1},
                                "uuid": {
                                    "type": "string",
                                    "format": "uuid",
                                    "nullable": True,
                                },
                            },
                            ["type"],
                        ),
                    },
                    ["external_account_uuid", "action", "target"],
                )
            ),
            "responses": {
                200: {
                    "description": "Capability and loss preflight",
                    "content": {
                        "application/json": {
                            "schema": _object_schema(
                                {
                                    "allowed": {"type": "boolean"},
                                    "action": {"type": "string"},
                                    "target": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["type"],
                                        "properties": {
                                            "type": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                            "uuid": {
                                                "type": "string",
                                                "format": "uuid",
                                                "nullable": True,
                                            },
                                        },
                                    },
                                    "losses": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "additionalProperties": True,
                                        },
                                    },
                                    "requires_confirmation": {"type": "boolean"},
                                },
                                [
                                    "allowed",
                                    "action",
                                    "target",
                                    "losses",
                                    "requires_confirmation",
                                ],
                            )
                        }
                    },
                }
            },
        }
    }
    if root.startswith("/v1/messenger/"):
        specification["paths"][preflight_path]["post"]["security"] = [
            {"bearerAuth": []}
        ]
    policy_parameter = {
        "name": "kind",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "enum": ["zulip"]},
    }
    policy_path = f"{root}external_provider_policies/"
    generated_policy_path = f"{policy_path}{{ExternalProviderPolicyProvider}}"
    canonical_policy_path = f"{policy_path}{{kind}}"
    specification["paths"][canonical_policy_path] = specification["paths"].pop(
        generated_policy_path
    )
    policy_resource = specification["paths"][canonical_policy_path]
    for operation in policy_resource.values():
        operation["parameters"] = [copy.deepcopy(policy_parameter)]
    policy_resource["put"]["parameters"].append(copy.deepcopy(DRAFT_IF_MATCH_PARAMETER))
    for method in ("get", "put"):
        policy_resource[method]["responses"][200] = _external_response(
            "#/components/schemas/ExternalProviderPolicy_Get",
            etag=True,
        )
    policy_settings = _object_schema(
        {
            "kind": {"type": "string", "enum": ["zulip"]},
            "enabled": {"type": "boolean"},
            "limits": _object_schema(
                {
                    "max_accounts": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100000,
                    },
                    "max_selected_chats_per_account": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1000000,
                    },
                    "max_file_bytes": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5368709120,
                    },
                },
                [
                    "max_accounts",
                    "max_selected_chats_per_account",
                    "max_file_bytes",
                ],
            ),
            "custom_ca_bundle": {
                "type": "object",
                "nullable": True,
                "additionalProperties": False,
                "required": ["certificates_pem"],
                "properties": {
                    "certificates_pem": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "items": {"type": "string", "format": "pem-certificate"},
                    }
                },
            },
        },
        ["kind", "enabled", "limits", "custom_ca_bundle"],
    )
    policy_resource["put"]["requestBody"] = _request_body(
        _object_schema({"settings": policy_settings}, ["settings"])
    )
    policy_schema = schemas["ExternalProviderPolicy_Get"]
    policy_schema["properties"]["provider"] = {
        "type": "string",
        "enum": ["zulip"],
        "readOnly": True,
    }
    policy_schema["properties"]["limits"] = copy.deepcopy(
        policy_settings["properties"]["limits"]
    )
    policy_schema["properties"]["custom_ca_bundle"] = {
        "type": "object",
        "nullable": True,
        "readOnly": True,
        "additionalProperties": False,
        "required": ["uuid", "generation", "sha256", "certificate_count"],
        "properties": {
            "uuid": {"type": "string", "format": "uuid"},
            "generation": {"type": "integer", "minimum": 1},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "certificate_count": {"type": "integer", "minimum": 1, "maximum": 32},
        },
    }
    for action in ("suspend", "resume"):
        generated_action_path = f"{generated_policy_path}/actions/{action}/invoke"
        canonical_action_path = f"{canonical_policy_path}/actions/{action}/invoke"
        specification["paths"][canonical_action_path] = specification["paths"].pop(
            generated_action_path
        )
        specification["paths"][canonical_action_path]["post"]["parameters"] = [
            copy.deepcopy(policy_parameter)
        ]
    generated_health_path = (
        f"{root}external_provider_health/{{ExternalProviderHealthProvider}}"
    )
    canonical_health_path = f"{root}external_provider_health/{{kind}}"
    specification["paths"][canonical_health_path] = specification["paths"].pop(
        generated_health_path
    )
    specification["paths"][canonical_health_path]["get"]["parameters"] = [
        copy.deepcopy(policy_parameter)
    ]
    return specification


def add_avatar_upload_schema(
    specification: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    schemas = specification["components"]["schemas"]
    schemas.setdefault(
        "WorkspaceUser_AvatarUpload",
        copy.deepcopy(schemas["WorkspaceUser_Get"]),
    )
    return specification


def add_public_projection_contract(
    specification: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    schemas = specification["components"]["schemas"]
    projection_schema_prefixes = (
        "WorkspaceUserStream_",
        "WorkspaceUserTopic_",
        "WorkspaceUserMessage_",
        "WorkspaceMessageReactions_",
    )
    for name, schema in schemas.items():
        if name.startswith(projection_schema_prefixes):
            properties = schema["properties"]
            properties["provider"] = copy.deepcopy(PROVIDER_SCHEMA)
            properties["delivery"] = copy.deepcopy(DELIVERY_SCHEMA)
        if name in ("WorkspaceUser_Filter", "WorkspaceUser_Get"):
            properties = schema["properties"]
            properties["identity_kind"] = {
                "type": "string",
                "enum": ["external"],
                "readOnly": True,
            }
            properties["display_name"] = {"type": "string", "readOnly": True}
            properties["provider"] = {
                "type": "object",
                "nullable": True,
                "readOnly": True,
                "required": ["kind", "account_uuid"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["zulip"]},
                    "account_uuid": {"type": "string", "format": "uuid"},
                },
            }
    return specification


def _pagination_marker_schema(path: str) -> dict[str, typing.Any]:
    if path.endswith("/events/"):
        return {"type": "integer", "minimum": 0}
    return {"type": "string", "format": "uuid"}


def add_collection_pagination_contract(
    specification: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    for path, path_item in specification["paths"].items():
        operation = path_item.get("get")
        if operation is None:
            continue
        response = operation.get("responses", {}).get(200, {})
        schema = (
            response.get("content", {}).get("application/json", {}).get("schema", {})
        )
        if schema.get("type") != "array":
            continue
        marker_schema = _pagination_marker_schema(path)
        parameters = operation.setdefault("parameters", [])
        existing = {(parameter["in"], parameter["name"]) for parameter in parameters}
        for parameter in (
            PAGINATION_LIMIT_PARAMETER,
            {
                "name": "page_marker",
                "in": "query",
                "description": "Last resource identifier from the previous page.",
                "schema": marker_schema,
            },
        ):
            if (parameter["in"], parameter["name"]) not in existing:
                parameters.append(copy.deepcopy(parameter))
        operation["responses"][200]["headers"] = {
            "X-Pagination-Limit": {
                "description": "Requested page limit.",
                "schema": {"type": "integer"},
            },
            "X-Pagination-Marker": {
                "description": (
                    "Continuation marker. Present only when another resource exists."
                ),
                "schema": copy.deepcopy(marker_schema),
            },
        }
    return specification


def add_message_pagination_contract(
    specification: dict[str, typing.Any],
    path: str,
) -> dict[str, typing.Any]:
    operation = specification["paths"][path]["get"]
    parameters = operation.setdefault("parameters", [])
    existing = {(parameter["in"], parameter["name"]) for parameter in parameters}
    parameters.extend(
        copy.deepcopy(parameter)
        for parameter in MESSAGE_PAGINATION_PARAMETERS
        if (parameter["in"], parameter["name"]) not in existing
    )
    operation["responses"][200]["headers"] = copy.deepcopy(MESSAGE_PAGINATION_HEADERS)
    return specification


def add_draft_contract(
    specification: dict[str, typing.Any],
    path: str,
    components: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    schemas = components["components"]["schemas"]
    collection = specification["paths"][path]
    operation = collection["get"]
    operation["description"] = DRAFT_SIDE_EFFECTS_DESCRIPTION
    operation["parameters"] = copy.deepcopy(DRAFT_PAGINATION_PARAMETERS)
    operation["responses"][200]["headers"] = copy.deepcopy(MESSAGE_PAGINATION_HEADERS)

    create = collection["post"]
    create["description"] = DRAFT_SIDE_EFFECTS_DESCRIPTION
    create_schema = schemas["WorkspaceDraft_Create"]
    create["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "object",
        "required": ["uuid", "stream_uuid", "topic_uuid", "payload"],
        "properties": {
            name: {
                key: value
                for key, value in copy.deepcopy(
                    create_schema["properties"][name]
                ).items()
                if key != "readOnly"
            }
            for name in ("uuid", "stream_uuid", "topic_uuid", "payload")
        },
    }
    create["requestBody"]["content"]["application/json"]["schema"]["properties"][
        "payload"
    ] = copy.deepcopy(DRAFT_PAYLOAD_SCHEMA)
    create["responses"][200] = copy.deepcopy(create["responses"][201])
    create["responses"][200]["description"] = (
        "Existing draft returned for an identical idempotent create."
    )
    for status in (200, 201):
        create["responses"][status]["headers"] = copy.deepcopy(DRAFT_ETAG_HEADER)
    create["responses"][409] = {
        "description": "The UUID exists with different canonical create fields.",
        "content": {
            "application/json": {
                "schema": copy.deepcopy(DRAFT_ERROR_SCHEMA),
            }
        },
    }

    resource_path = f"{path}{{WorkspaceDraftUuid}}"
    resource = specification["paths"][resource_path]
    resource["get"]["description"] = DRAFT_SIDE_EFFECTS_DESCRIPTION
    resource["get"]["responses"][200]["headers"] = copy.deepcopy(DRAFT_ETAG_HEADER)
    for method in ("put", "delete"):
        mutation = resource[method]
        mutation["description"] = DRAFT_SIDE_EFFECTS_DESCRIPTION
        mutation.setdefault("parameters", []).append(
            copy.deepcopy(DRAFT_IF_MATCH_PARAMETER)
        )
        mutation["responses"][412] = {
            "description": "Revision mismatch; body and ETag contain current draft.",
            "headers": copy.deepcopy(DRAFT_ETAG_HEADER),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["current"],
                        "properties": {
                            "current": {
                                "$ref": "#/components/schemas/WorkspaceDraft_Get"
                            }
                        },
                    }
                }
            },
        }
        mutation["responses"][428] = {
            "description": "If-Match is required.",
            "content": {
                "application/json": {
                    "schema": copy.deepcopy(DRAFT_ERROR_SCHEMA),
                }
            },
        }
    resource["put"]["responses"][200]["headers"] = copy.deepcopy(DRAFT_ETAG_HEADER)
    resource["put"]["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "object",
        "required": ["payload"],
        "additionalProperties": False,
        "properties": {"payload": copy.deepcopy(DRAFT_PAYLOAD_SCHEMA)},
    }
    return specification


def add_current_user_contract(
    specification: dict[str, typing.Any],
    path: str,
) -> dict[str, typing.Any]:
    operation = specification["paths"][path]["get"]
    operation["parameters"] = []
    operation["responses"][200] = {
        "description": "WorkspaceUser_Get",
        "content": {
            "application/json": {
                "schema": {
                    "$ref": "#/components/schemas/WorkspaceUser_Get",
                },
            },
        },
    }
    return specification


def add_events_cursor_contract(
    specification: dict[str, typing.Any],
    events_path: str,
    epoch_path: str,
) -> dict[str, typing.Any]:
    operation = specification["paths"][events_path]["get"]
    parameters = operation.setdefault("parameters", [])
    if not any(parameter["name"] == "epoch_generation" for parameter in parameters):
        parameters.append(
            {
                "name": "epoch_generation",
                "in": "query",
                "description": "Generation paired with a non-zero epoch cursor.",
                "schema": {"type": "string"},
            }
        )
    operation["responses"][410] = {
        "description": "The retained event journal cannot satisfy the cursor.",
        "headers": {
            "Cache-Control": {
                "schema": {"type": "string", "enum": ["no-store"]},
            }
        },
        "content": {
            "application/json": {"schema": copy.deepcopy(EVENT_CURSOR_EXPIRED_SCHEMA)}
        },
    }
    specification["paths"][epoch_path]["get"]["responses"][200]["content"] = {
        "application/json": {"schema": copy.deepcopy(EVENT_CURSOR_SCHEMA)}
    }
    return specification
