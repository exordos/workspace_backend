# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Apply the supported Provider API v1 projection events to Messenger state."""

import collections.abc
import typing
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models


SUPPORTED_EVENT_KINDS = {
    "message.delete",
    "message.upsert",
    "read_state.set",
    "reaction.delete",
    "reaction.upsert",
    "stream.delete",
    "stream.upsert",
    "topic.delete",
    "topic.upsert",
}

_PROVIDER_FIELDS = {
    "delivery_error",
    "delivery_metadata",
    "delivery_status",
    "delivery_updated_at",
    "external_account_uuid",
    "provider_external_id",
    "provider_metadata",
    "provider_uuid",
}


def _assignment(
    session: typing.Any,
    identity: typing.Any,
    event: dict[str, typing.Any],
) -> tuple[sys_uuid.UUID, sys_uuid.UUID, typing.Any]:
    chat_uuid = sys_uuid.UUID(str(event["external_chat_uuid"]))
    account_uuid = sys_uuid.UUID(str(event["external_account_uuid"]))
    project_id = sys_uuid.UUID(str(event["project_id"]))
    row = session.execute(
        """
        SELECT "owner_user_uuid", "projection_stream_uuid", "provider_chat_id"
        FROM "m_external_chats_v2" AS chat
        WHERE chat."uuid" = %s AND chat."external_account_uuid" = %s
          AND chat."provider" = %s AND chat."project_id" = %s
          AND chat."selected" AND chat."status" IN ('syncing', 'live', 'degraded')
          AND chat."projection_stream_uuid" IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM "m_external_bridge_desired_resources_v1" AS desired
            WHERE desired."bridge_instance_uuid" = %s
              AND desired."provider_kind" = %s
              AND desired."resource_type" = 'external_chat_assignment'
              AND desired."resource_uuid" = chat."uuid"
              AND desired."operation" = 'upsert'
              AND desired."resource"->>'external_account_uuid' = %s
              AND desired."resource"->>'project_id' = %s
              AND desired."resource"#>>'{workspace_projection,stream,uuid}' =
                  chat."projection_stream_uuid"::text
          )
        """,
        (
            chat_uuid,
            account_uuid,
            identity.provider_kind,
            project_id,
            identity.bridge_instance_uuid,
            identity.provider_kind,
            str(account_uuid),
            str(project_id),
        ),
    ).fetchone()
    if row is None:
        raise ValueError("Provider event chat assignment is not active")
    return account_uuid, project_id, row


def _resource(
    event: dict[str, typing.Any],
    identity: typing.Any,
    account_uuid: sys_uuid.UUID,
) -> dict[str, typing.Any]:
    resource = dict(event["payload"]["resource"])
    provider_external_id = resource["provider_external_id"]
    if not isinstance(provider_external_id, str) or not provider_external_id:
        raise ValueError("Provider resource external ID is invalid")
    provider_metadata = dict(resource.get("provider_metadata") or {})
    provider_metadata.update(
        {
            "kind": identity.provider_kind,
            "account_uuid": str(account_uuid),
            "external_id": provider_external_id,
            "provider_event_uuid": str(event["provider_event_uuid"]),
        }
    )
    provider_metadata.setdefault("capabilities", {})
    if event.get("provider_sequence") is not None:
        provider_metadata["provider_sequence"] = event["provider_sequence"]
    resource.update(
        {
            "provider_uuid": identity.bridge_instance_uuid,
            "external_account_uuid": account_uuid,
            "provider_metadata": provider_metadata,
        }
    )
    return resource


def _existing(
    model: typing.Any,
    project_id: sys_uuid.UUID,
    resource_uuid: sys_uuid.UUID,
    session: typing.Any,
) -> typing.Any:
    return model.objects.get_one_or_none(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "uuid": dm_filters.EQ(resource_uuid),
        },
        session=session,
    )


def _provider_values(
    resource: collections.abc.Mapping[str, typing.Any],
    names: collections.abc.Collection[str],
) -> dict[str, typing.Any]:
    allowed = set(names) | _PROVIDER_FIELDS
    return {name: value for name, value in resource.items() if name in allowed}


