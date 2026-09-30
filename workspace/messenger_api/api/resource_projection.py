# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Mail-free Messenger resource maps and public projection serializers."""

import datetime
import enum
import typing
import uuid as sys_uuid


from workspace.messenger_api.dm import models


RESOURCE_MODELS: dict[str, typing.Any] = {
    "folders": models.UserFolder,
    "folder_items": models.UserFolderItem,
    "streams": models.WorkspaceUserStream,
    "stream_bindings": models.WorkspaceStreamBinding,
    "stream_topics": models.WorkspaceUserTopic,
    "messages": models.WorkspaceUserMessage,
    "message_reactions": models.WorkspaceMessageReactions,
    "files": models.WorkspaceFile,
    "users": models.WorkspaceUser,
    "drafts": models.WorkspaceDraft,
}
USER_SCOPED_RESOURCES = {
    "folders",
    "folder_items",
    "streams",
    "stream_topics",
    "messages",
    "drafts",
}


def simple(value: typing.Any) -> typing.Any:
    if isinstance(value, sys_uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        else:
            value = value.astimezone(datetime.timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {name: simple(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [simple(item) for item in value]
    if hasattr(value, "properties") and hasattr(value.properties, "items"):
        return {
            name: simple(prop.value)
            for name, prop in value.properties.items()
            if prop.value is not None
        }
    return value


def as_dict(
    value: typing.Any,
    resource: str | None = None,
) -> dict[str, typing.Any]:
    result = simple(value)
    if not isinstance(result, dict):
        raise TypeError("Messenger projection rows must serialize to dictionaries")
    if resource == "streams":
        result.setdefault("current_encryption_key_uuid", None)
    if resource == "stream_topics":
        for name in (
            "summary",
            "summary_last_message_uuid",
            "summary_has_new_messages",
            "summary_enabled",
            "summary_system_prompt",
        ):
            result.setdefault(name, True if name == "summary_enabled" else None)
    return result


def projection_values(
    values: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    result = values.copy()
    for name in ("project_id", "user_uuid"):
        result.pop(name, None)
    source = result.get("source")
    if isinstance(source, dict):
        source_type = models.WorkspaceMessage.properties.properties[
            "source"
        ].get_property_type()
        result["source"] = source_type.from_simple_type(source)
    return result
