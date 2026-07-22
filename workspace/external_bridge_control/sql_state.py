# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import collections.abc
import contextlib
import datetime
import hashlib
import hmac
import json
import secrets
import uuid as sys_uuid
from typing import Any

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from workspace.external_bridge_control import state
from workspace.external_bridge_control import pki
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import external_projection
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import models


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


_BRIDGE_DEGRADED_AFTER = datetime.timedelta(seconds=30)
_BRIDGE_OFFLINE_AFTER = datetime.timedelta(seconds=60)
_CHANNEL_ONLY_CAPABILITIES = {
    "messenger.stream.rename",
    "messenger.topic.rename",
}
_BACKEND_UNAVAILABLE_REASONS = {
    "account_not_ready",
    "account_unavailable",
    "bridge_incompatible",
    "bridge_offline",
    "bridge_suspended",
    "chat_type_unsupported",
    "chat_unavailable",
    "policy_limit",
    "provider_capability_unavailable",
    "provider_disabled",
    "provider_suspended",
    "resource_unavailable",
}
_WORKSPACE_PROJECTION_NAMESPACE = sys_uuid.UUID("71bdfd0a-35b6-54ac-83d1-54869e3c7e67")


def _projection_uuid(
    scope_uuid: sys_uuid.UUID,
    kind: str,
    provider_id: str,
) -> sys_uuid.UUID:
    return sys_uuid.uuid5(
        _WORKSPACE_PROJECTION_NAMESPACE,
        f"{scope_uuid}:{kind}:{provider_id}",
    )


def external_chat_projection_stream_uuid(
    chat_uuid: sys_uuid.UUID,
) -> sys_uuid.UUID:
    """Return the stable Workspace stream UUID for an external chat."""
    return _projection_uuid(chat_uuid, "stream", "canonical")


