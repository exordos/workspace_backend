# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Apply the supported Provider API v1 projection events to Messenger state."""

import collections.abc
import contextvars
import datetime
import typing
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

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


def _assignment_cache_key(
    identity: typing.Any,
    account_uuid: object,
    chat_uuid: object,
    project_id: object,
) -> tuple[object, ...]:
    return (
        "assignment",
        identity.bridge_instance_uuid,
        identity.provider_kind,
        sys_uuid.UUID(str(account_uuid)),
        sys_uuid.UUID(str(chat_uuid)),
        sys_uuid.UUID(str(project_id)),
    )


def prime_assignment_cache(
    session: typing.Any,
    identity: typing.Any,
    assignments: collections.abc.Iterable[collections.abc.Mapping[str, typing.Any]],
) -> None:
    """Reuse assignment rows already authorized by the provider batch gate."""
    batch_cache = getattr(session, "_workspace_provider_event_batch_cache", None)
    if batch_cache is None:
        return
    for assignment in assignments:
        account_uuid = sys_uuid.UUID(str(assignment["account_uuid"]))
        chat_uuid = sys_uuid.UUID(str(assignment["chat_uuid"]))
        project_id = sys_uuid.UUID(str(assignment["project_id"]))
        row = {
            name: assignment[name]
            for name in (
                "owner_user_uuid",
                "projection_stream_uuid",
                "provider_chat_id",
                "display_name",
                "source",
                "capabilities",
                "account_settings",
                "provider_realm_uuid",
            )
        }
        for name in (
            "owner_user_uuid",
            "projection_stream_uuid",
            "provider_realm_uuid",
        ):
            if row[name] is not None:
                row[name] = sys_uuid.UUID(str(row[name]))
        batch_cache[
            _assignment_cache_key(
                identity,
                account_uuid,
                chat_uuid,
                project_id,
            )
        ] = (account_uuid, project_id, row)


