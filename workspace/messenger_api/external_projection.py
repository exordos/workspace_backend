# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Materialize backend-owned external chat streams in Messenger storage."""

import collections.abc
import typing
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models


def _workspace_source(
    provider_kind: str,
    provider_chat_id: str,
    chat_type: str,
    account_settings: collections.abc.Mapping[str, typing.Any],
) -> tuple[str, typing.Any]:
    if provider_kind == models.SourceName.ZULIP.value:
        provider_stream_id = provider_chat_id.removeprefix("channel:")
        stream_id = (
            int(provider_stream_id)
            if chat_type == "channel" and provider_stream_id.isdecimal()
            else 0
        )
        return provider_kind, models.ZulipSource(
            stream_id=stream_id,
            server_url=account_settings["server_url"],
        )
    return models.SourceName.NATIVE.value, models.NativeSource()


def ensure_external_chat_stream(
    session: typing.Any,
    *,
    project_id: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    projection_stream_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    external_account_uuid: sys_uuid.UUID,
    provider_kind: str,
    provider_chat_id: str,
    display_name: str,
    source: collections.abc.Mapping[str, typing.Any],
    capabilities: collections.abc.Mapping[str, typing.Any],
    account_settings: collections.abc.Mapping[str, typing.Any],
) -> None:
    """Create the canonical stream and repair its owner binding if necessary."""
    stream = models.WorkspaceStream.objects.get_one_or_none(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "uuid": dm_filters.EQ(projection_stream_uuid),
        },
        session=session,
    )
    if stream is None:
        chat_type = source["chat_type"]
        source_name, workspace_source = _workspace_source(
            provider_kind,
            provider_chat_id,
            chat_type,
            account_settings,
        )
        default_topic = next(
            (topic for topic in source["topics"] if topic["is_default"]),
            None,
        )
        helpers.get_or_create_workspace_user_stream(
            project_id,
            owner_user_uuid,
            session=session,
            uuid=projection_stream_uuid,
            name=display_name,
            description=source["description"],
            private=chat_type != "channel",
            invite_only=chat_type != "channel",
            source_name=source_name,
            source=workspace_source,
            canonical_default_topic_uuid=(
                None
                if default_topic is None
                else sys_uuid.UUID(str(default_topic["topic_uuid"]))
            ),
            default_topic_name=(
                "General Topic" if default_topic is None else default_topic["name"]
            ),
            create_default_topic=default_topic is not None,
            provider_uuid=bridge_instance_uuid,
            external_account_uuid=external_account_uuid,
            provider_external_id=provider_chat_id,
            provider_metadata={
                "kind": provider_kind,
                "account_uuid": str(external_account_uuid),
                "external_id": provider_chat_id,
                "capabilities": dict(capabilities),
            },
        )
        return
    if stream.user_uuid != owner_user_uuid:
        raise ValueError("Provider stream projection owner does not match assignment")
    binding = models.WorkspaceUserStream.objects.get_one_or_none(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "uuid": dm_filters.EQ(projection_stream_uuid),
            "user_uuid": dm_filters.EQ(owner_user_uuid),
        },
        session=session,
    )
    if binding is not None:
        return
    binding = helpers._create_owner_binding(
        project_id,
        projection_stream_uuid,
        owner_user_uuid,
        owner_user_uuid,
        session=session,
    )
    helpers.create_workspace_stream_binding_events(binding, session=session)