def external_chat_assignment_desired(chat: Any, session: Any = None) -> dict[str, Any]:
    """Return the complete backend-owned Workspace projection mapping."""
    source = chat.source
    chat_kind = {
        "channel": "channel",
        "personal": "personal_dm",
        "group": "group_dm",
    }[source["chat_type"]]
    topics = [dict(item) for item in source.get("topics", [])]
    stream_projection = {
        "uuid": str(chat.projection_stream_uuid),
        "name": chat.display_name,
        "description": source.get("description", ""),
        "chat_kind": chat_kind,
        "private": source.get("private", chat_kind != "channel"),
    }
    if session is not None and chat.projection_stream_uuid is not None:
        stream = session.execute(
            """
            SELECT name, description, private
            FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (chat.project_id, chat.projection_stream_uuid),
        ).fetchone()
        if stream is not None:
            stream_projection.update(
                {
                    "name": stream["name"],
                    "description": stream["description"] or "",
                    "private": stream["private"],
                }
            )
            existing_by_uuid = {item["topic_uuid"]: item for item in topics}
            topics = []
            materialized_topic_uuids = set()
            rows = session.execute(
                """
                SELECT uuid, name FROM m_workspace_stream_topics
                WHERE project_id = %s AND stream_uuid = %s
                ORDER BY created_at, uuid
                """,
                (chat.project_id, chat.projection_stream_uuid),
            ).fetchall()
            stream_id = chat.provider_chat_id.removeprefix("channel:")
            for row in rows:
                topic_uuid = str(row["uuid"])
                materialized_topic_uuids.add(topic_uuid)
                existing = existing_by_uuid.get(topic_uuid)
                if existing is not None:
                    existing["name"] = row["name"]
                    if chat_kind == "channel":
                        existing["provider_topic_id"] = f"{stream_id}:{row['name']}"
                    topics.append(existing)
                    continue
                provider_topic_id = (
                    f"{stream_id}:{row['name']}"
                    if chat_kind == "channel"
                    else f"{chat.provider_chat_id}:default"
                )
                topics.append(
                    {
                        "topic_uuid": topic_uuid,
                        "provider_topic_id": provider_topic_id,
                        "name": row["name"],
                        "is_default": False,
                    }
                )
            topics.extend(
                item
                for topic_uuid, item in existing_by_uuid.items()
                if topic_uuid not in materialized_topic_uuids
            )
    default_topic_uuid = next(
        (item["topic_uuid"] for item in topics if item["is_default"]),
        None,
    )
    stream_projection["default_topic_uuid"] = default_topic_uuid
    return {
        "resource_type": "external_chat_assignment",
        "uuid": str(chat.uuid),
        "generation": chat.revision,
        "external_account_uuid": str(chat.external_account_uuid),
        "provider_chat": {
            "kind": chat.provider,
            "chat_type": {
                "channel": "channel",
                "personal": "direct",
                "group": "group_direct",
            }[source["chat_type"]],
            "provider_chat_key": chat.provider_chat_id,
        },
        "project_id": str(chat.project_id),
        "selected": True,
        "history_depth": chat.history_depth,
        "workspace_projection": {
            "stream": stream_projection,
            "participants": [dict(item) for item in source.get("participants", [])],
            "topics": topics,
        },
    }


def _unavailable_reason(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _effective_descriptor(
    descriptor: dict[str, Any],
    available: bool,
    reason: dict[str, str] | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "available": available,
        "revision": int(descriptor.get("revision", 1)),
        "limits": dict(descriptor.get("limits", {}) if limits is None else limits),
    }
    if not available:
        result["unavailable_reason"] = reason or _unavailable_reason(
            "resource_unavailable",
            "The external resource is currently unavailable.",
        )
    return result


def _effective_account_capabilities(
    capabilities: dict[str, dict[str, Any]],
    available: bool,
    reason: dict[str, str] | None = None,
    policy_limits: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, descriptor in capabilities.items():
        limits = dict(descriptor.get("limits", {}))
        capability_available = available
        capability_reason = reason
        if name == "messenger.file.transfer" and policy_limits is not None:
            maximum = int(policy_limits.get("max_file_bytes", 0))
            provider_maximum = int(limits.get("max_file_bytes", maximum))
            limits["max_file_bytes"] = min(maximum, provider_maximum)
            if limits["max_file_bytes"] < 1:
                capability_available = False
                capability_reason = _unavailable_reason(
                    "policy_limit",
                    "External file transfer is disabled by realm policy.",
                )
        result[name] = _effective_descriptor(
            descriptor,
            capability_available,
            capability_reason,
            limits,
        )
    return result


def _effective_chat_capabilities(
    account_capabilities: dict[str, dict[str, Any]],
    chat_capabilities: dict[str, dict[str, Any]],
    chat: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    chat_available = chat["selected"] and chat["status"] in {"syncing", "live"}
    chat_type = chat["source"].get("chat_type")
    for name, descriptor in chat_capabilities.items():
        account_descriptor = account_capabilities.get(name)
        descriptor_reason = descriptor.get("unavailable_reason")
        provider_available = descriptor.get("available", True) is True or (
            isinstance(descriptor_reason, dict)
            and descriptor_reason.get("code") in _BACKEND_UNAVAILABLE_REASONS
        )
        available = (
            chat_available
            and isinstance(account_descriptor, dict)
            and account_descriptor.get("available") is True
            and provider_available
        )
        reason = None
        if not chat_available:
            reason = _unavailable_reason(
                "chat_unavailable",
                "The external chat is not currently synchronized.",
            )
        elif name in _CHANNEL_ONLY_CAPABILITIES and chat_type != "channel":
            available = False
            reason = _unavailable_reason(
                "chat_type_unsupported",
                "This action is not supported for the external chat type.",
            )
        elif not isinstance(account_descriptor, dict):
            reason = _unavailable_reason(
                "provider_capability_unavailable",
                "The connected provider does not advertise this capability.",
            )
        elif account_descriptor.get("available") is not True:
            reason = account_descriptor.get("unavailable_reason")
        elif not provider_available:
            reason = descriptor_reason
        limits = dict(descriptor.get("limits", {}))
        if isinstance(account_descriptor, dict):
            for limit_name, account_limit in account_descriptor.get(
                "limits", {}
            ).items():
                chat_limit = limits.get(limit_name, account_limit)
                if isinstance(account_limit, int) and isinstance(chat_limit, int):
                    limits[limit_name] = min(account_limit, chat_limit)
                elif chat_limit == account_limit:
                    limits[limit_name] = chat_limit
        result[name] = _effective_descriptor(descriptor, available, reason, limits)
    return result


def _emit_projected_capability_events(session: Any, chat: dict[str, Any]) -> None:
    if chat["project_id"] is None or chat["projection_stream_uuid"] is None:
        return
    filters = {
        "project_id": dm_filters.EQ(chat["project_id"]),
        "uuid": dm_filters.EQ(chat["projection_stream_uuid"]),
    }
    for stream in models.WorkspaceUserStream.objects.get_all(
        filters=filters,
        session=session,
    ):
        messenger_events.create_stream_updated_event(stream, session=session)
    for topic in models.WorkspaceUserTopic.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(chat["project_id"]),
            "stream_uuid": dm_filters.EQ(chat["projection_stream_uuid"]),
        },
        session=session,
    ):
        messenger_events.create_topic_updated_event(topic, session=session)
    for message in models.WorkspaceUserMessage.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(chat["project_id"]),
            "stream_uuid": dm_filters.EQ(chat["projection_stream_uuid"]),
        },
        session=session,
    ):
        messenger_events.create_message_updated_event(message, session=session)


def _update_projected_capabilities(
    session: Any,
    chat: dict[str, Any],
    capabilities: dict[str, Any],
) -> None:
    if chat["project_id"] is None or chat["projection_stream_uuid"] is None:
        return
    for table in (
        "m_workspace_streams",
        "m_workspace_stream_topics",
        "m_workspace_messages",
    ):
        session.execute(
            f"""
            UPDATE {table}
            SET provider_metadata = COALESCE(provider_metadata, '{{}}'::jsonb) ||
                jsonb_build_object('capabilities', %s::jsonb)
            WHERE project_id = %s AND external_account_uuid = %s
              AND {"uuid" if table == "m_workspace_streams" else "stream_uuid"} = %s
            """,
            (
                _json(capabilities),
                chat["project_id"],
                chat["external_account_uuid"],
                chat["projection_stream_uuid"],
            ),
        )
    _emit_projected_capability_events(session, chat)


def refresh_effective_capabilities(
    session: Any,
    provider_kind: str | None = None,
    now: datetime.datetime | None = None,
) -> None:
    """Converge public account, chat, and projected capability snapshots."""
    now = now or _utcnow()
    params = []
    provider_filter = ""
    if provider_kind is not None:
        provider_filter = "WHERE account.provider = %s"
        params.append(provider_kind)
    rows = session.execute(
        f"""
        SELECT account.uuid, account.owner_user_uuid, account.provider,
               account.settings, account.status, account.live_ready,
               account.capabilities AS effective_capabilities,
               policy.enabled AS policy_enabled,
               policy.emergency_suspended,
               policy.limits AS policy_limits,
               instance.uuid AS bridge_instance_uuid,
               instance.status AS bridge_status,
               instance.capabilities AS bridge_capabilities,
               instance.last_heartbeat_at
        FROM m_external_accounts_v2 AS account
        JOIN m_external_credentials_v2 AS credential
          ON credential.external_account_uuid = account.uuid
        LEFT JOIN m_external_provider_policies_v1 AS policy
          ON policy.provider = account.provider
        LEFT JOIN m_external_bridge_instances_v2 AS instance
          ON instance.uuid::text = credential.envelope #>>
             '{{associated_data,bridge_instance_uuid}}'
         AND instance.provider = account.provider
        {provider_filter}
        FOR UPDATE OF account
        """,
        tuple(params),
    ).fetchall()
    degraded_instances = set()
    for account in rows:
        available = True
        reason = None
        heartbeat_at = account["last_heartbeat_at"]
        if (
            account["bridge_status"] == "active"
            and heartbeat_at is not None
            and now - heartbeat_at > _BRIDGE_DEGRADED_AFTER
            and account["bridge_instance_uuid"] not in degraded_instances
        ):
            session.execute(
                """
                UPDATE m_external_bridge_instances_v2
                SET status = 'degraded', revision = revision + 1,
                    updated_at = %s
                WHERE uuid = %s AND status = 'active'
                """,
                (now, account["bridge_instance_uuid"]),
            )
            degraded_instances.add(account["bridge_instance_uuid"])
        if account["policy_enabled"] is False:
            available = False
            reason = _unavailable_reason(
                "provider_disabled",
                "The external provider is disabled by realm policy.",
            )
        elif account["emergency_suspended"]:
            available = False
            reason = _unavailable_reason(
                "provider_suspended",
                "The external provider is suspended by a realm administrator.",
            )
        elif account["bridge_instance_uuid"] is None:
            available = False
            reason = _unavailable_reason(
                "bridge_offline",
                "The external provider bridge is offline.",
            )
        elif account["bridge_status"] in {"suspended", "revoked"}:
            available = False
            reason = _unavailable_reason(
                "bridge_suspended",
                "The external provider bridge is suspended.",
            )
        elif account["bridge_status"] == "incompatible":
            available = False
            reason = _unavailable_reason(
                "bridge_incompatible",
                "The external provider bridge is incompatible with this backend.",
            )
        elif heartbeat_at is None or now - heartbeat_at > _BRIDGE_OFFLINE_AFTER:
            available = False
            reason = _unavailable_reason(
                "bridge_offline",
                "The external provider bridge is offline.",
            )
        if account["status"] not in {"backfill", "live"}:
            available = False
            reason = _unavailable_reason(
                "account_unavailable",
                "The external account is not ready for synchronization.",
            )
        elif account["status"] == "live" and not account["live_ready"]:
            available = False
            reason = _unavailable_reason(
                "account_not_ready",
                "The external account has not completed initial synchronization.",
            )
        effective = _effective_account_capabilities(
            account["bridge_capabilities"] or {},
            available,
            reason,
            account["policy_limits"],
        )
        if effective != account["effective_capabilities"]:
            session.execute(
                """
                UPDATE m_external_accounts_v2
                SET capabilities = %s::jsonb, revision = revision + 1,
                    updated_at = %s
                WHERE uuid = %s
                """,
                (_json(effective), now, account["uuid"]),
            )
            resource = external_models.ExternalAccount.objects.get_one(
                filters={"uuid": dm_filters.EQ(account["uuid"])},
                session=session,
            )
            messenger_events.create_external_resource_event(
                sys_uuid.UUID(str(account["settings"]["default_project_id"])),
                account["owner_user_uuid"],
                resource,
                messenger_events.EXTERNAL_ACCOUNT_UPDATED_EVENT,
                hidden_fields=("owner_user_uuid", "provider"),
                session=session,
            )
        chats = session.execute(
            """
            SELECT uuid, external_account_uuid, owner_user_uuid, source,
                   selected, project_id, projection_stream_uuid, status,
                   capabilities, catalog_capabilities
            FROM m_external_chats_v2
            WHERE external_account_uuid = %s
            FOR UPDATE
            """,
            (account["uuid"],),
        ).fetchall()
        for chat in chats:
            catalog_capabilities = chat["catalog_capabilities"]
            if catalog_capabilities is None:
                catalog_capabilities = chat["capabilities"]
                session.execute(
                    """
                    UPDATE m_external_chats_v2
                    SET catalog_capabilities = %s::jsonb
                    WHERE uuid = %s AND catalog_capabilities IS NULL
                    """,
                    (_json(catalog_capabilities), chat["uuid"]),
                )
            chat_effective = _effective_chat_capabilities(
                effective,
                catalog_capabilities,
                chat,
            )
            if chat_effective == chat["capabilities"]:
                continue
            session.execute(
                """
                UPDATE m_external_chats_v2
                SET capabilities = %s::jsonb, revision = revision + 1,
                    updated_at = %s
                WHERE uuid = %s
                """,
                (_json(chat_effective), now, chat["uuid"]),
            )
            _update_projected_capabilities(session, chat, chat_effective)
            if chat["project_id"] is not None:
                resource = external_models.ExternalChat.objects.get_one(
                    filters={"uuid": dm_filters.EQ(chat["uuid"])},
                    session=session,
                )
                messenger_events.create_external_resource_event(
                    chat["project_id"],
                    chat["owner_user_uuid"],
                    resource,
                    messenger_events.EXTERNAL_CHAT_UPDATED_EVENT,
                    hidden_fields=("owner_user_uuid", "provider", "provider_chat_id"),
                    session=session,
                )


def _timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def ensure_instance(
    session: Any,
    bridge_instance_uuid: sys_uuid.UUID,
    provider_kind: str,
) -> None:
    session.execute(
        """
        INSERT INTO "m_external_bridge_control_instances_v1" (
            "bridge_instance_uuid", "provider_kind"
        ) VALUES (%s, %s)
        ON CONFLICT ("bridge_instance_uuid") DO NOTHING
        """,
        (bridge_instance_uuid, provider_kind),
    )


def ensure_bridge_instance(
    session: Any,
    bridge_instance_uuid: sys_uuid.UUID | str,
    provider_kind: str,
    identity_generation: int,
) -> None:
    """Create or advance the SQL parent before opening a PKI generation."""
    bridge_instance_uuid = sys_uuid.UUID(str(bridge_instance_uuid))
    identity_generation = int(identity_generation)
    session.execute(
        """
        INSERT INTO "m_external_bridge_instances_v2" (
            "uuid", "provider", "identity_generation", "status"
        ) VALUES (%s, %s, %s, 'enrolling')
        ON CONFLICT ("uuid") DO NOTHING
        """,
        (bridge_instance_uuid, provider_kind, identity_generation),
    )
    ensure_instance(session, bridge_instance_uuid, provider_kind)
    instance = session.execute(
        """
        SELECT i."provider", i."identity_generation", i."status",
               c."identity_generation" AS "control_generation"
        FROM "m_external_bridge_instances_v2" AS i
        JOIN "m_external_bridge_control_instances_v1" AS c
          ON c."bridge_instance_uuid" = i."uuid"
         AND c."provider_kind" = i."provider"
        WHERE i."uuid" = %s
        FOR UPDATE
        """,
        (bridge_instance_uuid,),
    ).fetchone()
    if instance["provider"] != provider_kind:
        raise RuntimeError("Bridge instance provider does not match enrollment")
    if identity_generation < instance["identity_generation"]:
        raise RuntimeError("Bridge enrollment generation is stale")
    if identity_generation > instance["identity_generation"] or (
        identity_generation == instance["identity_generation"]
        and instance["status"] == "revoked"
        and (
            instance["control_generation"] is None
            or instance["control_generation"] < identity_generation
        )
    ):
        session.execute(
            """
            UPDATE "m_external_bridge_instances_v2"
            SET "identity_generation" = %s, "status" = 'enrolling',
                "capabilities" = '{}'::jsonb, "last_heartbeat_at" = NULL,
                "certificate_not_after" = NULL, "safe_error" = NULL,
                "revision" = "revision" + 1, "updated_at" = NOW()
            WHERE "uuid" = %s
            """,
            (identity_generation, bridge_instance_uuid),
        )


def persist_encryption_target(
    session: Any,
    identity: pki.BridgeIdentity,
    encryption_public_key: dict[str, str],
) -> None:
    """Persist only the enrolled public recipient; private key never reaches backend."""
    ensure_instance(
        session,
        identity.bridge_instance_uuid,
        identity.provider_kind,
    )
    session.execute(
        """
        UPDATE "m_external_bridge_control_instances_v1"
        SET "identity_generation" = %s,
            "encryption_key_uuid" = %s,
            "encryption_public_key" = %s,
            "updated_at" = NOW()
        WHERE "bridge_instance_uuid" = %s
            AND "provider_kind" = %s
            AND (
                "identity_generation" IS NULL OR
                "identity_generation" <= %s
            )
        """,
        (
            identity.identity_generation,
            encryption_public_key["key_uuid"],
            encryption_public_key["public_key"],
            identity.bridge_instance_uuid,
            identity.provider_kind,
            identity.identity_generation,
        ),
    )


def active_encryption_target(provider_kind: str, session: Any) -> dict[str, Any]:
    """Return the exact active bridge generation used for encryption-only HPKE."""
    row = session.execute(
        """
        SELECT c."bridge_instance_uuid", c."identity_generation",
               c."encryption_key_uuid", c."encryption_public_key"
        FROM "m_external_bridge_control_instances_v1" AS c
        JOIN "m_external_bridge_instances_v2" AS i
          ON i."uuid" = c."bridge_instance_uuid"
        WHERE c."provider_kind" = %s
          AND i."provider" = %s
          AND i."status" = 'active'
          AND i."identity_generation" = c."identity_generation"
          AND c."encryption_key_uuid" IS NOT NULL
        ORDER BY c."updated_at" DESC
        LIMIT 2
        """,
        (provider_kind, provider_kind),
    ).fetchall()
    if len(row) != 1:
        raise RuntimeError("Exactly one active bridge encryption target is required")
    target = row[0]
    return {
        "bridge_instance_uuid": str(target["bridge_instance_uuid"]),
        "provider_kind": provider_kind,
        "identity_generation": target["identity_generation"],
        "key_uuid": str(target["encryption_key_uuid"]),
        "algorithm": "X25519",
        "public_key": target["encryption_public_key"],
    }


def append_upsert(
    session: Any,
    bridge_instance_uuid: sys_uuid.UUID,
    provider_kind: str,
    resource: dict[str, Any],
    required_capabilities: dict[str, Any] | None = None,
) -> sys_uuid.UUID | None:
    """Atomically update desired projection and append its replacement change."""
    resource_type = resource["resource_type"]
    resource_uuid = sys_uuid.UUID(resource["uuid"])
    generation = int(resource["generation"])
    ensure_instance(session, bridge_instance_uuid, provider_kind)
    changed = session.execute(
        """
        INSERT INTO "m_external_bridge_desired_resources_v1" (
            "bridge_instance_uuid", "provider_kind", "resource_type",
            "resource_uuid", "operation", "generation",
            "required_capabilities", "resource"
        ) VALUES (%s, %s, %s, %s, 'upsert', %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (
            "bridge_instance_uuid", "provider_kind",
            "resource_type", "resource_uuid"
        ) DO UPDATE SET
            "operation" = 'upsert',
            "generation" = EXCLUDED."generation",
            "required_capabilities" = EXCLUDED."required_capabilities",
            "resource" = EXCLUDED."resource",
            "updated_at" = NOW()
        WHERE "m_external_bridge_desired_resources_v1"."generation"
              < EXCLUDED."generation"
        RETURNING "generation"
        """,
        (
            bridge_instance_uuid,
            provider_kind,
            resource_type,
            resource_uuid,
            generation,
            _json(required_capabilities or {}),
            _json(resource),
        ),
    ).fetchone()
    if changed is None:
        return None
    change_uuid = sys_uuid.uuid4()
    session.execute(
        """
        INSERT INTO "m_external_bridge_desired_changes_v1" (
            "change_uuid", "bridge_instance_uuid", "provider_kind",
            "resource_type", "resource_uuid", "operation", "generation",
            "required_capabilities", "resource"
        ) VALUES (%s, %s, %s, %s, %s, 'upsert', %s, %s::jsonb, %s::jsonb)
        """,
        (
            change_uuid,
            bridge_instance_uuid,
            provider_kind,
            resource_type,
            resource_uuid,
            generation,
            _json(required_capabilities or {}),
            _json(resource),
        ),
    )
    return change_uuid


def append_delete(
    session: Any,
    bridge_instance_uuid: sys_uuid.UUID,
    provider_kind: str,
    resource_type: str,
    resource_uuid: sys_uuid.UUID | str,
    generation: int,
) -> sys_uuid.UUID | None:
    """Atomically replace desired projection with a monotonic tombstone."""
    resource_uuid = sys_uuid.UUID(str(resource_uuid))
    ensure_instance(session, bridge_instance_uuid, provider_kind)
    changed = session.execute(
        """
        INSERT INTO "m_external_bridge_desired_resources_v1" (
            "bridge_instance_uuid", "provider_kind", "resource_type",
            "resource_uuid", "operation", "generation",
            "required_capabilities", "resource"
        ) VALUES (%s, %s, %s, %s, 'delete', %s, '{}'::jsonb, NULL)
        ON CONFLICT (
            "bridge_instance_uuid", "provider_kind",
            "resource_type", "resource_uuid"
        ) DO UPDATE SET
            "operation" = 'delete',
            "generation" = EXCLUDED."generation",
            "required_capabilities" = '{}'::jsonb,
            "resource" = NULL,
            "updated_at" = NOW()
        WHERE "m_external_bridge_desired_resources_v1"."generation"
              < EXCLUDED."generation"
        RETURNING "generation"
        """,
        (
            bridge_instance_uuid,
            provider_kind,
            resource_type,
            resource_uuid,
            generation,
        ),
    ).fetchone()
    if changed is None:
        return None
    change_uuid = sys_uuid.uuid4()
    session.execute(
        """
        INSERT INTO "m_external_bridge_desired_changes_v1" (
            "change_uuid", "bridge_instance_uuid", "provider_kind",
            "resource_type", "resource_uuid", "operation", "generation",
            "required_capabilities", "resource"
        ) VALUES (%s, %s, %s, %s, %s, 'delete', %s, '{}'::jsonb, NULL)
        """,
        (
            change_uuid,
            bridge_instance_uuid,
            provider_kind,
            resource_type,
            resource_uuid,
            generation,
        ),
    )
    return change_uuid


class SQLControlState:
    """Production control repository backed by Workspace PostgreSQL."""

    def __init__(
        self,
        realm_uuid: str | sys_uuid.UUID,
        signing_key: bytes,
    ) -> None:
        self.realm_uuid = sys_uuid.UUID(str(realm_uuid))
        self.signing_key = signing_key

    @staticmethod
    def _current_session() -> contextlib.AbstractContextManager[Any]:
        return contextlib.nullcontext(contexts.Context().get_session())

    def authorize_identity(self, identity: pki.BridgeIdentity) -> Any:
        """Recheck mutable SQL identity state for every private request."""
        with self._current_session() as session:
            instance = session.execute(
                """
                SELECT i."provider", i."identity_generation", i."status",
                       c."identity_generation" AS "control_generation"
                FROM "m_external_bridge_instances_v2" AS i
                LEFT JOIN "m_external_bridge_control_instances_v1" AS c
                  ON c."bridge_instance_uuid" = i."uuid"
                 AND c."provider_kind" = i."provider"
                WHERE i."uuid" = %s
                """,
                (identity.bridge_instance_uuid,),
            ).fetchone()
        if (
            instance is None
            or instance["provider"] != identity.provider_kind
            or instance["identity_generation"] != identity.identity_generation
            or instance["control_generation"] != identity.identity_generation
            or instance["status"] in {"suspended", "revoked"}
        ):
            raise state.BridgeForbiddenError(
                "Bridge identity is not authorized by current backend state"
            )
        return instance

    def initial_cursor(
        self,
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str] | None = None,
    ) -> str:
        types = self._normalize_types(resource_types)
        with self._current_session() as session:
            instance = self._instance(session, identity)
        return self._encode_cursor(identity, types, 0, instance["snapshot_generation"])

    def changes(
        self,
        identity: pki.BridgeIdentity,
        cursor: str,
        resource_types: collections.abc.Iterable[str] | None = None,
        limit: int = 200,
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        types = self._normalize_types(resource_types)
        if not 1 <= limit <= 500:
            raise ValueError("Change page limit is invalid")
        with self._current_session() as session:
            self._prune(session, now)
            instance = self._instance(session, identity)
            checkpoint = self._decode_cursor(
                cursor, identity, types, instance["snapshot_generation"]
            )
            watermarks = instance["pruned_through_sequence"]
            watermark = max((watermarks.get(value, 0) for value in types), default=0)
            if checkpoint["sequence"] < watermark:
                raise state.CursorExpiredError(
                    "retention", instance["snapshot_generation"]
                )
            rows = session.execute(
                """
                SELECT "change_uuid", "sequence", "resource_type",
                       "resource_uuid", "operation", "generation",
                       "required_capabilities", "resource"
                FROM "m_external_bridge_desired_changes_v1"
                WHERE "bridge_instance_uuid" = %s
                  AND "provider_kind" = %s
                  AND "resource_type" = ANY(%s)
                  AND "sequence" > %s
                ORDER BY "sequence"
                LIMIT %s
                """,
                (
                    identity.bridge_instance_uuid,
                    identity.provider_kind,
                    list(types),
                    checkpoint["sequence"],
                    limit,
                ),
            ).fetchall()
            changes = [self._wire_change(row) for row in rows]
            next_sequence = rows[-1]["sequence"] if rows else checkpoint["sequence"]
            next_cursor = self._encode_cursor(
                identity, types, next_sequence, instance["snapshot_generation"]
            )
        return {
            "control_schema_version": state.CONTROL_SCHEMA_VERSION,
            "snapshot_generation": instance["snapshot_generation"],
            "current_cursor": cursor,
            "next_cursor": next_cursor,
            "changes": changes,
            "retained_since": _timestamp(now - state.CHANGE_RETENTION),
        }

    def create_snapshot(
        self,
        identity: pki.BridgeIdentity,
        request_uuid: sys_uuid.UUID | str,
        resource_types: collections.abc.Iterable[str] | None = None,
        now: datetime.datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = now or _utcnow()
        request_uuid = sys_uuid.UUID(str(request_uuid))
        types = self._normalize_types(resource_types)
        with self._current_session() as session:
            session.execute(
                'DELETE FROM "m_external_bridge_snapshots_v1" WHERE "expires_at" <= %s',
                (now,),
            )
            existing = session.execute(
                """
                SELECT * FROM "m_external_bridge_snapshots_v1"
                WHERE "request_uuid" = %s AND "bridge_instance_uuid" = %s
                  AND "provider_kind" = %s AND "resource_types" = %s
                  AND "expires_at" > %s
                """,
                (
                    request_uuid,
                    identity.bridge_instance_uuid,
                    identity.provider_kind,
                    list(types),
                    now,
                ),
            ).fetchone()
            if existing is not None:
                return self._wire_snapshot(existing), False
            instance = self._instance(session, identity)
            anchor_row = session.execute(
                'SELECT COALESCE(MAX("sequence"), 0) AS "sequence" '
                'FROM "m_external_bridge_desired_changes_v1"'
            ).fetchone()
            anchor_sequence = anchor_row["sequence"]
            anchor_cursor = self._encode_cursor(
                identity,
                types,
                anchor_sequence,
                instance["snapshot_generation"],
            )
            rows = session.execute(
                """
                SELECT "required_capabilities", "resource"
                FROM "m_external_bridge_desired_resources_v1"
                WHERE "bridge_instance_uuid" = %s AND "provider_kind" = %s
                  AND "resource_type" = ANY(%s) AND "operation" = 'upsert'
                ORDER BY "resource_type", "resource_uuid"
                """,
                (identity.bridge_instance_uuid, identity.provider_kind, list(types)),
            ).fetchall()
            resources = [
                {
                    **row["resource"],
                    "required_capabilities": row["required_capabilities"],
                }
                for row in rows
            ]
            token = secrets.token_urlsafe(32)
            expires_at = now + state.SNAPSHOT_LIFETIME
            session.execute(
                """
                INSERT INTO "m_external_bridge_snapshots_v1" (
                    "snapshot_token", "request_uuid", "bridge_instance_uuid",
                    "provider_kind", "resource_types", "snapshot_generation",
                    "anchor_sequence", "anchor_cursor", "resources", "expires_at"
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    token,
                    request_uuid,
                    identity.bridge_instance_uuid,
                    identity.provider_kind,
                    list(types),
                    instance["snapshot_generation"],
                    anchor_sequence,
                    anchor_cursor,
                    _json(resources),
                    expires_at,
                ),
            )
        return {
            "request_uuid": str(request_uuid),
            "snapshot_token": token,
            "anchor_cursor": anchor_cursor,
            "snapshot_generation": instance["snapshot_generation"],
            "resource_types": list(types),
            "expires_at": _timestamp(expires_at),
        }, True

    def snapshot_page(
        self,
        identity: pki.BridgeIdentity,
        token: str,
        page_cursor: str | None = None,
        limit: int = 200,
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        with self._current_session() as session:
            snapshot = session.execute(
                'SELECT * FROM "m_external_bridge_snapshots_v1" '
                'WHERE "snapshot_token" = %s',
                (token,),
            ).fetchone()
        if snapshot is None:
            raise state.SnapshotExpiredError("unknown")
        if snapshot["expires_at"] <= now:
            raise state.SnapshotExpiredError("expired")
        if (
            snapshot["bridge_instance_uuid"] != identity.bridge_instance_uuid
            or snapshot["provider_kind"] != identity.provider_kind
        ):
            raise state.SnapshotExpiredError("scope_mismatch")
        offset = 0
        if page_cursor is not None:
            payload = self._verify(page_cursor)
            if payload.get("kind") != "snapshot_page" or payload.get("token") != token:
                raise state.SnapshotExpiredError("scope_mismatch")
            offset = payload["offset"]
        resources = snapshot["resources"][offset : offset + limit]
        next_offset = offset + len(resources)
        next_cursor = (
            None
            if next_offset >= len(snapshot["resources"])
            else self._sign(
                {"kind": "snapshot_page", "token": token, "offset": next_offset}
            )
        )
        return {
            "snapshot_generation": snapshot["snapshot_generation"],
            "anchor_cursor": snapshot["anchor_cursor"],
            "resources": resources,
            "next_page_cursor": next_cursor,
            "complete": next_cursor is None,
        }

    def heartbeat(
        self,
        identity: pki.BridgeIdentity,
        request: dict[str, Any],
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        if request["provider_kind"] != identity.provider_kind:
            raise state.StateConflictError(
                "Heartbeat provider does not match certificate"
            )
        heartbeat_uuid = sys_uuid.UUID(request["heartbeat_uuid"])
        canonical = hashlib.sha256(_json(request).encode()).hexdigest()
        with self._current_session() as session:
            instance = self._instance(session, identity)
            existing = session.execute(
                'SELECT "canonical_sha256", "response" '
                'FROM "m_external_bridge_heartbeats_v1" '
                'WHERE "heartbeat_uuid" = %s',
                (heartbeat_uuid,),
            ).fetchone()
            if existing is not None:
                if existing["canonical_sha256"] != canonical:
                    raise state.StateConflictError("Heartbeat UUID was reused")
                return existing["response"]
            capabilities = {
                name: descriptor
                for name, descriptor in request["capabilities"].items()
                if name in state.KNOWN_CAPABILITIES
            }
            response = {
                "heartbeat_uuid": str(heartbeat_uuid),
                "received_at": _timestamp(now),
                "instance_state": "incompatible"
                if request["blocked_batch"]
                else "healthy",
                "negotiated_capabilities": capabilities,
                "poll_interval_seconds": 2,
                "heartbeat_interval_seconds": 10,
                "degraded_after_seconds": 30,
                "offline_after_seconds": 60,
                "snapshot_generation": instance["snapshot_generation"],
                "ca_migration": {
                    "active_ca_generations": [1],
                    "renewal_required": False,
                    "overlap_ends_at": None,
                },
                "incompatibility": (
                    request["blocked_batch"]["safe_error"]
                    if request["blocked_batch"]
                    else None
                ),
            }
            session.execute(
                """
                INSERT INTO "m_external_bridge_heartbeats_v1" (
                    "heartbeat_uuid", "bridge_instance_uuid", "canonical_sha256",
                    "response", "received_at"
                ) VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (
                    heartbeat_uuid,
                    identity.bridge_instance_uuid,
                    canonical,
                    _json(response),
                    now,
                ),
            )
            session.execute(
                """
                UPDATE "m_external_bridge_instances_v2"
                SET "status" = %s, "capabilities" = %s::jsonb,
                    "last_heartbeat_at" = %s, "updated_at" = %s
                WHERE "uuid" = %s AND "identity_generation" = %s
                  AND "status" NOT IN ('suspended', 'revoked')
                """,
                (
                    "incompatible" if request["blocked_batch"] else "active",
                    _json(capabilities),
                    now,
                    now,
                    identity.bridge_instance_uuid,
                    identity.identity_generation,
                ),
            )
            refresh_effective_capabilities(
                session,
                provider_kind=identity.provider_kind,
                now=now,
            )
        return response

    @staticmethod
    def _safe_error_message(report: dict[str, Any]) -> str | None:
        safe_error = report["safe_error"]
        return None if safe_error is None else safe_error["message"]

    @staticmethod
    def _progress_timestamp(report: dict[str, Any]) -> datetime.datetime:
        value = report["progress"]["last_progress_at"] or report["observed_at"]
        if isinstance(value, datetime.datetime):
            return value
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _reconcile_catalog_report(
        self,
        session: Any,
        identity: pki.BridgeIdentity,
        report: dict[str, Any],
    ) -> None:
        catalog = report.get("catalog")
        required = {
            "operation",
            "external_account_uuid",
            "owner_user_uuid",
            "provider_kind",
            "project_id",
            "source",
            "display_name",
            "capabilities",
            "description",
            "participants",
            "topics",
        }
        if (
            not isinstance(catalog, dict)
            or not required <= set(catalog)
            or set(catalog) - required
        ):
            raise ValueError("External chat catalog report schema is invalid")
        if catalog["operation"] not in {"upsert", "delete"}:
            raise ValueError("External chat catalog operation is invalid")
        source = catalog["source"]
        if (
            not isinstance(source, dict)
            or not {"kind", "chat_type", "provider_chat_key"} <= set(source)
            or set(source) - {"kind", "chat_type", "provider_chat_key", "original_url"}
            or source["kind"] != identity.provider_kind
            or source["chat_type"] not in {"channel", "direct", "group_direct"}
            or not isinstance(source["provider_chat_key"], str)
            or not source["provider_chat_key"]
            or (
                source.get("original_url") is not None
                and (
                    not isinstance(source["original_url"], str)
                    or len(source["original_url"]) > 2048
                )
            )
        ):
            raise ValueError("External chat catalog source is invalid")
        account_uuid = sys_uuid.UUID(str(catalog["external_account_uuid"]))
        owner_uuid = sys_uuid.UUID(str(catalog["owner_user_uuid"]))
        project_uuid = sys_uuid.UUID(str(catalog["project_id"]))
        chat_uuid = sys_uuid.UUID(str(report["resource_uuid"]))
        account = session.execute(
            """
            SELECT account.owner_user_uuid, account.provider, account.settings,
                   account.desired_generation, policy.limits
            FROM m_external_accounts_v2 AS account
            JOIN m_external_provider_policies_v1 AS policy
              ON policy.provider = account.provider
            WHERE account.uuid = %s FOR UPDATE OF account
            """,
            (account_uuid,),
        ).fetchone()
        if (
            account is None
            or account["owner_user_uuid"] != owner_uuid
            or account["provider"] != identity.provider_kind
            or catalog["provider_kind"] != identity.provider_kind
            or account["desired_generation"] != report["observed_generation"]
            or str(account["settings"].get("default_project_id")) != str(project_uuid)
        ):
            raise ValueError("External chat catalog ownership is invalid")
        existing = session.execute(
            """
            SELECT uuid, selected FROM m_external_chats_v2
            WHERE external_account_uuid = %s AND provider_chat_id = %s
            FOR UPDATE
            """,
            (account_uuid, source["provider_chat_key"]),
        ).fetchone()
        if existing is not None and existing["uuid"] != chat_uuid:
            raise ValueError("External chat provider identity changed UUID")
        uuid_owner = session.execute(
            """
            SELECT external_account_uuid, owner_user_uuid, provider,
                   provider_chat_id
            FROM m_external_chats_v2 WHERE uuid = %s FOR UPDATE
            """,
            (chat_uuid,),
        ).fetchone()
        if uuid_owner is not None and (
            uuid_owner["external_account_uuid"] != account_uuid
            or uuid_owner["owner_user_uuid"] != owner_uuid
            or uuid_owner["provider"] != identity.provider_kind
            or uuid_owner["provider_chat_id"] != source["provider_chat_key"]
        ):
            raise ValueError("External chat UUID belongs to another provider identity")
        if catalog["operation"] == "delete":
            if existing is not None and existing["selected"]:
                session.execute(
                    """
                    UPDATE m_external_chats_v2
                    SET status = 'degraded',
                        safe_error = 'Provider catalog entry was removed',
                        revision = revision + 1, updated_at = NOW()
                    WHERE uuid = %s
                    """,
                    (chat_uuid,),
                )
            elif existing is not None:
                session.execute(
                    "DELETE FROM m_external_chats_v2 WHERE uuid = %s",
                    (chat_uuid,),
                )
            return
        if (
            not isinstance(catalog["display_name"], str)
            or not catalog["display_name"].strip()
            or len(catalog["display_name"]) > 512
            or not isinstance(catalog["capabilities"], dict)
        ):
            raise ValueError("External chat catalog metadata is invalid")
        participants = catalog["participants"]
        topics = catalog["topics"]
        if (
            not isinstance(catalog["description"], str)
            or len(catalog["description"]) > 4096
            or not isinstance(participants, list)
            or not participants
            or not isinstance(topics, list)
        ):
            raise ValueError("External chat catalog topology is invalid")
        provider_user_ids = set()
        normalized_participants = []
        owner_count = 0
        for participant in participants:
            required_participant = {"provider_user_id", "display_name", "is_owner"}
            if (
                not isinstance(participant, dict)
                or not required_participant <= set(participant)
                or set(participant) - required_participant - {"email", "avatar_urn"}
                or not isinstance(participant["provider_user_id"], str)
                or not participant["provider_user_id"]
                or participant["provider_user_id"] in provider_user_ids
                or not isinstance(participant["display_name"], str)
                or not participant["display_name"].strip()
                or not isinstance(participant["is_owner"], bool)
                or (
                    participant.get("email") is not None
                    and not isinstance(participant["email"], str)
                )
                or (
                    participant.get("avatar_urn") is not None
                    and not isinstance(participant["avatar_urn"], str)
                )
            ):
                raise ValueError("External chat catalog participant is invalid")
            provider_user_ids.add(participant["provider_user_id"])
            owner_count += int(participant["is_owner"])
            normalized_participants.append(
                {
                    "identity_uuid": str(
                        owner_uuid
                        if participant["is_owner"]
                        else _projection_uuid(
                            account_uuid,
                            "identity",
                            participant["provider_user_id"],
                        )
                    ),
                    "provider_user_id": participant["provider_user_id"],
                    "display_name": participant["display_name"].strip(),
                    "email": participant.get("email"),
                    "avatar_urn": participant.get("avatar_urn"),
                    "role": "owner" if participant["is_owner"] else "member",
                }
            )
        if owner_count != 1:
            raise ValueError("External chat catalog participant owner is invalid")
        if (source["chat_type"] == "direct" and len(normalized_participants) != 2) or (
            source["chat_type"] == "group_direct" and len(normalized_participants) < 3
        ):
            raise ValueError("External chat catalog participant topology is invalid")
        provider_topic_ids = set()
        normalized_topics = []
        default_count = 0
        for topic in topics:
            if (
                not isinstance(topic, dict)
                or set(topic) != {"provider_topic_id", "name", "is_default"}
                or not isinstance(topic["provider_topic_id"], str)
                or not topic["provider_topic_id"]
                or topic["provider_topic_id"] in provider_topic_ids
                or not isinstance(topic["name"], str)
                or not topic["name"].strip()
                or not isinstance(topic["is_default"], bool)
            ):
                raise ValueError("External chat catalog topic is invalid")
            provider_topic_ids.add(topic["provider_topic_id"])
            default_count += int(topic["is_default"])
            normalized_topics.append(
                {
                    "topic_uuid": str(
                        _projection_uuid(
                            chat_uuid,
                            "topic",
                            topic["provider_topic_id"],
                        )
                    ),
                    "provider_topic_id": topic["provider_topic_id"],
                    "name": topic["name"].strip(),
                    "is_default": topic["is_default"],
                }
            )
        if default_count > 1 or (
            source["chat_type"] in {"direct", "group_direct"} and default_count != 1
        ):
            raise ValueError("External chat catalog default topic is invalid")
        projection_stream_uuid = external_chat_projection_stream_uuid(chat_uuid)
        selection_all = account["settings"].get("selection_mode") == "all"
        maximum = account["limits"].get("max_selected_chats_per_account")
        if not isinstance(maximum, int):
            maximum = 0
        selected_count = session.execute(
            """
            SELECT COUNT(*) AS count FROM m_external_chats_v2
            WHERE external_account_uuid = %s AND selected AND uuid != %s
            """,
            (account_uuid, chat_uuid),
        ).fetchone()["count"]
        select_discovered = selection_all and selected_count < maximum
        session.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid, status, capabilities, catalog_capabilities
            ) VALUES (
                %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb
            )
            ON CONFLICT (uuid) DO UPDATE SET
                source = EXCLUDED.source,
                display_name = EXCLUDED.display_name,
                projection_stream_uuid = COALESCE(
                    m_external_chats_v2.projection_stream_uuid,
                    EXCLUDED.projection_stream_uuid
                ),
                catalog_capabilities = EXCLUDED.catalog_capabilities,
                selected = m_external_chats_v2.selected OR EXCLUDED.selected,
                project_id = CASE
                    WHEN m_external_chats_v2.selected
                    THEN m_external_chats_v2.project_id
                    ELSE EXCLUDED.project_id
                END,
                status = CASE
                    WHEN m_external_chats_v2.selected
                    THEN m_external_chats_v2.status
                    ELSE EXCLUDED.status
                END,
                updated_at = NOW(),
                revision = m_external_chats_v2.revision + 1
            WHERE m_external_chats_v2.external_account_uuid = EXCLUDED.external_account_uuid
              AND m_external_chats_v2.owner_user_uuid = EXCLUDED.owner_user_uuid
              AND m_external_chats_v2.provider = EXCLUDED.provider
              AND m_external_chats_v2.provider_chat_id = EXCLUDED.provider_chat_id
            """,
            (
                chat_uuid,
                account_uuid,
                owner_uuid,
                identity.provider_kind,
                source["provider_chat_key"],
                _json(
                    {
                        "kind": source["kind"],
                        "chat_type": {
                            "channel": "channel",
                            "direct": "personal",
                            "group_direct": "group",
                        }[source["chat_type"]],
                        "original_url": source.get("original_url"),
                        "description": catalog["description"],
                        "participants": normalized_participants,
                        "topics": normalized_topics,
                    }
                ),
                catalog["display_name"].strip(),
                select_discovered,
                project_uuid if select_discovered else None,
                projection_stream_uuid,
                "syncing" if select_discovered else "available",
                _json(catalog["capabilities"]),
                _json(catalog["capabilities"]),
            ),
        )
        chat = external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(chat_uuid)}, session=session
        )
        if chat.selected:
            external_projection.ensure_external_chat_stream(
                session,
                project_id=chat.project_id,
                owner_user_uuid=chat.owner_user_uuid,
                projection_stream_uuid=chat.projection_stream_uuid,
                bridge_instance_uuid=identity.bridge_instance_uuid,
                external_account_uuid=chat.external_account_uuid,
                provider_kind=chat.provider,
                provider_chat_id=chat.provider_chat_id,
                display_name=chat.display_name,
                source=chat.source,
                capabilities=chat.capabilities,
                account_settings=account["settings"],
            )
            append_upsert(
                session,
                identity.bridge_instance_uuid,
                identity.provider_kind,
                external_chat_assignment_desired(chat, session=session),
            )
        messenger_events.create_external_resource_event(
            project_uuid,
            owner_uuid,
            chat,
            messenger_events.EXTERNAL_CHAT_UPDATED_EVENT,
            hidden_fields=("owner_user_uuid", "provider", "provider_chat_id"),
            session=session,
        )

    def _reconcile_observed_report(
        self,
        session: Any,
        identity: pki.BridgeIdentity,
        report: dict[str, Any],
    ) -> None:
        if report["resource_type"] == "external_chat_catalog":
            self._reconcile_catalog_report(session, identity, report)
            return
        resource_uuid = sys_uuid.UUID(str(report["resource_uuid"]))
        generation = int(report["observed_generation"])
        safe_error = self._safe_error_message(report)
        if report["resource_type"] == "external_account":
            account = external_models.ExternalAccount.objects.get_one_or_none(
                filters={"uuid": dm_filters.EQ(resource_uuid)},
                session=session,
            )
            if account is None or account.desired_generation != generation:
                return
            status_map = {
                "applying": external_models.ExternalAccountStatus.CONNECTING.value,
                "ready": external_models.ExternalAccountStatus.CONNECTING.value,
                "live_ready": external_models.ExternalAccountStatus.LIVE.value,
                "backfill": external_models.ExternalAccountStatus.BACKFILL.value,
                "degraded": external_models.ExternalAccountStatus.DEGRADED.value,
                "auth_required": (
                    external_models.ExternalAccountStatus.AUTH_REQUIRED.value
                ),
                "disconnected": (
                    external_models.ExternalAccountStatus.DISCONNECTED.value
                ),
                "suspended": external_models.ExternalAccountStatus.SUSPENDED.value,
                "unsupported_capability": (
                    external_models.ExternalAccountStatus.DEGRADED.value
                ),
                "failed": external_models.ExternalAccountStatus.DEGRADED.value,
            }
            status = status_map.get(report["status"])
            if status is None:
                return
            live_ready = account.live_ready
            if report["status"] == "live_ready":
                live_ready = True
            elif report["status"] in {
                "auth_required",
                "disconnected",
                "suspended",
                "failed",
            }:
                live_ready = False
            account.properties["status"].set_value_force(status)
            account.properties["live_ready"].set_value_force(live_ready)
            account.properties["safe_error"].set_value_force(safe_error)
            account.properties["applied_generation"].set_value_force(generation)
            account.properties["last_progress_at"].set_value_force(
                self._progress_timestamp(report)
            )
            account.properties["revision"].set_value_force(account.revision + 1)
            account.update(session=session)
            messenger_events.create_external_resource_event(
                sys_uuid.UUID(str(account.settings["default_project_id"])),
                account.owner_user_uuid,
                account,
                messenger_events.EXTERNAL_ACCOUNT_UPDATED_EVENT,
                hidden_fields=("owner_user_uuid", "provider"),
                session=session,
            )
            return
        if report["resource_type"] == "external_chat_assignment":
            chat = external_models.ExternalChat.objects.get_one_or_none(
                filters={"uuid": dm_filters.EQ(resource_uuid)},
                session=session,
            )
            if chat is None:
                return
            status_map = {
                "applying": external_models.ExternalChatStatus.SYNCING.value,
                "ready": external_models.ExternalChatStatus.SYNCING.value,
                "live_ready": external_models.ExternalChatStatus.LIVE.value,
                "backfill": external_models.ExternalChatStatus.SYNCING.value,
                "degraded": external_models.ExternalChatStatus.DEGRADED.value,
                "auth_required": external_models.ExternalChatStatus.DEGRADED.value,
                "disconnected": external_models.ExternalChatStatus.DESELECTED.value,
                "suspended": external_models.ExternalChatStatus.DESELECTED.value,
                "unsupported_capability": (
                    external_models.ExternalChatStatus.DEGRADED.value
                ),
                "failed": external_models.ExternalChatStatus.DEGRADED.value,
            }
            status = status_map.get(report["status"])
            if status is None:
                return
            chat.properties["status"].set_value_force(status)
            chat.properties["safe_error"].set_value_force(safe_error)
            chat.properties["revision"].set_value_force(chat.revision + 1)
            chat.update(session=session)
            if chat.project_id is not None:
                messenger_events.create_external_resource_event(
                    chat.project_id,
                    chat.owner_user_uuid,
                    chat,
                    messenger_events.EXTERNAL_CHAT_UPDATED_EVENT,
                    hidden_fields=(
                        "owner_user_uuid",
                        "provider",
                        "provider_chat_id",
                    ),
                    session=session,
                )

    def reconcile_observed_reports(
        self,
        session: Any,
        identity: pki.BridgeIdentity,
        reports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not 1 <= len(reports) <= 500:
            raise ValueError("Observed report batch size is invalid")
        results = []
        for report in reports:
            report_uuid = sys_uuid.UUID(report["report_uuid"])
            canonical = hashlib.sha256(_json(report).encode()).hexdigest()
            existing = session.execute(
                'SELECT "canonical_sha256" '
                'FROM "m_external_bridge_observed_reports_v1" '
                'WHERE "report_uuid" = %s',
                (report_uuid,),
            ).fetchone()
            if existing is not None:
                status = (
                    "duplicate"
                    if existing["canonical_sha256"] == canonical
                    else "rejected"
                )
            else:
                desired_resource_type = report["resource_type"]
                desired_resource_uuid = report["resource_uuid"]
                if report["resource_type"] == "external_chat_catalog":
                    catalog = report.get("catalog")
                    if not isinstance(catalog, dict):
                        raise ValueError("External chat catalog report is missing")
                    desired_resource_type = "external_account"
                    desired_resource_uuid = catalog["external_account_uuid"]
                desired = session.execute(
                    """
                        SELECT "operation", "generation"
                        FROM "m_external_bridge_desired_resources_v1"
                        WHERE "bridge_instance_uuid" = %s
                          AND "provider_kind" = %s
                          AND "resource_type" = %s AND "resource_uuid" = %s
                        """,
                    (
                        identity.bridge_instance_uuid,
                        identity.provider_kind,
                        desired_resource_type,
                        desired_resource_uuid,
                    ),
                ).fetchone()
                latest = session.execute(
                    """
                        SELECT MAX("observed_generation") AS "generation"
                        FROM "m_external_bridge_observed_reports_v1"
                        WHERE "bridge_instance_uuid" = %s
                          AND "resource_type" = %s AND "resource_uuid" = %s
                        """,
                    (
                        identity.bridge_instance_uuid,
                        report["resource_type"],
                        report["resource_uuid"],
                    ),
                ).fetchone()["generation"]
                if desired is None:
                    status = "rejected"
                elif (
                    latest is not None and latest > report["observed_generation"]
                ) or desired["generation"] > report["observed_generation"]:
                    status = "stale"
                elif (
                    desired["operation"] != "upsert"
                    or desired["generation"] < report["observed_generation"]
                ):
                    status = "rejected"
                else:
                    status = "applied"
                session.execute(
                    """
                        INSERT INTO "m_external_bridge_observed_reports_v1" (
                            "report_uuid", "bridge_instance_uuid",
                            "canonical_sha256", "resource_type", "resource_uuid",
                            "observed_generation", "payload", "observed_at"
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                        """,
                    (
                        report_uuid,
                        identity.bridge_instance_uuid,
                        canonical,
                        report["resource_type"],
                        report["resource_uuid"],
                        report["observed_generation"],
                        _json(report),
                        report["observed_at"],
                    ),
                )
                if status == "applied":
                    try:
                        self._reconcile_observed_report(session, identity, report)
                    except ValueError:
                        status = "rejected"
            results.append(
                {
                    "report_uuid": str(report_uuid),
                    "status": status,
                    "safe_error": (
                        None
                        if status != "rejected"
                        else {
                            "code": "observed_report_rejected",
                            "message": "Observed report does not match current desired state",
                            "retryable": False,
                        }
                    ),
                }
            )
        refresh_effective_capabilities(
            session,
            provider_kind=identity.provider_kind,
        )
        return {"results": results}

    def observed_reports(
        self,
        identity: pki.BridgeIdentity,
        reports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._current_session() as session:
            return self.reconcile_observed_reports(session, identity, reports)

    def assignment(
        self,
        identity: pki.BridgeIdentity,
        external_account_uuid: sys_uuid.UUID | str,
        external_chat_uuid: sys_uuid.UUID | str,
    ) -> dict[str, Any] | None:
        with self._current_session() as session:
            rows = session.execute(
                """
                SELECT "resource_type", "resource"
                FROM "m_external_bridge_desired_resources_v1"
                WHERE "bridge_instance_uuid" = %s AND "provider_kind" = %s
                  AND "operation" = 'upsert'
                  AND (
                    ("resource_type" = 'external_account' AND "resource_uuid" = %s) OR
                    ("resource_type" = 'external_chat_assignment' AND "resource_uuid" = %s)
                  )
                """,
                (
                    identity.bridge_instance_uuid,
                    identity.provider_kind,
                    external_account_uuid,
                    external_chat_uuid,
                ),
            ).fetchall()
        resources = {row["resource_type"]: row["resource"] for row in rows}
        account = resources.get("external_account")
        chat = resources.get("external_chat_assignment")
        if (
            account is None
            or chat is None
            or chat["external_account_uuid"] != str(external_account_uuid)
        ):
            return None
        return {"account": account, "chat": chat}

    def file_transfer_get(self, key: str) -> dict[str, Any] | None:
        with self._current_session() as session:
            row = session.execute(
                'SELECT "value" FROM "m_external_bridge_file_transfers_v1" '
                'WHERE "transfer_key" = %s',
                (key,),
            ).fetchone()
        return None if row is None else row["value"]

    def file_transfer_put(self, key: str, value: dict[str, object]) -> None:
        with self._current_session() as session:
            session.execute(
                """
                INSERT INTO "m_external_bridge_file_transfers_v1" (
                    "transfer_key", "bridge_instance_uuid", "value"
                ) VALUES (%s, %s, %s::jsonb)
                ON CONFLICT ("transfer_key") DO UPDATE
                SET "value" = EXCLUDED."value", "updated_at" = NOW()
                WHERE "m_external_bridge_file_transfers_v1"."bridge_instance_uuid"
                      = EXCLUDED."bridge_instance_uuid"
                """,
                (key, value["bridge_instance_uuid"], _json(value)),
            )

    def _instance(self, session: Any, identity: pki.BridgeIdentity) -> Any:
        ensure_instance(session, identity.bridge_instance_uuid, identity.provider_kind)
        return session.execute(
            'SELECT * FROM "m_external_bridge_control_instances_v1" '
            'WHERE "bridge_instance_uuid" = %s AND "provider_kind" = %s',
            (identity.bridge_instance_uuid, identity.provider_kind),
        ).fetchone()

    def _prune(self, session: Any, now: datetime.datetime) -> None:
        rows = session.execute(
            """
            DELETE FROM "m_external_bridge_desired_changes_v1"
            WHERE "created_at" < %s
            RETURNING "bridge_instance_uuid", "provider_kind",
                      "resource_type", "sequence"
            """,
            (now - state.CHANGE_RETENTION,),
        ).fetchall()
        watermarks: dict[tuple[sys_uuid.UUID, str], dict[str, int]] = {}
        for row in rows:
            key = (row["bridge_instance_uuid"], row["provider_kind"])
            watermarks.setdefault(key, {})[row["resource_type"]] = max(
                watermarks.setdefault(key, {}).get(row["resource_type"], 0),
                row["sequence"],
            )
        for (instance_uuid, provider_kind), updates in watermarks.items():
            current = session.execute(
                'SELECT "pruned_through_sequence" '
                'FROM "m_external_bridge_control_instances_v1" '
                'WHERE "bridge_instance_uuid" = %s AND "provider_kind" = %s '
                "FOR UPDATE",
                (instance_uuid, provider_kind),
            ).fetchone()["pruned_through_sequence"]
            for resource_type, sequence in updates.items():
                current[resource_type] = max(current.get(resource_type, 0), sequence)
            session.execute(
                'UPDATE "m_external_bridge_control_instances_v1" '
                'SET "pruned_through_sequence" = %s::jsonb, "updated_at" = %s '
                'WHERE "bridge_instance_uuid" = %s',
                (_json(current), now, instance_uuid),
            )

    @staticmethod
    def _normalize_types(
        resource_types: collections.abc.Iterable[str] | None,
    ) -> tuple[str, ...]:
        return state.PersistentControlState._normalize_types(resource_types)

    def _scope(
        self,
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str],
    ) -> dict[str, object]:
        return {
            "realm_uuid": str(self.realm_uuid),
            "bridge_instance_uuid": str(identity.bridge_instance_uuid),
            "provider_kind": identity.provider_kind,
            "resource_types": list(resource_types),
            "control_schema_version": state.CONTROL_SCHEMA_VERSION,
        }

    def _encode_cursor(
        self,
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str],
        sequence: int,
        generation: int,
    ) -> str:
        return self._sign(
            {
                **self._scope(identity, resource_types),
                "sequence": sequence,
                "snapshot_generation": generation,
            }
        )

    def _decode_cursor(
        self,
        cursor: str,
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str],
        generation: int,
    ) -> dict[str, Any]:
        try:
            payload = self._verify(cursor)
        except ValueError as error:
            raise state.CursorExpiredError("schema_mismatch", generation) from error
        expected = self._scope(identity, resource_types)
        if any(payload.get(key) != value for key, value in expected.items()):
            raise state.CursorExpiredError("scope_mismatch", generation)
        if payload.get("snapshot_generation") != generation:
            raise state.CursorExpiredError("generation_mismatch", generation)
        return payload

    def _sign(self, payload: object) -> str:
        content = _json(payload).encode()
        signature = hmac.new(self.signing_key, content, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(content + signature).rstrip(b"=").decode()

    def _verify(self, value: str) -> dict[str, Any]:
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            content, signature = decoded[:-32], decoded[-32:]
            expected = hmac.new(self.signing_key, content, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            return json.loads(content)
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid opaque cursor") from error

    @staticmethod
    def _wire_change(row: dict[str, object]) -> dict[str, object]:
        value: dict[str, object] = {
            "change_uuid": str(row["change_uuid"]),
            "sequence": row["sequence"],
            "resource_type": row["resource_type"],
            "resource_uuid": str(row["resource_uuid"]),
            "operation": row["operation"],
            "generation": row["generation"],
        }
        if row["operation"] == "upsert":
            value["required_capabilities"] = row["required_capabilities"]
            value["resource"] = row["resource"]
        return value

    @staticmethod
    def _wire_snapshot(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_uuid": str(row["request_uuid"]),
            "snapshot_token": row["snapshot_token"],
            "anchor_cursor": row["anchor_cursor"],
            "snapshot_generation": row["snapshot_generation"],
            "resource_types": row["resource_types"],
            "expires_at": _timestamp(row["expires_at"]),
        }
