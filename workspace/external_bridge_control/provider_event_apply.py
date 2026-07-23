# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Apply the supported Provider API v1 projection events to Messenger state."""

import collections.abc
import datetime
import typing
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exc

from workspace.messenger_api import external_projection
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models


SUPPORTED_EVENT_KINDS = {
    "identity.upsert",
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
        SELECT chat."owner_user_uuid", chat."projection_stream_uuid",
               chat."provider_chat_id", chat."display_name", chat."source",
               chat."capabilities", account."settings" AS account_settings
        FROM "m_external_chats_v2" AS chat
        JOIN "m_external_accounts_v2" AS account
          ON account."uuid" = chat."external_account_uuid"
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


def _upsert_provider_identity(
    session: typing.Any,
    identity: typing.Any,
    account_uuid: sys_uuid.UUID,
    identity_uuid: sys_uuid.UUID,
    provider_external_id: str,
    values: collections.abc.Mapping[str, typing.Any],
) -> sys_uuid.UUID:
    existing = models.WorkspaceUser.objects.get_one_or_none(
        filters={"uuid": dm_filters.EQ(identity_uuid)},
        session=session,
    )
    user_values = {
        "first_name": values["display_name"],
        "email": values.get("email"),
        "status": (
            models.WorkspaceUserStatus.ACTIVE.value
            if values["active"]
            else models.WorkspaceUserStatus.OFFLINE.value
        ),
    }
    avatar_urn = values.get("avatar_urn")
    if avatar_urn is not None:
        user_values["avatar"] = avatar_urn
    if existing is None:
        user = models.WorkspaceUser(
            uuid=identity_uuid,
            username=f"{identity.provider_kind}-{identity_uuid}",
            source=models.WorkspaceUserSource.ZULIP.value,
            provider_uuid=identity.bridge_instance_uuid,
            external_account_uuid=account_uuid,
            provider_external_id=provider_external_id,
            **user_values,
        )
        user.insert(session=session)
        return identity_uuid
    if (
        existing.provider_uuid != identity.bridge_instance_uuid
        or existing.external_account_uuid != account_uuid
        or existing.provider_external_id != provider_external_id
    ):
        raise ValueError("Provider identity UUID belongs to another identity")
    existing.update_dm(values=user_values)
    if existing.is_dirty():
        existing.update(session=session)
    return identity_uuid


def _identity_event(
    session: typing.Any,
    event: dict[str, typing.Any],
    identity: typing.Any,
    resource: dict[str, typing.Any],
) -> sys_uuid.UUID:
    return _upsert_provider_identity(
        session,
        identity,
        sys_uuid.UUID(str(event["external_account_uuid"])),
        sys_uuid.UUID(str(resource["uuid"])),
        str(resource["provider_external_id"]),
        resource,
    )


def _provider_values(
    resource: collections.abc.Mapping[str, typing.Any],
    names: collections.abc.Collection[str],
) -> dict[str, typing.Any]:
    allowed = set(names) | _PROVIDER_FIELDS
    return {name: value for name, value in resource.items() if name in allowed}


def _message_payload(value: typing.Any) -> message_payloads.MarkdownPayload:
    if isinstance(value, message_payloads.MarkdownPayload):
        return value
    if (
        not isinstance(value, collections.abc.Mapping)
        or value.get("kind") != message_payloads.MarkdownPayload.KIND
    ):
        raise ValueError("Provider message payload kind is not supported")
    return message_payloads.MarkdownPayload(content=value["content"])


def _message_created_at(value: typing.Any) -> datetime.datetime:
    if not isinstance(value, str):
        raise ValueError("Provider message creation time is invalid")
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Provider message creation time is invalid")
    return parsed.astimezone(datetime.timezone.utc)


def _normalized_message_created_at(value: typing.Any) -> typing.Any:
    if not isinstance(value, datetime.datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _message_projection_is_unchanged(
    existing: typing.Any,
    values: collections.abc.Mapping[str, typing.Any],
) -> bool:
    incoming_payload = values.get("payload")
    current_payload = getattr(existing, "payload", None)
    if (
        not isinstance(incoming_payload, message_payloads.MarkdownPayload)
        or not isinstance(current_payload, message_payloads.MarkdownPayload)
        or incoming_payload.content != current_payload.content
    ):
        return False
    if values.get("provider_external_id") != getattr(
        existing, "provider_external_id", None
    ):
        return False
    incoming_created_at = values.get("created_at")
    if (
        incoming_created_at is not None
        and incoming_created_at
        != _normalized_message_created_at(
            getattr(existing, "created_at", incoming_created_at)
        )
    ):
        return False

    def stable_metadata(value: typing.Any) -> dict[str, typing.Any]:
        metadata = dict(value or {})
        for name in ("delivery_class", "provider_event_uuid", "provider_sequence"):
            metadata.pop(name, None)
        return metadata

    return stable_metadata(values.get("provider_metadata")) == stable_metadata(
        getattr(existing, "provider_metadata", None)
    )


def _ensure_projection_owner_stream(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    assignment: typing.Mapping[str, typing.Any],
    identity: typing.Any,
    account_uuid: sys_uuid.UUID,
) -> None:
    external_projection.ensure_external_chat_stream(
        session=session,
        project_id=project_id,
        owner_user_uuid=assignment["owner_user_uuid"],
        projection_stream_uuid=assignment["projection_stream_uuid"],
        bridge_instance_uuid=identity.bridge_instance_uuid,
        external_account_uuid=account_uuid,
        provider_kind=identity.provider_kind,
        provider_chat_id=assignment["provider_chat_id"],
        display_name=assignment["display_name"],
        source=assignment["source"],
        capabilities=assignment["capabilities"],
        account_settings=assignment["account_settings"],
    )


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
    identity: typing.Any,
) -> sys_uuid.UUID:
    topic_uuid = sys_uuid.UUID(str(resource["uuid"]))
    stream_uuid = sys_uuid.UUID(str(resource["stream_uuid"]))
    if stream_uuid != assignment["projection_stream_uuid"]:
        raise ValueError("Provider topic does not belong to the selected stream")
    _ensure_projection_owner_stream(
        session,
        project_id,
        assignment,
        identity,
        sys_uuid.UUID(str(event["external_account_uuid"])),
    )
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
        values.update({"uuid": topic_uuid, "stream_uuid": stream_uuid})
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
    identity: typing.Any,
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
    read_value = resource.get("read")
    if read_value is not None and not isinstance(read_value, bool):
        raise ValueError("Provider message read state is invalid")
    author_identity = resource.get("author_identity")
    if (
        isinstance(author_identity, collections.abc.Mapping)
        and sys_uuid.UUID(str(resource["user_uuid"]))
        != assignment["owner_user_uuid"]
    ):
        _upsert_provider_identity(
            session,
            identity,
            sys_uuid.UUID(str(event["external_account_uuid"])),
            sys_uuid.UUID(str(resource["user_uuid"])),
            str(author_identity["provider_external_id"]),
            author_identity,
        )
    values = _provider_values(
        resource,
        {
            "payload",
            "created_at",
            "source",
            "source_name",
            "stream_uuid",
            "topic_uuid",
            "user_uuid",
            "uuid",
        },
    )
    if "payload" in values:
        values["payload"] = _message_payload(values["payload"])
    if "created_at" in values:
        values["created_at"] = _message_created_at(values["created_at"])
    if existing is None:
        _ensure_projection_owner_stream(
            session,
            project_id,
            assignment,
            identity,
            sys_uuid.UUID(str(event["external_account_uuid"])),
        )
        values.update(
            {
                "uuid": message_uuid,
                "stream_uuid": stream_uuid,
                "topic_uuid": sys_uuid.UUID(str(resource["topic_uuid"])),
                "user_uuid": sys_uuid.UUID(str(resource["user_uuid"])),
            }
        )
        helpers.create_workspace_user_message(
            project_id,
            values.pop("user_uuid"),
            session=session,
            enforce_visibility=False,
            return_visible=False,
            compact_events=True,
            **values,
        )
    else:
        if existing.stream_uuid != stream_uuid:
            raise ValueError("Provider message UUID belongs to another stream")
        update_values = _provider_values(
            values,
            {
                "created_at",
                "payload",
                "provider_external_id",
                "provider_metadata",
            },
        )
        if not _message_projection_is_unchanged(existing, update_values):
            created_at = update_values.pop("created_at", None)
            existing.update_dm(values=update_values)
            existing.update(session=session)
            if created_at is not None and created_at != _normalized_message_created_at(
                getattr(existing, "created_at", created_at)
            ):
                session.execute(
                    """
                    UPDATE "m_workspace_messages"
                    SET "created_at" = %s
                    WHERE "project_id" = %s AND "uuid" = %s
                    """,
                    (created_at, project_id, message_uuid),
                )
            helpers._create_workspace_message_updated_events(
                project_id,
                message_uuid,
                session=session,
                compact_events=True,
            )
    if read_value is not None:
        helpers.sync_workspace_user_message_flags(
            project_id,
            assignment["owner_user_uuid"],
            message_uuid,
            {"read": read_value},
            session=session,
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
        values.update({"uuid": reaction_uuid, "message_uuid": message_uuid})
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
    # Workspace events acquire this project-scoped lock after mutating message
    # flags. Take it first for imported read-state batches so a concurrent
    # writer cannot hold the event lock while waiting for one of those rows.
    session.execute(
        """
        SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))
        """,
        (project_id,),
    )
    topic_value = resource.get("topic_uuid")
    topic_uuid = None if topic_value is None else sys_uuid.UUID(str(topic_value))
    messages = []
    existing_message_uuids = []
    for message_uuid in message_uuids:
        try:
            message = helpers.get_workspace_user_message(
                project_id,
                reader_uuid,
                message_uuid,
            )
        except storage_exc.RecordNotFound:
            # Queue catch-up is deliberately started before background history
            # import. A live flag event can therefore arrive before its message;
            # the later history upsert carries the provider's current read flag.
            continue
        messages.append(message)
        existing_message_uuids.append(message_uuid)
    if any(
        message.stream_uuid != stream_uuid
        or (topic_uuid is not None and message.topic_uuid != topic_uuid)
        for message in messages
    ):
        raise ValueError("Provider read state message is outside the selected chat")
    for message_uuid in existing_message_uuids:
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
    if event["kind"] == "identity.upsert":
        return _identity_event(session, event, identity, resource)
    if event["kind"] == "read_state.set":
        return _read_state_event(session, project_id, assignment, resource)
    resource_type = event["kind"].split(".", 1)[0]
    if resource_type == "stream":
        return _stream_event(session, event, project_id, assignment, resource)
    if resource_type == "topic":
        return _topic_event(
            session,
            event,
            project_id,
            assignment,
            resource,
            identity,
        )
    if resource_type == "message":
        return _message_event(
            session,
            event,
            project_id,
            assignment,
            resource,
            identity,
        )
    return _reaction_event(session, event, project_id, assignment, resource)
