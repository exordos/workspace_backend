# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import copy


MESSAGE_PAGINATION_PARAMETERS = (
    {
        "name": "page_limit",
        "in": "query",
        "description": "Maximum messages returned in this page.",
        "schema": {"type": "integer", "minimum": 0},
    },
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