def _stream_event(
    session: typing.Any,
    event: dict[str, typing.Any],
    project_id: sys_uuid.UUID,
    assignment: typing.Mapping[str, typing.Any],
    resource: dict[str, typing.Any],
) -> sys_uuid.UUID:
    stream_uuid = sys_uuid.UUID(str(resource["uuid"]))
    if stream_uuid != assignment["projection_stream_uuid"]:
        raise ValueError("Provider stream does not match the selected projection")
    existing = _existing(models.WorkspaceStream, project_id, stream_uuid, session)
    if event["kind"] == "stream.delete":
        if existing is None:
            return stream_uuid
        helpers.delete_workspace_user_stream(
            project_id,
            assignment["owner_user_uuid"],
            stream_uuid,
            session=session,
        )
        return stream_uuid
    if existing is None:
        raise ValueError("Provider stream projection must be materialized by control")
    helpers.update_workspace_user_stream(
        project_id,
        assignment["owner_user_uuid"],
        stream_uuid,
        _provider_values(
            resource,
            {"announce", "color", "description", "invite_only", "name"},
        ),
        session=session,
    )
    return stream_uuid


def _topic_event(
    session: typing.Any,
    event: dict[str, typing.Any],
    project_id: sys_uuid.UUID,
    assignment: typing.Mapping[str, typing.Any],
    resource: dict[str, typing.Any],
) -> sys_uuid.UUID:
    topic_uuid = sys_uuid.UUID(str(resource["uuid"]))
    stream_uuid = sys_uuid.UUID(str(resource["stream_uuid"]))
    if stream_uuid != assignment["projection_stream_uuid"]:
        raise ValueError("Provider topic does not belong to the selected stream")
    existing = _existing(models.WorkspaceStreamTopic, project_id, topic_uuid, session)
    if event["kind"] == "topic.delete":
        if existing is None:
            return topic_uuid
        helpers.delete_workspace_user_stream_topic(
            project_id,
            assignment["owner_user_uuid"],
            topic_uuid,
            session=session,
        )
        return topic_uuid
    values = _provider_values(
        resource,
        {"color", "name", "source", "source_name", "stream_uuid", "uuid"},
    )
    if existing is None:
        helpers.create_workspace_user_stream_topic(
            project_id,
            assignment["owner_user_uuid"],
            values,
            session=session,
        )
    else:
        values.pop("uuid", None)
        values.pop("stream_uuid", None)
        helpers.update_workspace_user_stream_topic(
            project_id,
            assignment["owner_user_uuid"],
            topic_uuid,
            values,
            session=session,
        )
    return topic_uuid


def _message_event(
    session: typing.Any,
    event: dict[str, typing.Any],
    project_id: sys_uuid.UUID,
    assignment: typing.Mapping[str, typing.Any],
    resource: dict[str, typing.Any],
) -> sys_uuid.UUID:
    message_uuid = sys_uuid.UUID(str(resource["uuid"]))
    stream_uuid = sys_uuid.UUID(str(resource["stream_uuid"]))
    if stream_uuid != assignment["projection_stream_uuid"]:
        raise ValueError("Provider message does not belong to the selected stream")
    existing = _existing(models.WorkspaceMessage, project_id, message_uuid, session)
    if event["kind"] == "message.delete":
        if existing is None:
            return message_uuid
        helpers.delete_workspace_user_message(
            project_id,
            existing.user_uuid,
            message_uuid,
            session=session,
            enforce_visibility=False,
            compact_events=True,
        )
        return message_uuid
    values = _provider_values(
        resource,
        {
            "payload",
            "source",
            "source_name",
            "stream_uuid",
            "topic_uuid",
            "user_uuid",
            "uuid",
        },
    )
    if existing is None:
        helpers.create_workspace_user_message(
            project_id,
            values.pop("user_uuid"),
            session=session,
            enforce_visibility=False,
            return_visible=False,
            compact_events=True,
            **values,
        )
        return message_uuid
    if existing.stream_uuid != stream_uuid:
        raise ValueError("Provider message UUID belongs to another stream")
    existing.update_dm(
        values=_provider_values(
            resource,
            {
                "payload",
                "provider_external_id",
                "provider_metadata",
            },
        )
    )
    existing.update(session=session)
    helpers._create_workspace_message_updated_events(
        project_id,
        message_uuid,
        session=session,
        compact_events=True,
    )
    return message_uuid


