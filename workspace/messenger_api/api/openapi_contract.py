# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import copy
import typing

from workspace.messenger_api.dm import message_payloads


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
        "content": {
            "type": "string",
            "minLength": 1,
            "maxLength": message_payloads.MARKDOWN_CONTENT_MAX_LENGTH,
        },
    },
}

DRAFT_ERROR_SCHEMA = {
    "type": "object",
    "required": ["message"],
    "additionalProperties": False,
    "properties": {"message": {"type": "string"}},
}

TOPIC_SUMMARY_ERROR_SCHEMA = {
    "type": "object",
    "required": ["message"],
    "additionalProperties": True,
    "properties": {"message": {"type": "string"}},
}

DRAFT_SIDE_EFFECTS_DESCRIPTION = (
    "Drafts are PostgreSQL-only client state. This operation emits no Workspace "
    "events, websocket or desktop notifications, or messages. Other clients "
    "observe changes after reload or an explicit API "
    "refetch."
)


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


def _component_schema(
    schemas: dict[str, typing.Any],
    name: str,
) -> dict[str, typing.Any]:
    schema = schemas[name]
    reference = schema.get("$ref")
    if reference is None:
        return schema
    return schemas[reference.removeprefix("#/components/schemas/")]


def _component_parameter(
    components: dict[str, typing.Any],
    parameter: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    reference = parameter.get("$ref")
    if reference is None:
        return parameter
    name = reference.removeprefix("#/components/parameters/")
    return components["components"]["parameters"][name]


def _object_schema(
    properties: dict[str, typing.Any],
    required: list[str],
) -> dict[str, typing.Any]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": copy.deepcopy(properties),
    }
    if required:
        schema["required"] = required
    return schema


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
    for name in schemas:
        schema = _component_schema(schemas, name)
        if name.startswith("WorkspaceUserMessage_"):
            payload = schema["properties"]["payload"]
            payload.pop("example", None)
            payload["description"] = (
                "Fixed message payload selected by kind. Only markdown is "
                "interpreted as Markdown and used for mentions and summaries. "
                "Other kinds remain opaque in history and realtime."
            )
            for variant in payload["oneOf"]:
                variant["required"] = list(variant["properties"])
                variant["additionalProperties"] = False
                variant.pop("example", None)
            schema["properties"]["reaction_users"] = {
                "type": "object",
                "readOnly": True,
                "default": {},
                "description": (
                    "Complete user UUID lists persisted on reaction writes. "
                    "An empty object or missing emoji key means count-only; "
                    "lists are never partial."
                ),
                "example": {
                    "heart": [
                        "11111111-1111-1111-1111-111111111111",
                        "33333333-3333-3333-3333-333333333333",
                    ]
                },
                "additionalProperties": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "format": "uuid"},
                },
            }
        if name.startswith("WorkspaceUserStream_"):
            schema["properties"]["encryption"].update(
                {
                    "readOnly": True,
                    "description": (
                        "Shared stream encryption flag, immutable after creation. "
                        "Zulip streams cannot enable encryption."
                    ),
                }
            )
            schema["properties"]["current_encryption_key_uuid"].update(
                {
                    "readOnly": True,
                    "description": (
                        "Current symmetric key UUID, or null before a key is set. "
                        "Changes only through an owner key_announced message; "
                        "no stream key material is stored."
                    ),
                }
            )
            for field in ("encryption", "current_encryption_key_uuid"):
                if field not in schema["required"]:
                    schema["required"].append(field)
    return specification


