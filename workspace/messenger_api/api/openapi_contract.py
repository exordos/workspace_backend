# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import copy


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
    "events, websocket or desktop notifications, messages, or IMAP/Maildir "
    "records. Other clients observe changes after reload or an explicit API "
    "refetch."
)

PROVIDER_SCHEMA = {
    "type": "object",
    "nullable": True,
    "readOnly": True,
    "required": ["uuid", "name", "kind"],
    "properties": {
        "uuid": {"type": "string", "format": "uuid"},
        "name": {"type": "string"},
        "kind": {"type": "string"},
    },
}

DELIVERY_SCHEMA = {
    "type": "object",
    "nullable": True,
    "readOnly": True,
    "required": ["status", "safe_error", "updated_at"],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["pending", "delivered", "failed"],
        },
        "safe_error": {"type": "string", "nullable": True},
        "updated_at": {
            "type": "string",
            "format": "date-time",
            "nullable": True,
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


def add_avatar_upload_schema(specification):
    schemas = specification["components"]["schemas"]
    schemas.setdefault(
        "WorkspaceUser_AvatarUpload",
        copy.deepcopy(schemas["WorkspaceUser_Get"]),
    )
    return specification


def add_public_projection_contract(specification):
    schemas = specification["components"]["schemas"]
    for name, schema in schemas.items():
        if not name.startswith("WorkspaceMessageReactions_"):
            continue
        properties = schema["properties"]
        properties["provider"] = copy.deepcopy(PROVIDER_SCHEMA)
        properties["delivery"] = copy.deepcopy(DELIVERY_SCHEMA)
    return specification


def _pagination_marker_schema(path):
    if path.endswith("/events/"):
        return {"type": "integer", "minimum": 0}
    return {"type": "string", "format": "uuid"}


def add_collection_pagination_contract(specification):
    for path, path_item in specification["paths"].items():
        operation = path_item.get("get")
        if operation is None:
            continue
        response = operation.get("responses", {}).get(200, {})
        schema = (
            response.get("content", {})
            .get("application/json", {})
            .get("schema", {})
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


def add_message_pagination_contract(specification, path):
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


def add_draft_contract(specification, path, components):
    schemas = components["components"]["schemas"]
    collection = specification["paths"][path]
    operation = collection["get"]
    operation["description"] = DRAFT_SIDE_EFFECTS_DESCRIPTION
    operation["parameters"] = copy.deepcopy(DRAFT_PAGINATION_PARAMETERS)
    operation["responses"][200]["headers"] = copy.deepcopy(
        MESSAGE_PAGINATION_HEADERS
    )

    create = collection["post"]
    create["description"] = DRAFT_SIDE_EFFECTS_DESCRIPTION
    create_schema = schemas["WorkspaceDraft_Create"]
    create["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "object",
        "required": ["uuid", "stream_uuid", "topic_uuid", "payload"],
        "properties": {
            name: {
                key: value
                for key, value in copy.deepcopy(create_schema["properties"][name]).items()
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
    resource["get"]["responses"][200]["headers"] = copy.deepcopy(
        DRAFT_ETAG_HEADER
    )
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
    resource["put"]["responses"][200]["headers"] = copy.deepcopy(
        DRAFT_ETAG_HEADER
    )
    resource["put"]["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "object",
        "required": ["payload"],
        "additionalProperties": False,
        "properties": {
            "payload": copy.deepcopy(DRAFT_PAYLOAD_SCHEMA)
        },
    }
    return specification


def add_current_user_contract(specification, path):
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


def add_events_cursor_contract(specification, events_path, epoch_path):
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