def _reaction_event(
    session: typing.Any,
    event: dict[str, typing.Any],
    project_id: sys_uuid.UUID,
    assignment: typing.Mapping[str, typing.Any],
    resource: dict[str, typing.Any],
) -> sys_uuid.UUID:
    reaction_uuid = sys_uuid.UUID(str(resource["uuid"]))
    message_uuid = sys_uuid.UUID(str(resource["message_uuid"]))
    message = _existing(models.WorkspaceMessage, project_id, message_uuid, session)
    if message is None or message.stream_uuid != assignment["projection_stream_uuid"]:
        raise ValueError("Provider reaction message is outside the selected stream")
    existing = _existing(
        models.WorkspaceMessageReactions,
        project_id,
        reaction_uuid,
        session,
    )
    actor_uuid = sys_uuid.UUID(str(resource.get("user_uuid") or message.user_uuid))
    if event["kind"] == "reaction.delete":
        if existing is None:
            return reaction_uuid
        helpers.delete_workspace_message_reaction(
            project_id,
            existing.user_uuid,
            reaction_uuid,
            session=session,
            enforce_visibility=False,
        )
        return reaction_uuid
    values = _provider_values(
        resource,
        {"emoji_name", "message_uuid", "uuid"},
    )
    if existing is None:
        helpers.create_workspace_message_reaction(
            project_id,
            actor_uuid,
            session=session,
            enforce_visibility=False,
            **values,
        )
    else:
        values.pop("uuid", None)
        helpers.update_workspace_message_reaction(
            project_id,
            existing.user_uuid,
            reaction_uuid,
            values,
            session=session,
        )
    return reaction_uuid


def _read_state_event(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    assignment: typing.Mapping[str, typing.Any],
    resource: dict[str, typing.Any],
) -> sys_uuid.UUID:
    stream_uuid = sys_uuid.UUID(str(resource["stream_uuid"]))
    if stream_uuid != assignment["projection_stream_uuid"]:
        raise ValueError("Provider read state is outside the selected stream")
    reader_uuid = sys_uuid.UUID(str(resource["reader_uuid"]))
    if reader_uuid != assignment["owner_user_uuid"]:
        raise ValueError("Provider read state reader is not the account owner")
    read = resource["read"]
    message_values = resource["message_uuids"]
    if not isinstance(read, bool):
        raise ValueError("Provider read state value is invalid")
    if (
        not isinstance(message_values, list)
        or not message_values
        or len(message_values) > 500
    ):
        raise ValueError("Provider read state message list is invalid")
    message_uuids = [sys_uuid.UUID(str(value)) for value in message_values]
    if len(set(message_uuids)) != len(message_uuids):
        raise ValueError("Provider read state message list contains duplicates")
    topic_value = resource.get("topic_uuid")
    topic_uuid = None if topic_value is None else sys_uuid.UUID(str(topic_value))
    messages = [
        helpers.get_workspace_user_message(
            project_id,
            reader_uuid,
            message_uuid,
        )
        for message_uuid in message_uuids
    ]
    if any(
        message.stream_uuid != stream_uuid
        or (topic_uuid is not None and message.topic_uuid != topic_uuid)
        for message in messages
    ):
        raise ValueError("Provider read state message is outside the selected chat")
    for message_uuid in message_uuids:
        helpers.sync_workspace_user_message_flags(
            project_id,
            reader_uuid,
            message_uuid,
            {"read": read},
            session=session,
        )
    return stream_uuid


def apply_event(
    event: dict[str, typing.Any],
    session: typing.Any,
    identity: typing.Any,
) -> sys_uuid.UUID:
    """Apply one validated inbound event inside the HTTP request transaction."""
    if event["kind"] not in SUPPORTED_EVENT_KINDS:
        raise ValueError("Provider event kind is not supported")
    account_uuid, project_id, assignment = _assignment(session, identity, event)
    resource = _resource(event, identity, account_uuid)
    if event["kind"] == "read_state.set":
        return _read_state_event(session, project_id, assignment, resource)
    resource_type = event["kind"].split(".", 1)[0]
    if resource_type == "stream":
        return _stream_event(session, event, project_id, assignment, resource)
    if resource_type == "topic":
        return _topic_event(session, event, project_id, assignment, resource)
    if resource_type == "message":
        return _message_event(session, event, project_id, assignment, resource)
    return _reaction_event(session, event, project_id, assignment, resource)
