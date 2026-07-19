# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Mail-free Messenger resource maps and public projection serializers."""

import copy
import datetime
import enum
import typing
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

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
EXTENSION_RESOURCES = {
    "streams",
    "stream_topics",
    "messages",
    "message_reactions",
}
EXTENSION_CANONICAL_MODELS: dict[str, typing.Any] = {
    "streams": models.WorkspaceStream,
    "stream_topics": models.WorkspaceStreamTopic,
    "messages": models.WorkspaceMessage,
}
CANONICAL_NOT_PROVIDED = object()


def simple(value: typing.Any) -> typing.Any:
    if isinstance(value, sys_uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
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
    canonical: typing.Any = CANONICAL_NOT_PROVIDED,
) -> dict[str, typing.Any]:
    result = simple(value)
    if not isinstance(result, dict):
        raise TypeError("Messenger projection rows must serialize to dictionaries")
    if resource in EXTENSION_RESOURCES:
        canonical_was_provided = canonical is not CANONICAL_NOT_PROVIDED
        if not canonical_was_provided:
            canonical = value
        source = result.get("source")
        source_kind = (
            source.get("kind")
            if isinstance(source, dict)
            else result.get("source_name")
        )
        if (
            source_kind == models.SourceName.ZULIP.value
            and resource in EXTENSION_CANONICAL_MODELS
            and result.get("uuid") is not None
            and result.get("project_id") is not None
            and not canonical_was_provided
        ):
            canonical = EXTENSION_CANONICAL_MODELS[resource].objects.get_one_or_none(
                filters={
                    "uuid": dm_filters.EQ(result["uuid"]),
                    "project_id": dm_filters.EQ(result["project_id"]),
                }
            )
        canonical_values = simple(canonical) if canonical is not None else {}
        provider = canonical_values.get("provider_metadata")
        delivery = canonical_values.get("delivery_metadata")
        delivery_status = result.pop("delivery_status", None)
        delivery_error = result.pop("delivery_error", None)
        delivery_updated_at = result.pop("delivery_updated_at", None)
        result.pop("provider_uuid", None)
        result.pop("external_account_uuid", None)
        result.pop("provider_external_id", None)
        result.pop("provider_metadata", None)
        result.pop("delivery_metadata", None)
        if provider is None:
            account_uuid = canonical_values.get("external_account_uuid")
            external_id = canonical_values.get("provider_external_id")
            provider = (
                None
                if account_uuid is None
                else {
                    "kind": models.SourceName.ZULIP.value,
                    "account_uuid": account_uuid,
                    "external_id": external_id,
                    "capabilities": {},
                }
            )
        if delivery is None and delivery_status is not None:
            delivery = {
                "status": delivery_status,
                "safe_error": delivery_error,
                "updated_at": delivery_updated_at,
            }
        result["provider"] = provider
        result["delivery"] = delivery
    elif resource == "users" and result.get("source") == (
        models.WorkspaceUserSource.ZULIP.value
    ):
        result["identity_kind"] = "external"
        result["display_name"] = (
            " ".join(
                value
                for value in (result.get("first_name"), result.get("last_name"))
                if value
            )
            or result["username"]
        )
        result["provider"] = {
            "kind": models.SourceName.ZULIP.value,
            "account_uuid": result.pop("external_account_uuid"),
        }
        result.pop("provider_uuid", None)
        result.pop("provider_external_id", None)
    return result


def plain_values(
    values: typing.Mapping[str, typing.Any],
) -> dict[str, typing.Any]:
    return {
        name: value
        for name, value in values.items()
        if name not in {"provider", "delivery"}
    }


def projection_values(
    values: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    result = values.copy()
    provider = result.pop("provider", None)
    delivery = result.pop("delivery", None)
    for name in ("project_id", "user_uuid"):
        result.pop(name, None)
    if provider is not None:
        result["provider_metadata"] = copy.deepcopy(provider)
        result["external_account_uuid"] = provider["account_uuid"]
        result["provider_external_id"] = provider.get("external_id")
    if delivery is not None:
        result["delivery_metadata"] = copy.deepcopy(delivery)
        result["delivery_status"] = delivery["status"]
        result["delivery_error"] = delivery.get("safe_error")
        result["delivery_updated_at"] = delivery.get("updated_at")
    source = result.get("source")
    if isinstance(source, dict):
        kind = source.get("kind", result.get("source_name", "native"))
        if kind == models.SourceName.NATIVE.value:
            result["source"] = models.NativeSource()
        else:
            source_values = source.copy()
            source_values.pop("kind", None)
            result["source"] = models.ZulipSource(**source_values)
    return result
