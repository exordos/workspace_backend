# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Server-owned provider identity adapter for Provider Data API v2."""

import collections.abc
import copy
import json
import typing
import uuid as sys_uuid

from workspace.external_bridge_control import provider_data
from workspace.external_bridge_control import provider_event_apply


COMMAND_MAX_ITEMS = 500
_COMMAND_FIELDS = {
    "provider_event_key",
    "delivery_uuid",
    "external_account_uuid",
    "provider_chat_key",
    "provider_sequence",
    "kind",
    "provider_object",
    "provider_references",
    "payload",
}
_REFERENCE_FIELDS = {"message", "messages", "reader", "topic", "user"}
_DIRECT_PREFIX = "direct-conversation:v1:"


def _numeric_provider_id(value: object, field: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an unsigned provider ID")
    text = str(value)
    if not text.isascii() or not text.isdecimal():
        raise ValueError(f"{field} must be an unsigned provider ID")
    parsed = int(text)
    canonical = str(parsed)
    if text != canonical:
        raise ValueError(f"{field} must use shortest decimal form")
    return canonical


def _direct_participants(provider_chat_key: str) -> tuple[str, ...]:
    if not provider_chat_key.startswith(_DIRECT_PREFIX):
        raise ValueError("Direct conversation key is invalid")
    count_text, separator, identifiers = provider_chat_key.removeprefix(
        _DIRECT_PREFIX
    ).partition(":")
    if not separator:
        raise ValueError("Direct conversation key is invalid")
    count = int(_numeric_provider_id(count_text, "direct participant count"))
    values = tuple(
        _numeric_provider_id(value, "direct participant ID")
        for value in identifiers.split(",")
        if value
    )
    if (
        count < 1
        or len(values) != count
        or values != tuple(sorted(set(values), key=int))
    ):
        raise ValueError("Direct conversation participants are not canonical")
    return values


def _legacy_chat_key(provider_chat_key: str) -> str:
    if provider_chat_key.startswith("channel:"):
        provider_id = _numeric_provider_id(
            provider_chat_key.removeprefix("channel:"),
            "provider channel ID",
        )
        return f"channel:{provider_id}"
    participants = _direct_participants(provider_chat_key)
    chat_type = "direct" if len(participants) == 2 else "group_direct"
    return f"{chat_type}:{','.join(participants)}"


def _provider_uuid(
    namespace: sys_uuid.UUID, kind: str, provider_id: str
) -> sys_uuid.UUID:
    return sys_uuid.uuid5(namespace, f"{kind}:{provider_id}")


def _resolve_route(
    session: typing.Any,
    identity: typing.Any,
    account_uuid: sys_uuid.UUID,
    provider_chat_key: str,
    *,
    account_global: bool,
) -> typing.Mapping[str, typing.Any]:
    if account_global:
        row = session.execute(
            """
            SELECT account.uuid AS account_uuid, account.owner_user_uuid,
                   account.provider_realm_uuid, account.provider_owner_user_id,
                   (account.settings->>'default_project_id')::uuid AS project_id,
                   account.settings AS account_settings
            FROM m_external_accounts_v2 AS account
            WHERE account.uuid = %s AND account.provider = %s
              AND account.provider_realm_uuid IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM m_external_bridge_desired_resources_v1 AS desired
                  WHERE desired.bridge_instance_uuid = %s
                    AND desired.provider_kind = %s
                    AND desired.resource_type = 'external_account'
                    AND desired.resource_uuid = account.uuid
                    AND desired.operation = 'upsert'
              )
            """,
            (
                account_uuid,
                identity.provider_kind,
                identity.bridge_instance_uuid,
                identity.provider_kind,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("External account is not assigned to this bridge")
        return row
    legacy_chat_key = _legacy_chat_key(provider_chat_key)
    row = session.execute(
        """
        SELECT account.uuid AS account_uuid, account.owner_user_uuid,
               account.provider_realm_uuid, account.provider_owner_user_id,
               account.settings AS account_settings,
               chat.uuid AS chat_uuid, chat.project_id,
               chat.projection_stream_uuid, chat.provider_chat_id,
               chat.source
        FROM m_external_accounts_v2 AS account
        JOIN m_external_chats_v2 AS chat
          ON chat.external_account_uuid = account.uuid
         AND chat.provider = account.provider
        WHERE account.uuid = %s AND account.provider = %s
          AND account.provider_realm_uuid IS NOT NULL
          AND chat.provider_chat_id IN (%s, %s)
          AND chat.selected
          AND chat.status IN ('syncing', 'live', 'degraded')
          AND NOT chat.transition_pending
          AND chat.project_id IS NOT NULL
          AND chat.projection_stream_uuid IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM m_external_bridge_desired_resources_v1 AS desired
              WHERE desired.bridge_instance_uuid = %s
                AND desired.provider_kind = %s
                AND desired.resource_type = 'external_chat_assignment'
                AND desired.resource_uuid = chat.uuid
                AND desired.operation = 'upsert'
                AND desired.resource->>'selected' = 'true'
          )
        ORDER BY (chat.provider_chat_id = %s) DESC
        LIMIT 1
        """,
        (
            account_uuid,
            identity.provider_kind,
            provider_chat_key,
            legacy_chat_key,
            identity.bridge_instance_uuid,
            identity.provider_kind,
            provider_chat_key,
        ),
    ).fetchone()
    if row is None:
        raise ValueError("Provider chat is not assigned to this bridge")
    return row


def _topic_uuid(
    route: typing.Mapping[str, typing.Any], provider_topic_id: object
) -> sys_uuid.UUID:
    provider_topic_id = str(provider_topic_id)
    source = route["source"]
    topics = source.get("topics", []) if isinstance(source, dict) else []
    matches = [
        item
        for item in topics
        if isinstance(item, dict) and item.get("provider_topic_id") == provider_topic_id
    ]
    if len(matches) != 1:
        raise ValueError("Provider topic mapping is missing")
    return sys_uuid.UUID(str(matches[0]["topic_uuid"]))


def _identity_uuid(
    session: typing.Any,
    route: typing.Mapping[str, typing.Any],
    provider_user_id: object,
) -> sys_uuid.UUID:
    provider_user_id = _numeric_provider_id(provider_user_id, "provider user ID")
    row = session.execute(
        """
        SELECT workspace_user_uuid
        FROM m_external_provider_identity_links_v1
        WHERE provider = 'zulip' AND provider_realm_uuid = %s
          AND provider_user_id = %s
        """,
        (route["provider_realm_uuid"], provider_user_id),
    ).fetchone()
    if row is not None:
        return sys_uuid.UUID(str(row["workspace_user_uuid"]))
    return _provider_uuid(
        sys_uuid.UUID(str(route["provider_realm_uuid"])),
        "user",
        provider_user_id,
    )


def _message_uuid(
    session: typing.Any,
    route: typing.Mapping[str, typing.Any],
    provider_message_id: object,
    *,
    topic_uuid: sys_uuid.UUID | None = None,
) -> sys_uuid.UUID:
    provider_message_id = _numeric_provider_id(
        provider_message_id,
        "provider message ID",
    )
    row = session.execute(
        """
        SELECT COALESCE(placement.legacy_public_uuid, placement.uuid) AS public_uuid
        FROM messenger_messages AS message
        JOIN messenger_message_placements AS placement
          ON placement.project_id = message.project_id
         AND placement.message_uuid = message.uuid
        WHERE message.provider_realm_uuid = %s
          AND message.provider_message_id = %s
        ORDER BY placement.created_at, placement.uuid
        LIMIT 1
        """,
        (route["provider_realm_uuid"], provider_message_id),
    ).fetchone()
    if row is None:
        row = session.execute(
            """
            SELECT uuid AS public_uuid
            FROM m_workspace_messages
            WHERE external_account_uuid = %s AND provider_external_id = %s
            ORDER BY created_at, uuid
            LIMIT 1
            """,
            (route["account_uuid"], provider_message_id),
        ).fetchone()
    if row is not None:
        return sys_uuid.UUID(str(row["public_uuid"]))
    if topic_uuid is None:
        raise ValueError("Provider message mapping is missing")
    canonical_uuid = _provider_uuid(
        sys_uuid.UUID(str(route["provider_realm_uuid"])),
        "message",
        provider_message_id,
    )
    return sys_uuid.uuid5(topic_uuid, str(canonical_uuid))


def _reference(
    references: collections.abc.Mapping[str, typing.Any],
    name: str,
) -> object:
    value = references.get(name)
    if value is None:
        raise ValueError(f"Provider reference '{name}' is required")
    return value


def _canonical_resource(
    session: typing.Any,
    route: typing.Mapping[str, typing.Any],
    command: collections.abc.Mapping[str, typing.Any],
) -> dict[str, typing.Any]:
    kind = str(command["kind"])
    provider_object = command["provider_object"]
    references = command["provider_references"]
    payload = command["payload"]
    if not isinstance(provider_object, dict) or set(provider_object) != {"kind", "id"}:
        raise ValueError("Provider object identity is invalid")
    if not isinstance(references, dict) or set(references) - _REFERENCE_FIELDS:
        raise ValueError("Provider references are invalid")
    if not isinstance(payload, dict):
        raise ValueError("Provider command payload must be an object")
    resource = copy.deepcopy(payload)
    for field in (
        "uuid",
        "stream_uuid",
        "topic_uuid",
        "message_uuid",
        "message_uuids",
        "user_uuid",
        "reader_uuid",
        "project_id",
        "external_account_uuid",
        "external_chat_uuid",
        "provider_uuid",
    ):
        resource.pop(field, None)
    object_kind = str(provider_object["kind"])
    object_id = str(provider_object["id"])
    stream_uuid = route.get("projection_stream_uuid")
    topic_uuid = (
        _topic_uuid(route, references["topic"])
        if references.get("topic") is not None
        else None
    )
    if kind == "identity.upsert":
        if object_kind != "user":
            raise ValueError("Identity command requires a provider user")
        resource["uuid"] = _identity_uuid(session, route, object_id)
        resource["provider_external_id"] = _numeric_provider_id(
            object_id, "provider user ID"
        )
        resource.setdefault("provider_metadata", {})["chat_key"] = "account"
        return resource
    if stream_uuid is None:
        raise ValueError("Provider command requires a selected chat")
    stream_uuid = sys_uuid.UUID(str(stream_uuid))
    if kind.startswith("stream."):
        resource.update(
            {
                "uuid": stream_uuid,
                "stream_uuid": stream_uuid,
                "provider_external_id": str(route["provider_chat_id"]),
            }
        )
    elif kind.startswith("topic."):
        if topic_uuid is None:
            raise ValueError("Topic command requires a provider topic mapping")
        resource.update(
            {
                "uuid": topic_uuid,
                "stream_uuid": stream_uuid,
                "provider_external_id": object_id,
            }
        )
    elif kind.startswith("message."):
        if object_kind != "message":
            raise ValueError("Message command requires a provider message")
        provider_message_id = _numeric_provider_id(object_id, "provider message ID")
        message_uuid = _message_uuid(
            session,
            route,
            provider_message_id,
            topic_uuid=topic_uuid,
        )
        resource.update(
            {
                "uuid": message_uuid,
                "stream_uuid": stream_uuid,
                "provider_external_id": provider_message_id,
            }
        )
        if topic_uuid is not None:
            resource["topic_uuid"] = topic_uuid
        if references.get("user") is not None:
            resource["user_uuid"] = _identity_uuid(session, route, references["user"])
    elif kind.startswith("reaction."):
        reaction_message_id = _reference(references, "message")
        provider_user_id = _reference(references, "user")
        message_uuid = _message_uuid(session, route, reaction_message_id)
        user_uuid = _identity_uuid(session, route, provider_user_id)
        emoji_name = resource["emoji_name"]
        resource.update(
            {
                "uuid": sys_uuid.uuid5(
                    message_uuid,
                    f"reaction:{_numeric_provider_id(provider_user_id, 'provider user ID')}:{emoji_name}",
                ),
                "message_uuid": message_uuid,
                "user_uuid": user_uuid,
                "provider_external_id": object_id,
            }
        )
    elif kind == "read_state.set":
        provider_message_ids = _reference(references, "messages")
        if not isinstance(provider_message_ids, list) or not provider_message_ids:
            raise ValueError("Read-state command requires provider messages")
        resource.update(
            {
                "uuid": stream_uuid,
                "stream_uuid": stream_uuid,
                "message_uuids": [
                    _message_uuid(session, route, provider_message_id)
                    for provider_message_id in provider_message_ids
                ],
                "reader_uuid": _identity_uuid(
                    session,
                    route,
                    _reference(references, "reader"),
                ),
                "provider_external_id": object_id,
            }
        )
        if topic_uuid is not None:
            resource["topic_uuid"] = topic_uuid
    else:
        raise ValueError("Provider command kind is not supported")
    if kind in {"stream.notification.update", "topic.notification.update"}:
        resource["user_uuid"] = route["owner_user_uuid"]
    return resource


def _canonical_event(
    session: typing.Any,
    identity: typing.Any,
    command: object,
) -> tuple[str, dict[str, typing.Any]]:
    if not isinstance(command, dict) or set(command) != _COMMAND_FIELDS:
        raise ValueError("Provider command envelope is invalid")
    event_key = command["provider_event_key"]
    if not isinstance(event_key, str) or not 1 <= len(event_key) <= 2048:
        raise ValueError("Provider event key is invalid")
    delivery_uuid = sys_uuid.UUID(str(command["delivery_uuid"]))
    if str(delivery_uuid) != command["delivery_uuid"]:
        raise ValueError("Provider delivery UUID must use canonical form")
    kind = command["kind"]
    if kind not in provider_event_apply.SUPPORTED_EVENT_KINDS:
        raise ValueError("Provider command kind is not supported")
    account_uuid = sys_uuid.UUID(str(command["external_account_uuid"]))
    provider_chat_key = command["provider_chat_key"]
    if not isinstance(provider_chat_key, str) or not provider_chat_key:
        raise ValueError("Provider chat key is invalid")
    account_global = kind == "identity.upsert"
    if account_global and provider_chat_key != "account":
        raise ValueError("Account-global provider command has invalid scope")
    route = _resolve_route(
        session,
        identity,
        account_uuid,
        provider_chat_key,
        account_global=account_global,
    )
    realm_uuid = sys_uuid.UUID(str(route["provider_realm_uuid"]))
    provider_event_uuid = sys_uuid.uuid5(
        realm_uuid,
        f"provider-delivery:v2:{event_key}:{delivery_uuid}",
    )
    resource = json.loads(
        json.dumps(_canonical_resource(session, route, command), default=str)
    )
    return event_key, {
        "provider_event_uuid": str(provider_event_uuid),
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(
            route.get("chat_uuid") or sys_uuid.uuid5(account_uuid, "provider-account")
        ),
        "project_id": str(route["project_id"]),
        "provider_sequence": command["provider_sequence"],
        "kind": kind,
        "payload": {"resource": resource},
    }


def apply_provider_command_batch(
    session: typing.Any,
    identity: typing.Any,
    commands: object,
    now: typing.Any = None,
) -> dict[str, list[dict[str, typing.Any]]]:
    """Resolve provider-native keys, then reuse the transactional v1 mutator."""
    if not isinstance(commands, list) or not 1 <= len(commands) <= COMMAND_MAX_ITEMS:
        raise provider_data.ProviderBatchError("Provider command batch size is invalid")
    try:
        canonical = [
            _canonical_event(session, identity, command) for command in commands
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise provider_data.ProviderBatchError(str(error)) from error
    response = provider_data.apply_provider_event_batch(
        session,
        identity,
        [event for _key, event in canonical],
        provider_event_apply.apply_event,
        now=now,
    )
    keys = {event["provider_event_uuid"]: key for key, event in canonical}
    return {
        "results": [
            {
                "provider_event_key": keys[result["provider_event_uuid"]],
                "status": result["status"],
                "target_uuid": result["target_uuid"],
                "safe_error": result["safe_error"],
                "duplicate": result["duplicate"],
            }
            for result in response["results"]
        ]
    }