def _assignment(
    session: typing.Any,
    identity: typing.Any,
    event: dict[str, typing.Any],
) -> tuple[sys_uuid.UUID, sys_uuid.UUID, typing.Any]:
    chat_uuid = sys_uuid.UUID(str(event["external_chat_uuid"]))
    account_uuid = sys_uuid.UUID(str(event["external_account_uuid"]))
    project_id = sys_uuid.UUID(str(event["project_id"]))
    batch_cache = getattr(session, "_workspace_provider_event_batch_cache", None)
    cache_key = _assignment_cache_key(
        identity,
        account_uuid,
        chat_uuid,
        project_id,
    )
    if batch_cache is not None and cache_key in batch_cache:
        return batch_cache[cache_key]
    row = session.execute(
        """
        SELECT chat."owner_user_uuid", chat."projection_stream_uuid",
               chat."provider_chat_id", chat."display_name", chat."source",
               chat."capabilities", account."settings" AS account_settings,
               account."provider_realm_uuid"
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
    result = (account_uuid, project_id, row)
    if batch_cache is not None:
        batch_cache[cache_key] = result
    return result


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
    link = session.execute(
        """
        SELECT link.workspace_user_uuid, link.link_kind
        FROM m_external_accounts_v2 AS account
        JOIN m_external_provider_identity_links_v1 AS link
          ON link.provider = account.provider
         AND link.provider_realm_uuid = account.provider_realm_uuid
         AND link.provider_user_id = %s
        WHERE account.uuid = %s
          AND account.provider = %s
        """,
        (provider_external_id, account_uuid, identity.provider_kind),
    ).fetchone()
    if link is not None:
        # Catalog reconciliation owns this realm-scoped provider link. Queued
        # events may still contain the former account-scoped UUID, but their
        # untrusted UUID or email must never override the verified link.
        identity_uuid = sys_uuid.UUID(str(link["workspace_user_uuid"]))
    existing = models.WorkspaceUser.objects.get_one_or_none(
        filters={"uuid": dm_filters.EQ(identity_uuid)},
        session=session,
    )
    user_values = {
        "first_name": values["display_name"],
        "email": values.get("email"),
        # Provider directory "active" means enabled, not currently online.
        # No provider presence capability exists in v1, so imported identities
        # must not keep reviving the browser-presence state.
        "status": models.WorkspaceUserStatus.OFFLINE.value,
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
    if link is not None and existing.source == models.WorkspaceUserSource.IAM.value:
        return identity_uuid
    if link is None and (
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


def _scoped_provider_values(
    resource: collections.abc.Mapping[str, typing.Any],
    names: collections.abc.Collection[str],
    external_account_uuid: sys_uuid.UUID,
) -> dict[str, typing.Any]:
    values = _provider_values(resource, names)
    if values.get("source_name") == models.SourceName.ZULIP.value:
        source = dict(values["source"])
        source["source_scope"] = str(external_account_uuid)
        values["source"] = source
    return values


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


def _message_projection_is_unchanged(
    existing: typing.Any,
    values: collections.abc.Mapping[str, typing.Any],
) -> bool:
    incoming_payload = values.get("payload")
    current_payload = getattr(existing, "payload", None)
    current_content = (
        current_payload.get("content")
        if isinstance(current_payload, collections.abc.Mapping)
        else getattr(current_payload, "content", None)
    )
    if (
        not isinstance(incoming_payload, message_payloads.MarkdownPayload)
        or incoming_payload.content != current_content
    ):
        return False
    if values.get("provider_external_id") != getattr(
        existing, "provider_external_id", None
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


def _provider_sequence(value: typing.Any) -> int | None:
    if not isinstance(value, collections.abc.Mapping):
        return None
    sequence = value.get("provider_sequence")
    if sequence is None or isinstance(sequence, bool):
        return None
    try:
        parsed = int(sequence)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _is_stale_provider_message(existing: typing.Any, resource: typing.Any) -> bool:
    current_sequence = _provider_sequence(getattr(existing, "provider_metadata", None))
    incoming_sequence = _provider_sequence(resource.get("provider_metadata"))
    return (
        current_sequence is not None
        and incoming_sequence is not None
        and incoming_sequence < current_sequence
    )


def _ensure_projection_owner_stream(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    assignment: typing.Mapping[str, typing.Any],
    identity: typing.Any,
    account_uuid: sys_uuid.UUID,
    emit_events: bool = True,
) -> None:
    batch_cache = getattr(session, "_workspace_provider_event_batch_cache", None)
    cache_key = (
        "projection",
        identity.bridge_instance_uuid,
        identity.provider_kind,
        account_uuid,
        project_id,
        assignment["projection_stream_uuid"],
    )
    if batch_cache is not None and cache_key in batch_cache:
        return
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
        emit_events=emit_events,
        reconcile_participants=False,
    )
    if batch_cache is not None:
        batch_cache[cache_key] = True


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
    native_direct = external_projection.is_native_direct_projection(
        session,
        project_id,
        stream_uuid,
    )
    if event["kind"] == "stream.delete":
        if existing is None or native_direct:
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
    if native_direct:
        return stream_uuid
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
    native_direct = external_projection.is_native_direct_projection(
        session,
        project_id,
        stream_uuid,
    )
    provider_metadata = resource.get("provider_metadata")
    quiet_backfill = (
        isinstance(provider_metadata, collections.abc.Mapping)
        and provider_metadata.get("delivery_class") == "backfill"
    )
    _ensure_projection_owner_stream(
        session,
        project_id,
        assignment,
        identity,
        sys_uuid.UUID(str(event["external_account_uuid"])),
        emit_events=not quiet_backfill,
    )
    existing = _existing(models.WorkspaceStreamTopic, project_id, topic_uuid, session)
    if event["kind"] == "topic.delete":
        if existing is None or native_direct:
            return topic_uuid
        helpers.delete_workspace_user_stream_topic(
            project_id,
            assignment["owner_user_uuid"],
            topic_uuid,
            session=session,
        )
        return topic_uuid
    if native_direct:
        values = {
            name: resource[name]
            for name in ("color", "stream_uuid", "uuid")
            if name in resource
        }
        values["name"] = external_projection.provider_topic_name(identity.provider_kind)
    else:
        values = _scoped_provider_values(
            resource,
            {"color", "name", "source", "source_name", "stream_uuid", "uuid"},
            sys_uuid.UUID(str(event["external_account_uuid"])),
        )
    if existing is None:
        values.update({"uuid": topic_uuid, "stream_uuid": stream_uuid})
        helpers.create_workspace_stream_topic_with_flags(
            project_id=project_id,
            session=session,
            **values,
        )
    else:
        values.pop("uuid", None)
        values.pop("stream_uuid", None)
        existing.update_dm(values=values)
        existing.update(session=session)
    if not quiet_backfill:
        helpers.create_compact_workspace_stream_topic_events(
            project_id,
            stream_uuid,
            topic_uuid,
            created=existing is None,
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
    validation_token = models._PROVIDER_MESSAGE_VALIDATION_CACHE.set(set())
    try:
        # A provider echo can arrive after the native author's stream binding
        # was removed. Load the persisted message in this trusted reconciliation
        # scope, then attach provider identity before the updated model is saved.
        existing = _existing(models.WorkspaceMessage, project_id, message_uuid, session)
    finally:
        models._PROVIDER_MESSAGE_VALIDATION_CACHE.reset(validation_token)
    if existing is not None and _is_stale_provider_message(existing, resource):
        # A live record may overtake an older history record by design. Once the
        # newer provider sequence is stored, the delayed snapshot must not
        # regress content, reactions, or read state.
        return message_uuid
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
    provider_metadata = resource.get("provider_metadata")
    quiet_backfill = (
        isinstance(provider_metadata, collections.abc.Mapping)
        and provider_metadata.get("delivery_class") == "backfill"
    )
    author_identity = resource.get("author_identity")
    if (
        isinstance(author_identity, collections.abc.Mapping)
        and sys_uuid.UUID(str(resource["user_uuid"])) != assignment["owner_user_uuid"]
    ):
        account_uuid = sys_uuid.UUID(str(event["external_account_uuid"]))
        batch_cache = getattr(session, "_workspace_provider_event_batch_cache", None)
        provider_realm_key = assignment.get("provider_realm_uuid") or (
            assignment.get("account_settings") or {}
        ).get("server_url")
        author_key = (
            "author_identity",
            identity.bridge_instance_uuid,
            identity.provider_kind,
            provider_realm_key or account_uuid,
            str(author_identity["provider_external_id"]),
        )
        author_uuid = batch_cache.get(author_key) if batch_cache is not None else None
        if author_uuid is None:
            author_uuid = _upsert_provider_identity(
                session,
                identity,
                account_uuid,
                sys_uuid.UUID(str(resource["user_uuid"])),
                str(author_identity["provider_external_id"]),
                author_identity,
            )
            if batch_cache is not None:
                batch_cache[author_key] = author_uuid
        resource["user_uuid"] = author_uuid
    if existing is None:
        _ensure_projection_owner_stream(
            session,
            project_id,
            assignment,
            identity,
            sys_uuid.UUID(str(event["external_account_uuid"])),
            emit_events=not quiet_backfill,
        )
    values = _scoped_provider_values(
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
        sys_uuid.UUID(str(event["external_account_uuid"])),
    )
    if (
        existing is not None
        and getattr(existing, "source_name", None) == models.SourceName.NATIVE.value
    ):
        values.pop("source_name", None)
        values.pop("source", None)
    elif identity.provider_kind != models.SourceName.NATIVE.value and (
        values.get("source_name") != identity.provider_kind or "source" not in values
    ):
        assignment_source = assignment.get("source") or {}
        source_name, source = external_projection._workspace_source(
            identity.provider_kind,
            assignment["provider_chat_id"],
            assignment_source.get("chat_type", "channel"),
            assignment.get("account_settings") or {},
            sys_uuid.UUID(str(event["external_account_uuid"])),
        )
        values["source_name"] = source_name
        values["source"] = source
    if "payload" in values:
        values["payload"] = _message_payload(values["payload"])
    if "created_at" in values:
        values["created_at"] = _message_created_at(values["created_at"])
    if existing is None:
        values.update(
            {
                "uuid": message_uuid,
                "stream_uuid": stream_uuid,
                "topic_uuid": sys_uuid.UUID(str(resource["topic_uuid"])),
                "user_uuid": sys_uuid.UUID(str(resource["user_uuid"])),
            }
        )
        create_options = {"emit_events": False} if quiet_backfill else {}
        batch_validation_token: (
            contextvars.Token[set[tuple[object, ...]] | None] | None
        ) = None
        if quiet_backfill:
            batch_cache = getattr(
                session, "_workspace_provider_event_batch_cache", None
            )
            if batch_cache is not None:
                validation_cache = batch_cache.setdefault(
                    ("message_validation",), set()
                )
                batch_validation_token = models._PROVIDER_MESSAGE_VALIDATION_CACHE.set(
                    validation_cache
                )
        try:
            helpers.create_workspace_user_message(
                project_id,
                values.pop("user_uuid"),
                session=session,
                enforce_visibility=False,
                return_visible=False,
                compact_events=True,
                scoped_recipient_uuids=[assignment["owner_user_uuid"]],
                **create_options,
                **values,
            )
        finally:
            if batch_validation_token is not None:
                models._PROVIDER_MESSAGE_VALIDATION_CACHE.reset(batch_validation_token)
    else:
        if existing.stream_uuid != stream_uuid:
            raise ValueError("Provider message UUID belongs to another stream")
        helpers.ensure_workspace_message_recipients(
            project_id,
            existing,
            [assignment["owner_user_uuid"]],
            session,
            emit_events=not quiet_backfill,
        )
        # A provider replay may report the edit time as created_at. Once the
        # message exists, its creation timestamp is immutable.
        update_values = _provider_values(
            values,
            {
                "payload",
                "provider_external_id",
                "provider_metadata",
                "source",
                "source_name",
            },
        )
        source_changed = (
            update_values.get("source_name") is not None
            and getattr(
                existing,
                "source_name",
                update_values["source_name"],
            )
            != update_values["source_name"]
        )
        projection_changed = not _message_projection_is_unchanged(
            existing,
            update_values,
        )
        if source_changed or projection_changed:
            existing.update_dm(values=update_values)
            existing.update(session=session)
            if not quiet_backfill:
                helpers.create_compact_workspace_message_updated_events(
                    project_id,
                    existing,
                    session=session,
                )
        elif _provider_sequence(update_values.get("provider_metadata")) != (
            _provider_sequence(getattr(existing, "provider_metadata", None))
        ):
            # Advance the freshness watermark without broadcasting an
            # unchanged message snapshot.
            existing.update_dm(
                values={"provider_metadata": update_values["provider_metadata"]}
            )
            existing.update(session=session)
    if read_value is not None:
        if quiet_backfill:
            helpers.sync_workspace_user_message_flags(
                project_id,
                assignment["owner_user_uuid"],
                message_uuid,
                {"read": read_value},
                session=session,
                emit_events=False,
            )
        else:
            _sync_provider_read_state(
                session,
                project_id,
                assignment["owner_user_uuid"],
                stream_uuid,
                sys_uuid.UUID(str(resource["topic_uuid"])),
                [message_uuid],
                read_value,
            )
    return message_uuid


def _reaction_event(
    session: typing.Any,
    event: dict[str, typing.Any],
    project_id: sys_uuid.UUID,
    assignment: typing.Mapping[str, typing.Any],
    resource: dict[str, typing.Any],
    identity: typing.Any,
) -> sys_uuid.UUID:
    reaction_uuid = sys_uuid.UUID(str(resource["uuid"]))
    message_uuid = sys_uuid.UUID(str(resource["message_uuid"]))
    provider_metadata = resource.get("provider_metadata")
    quiet_backfill = (
        isinstance(provider_metadata, collections.abc.Mapping)
        and provider_metadata.get("delivery_class") == "backfill"
    )
    event_options = {
        "compact_events": True,
        "enforce_visibility": False,
        **({"emit_events": False} if quiet_backfill else {}),
    }
    message = _existing(models.WorkspaceMessage, project_id, message_uuid, session)
    if message is None or message.stream_uuid != assignment["projection_stream_uuid"]:
        raise ValueError("Provider reaction message is outside the selected stream")
    actor_uuid = sys_uuid.UUID(str(resource["user_uuid"]))
    user_identity = resource.get("user_identity")
    if isinstance(user_identity, collections.abc.Mapping):
        actor_uuid = _upsert_provider_identity(
            session,
            identity,
            sys_uuid.UUID(str(event["external_account_uuid"])),
            actor_uuid,
            str(user_identity["provider_external_id"]),
            user_identity,
        )
        resource["user_uuid"] = actor_uuid
    existing = _existing(
        models.WorkspaceMessageReactions,
        project_id,
        reaction_uuid,
        session,
    )
    matching = models.WorkspaceMessageReactions.objects.get_one_or_none(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "message_uuid": dm_filters.EQ(message_uuid),
            "user_uuid": dm_filters.EQ(actor_uuid),
            "emoji_name": dm_filters.EQ(resource["emoji_name"]),
        },
        session=session,
    )
    if existing is None and matching is not None:
        existing = matching
        reaction_uuid = existing.uuid
    elif (
        existing is not None and matching is not None and existing.uuid != matching.uuid
    ):
        raise ValueError("Provider reaction conflicts with an existing reaction")
    if event["kind"] == "reaction.delete":
        if existing is None:
            return reaction_uuid
        helpers.delete_workspace_message_reaction(
            project_id,
            existing.user_uuid,
            reaction_uuid,
            session=session,
            **event_options,
        )
        return reaction_uuid
    values = _provider_values(
        resource,
        {"emoji_name", "message_uuid", "uuid"},
    )
    values["message_uuid"] = message_uuid
    if existing is None:
        values.update({"uuid": reaction_uuid, "message_uuid": message_uuid})
        helpers.create_workspace_message_reaction(
            project_id,
            actor_uuid,
            session=session,
            **event_options,
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
            **event_options,
        )
    return reaction_uuid


def _sync_provider_read_state(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    reader_uuid: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
    topic_uuid: sys_uuid.UUID | None,
    message_uuids: list[sys_uuid.UUID],
    read: bool,
) -> None:
    # Workspace events acquire this project-scoped lock after mutating message
    # flags. Take it first for imported read-state batches so a concurrent
    # writer cannot hold the event lock while waiting for one of those rows.
    session.execute(
        """
        SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))
        """,
        (project_id,),
    )
    messages = session.execute(
        """
        SELECT
            message.uuid,
            message.user_uuid AS author_uuid,
            message.stream_uuid,
            message.topic_uuid,
            flags.read
        FROM m_workspace_messages AS message
        JOIN m_workspace_user_message_flags AS flags
          ON flags.uuid = message.uuid
         AND flags.project_id = message.project_id
         AND flags.user_uuid = %s
        WHERE message.project_id = %s
          AND message.uuid = ANY(%s::uuid[])
        ORDER BY message.uuid
        """,
        (reader_uuid, project_id, message_uuids),
    ).fetchall()
    if any(message["stream_uuid"] != stream_uuid for message in messages):
        raise ValueError("Provider read state message is outside the selected chat")
    changed_message_uuids_by_read: dict[bool, list[sys_uuid.UUID]] = {
        True: [],
        False: [],
    }
    message_by_uuid = {message["uuid"]: message for message in messages}
    for message in messages:
        effective_read = read or message["author_uuid"] == reader_uuid
        if message["read"] != effective_read:
            changed_message_uuids_by_read[effective_read].append(message["uuid"])
    if not any(changed_message_uuids_by_read.values()):
        return
    updated_message_uuids_by_read: dict[bool, list[sys_uuid.UUID]] = {
        True: [],
        False: [],
    }
    for effective_read, changed_message_uuids in changed_message_uuids_by_read.items():
        if not changed_message_uuids:
            continue
        updated_rows = session.execute(
            """
            UPDATE m_workspace_user_message_flags
            SET read = %s, updated_at = NOW()
            WHERE project_id = %s
              AND user_uuid = %s
              AND uuid = ANY(%s::uuid[])
              AND read IS DISTINCT FROM %s
            RETURNING uuid
            """,
            (
                effective_read,
                project_id,
                reader_uuid,
                changed_message_uuids,
                effective_read,
            ),
        ).fetchall()
        updated_message_uuids_by_read[effective_read] = [
            row["uuid"] for row in updated_rows
        ]
    updated_message_uuids = [
        *updated_message_uuids_by_read[True],
        *updated_message_uuids_by_read[False],
    ]
    if not updated_message_uuids:
        return
    if updated_message_uuids_by_read[True]:
        helpers.messenger_events.create_messages_read_event(
            project_id,
            reader_uuid,
            updated_message_uuids_by_read[True],
            session=session,
        )
    if updated_message_uuids_by_read[False]:
        # Aggregates alone cannot update the read flag of an already-open
        # message. Emit the same exact per-user snapshots as the single-message
        # flag path, but fetch the changed set in one query.
        changed_messages = models.WorkspaceUserMessage.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(project_id),
                "user_uuid": dm_filters.EQ(reader_uuid),
                "uuid": dm_filters.In(updated_message_uuids_by_read[False]),
            },
            order_by={"uuid": "asc"},
            session=session,
        )
        for changed_message in changed_messages:
            helpers.messenger_events.create_message_updated_event(
                message=changed_message,
                session=session,
            )
    changed_topic_uuids = sorted(
        {
            message_by_uuid[message_uuid]["topic_uuid"]
            for message_uuid in updated_message_uuids
        },
        key=str,
    )
    for changed_topic_uuid in changed_topic_uuids:
        helpers._create_compact_messages_unread_updated_events(
            project_id,
            [reader_uuid],
            stream_uuid,
            changed_topic_uuid,
            session=session,
        )


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
    _sync_provider_read_state(
        session,
        project_id,
        reader_uuid,
        stream_uuid,
        topic_uuid,
        message_uuids,
        read,
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
    account_uuid = sys_uuid.UUID(str(event["external_account_uuid"]))
    if event["kind"] == "identity.upsert":
        # Identity directory updates belong to the external account, not to an
        # individual selected chat. The batch authorization gate has already
        # proven that this bridge owns the account in the target project, so a
        # synthetic account-level external_chat_uuid must not force a chat
        # assignment lookup here.
        resource = _resource(event, identity, account_uuid)
        return _identity_event(session, event, identity, resource)
    account_uuid, project_id, assignment = _assignment(session, identity, event)
    resource = _resource(event, identity, account_uuid)
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
    return _reaction_event(
        session,
        event,
        project_id,
        assignment,
        resource,
        identity,
    )