def add_message_encryption_contract(
    specification: dict[str, typing.Any],
    root: str,
    components: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    schemas = components["components"]["schemas"]
    stream_properties = _component_schema(schemas, "WorkspaceUserStream_Create")[
        "properties"
    ]
    create_properties = copy.deepcopy(
        {
            name: stream_properties[name]
            for name in (
                "uuid",
                "name",
                "description",
                "source_name",
                "invite_only",
                "announce",
                "direct_user_uuid",
                "private",
                "color",
                "encryption",
                "current_encryption_key_uuid",
            )
        }
    )
    for property_schema in create_properties.values():
        property_schema.pop("readOnly", None)
    create_properties["source_name"].update({"enum": ["native"], "default": "native"})
    create_schema = _object_schema(create_properties, ["name"])
    create_schema["oneOf"] = [
        {
            "properties": {
                "encryption": {"enum": [False]},
                "current_encryption_key_uuid": {
                    "type": "string",
                    "nullable": True,
                    "enum": [None],
                },
            },
        },
        {
            "required": ["encryption", "current_encryption_key_uuid"],
            "properties": {
                "encryption": {"enum": [True]},
                "current_encryption_key_uuid": {
                    "type": "string",
                    "format": "uuid",
                    "nullable": False,
                },
            },
        },
    ]
    streams = specification["paths"][f"{root}streams/"]
    streams["post"]["requestBody"] = _request_body(create_schema)
    streams["post"]["description"] = (
        "Create a native stream. encryption defaults to false and cannot "
        "change after creation. True requires current_encryption_key_uuid; "
        "false accepts only an absent or null key UUID. Zulip streams are "
        "provider-managed and cannot enable encryption."
    )
    stream_update = specification["paths"][
        f"{root}streams/{{WorkspaceUserStreamUuid}}"
    ]["put"]
    update_schema = _object_schema(
        {
            name: stream_properties[name]
            for name in ("name", "description", "invite_only", "announce", "color")
        },
        [],
    )
    update_schema["minProperties"] = 1
    stream_update["requestBody"] = _request_body(update_schema)
    stream_update["description"] = (
        "Update stream metadata. encryption and current_encryption_key_uuid "
        "are forbidden, including unchanged values. Only an owner "
        "key_announced message may change the current key UUID."
    )
    message_properties = _component_schema(schemas, "WorkspaceUserMessage_Create")[
        "properties"
    ]
    message_create = specification["paths"][f"{root}messages/"]["post"]
    message_create_schema = _object_schema(
        {
            name: message_properties[name]
            for name in ("uuid", "stream_uuid", "topic_uuid", "payload")
        },
        ["stream_uuid", "payload"],
    )
    for property_schema in message_create_schema["properties"].values():
        property_schema.pop("readOnly", None)
    message_create_schema["properties"]["uuid"]["description"] = (
        "Optional canonical creation UUID for retries; the response UUID "
        "identifies the placement."
    )
    message_create_schema["properties"]["topic_uuid"].update(
        {
            "nullable": True,
            "description": "An absent or null topic UUID selects the default topic.",
        }
    )
    message_create["requestBody"] = _request_body(message_create_schema)
    message_create["description"] = (
        "Send a message; an absent or null topic_uuid selects the stream's "
        "default topic. All six payload kinds are allowed regardless of "
        "encryption, except e2ee is forbidden for a shared Zulip stream. "
        "Only the owner role may send key_announced; saving the message and "
        "updating current_encryption_key_uuid is atomic. Old e2ee key UUIDs "
        "are accepted. The backend does not validate Base64, encrypt, or store "
        "plaintext private or symmetric keys."
    )
    editable_payload = copy.deepcopy(message_properties["payload"])
    editable_payload["oneOf"] = [
        variant
        for variant in editable_payload["oneOf"]
        if variant["properties"]["kind"]["enum"] != ["key_announced"]
    ]
    message_update = specification["paths"][
        f"{root}messages/{{WorkspaceUserMessageUuid}}"
    ]["put"]
    message_update["requestBody"] = _request_body(
        _object_schema({"payload": editable_payload}, ["payload"])
    )
    message_update["description"] = (
        "Replace a message payload as its author. PUT cannot create or edit "
        "key_announced messages. e2ee is rejected if any placement of the "
        "canonical message belongs to a shared Zulip stream."
    )
    for operation in (streams["post"], stream_update, message_create, message_update):
        operation["responses"][400] = {
            "description": "Invalid payload, encryption metadata, kind, or stream role.",
        }
    return specification


def _pagination_marker_schema(path: str) -> dict[str, typing.Any]:
    if path.endswith("/events/"):
        return {"type": "integer", "minimum": 0}
    return {"type": "string", "format": "uuid"}


def add_collection_pagination_contract(
    specification: dict[str, typing.Any],
    components: dict[str, typing.Any],
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
        resolved_parameters = (
            _component_parameter(components, parameter) for parameter in parameters
        )
        existing = {
            (parameter["in"], parameter["name"]) for parameter in resolved_parameters
        }
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
    components: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    operation = specification["paths"][path]["get"]
    parameters = operation.setdefault("parameters", [])
    resolved_parameters = (
        _component_parameter(components, parameter) for parameter in parameters
    )
    existing = {
        (parameter["in"], parameter["name"]) for parameter in resolved_parameters
    }
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
    create_schema = _component_schema(schemas, "WorkspaceDraft_Create")
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


def add_topic_summary_contract(
    specification: dict[str, typing.Any],
    root: str,
    components: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    schemas = components["components"]["schemas"]
    summary_properties = {
        "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
            "nullable": True,
            "readOnly": True,
            "description": "Latest LLM-generated topic summary.",
        },
        "summary_last_message_uuid": {
            "type": "string",
            "format": "uuid",
            "nullable": True,
            "readOnly": True,
            "description": "Latest topic message included in the summary.",
        },
        "summary_has_new_messages": {
            "type": "boolean",
            "nullable": True,
            "readOnly": True,
            "description": (
                "Null without a summary; otherwise whether the current latest "
                "message differs from the stored summary boundary."
            ),
        },
        "summary_enabled": {
            "type": "boolean",
            "readOnly": True,
            "description": (
                "Whether the server-side worker may generate summaries for "
                "this topic. Existing summary text remains readable when false."
            ),
        },
        "summary_system_prompt": {
            "type": "string",
            "minLength": 1,
            "maxLength": 16384,
            "nullable": True,
            "readOnly": True,
            "description": (
                "Topic-specific LLM system prompt, or null for the default."
            ),
        },
        "summary_reasoning_effort": {
            "type": "string",
            "enum": ["off", "minimal", "low", "medium", "high"],
            "nullable": True,
            "readOnly": True,
            "description": (
                "Per-summary reasoning choice. Off explicitly disables "
                "reasoning; null omits the provider option."
            ),
        },
    }
    for name in schemas:
        if name.startswith("WorkspaceUserTopic_"):
            schema = _component_schema(schemas, name)
            schema["properties"].update(copy.deepcopy(summary_properties))

    topic_path = f"{root}stream_topics/{{WorkspaceUserTopicUuid}}"
    response = _external_response(
        "#/components/schemas/WorkspaceUserTopic_Get",
    )
    prompt_action = specification["paths"][
        f"{topic_path}/actions/set_summary_prompt/invoke"
    ]["post"]
    prompt_action["description"] = (
        "Update topic-specific summary configuration. A null prompt restores "
        "the default; summary_enabled controls whether the worker may refresh "
        "this topic. The caller must be a stream owner or administrator."
    )
    prompt_schema = _object_schema(
        {
            "summary_system_prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16384,
                "nullable": True,
            },
            "summary_reasoning_effort": {
                "type": "string",
                "enum": ["off", "minimal", "low", "medium", "high"],
                "nullable": True,
                "description": (
                    "Per-summary reasoning choice. Off explicitly disables "
                    "reasoning; null omits the provider option."
                ),
            },
            "summary_enabled": {"type": "boolean"},
        },
        [],
    )
    prompt_schema["minProperties"] = 1
    prompt_action["requestBody"] = _request_body(prompt_schema)
    prompt_action["responses"][200] = copy.deepcopy(response)
    prompt_action["responses"][403] = {
        "description": "The stream role cannot update summary configuration.",
        "content": {
            "application/json": {
                "schema": copy.deepcopy(TOPIC_SUMMARY_ERROR_SCHEMA),
            }
        },
    }
    return specification


def add_topic_summary_management_contract(
    specification: dict[str, typing.Any],
    root: str,
    components: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    del components
    endpoint_properties = {
        "name": {"type": "string", "minLength": 1, "maxLength": 255},
        "base_url": {
            "type": "string",
            "format": "uri",
            "maxLength": 2048,
            "description": "OpenAI-compatible API base URL.",
        },
        "model": {"type": "string", "minLength": 1, "maxLength": 255},
        "enabled": {"type": "boolean", "default": True},
        "priority": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000,
            "default": 100,
            "description": "Lower values are selected first; UUID breaks ties.",
        },
        "supports_vision": {"type": "boolean", "default": False},
        "supports_reasoning": {"type": "boolean", "default": False},
        "temperature": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 2.0,
            "default": 0.2,
        },
        "max_output_tokens": {
            "type": "integer",
            "minimum": 1,
            "maximum": 32768,
            "default": 512,
        },
        "top_p": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 1.0,
        },
        "presence_penalty": {
            "type": "number",
            "minimum": -2.0,
            "maximum": 2.0,
            "default": 0.0,
        },
        "frequency_penalty": {
            "type": "number",
            "minimum": -2.0,
            "maximum": 2.0,
            "default": 0.0,
        },
        "api_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 8192,
            "writeOnly": True,
            "description": (
                "Stored encrypted by the server and never returned by any API."
            ),
        },
    }
    collection = specification["paths"][f"{root}topic_summary_endpoints/"]
    collection["get"]["parameters"] = []
    collection["get"]["description"] = (
        "List global endpoints in deterministic priority and UUID order. "
        "Requires workspace.topic_summary_endpoint.manage."
    )
    collection["post"]["description"] = (
        "Create a global OpenAI-compatible LLM endpoint. Requires "
        "workspace.topic_summary_endpoint.manage."
    )
    collection["post"]["requestBody"] = _request_body(
        _object_schema(
            {
                "uuid": {"type": "string", "format": "uuid"},
                **copy.deepcopy(endpoint_properties),
            },
            ["uuid", "name", "base_url", "model", "api_key"],
        )
    )
    endpoint_path = next(
        path
        for path in specification["paths"]
        if path.startswith(f"{root}topic_summary_endpoints/{{")
    )
    resource = specification["paths"][endpoint_path]
    resource["get"]["description"] = (
        "Read a global endpoint. Requires workspace.topic_summary_endpoint.manage."
    )
    resource["put"]["description"] = (
        "Update, enable, disable, or reprioritize a global endpoint. "
        "Requires workspace.topic_summary_endpoint.manage."
    )
    update_schema = _object_schema(copy.deepcopy(endpoint_properties), [])
    update_schema["minProperties"] = 1
    resource["put"]["requestBody"] = _request_body(update_schema)
    resource["delete"]["description"] = (
        "Delete a global endpoint. Requires workspace.topic_summary_endpoint.manage."
    )

    settings_path = next(
        path
        for path in specification["paths"]
        if path.startswith(f"{root}topic_summary_settings/{{")
    )
    settings = specification["paths"][settings_path]
    settings_parameter_name = settings_path.rsplit("{", 1)[1].removesuffix("}")
    settings_parameter = {
        "name": settings_parameter_name,
        "in": "path",
        "required": True,
        "description": "Current Workspace project UUID.",
        "schema": {"type": "string", "format": "uuid"},
    }
    for operation in settings.values():
        operation["parameters"] = [copy.deepcopy(settings_parameter)]
    settings["get"]["description"] = (
        "Read the global and current-project topic summarization gates."
    )
    settings["put"]["description"] = (
        "Set both topic summarization gates. The worker runs only when both "
        "are enabled. Requires workspace.topic_summary_settings.manage."
    )
    settings["put"]["requestBody"] = _request_body(
        _object_schema(
            {
                "global_enabled": {"type": "boolean"},
                "project_enabled": {"type": "boolean"},
            },
            ["global_enabled", "project_enabled"],
        )
    )
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
    components: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    operation = specification["paths"][events_path]["get"]
    parameters = operation.setdefault("parameters", [])
    if not any(
        _component_parameter(components, parameter)["name"] == "epoch_generation"
        for parameter in parameters
    ):
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
