# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Transactional PostgreSQL data plane for external provider services."""

import datetime
import hashlib
import json
import logging
import time
import typing
import uuid as sys_uuid

from restalchemy.common import exceptions as ra_exceptions
from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exceptions

from workspace.external_bridge_control import provider_event_apply
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import helpers as messenger_helpers
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import models


LEASE_MIN_SECONDS = 10
LEASE_MAX_SECONDS = 300
LEASE_MAX_ITEMS = 100
RESULT_MAX_ITEMS = 500
EVENT_MAX_ITEMS = 500
HEARTBEAT_MAX_AGE = datetime.timedelta(seconds=60)
LOG = logging.getLogger(__name__)

_OPERATION_CAPABILITIES = {
    "message.create": "messenger.message.send",
    "message.update": "messenger.message.edit",
    "message.delete": "messenger.message.delete",
    "read_state.set": "messenger.message.read",
    "reaction.create": "messenger.reaction.write",
    "reaction.update": "messenger.reaction.write",
    "reaction.delete": "messenger.reaction.write",
    "membership.add": "messenger.membership.write",
    "membership.remove": "messenger.membership.write",
    "stream.notification.update": "messenger.notification.write",
    "topic.notification.update": "messenger.notification.write",
    "stream.delete": "messenger.stream.delete",
    "topic.create": "messenger.topic.create",
    "stream.update": "messenger.stream.rename",
    "topic.update": "messenger.topic.rename",
    "topic.delete": "messenger.topic.delete",
}
_RECONCILIATION_REASONS = {
    "provider_history_unavailable",
    "no_match_after_auto_resend",
    "unsafe_provider_state",
}
_DELIVERY_STATUS_BY_OPERATION_STATUS = {
    "queued": "pending",
    "running": "pending",
    "succeeded": "delivered",
    "failed": "failed",
    "manual_reconciliation_required": "manual_reconciliation_required",
    "discarded": "discarded",
}


def _is_quiet_backfill_event(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    # Only event kinds whose apply path suppresses every Workspace broadcast
    # may bypass the project event lock. Other backfill operations still need
    # the normal lock order even when their resource metadata says backfill.
    if event.get("kind") not in {
        "message.upsert",
        "reaction.upsert",
        "reaction.delete",
        "topic.upsert",
    }:
        return False
    payload = event.get("payload")
    resource = payload.get("resource") if isinstance(payload, dict) else None
    metadata = resource.get("provider_metadata") if isinstance(resource, dict) else None
    return isinstance(metadata, dict) and metadata.get("delivery_class") == "backfill"


def _is_account_global_identity_event(event: object) -> bool:
    if not isinstance(event, dict) or event.get("kind") != "identity.upsert":
        return False
    payload = event.get("payload")
    resource = payload.get("resource") if isinstance(payload, dict) else None
    metadata = resource.get("provider_metadata") if isinstance(resource, dict) else None
    return isinstance(metadata, dict) and metadata.get("chat_key") == "account"


class ProviderDataError(RuntimeError):
    status = 400
    error = "provider_request_invalid"


class ProviderUnavailableError(ProviderDataError):
    status = 409
    error = "provider_bridge_unavailable"


class ProviderPolicyBlockedError(ProviderUnavailableError):
    """Current administrative policy forbids accepting the operation."""


class ProviderBatchError(ProviderDataError):
    status = 422
    error = "provider_event_batch_rejected"


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid_string(value: str | sys_uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def _bridge_capabilities(
    session: typing.Any,
    identity: typing.Any,
    now: datetime.datetime,
) -> object:
    row = session.execute(
        """
        SELECT "status", "capabilities", "last_heartbeat_at"
        FROM "m_external_bridge_instances_v2"
        WHERE "uuid" = %s AND "provider" = %s AND "identity_generation" = %s
        """,
        (
            identity.bridge_instance_uuid,
            identity.provider_kind,
            identity.identity_generation,
        ),
    ).fetchone()
    if (
        row is None
        or row["status"] != external_models.ExternalBridgeInstanceStatus.ACTIVE.value
        or row["last_heartbeat_at"] is None
        or row["last_heartbeat_at"] < now - HEARTBEAT_MAX_AGE
    ):
        raise ProviderUnavailableError("A current healthy bridge heartbeat is required")
    return row["capabilities"]


def _required_capability(operation_kind: str) -> str | None:
    return _OPERATION_CAPABILITIES.get(operation_kind)


def _advertises_capability(capabilities: object, name: str) -> bool:
    descriptor = capabilities.get(name) if isinstance(capabilities, dict) else None
    return (
        isinstance(descriptor, dict) and descriptor.get("available", True) is not False
    )


def _effective_capability_available(capabilities: object, name: str) -> bool:
    descriptor = capabilities.get(name) if isinstance(capabilities, dict) else None
    return isinstance(descriptor, dict) and descriptor.get("available") is True


def _require_current_provider_policy(
    session: typing.Any,
    provider: object,
    *,
    capability_name: str | None = None,
    capabilities: object = None,
) -> typing.Mapping[str, typing.Any]:
    """Serialize provider operations with the current administrative policy."""
    policy = session.execute(
        """
        SELECT enabled, emergency_suspended, limits
        FROM m_external_provider_policies_v1
        WHERE provider = %s
        FOR SHARE
        """,
        (provider,),
    ).fetchone()
    if policy is None or policy["enabled"] is not True:
        raise ProviderPolicyBlockedError(
            "External provider is disabled by realm policy"
        )
    if policy["emergency_suspended"]:
        raise ProviderPolicyBlockedError(
            "External provider is administratively suspended"
        )
    if capability_name == "messenger.file.transfer":
        policy_limit = policy["limits"].get("max_file_bytes", 0)
        descriptor = (
            capabilities.get(capability_name)
            if isinstance(capabilities, dict)
            else None
        )
        cached_limit = (
            descriptor.get("limits", {}).get("max_file_bytes")
            if isinstance(descriptor, dict)
            else None
        )
        if (
            isinstance(policy_limit, bool)
            or not isinstance(policy_limit, int)
            or policy_limit < 1
            or isinstance(cached_limit, bool)
            or not isinstance(cached_limit, int)
            or cached_limit > policy_limit
        ):
            raise ProviderPolicyBlockedError(
                "External provider capability limits require refresh"
            )
    return policy


def _capability_limit(capabilities: object, name: str, limit_name: str) -> int | None:
    descriptor = capabilities.get(name) if isinstance(capabilities, dict) else None
    limits = descriptor.get("limits") if isinstance(descriptor, dict) else None
    value = limits.get(limit_name) if isinstance(limits, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _require_current_provider_inputs(
    session: typing.Any,
    *,
    chat_uuid: object,
    bridge_uuid: object,
    capability_name: str,
    account_capabilities: object,
    chat_capabilities: object,
    now: datetime.datetime | None = None,
) -> None:
    """Lock and intersect the current heartbeat and chat-catalog inputs."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    current = session.execute(
        """
        SELECT chat.selected, chat.status AS chat_status,
               chat.transition_pending, chat.catalog_capabilities,
               bridge.status AS bridge_status,
               bridge.capabilities AS bridge_capabilities,
               bridge.last_heartbeat_at
        FROM m_external_chats_v2 AS chat
        CROSS JOIN m_external_bridge_instances_v2 AS bridge
        WHERE chat.uuid = %s AND bridge.uuid = %s
        FOR SHARE OF chat, bridge
        """,
        (chat_uuid, bridge_uuid),
    ).fetchone()
    if (
        current is None
        or current["selected"] is not True
        or current["transition_pending"] is True
        or current["chat_status"] not in {"syncing", "live"}
        or current["bridge_status"] not in {"active", "degraded"}
        or current["last_heartbeat_at"] is None
        or current["last_heartbeat_at"] < now - HEARTBEAT_MAX_AGE
        or not _advertises_capability(current["bridge_capabilities"], capability_name)
        or not _advertises_capability(current["catalog_capabilities"], capability_name)
    ):
        raise ProviderUnavailableError("External provider capability is unavailable")
    if capability_name != "messenger.file.transfer":
        return
    cached_limits = [
        value
        for value in (
            _capability_limit(
                account_capabilities,
                capability_name,
                "max_file_bytes",
            ),
            _capability_limit(
                chat_capabilities,
                capability_name,
                "max_file_bytes",
            ),
        )
        if value is not None
    ]
    current_limits = [
        value
        for value in (
            _capability_limit(
                current["bridge_capabilities"],
                capability_name,
                "max_file_bytes",
            ),
            _capability_limit(
                current["catalog_capabilities"],
                capability_name,
                "max_file_bytes",
            ),
        )
        if value is not None
    ]
    if cached_limits and current_limits and min(cached_limits) > min(current_limits):
        raise ProviderUnavailableError(
            "External provider capability limits require refresh"
        )


def _lock_associated_bridge(
    session: typing.Any,
    *,
    account_uuid: object,
    provider: object,
    statuses: tuple[str, ...],
) -> external_models.ExternalBridgeInstance:
    """Lock the bridge named by the account credential, not a provider peer."""
    row = session.execute(
        """
        SELECT bridge.uuid
        FROM m_external_credentials_v2 AS credential
        JOIN m_external_bridge_instances_v2 AS bridge
          ON bridge.uuid::text = credential.envelope #>>
             '{associated_data,bridge_instance_uuid}'
        WHERE credential.external_account_uuid = %s
          AND bridge.provider = %s
          AND bridge.status = ANY(%s::text[])
        FOR SHARE OF credential, bridge
        """,
        (account_uuid, provider, list(statuses)),
    ).fetchone()
    if row is None:
        raise ProviderUnavailableError("External account bridge routing is unavailable")
    return external_models.ExternalBridgeInstance.objects.get_one(
        filters={"uuid": dm_filters.EQ(row["uuid"])},
        session=session,
    )


def resolve_provider_target(
    session: typing.Any,
    *,
    project_id: object,
    owner_user_uuid: object,
    external_account_uuid: object,
    stream_uuid: object,
    capability_name: str,
) -> tuple[
    external_models.ExternalAccount,
    external_models.ExternalChat,
    external_models.ExternalBridgeInstance,
]:
    """Resolve one provider-capable selected chat and its bridge."""
    account = external_models.ExternalAccount.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(external_account_uuid),
            "owner_user_uuid": dm_filters.EQ(owner_user_uuid),
        },
        session=session,
    )
    _require_current_provider_policy(
        session,
        account.provider,
        capability_name=capability_name,
        capabilities=account.capabilities,
    )
    chats = external_models.ExternalChat.objects.get_all(
        filters={
            "external_account_uuid": dm_filters.EQ(account.uuid),
            "owner_user_uuid": dm_filters.EQ(owner_user_uuid),
            "project_id": dm_filters.EQ(project_id),
            "projection_stream_uuid": dm_filters.EQ(stream_uuid),
            "selected": dm_filters.EQ(True),
            "status": dm_filters.In(
                (
                    external_models.ExternalChatStatus.SYNCING.value,
                    external_models.ExternalChatStatus.LIVE.value,
                )
            ),
            "transition_pending": dm_filters.EQ(False),
        },
        session=session,
        limit=2,
    )
    if len(chats) != 1:
        raise ProviderUnavailableError("External chat is not live and selected")
    chat = chats[0]
    if (
        (
            not account.live_ready
            and account.status
            != external_models.ExternalAccountStatus.BACKFILL.value
        )
        or not _effective_capability_available(account.capabilities, capability_name)
        or not _effective_capability_available(chat.capabilities, capability_name)
    ):
        raise ProviderUnavailableError("External chat capability is unavailable")
    bridge = _lock_associated_bridge(
        session=session,
        account_uuid=account.uuid,
        provider=account.provider,
        statuses=(
            external_models.ExternalBridgeInstanceStatus.ACTIVE.value,
            external_models.ExternalBridgeInstanceStatus.DEGRADED.value,
        ),
    )
    _require_current_provider_inputs(
        session,
        chat_uuid=chat.uuid,
        bridge_uuid=bridge.uuid,
        capability_name=capability_name,
        account_capabilities=account.capabilities,
        chat_capabilities=chat.capabilities,
    )
    return account, chat, bridge


def resolve_provider_queue_target(
    session: typing.Any,
    *,
    project_id: object,
    owner_user_uuid: object,
    external_account_uuid: object,
    stream_uuid: object,
    allow_policy_blocked: bool = False,
) -> tuple[
    external_models.ExternalAccount,
    external_models.ExternalChat,
    external_models.ExternalBridgeInstance,
]:
    """Resolve durable routing even while provider capability is unavailable."""
    account = external_models.ExternalAccount.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(external_account_uuid),
            "owner_user_uuid": dm_filters.EQ(owner_user_uuid),
        },
        session=session,
    )
    if not allow_policy_blocked:
        _require_current_provider_policy(session, account.provider)
    chats = external_models.ExternalChat.objects.get_all(
        filters={
            "external_account_uuid": dm_filters.EQ(account.uuid),
            "owner_user_uuid": dm_filters.EQ(owner_user_uuid),
            "project_id": dm_filters.EQ(project_id),
            "projection_stream_uuid": dm_filters.EQ(stream_uuid),
            "selected": dm_filters.EQ(True),
            "status": dm_filters.In(
                (
                    external_models.ExternalChatStatus.SYNCING.value,
                    external_models.ExternalChatStatus.LIVE.value,
                    external_models.ExternalChatStatus.DEGRADED.value,
                )
            ),
            "transition_pending": dm_filters.EQ(False),
        },
        session=session,
        limit=2,
    )
    if len(chats) != 1:
        raise ProviderUnavailableError("External chat routing is unavailable")
    bridge = _lock_associated_bridge(
        session=session,
        account_uuid=account.uuid,
        provider=account.provider,
        statuses=(
            external_models.ExternalBridgeInstanceStatus.ACTIVE.value,
            external_models.ExternalBridgeInstanceStatus.DEGRADED.value,
            external_models.ExternalBridgeInstanceStatus.INCOMPATIBLE.value,
            external_models.ExternalBridgeInstanceStatus.SUSPENDED.value,
        ),
    )
    return account, chats[0], bridge


def _operation_dict(
    row: typing.Mapping[str, typing.Any],
) -> dict[str, typing.Any]:
    required_capability = _required_capability(row["operation_kind"])
    return {
        "provider_operation_uuid": str(row["uuid"]),
        "external_operation_uuid": str(row["external_operation_uuid"]),
        "lease_uuid": str(row["lease_uuid"]),
        "lease_expires_at": _timestamp(row["lease_expires_at"]),
        "external_account_uuid": str(row["external_account_uuid"]),
        "project_id": str(row["project_id"]),
        "operation_kind": row["operation_kind"],
        "required_capability": required_capability,
        "attempt": row["attempt"],
        "payload": row["payload"],
    }


def _operation_delivery(
    operation: external_models.ExternalOperation,
) -> dict[str, typing.Any]:
    status = _DELIVERY_STATUS_BY_OPERATION_STATUS[operation.status]
    return {
        "external_operation_uuid": str(operation.uuid),
        "status": status,
        "safe_error": operation.safe_error,
        "can_retry": operation.can_retry,
        "can_discard": operation.can_discard,
        "updated_at": _timestamp(operation.updated_at),
        "duplicate_risk": operation.duplicate_risk,
        "retry_requires_confirmation": operation.retry_requires_confirmation,
        "original_url": operation.original_url,
        "reconciliation_reason": operation.reconciliation_reason,
    }


def _emit_target_updated_events(
    session: typing.Any,
    project_id: object,
    target_type: str,
    target_uuid: object,
) -> None:
    if target_type == "message":
        message = models.WorkspaceMessage.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(project_id),
                "uuid": dm_filters.EQ(target_uuid),
            },
            session=session,
        )
        messenger_helpers.create_compact_workspace_message_updated_events(
            project_id,
            message,
            session=session,
        )
        return
    model_and_event = {
        "stream": (
            models.WorkspaceUserStream,
            messenger_events.create_stream_updated_event,
        ),
        "topic": (
            models.WorkspaceUserTopic,
            messenger_events.create_topic_updated_event,
        ),
    }.get(target_type)
    if model_and_event is None:
        return
    model, create_event = model_and_event
    for resource in model.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "uuid": dm_filters.EQ(target_uuid),
        },
        session=session,
    ):
        create_event(resource, session=session)


def sync_operation_target_delivery(
    session: typing.Any,
    operation: external_models.ExternalOperation,
    project_id: object,
) -> None:
    """Project one public operation status onto its canonical target."""
    target = {
        "stream": ("m_workspace_streams", "uuid"),
        "topic": ("m_workspace_stream_topics", "uuid"),
        "message": ("m_workspace_messages", "uuid"),
    }.get(operation.target_type)
    if target is None or operation.target_uuid is None:
        return
    delivery = _operation_delivery(operation)
    legacy_status = {
        "pending": "pending",
        "delivered": "delivered",
    }.get(delivery["status"], "failed")
    table, uuid_column = target
    changed = session.execute(
        f"""
        UPDATE {table}
        SET delivery_metadata = %s::jsonb,
            delivery_status = %s,
            delivery_error = %s,
            delivery_updated_at = %s
        WHERE project_id = %s AND {uuid_column} = %s
        RETURNING {uuid_column}
        """,
        (
            _canonical_json(delivery),
            legacy_status,
            operation.safe_error,
            operation.updated_at,
            project_id,
            operation.target_uuid,
        ),
    ).fetchone()
    if changed is not None:
        _emit_target_updated_events(
            session,
            project_id,
            operation.target_type,
            operation.target_uuid,
        )


def publish_operation_event(
    session: typing.Any,
    operation: external_models.ExternalOperation,
    project_id: object,
    event_kind: str,
) -> None:
    """Publish one operation and its target delivery snapshot atomically."""
    messenger_events.create_external_resource_event(
        project_id,
        operation.owner_user_uuid,
        operation,
        event_kind,
        hidden_fields=("owner_user_uuid",),
        session=session,
    )
    sync_operation_target_delivery(session, operation, project_id)


def _emit_operation_event(
    session: typing.Any,
    operation_uuid: object,
    project_id: object,
    event_kind: str,
) -> None:
    operation = external_models.ExternalOperation.objects.get_one(
        filters={"uuid": dm_filters.EQ(operation_uuid)},
        session=session,
    )
    publish_operation_event(session, operation, project_id, event_kind)


def lease_provider_operations(
    session: typing.Any,
    identity: typing.Any,
    *,
    request_uuid: object,
    limit: int,
    lease_seconds: int,
    now: datetime.datetime | None = None,
) -> dict[str, object]:
    """Lease one FIFO batch in the request-owned transaction."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    request_uuid = sys_uuid.UUID(str(request_uuid))
    limit = int(limit)
    lease_seconds = int(lease_seconds)
    if not 1 <= limit <= LEASE_MAX_ITEMS:
        raise ValueError("Lease limit is outside the supported range")
    if not LEASE_MIN_SECONDS <= lease_seconds <= LEASE_MAX_SECONDS:
        raise ValueError("Lease duration is outside the supported range")
    capabilities = _bridge_capabilities(session, identity, now)
    existing = session.execute(
        """
        SELECT *
        FROM "m_external_provider_operations_v1"
        WHERE "bridge_instance_uuid" = %s AND "lease_uuid" = %s
          AND "status" = 'leased' AND "lease_expires_at" > %s
        ORDER BY "sequence"
        """,
        (identity.bridge_instance_uuid, request_uuid, now),
    ).fetchall()
    if existing:
        return {
            "request_uuid": str(request_uuid),
            "operations": [_operation_dict(row) for row in existing],
        }
    session.execute(
        """
        UPDATE "m_external_provider_operations_v1"
        SET "status" = 'queued', "lease_uuid" = NULL,
            "lease_expires_at" = NULL, "available_at" = %s,
            "updated_at" = %s
        WHERE "bridge_instance_uuid" = %s AND "status" = 'leased'
          AND "lease_expires_at" <= %s
        """,
        (now, now, identity.bridge_instance_uuid, now),
    )
    allowed_kinds = tuple(
        operation_kind
        for operation_kind, capability in _OPERATION_CAPABILITIES.items()
        if _advertises_capability(capabilities, capability)
    )
    if not allowed_kinds:
        return {"request_uuid": str(request_uuid), "operations": []}
    rows = session.execute(
        """
        WITH candidates AS (
            SELECT operation."uuid"
            FROM "m_external_provider_operations_v1" AS operation
            JOIN "m_external_accounts_v2" AS account
              ON account."uuid" = operation."external_account_uuid"
            JOIN "m_external_provider_policies_v1" AS policy
              ON policy."provider" = account."provider"
             AND policy."enabled" = TRUE
             AND policy."emergency_suspended" = FALSE
            WHERE operation."bridge_instance_uuid" = %s
              AND operation."status" = 'queued'
              AND operation."available_at" <= %s
              AND operation."operation_kind" = ANY(%s::text[])
            ORDER BY operation."sequence"
            LIMIT %s
            FOR SHARE OF policy
            FOR UPDATE OF operation SKIP LOCKED
        )
        UPDATE "m_external_provider_operations_v1" AS operation
        SET "status" = 'leased', "attempt" = operation."attempt" + 1,
            "lease_uuid" = %s, "lease_expires_at" = %s,
            "updated_at" = %s
        FROM candidates
        WHERE operation."uuid" = candidates."uuid"
        RETURNING operation.*
        """,
        (
            identity.bridge_instance_uuid,
            now,
            list(allowed_kinds),
            limit,
            request_uuid,
            now + datetime.timedelta(seconds=lease_seconds),
            now,
        ),
    ).fetchall()
    if rows:
        session.execute(
            """
            UPDATE "m_external_operations_v2" AS public_operation
            SET "status" = 'running', "attempt" = provider_operation."attempt",
                "can_retry" = FALSE, "can_discard" = FALSE,
                "revision" = public_operation."revision" + 1,
                "updated_at" = %s
            FROM "m_external_provider_operations_v1" AS provider_operation
            WHERE public_operation."uuid" = provider_operation."external_operation_uuid"
              AND provider_operation."lease_uuid" = %s
            """,
            (now, request_uuid),
        )
        for row in rows:
            _emit_operation_event(
                session,
                row["external_operation_uuid"],
                row["project_id"],
                messenger_events.EXTERNAL_OPERATION_UPDATED_EVENT,
            )
    return {
        "request_uuid": str(request_uuid),
        "operations": [_operation_dict(row) for row in rows],
    }


def _result_status(result: dict[str, object]) -> tuple[str, str]:
    status = result["status"]
    if status == "succeeded":
        return "succeeded", "succeeded"
    if status == "failed":
        return "failed", "failed"
    if status == "manual_reconciliation_required":
        return "failed", "manual_reconciliation_required"
    raise ValueError("Unsupported provider result status")


def _validated_result(result: object) -> dict[str, typing.Any]:
    if not isinstance(result, dict):
        raise TypeError("Provider result must be an object")
    result_uuid = sys_uuid.UUID(str(result["result_uuid"]))
    provider_operation_uuid = sys_uuid.UUID(str(result["provider_operation_uuid"]))
    lease_uuid = sys_uuid.UUID(str(result["lease_uuid"]))
    queue_status, public_status = _result_status(result)
    safe_error = result.get("safe_error")
    if safe_error is not None and (
        not isinstance(safe_error, str) or len(safe_error) > 1024
    ):
        raise ValueError("Provider safe error is invalid")
    original_url = result.get("original_url")
    if original_url is not None and not isinstance(original_url, str):
        raise ValueError("Provider original URL is invalid")
    reconciliation = result.get("reconciliation") or {}
    if not isinstance(reconciliation, dict):
        raise ValueError("Provider reconciliation data is invalid")
    evidence = reconciliation.get("evidence", {})
    if not isinstance(evidence, dict):
        raise ValueError("Provider reconciliation evidence is invalid")
    manual = public_status == "manual_reconciliation_required"
    if manual and reconciliation.get("reason") not in _RECONCILIATION_REASONS:
        raise ValueError("Manual reconciliation reason is invalid")
    return {
        "result_uuid": result_uuid,
        "provider_operation_uuid": provider_operation_uuid,
        "lease_uuid": lease_uuid,
        "queue_status": queue_status,
        "public_status": public_status,
        "safe_error": safe_error,
        "original_url": original_url,
        "reconciliation": reconciliation,
        "manual": manual,
    }


def report_provider_result(
    session: typing.Any,
    identity: typing.Any,
    result: object,
    now: datetime.datetime | None = None,
) -> dict[str, str]:
    """Apply one idempotent terminal provider result."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    validated = _validated_result(result)
    result_uuid = validated["result_uuid"]
    provider_operation_uuid = validated["provider_operation_uuid"]
    lease_uuid = validated["lease_uuid"]
    queue_status = validated["queue_status"]
    public_status = validated["public_status"]
    safe_error = validated["safe_error"]
    canonical_hash = _sha256(result)
    existing = session.execute(
        """
        SELECT "operation_uuid", "payload_sha256"
        FROM "m_external_provider_operation_results_v1"
        WHERE "result_uuid" = %s
        """,
        (result_uuid,),
    ).fetchone()
    if existing is not None:
        if (
            existing["operation_uuid"] != provider_operation_uuid
            or existing["payload_sha256"] != canonical_hash
        ):
            return {"result_uuid": str(result_uuid), "status": "conflict"}
        return {"result_uuid": str(result_uuid), "status": "duplicate"}
    operation = session.execute(
        """
        SELECT "external_operation_uuid", "project_id", "status", "lease_uuid",
               "attempt"
        FROM "m_external_provider_operations_v1"
        WHERE "uuid" = %s AND "bridge_instance_uuid" = %s
        FOR UPDATE
        """,
        (provider_operation_uuid, identity.bridge_instance_uuid),
    ).fetchone()
    if operation is None:
        return {"result_uuid": str(result_uuid), "status": "not_found"}
    if operation["status"] != "leased" or operation["lease_uuid"] != lease_uuid:
        return {"result_uuid": str(result_uuid), "status": "stale_lease"}
    inserted = session.execute(
        """
        INSERT INTO "m_external_provider_operation_results_v1" (
            "result_uuid", "operation_uuid", "payload_sha256", "created_at"
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT ("result_uuid") DO NOTHING
        RETURNING "result_uuid"
        """,
        (result_uuid, provider_operation_uuid, canonical_hash, now),
    ).fetchone()
    if inserted is None:
        existing = session.execute(
            """
            SELECT "operation_uuid", "payload_sha256"
            FROM "m_external_provider_operation_results_v1"
            WHERE "result_uuid" = %s
            """,
            (result_uuid,),
        ).fetchone()
        if (
            existing is None
            or existing["operation_uuid"] != provider_operation_uuid
            or existing["payload_sha256"] != canonical_hash
        ):
            return {"result_uuid": str(result_uuid), "status": "conflict"}
        return {"result_uuid": str(result_uuid), "status": "duplicate"}
    session.execute(
        """
        UPDATE "m_external_provider_operations_v1"
        SET "status" = %s, "lease_uuid" = NULL, "lease_expires_at" = NULL,
            "safe_error" = %s, "completed_at" = %s, "updated_at" = %s
        WHERE "uuid" = %s
        """,
        (queue_status, safe_error, now, now, provider_operation_uuid),
    )
    manual = validated["manual"]
    reconciliation = validated["reconciliation"]
    session.execute(
        """
        UPDATE "m_external_operations_v2"
        SET "status" = %s, "attempt" = %s, "safe_error" = %s,
            "can_retry" = %s, "can_discard" = %s,
            "duplicate_risk" = %s, "retry_requires_confirmation" = %s,
            "original_url" = %s,
            "reconciliation_state" = %s,
            "reconciliation_reason" = %s,
            "reconciliation_evidence" = %s::jsonb,
            "details" = "details" || jsonb_build_object(
                'provider_result', %s::jsonb
            ),
            "attempt_history" = array_append(
                "attempt_history", %s::jsonb
            ),
            "revision" = "revision" + 1, "updated_at" = %s
        WHERE "uuid" = %s
        """,
        (
            public_status,
            operation["attempt"],
            safe_error,
            public_status == "failed",
            public_status == "failed",
            manual,
            manual,
            validated["original_url"],
            "manual_required"
            if manual
            else reconciliation.get("state", "not_required"),
            reconciliation.get("reason") if manual else None,
            _canonical_json(reconciliation.get("evidence", {})),
            _canonical_json(result),
            _canonical_json(
                {
                    "attempt": operation["attempt"],
                    "status": public_status,
                    "completed_at": _timestamp(now),
                    "safe_error": safe_error,
                }
            ),
            now,
            operation["external_operation_uuid"],
        ),
    )
    _emit_operation_event(
        session,
        operation["external_operation_uuid"],
        operation["project_id"],
        messenger_events.EXTERNAL_OPERATION_UPDATED_EVENT,
    )
    return {"result_uuid": str(result_uuid), "status": "applied"}


def report_provider_results(
    session: typing.Any,
    identity: typing.Any,
    results: object,
    now: datetime.datetime | None = None,
) -> dict[str, list[dict[str, str]]]:
    if not isinstance(results, list) or not 1 <= len(results) <= RESULT_MAX_ITEMS:
        raise ValueError("Provider result batch size is invalid")
    response = []
    for result in results:
        session.execute("SAVEPOINT provider_result_item")
        try:
            response.append(report_provider_result(session, identity, result, now=now))
        except (AttributeError, KeyError, TypeError, ValueError):
            session.execute("ROLLBACK TO SAVEPOINT provider_result_item")
            response.append(
                {
                    "result_uuid": str(
                        result.get("result_uuid", "")
                        if isinstance(result, dict)
                        else ""
                    ),
                    "status": "rejected",
                }
            )
        finally:
            session.execute("RELEASE SAVEPOINT provider_result_item")
    return {"results": response}


def apply_provider_event_batch(
    session: typing.Any,
    identity: typing.Any,
    events: object,
    apply: typing.Callable[
        [dict[str, typing.Any], typing.Any, typing.Any],
        str | sys_uuid.UUID | None,
    ],
    now: datetime.datetime | None = None,
) -> dict[str, list[dict[str, typing.Any]]]:
    """Apply an inbound batch atomically; any rejected event rolls it back."""
    started_at = time.monotonic()
    if not isinstance(events, list) or not 1 <= len(events) <= EVENT_MAX_ITEMS:
        raise ProviderBatchError("Provider event batch size is invalid")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    _bridge_capabilities(session, identity, now)
    cache_attribute = "_workspace_provider_event_batch_cache"
    missing_cache = object()
    previous_cache = getattr(session, cache_attribute, missing_cache)
    setattr(session, cache_attribute, {})
    try:
        # A timed-out request can still be committing while the bridge retries
        # an overlapping batch. Serialize all provider-event writers before
        # either request inserts its idempotency ledger rows; otherwise one
        # transaction can own a project event lock while waiting for a
        # duplicate ledger row held by the other transaction.
        project_ids = sorted(
            {
                sys_uuid.UUID(str(event["project_id"]))
                for event in events
                if not _is_quiet_backfill_event(event)
            },
            key=str,
        )
        for project_id in project_ids:
            session.execute(
                """
                SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))
                """,
                (project_id,),
            )
        requested_routes = sorted(
            {
                (
                    sys_uuid.UUID(str(event["external_account_uuid"])),
                    sys_uuid.UUID(str(event["external_chat_uuid"])),
                    sys_uuid.UUID(str(event["project_id"])),
                    _is_account_global_identity_event(event),
                )
                for event in events
            },
            key=lambda value: tuple(map(str, value)),
        )
        validation = session.execute(
            """
            WITH requested(
                account_uuid, chat_uuid, project_id, account_global
            ) AS (
                SELECT * FROM unnest(
                    %s::uuid[], %s::uuid[], %s::uuid[], %s::boolean[]
                )
            ), authorized AS (
                SELECT
                    requested.account_uuid,
                    NULL::uuid AS chat_uuid,
                    requested.project_id,
                    NULL::uuid AS owner_user_uuid,
                    NULL::uuid AS projection_stream_uuid,
                    NULL::text AS provider_chat_id,
                    NULL::text AS display_name,
                    NULL::jsonb AS source,
                    NULL::jsonb AS capabilities,
                    account.settings AS account_settings,
                    account.provider_realm_uuid
                FROM requested
                JOIN "m_external_accounts_v2" AS account
                  ON account."uuid" = requested.account_uuid
                 AND account."provider" = %s
                 AND account."settings"->>'default_project_id' =
                     requested.project_id::text
                WHERE requested.account_global
                  AND EXISTS (
                    SELECT 1
                    FROM "m_external_bridge_desired_resources_v1" AS desired
                    WHERE desired."bridge_instance_uuid" = %s
                      AND desired."provider_kind" = %s
                      AND desired."resource_type" = 'external_account'
                      AND desired."resource_uuid" = account."uuid"
                      AND desired."operation" = 'upsert'
                      AND desired."resource"#>>'{settings,default_project_id}' =
                          requested.project_id::text
                  )
                UNION ALL
                SELECT
                    requested.account_uuid,
                    requested.chat_uuid,
                    requested.project_id,
                    chat.owner_user_uuid,
                    chat.projection_stream_uuid,
                    chat.provider_chat_id,
                    chat.display_name,
                    chat.source,
                    chat.capabilities,
                    account.settings AS account_settings,
                    account.provider_realm_uuid
                FROM requested
                JOIN "m_external_accounts_v2" AS account
                  ON account."uuid" = requested.account_uuid
                 AND account."provider" = %s
                JOIN "m_external_chats_v2" AS chat
                  ON chat."uuid" = requested.chat_uuid
                 AND chat."external_account_uuid" = requested.account_uuid
                 AND chat."project_id" = requested.project_id
                 AND chat."provider" = account."provider"
                 AND chat."selected"
                 AND chat."status" IN ('syncing', 'live', 'degraded')
                 AND chat."projection_stream_uuid" IS NOT NULL
                WHERE NOT requested.account_global
                  AND EXISTS (
                    SELECT 1
                    FROM "m_external_bridge_desired_resources_v1" AS desired
                    WHERE desired."bridge_instance_uuid" = %s
                      AND desired."provider_kind" = %s
                      AND desired."resource_type" = 'external_account'
                      AND desired."resource_uuid" = account."uuid"
                      AND desired."operation" = 'upsert'
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM "m_external_bridge_desired_resources_v1" AS desired
                    WHERE desired."bridge_instance_uuid" = %s
                      AND desired."provider_kind" = %s
                      AND desired."resource_type" = 'external_chat_assignment'
                      AND desired."resource_uuid" = chat."uuid"
                      AND desired."operation" = 'upsert'
                      AND desired."resource"->>'external_account_uuid' =
                          requested.account_uuid::text
                      AND desired."resource"->>'project_id' =
                          requested.project_id::text
                      AND desired."resource"->>'selected' = 'true'
                      AND desired."resource"#>>'{workspace_projection,stream,uuid}' =
                          chat."projection_stream_uuid"::text
                  )
            )
            SELECT count(*) AS matched,
                   COALESCE(
                       jsonb_agg(jsonb_build_object(
                           'account_uuid', authorized.account_uuid,
                           'chat_uuid', authorized.chat_uuid,
                           'project_id', authorized.project_id,
                           'owner_user_uuid', authorized.owner_user_uuid,
                           'projection_stream_uuid',
                               authorized.projection_stream_uuid,
                           'provider_chat_id', authorized.provider_chat_id,
                           'display_name', authorized.display_name,
                           'source', authorized.source,
                           'capabilities', authorized.capabilities,
                           'account_settings', authorized.account_settings,
                           'provider_realm_uuid',
                               authorized.provider_realm_uuid
                       )) FILTER (WHERE authorized.chat_uuid IS NOT NULL),
                       '[]'::jsonb
                   ) AS assignments
            FROM authorized
            """,
            (
                [value[0] for value in requested_routes],
                [value[1] for value in requested_routes],
                [value[2] for value in requested_routes],
                [value[3] for value in requested_routes],
                identity.provider_kind,
                identity.bridge_instance_uuid,
                identity.provider_kind,
                identity.provider_kind,
                identity.bridge_instance_uuid,
                identity.provider_kind,
                identity.bridge_instance_uuid,
                identity.provider_kind,
            ),
        ).fetchone()
        if validation is None or int(validation["matched"]) != len(requested_routes):
            raise ValueError(
                "External account and chat are not assigned to this bridge"
            )
        provider_event_apply.prime_assignment_cache(
            session,
            identity,
            validation["assignments"] or (),
        )
        event_inputs: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        for event in events:
            event_uuid = sys_uuid.UUID(str(event["provider_event_uuid"]))
            payload_hash = _sha256(event)
            previous = event_inputs.get(event_uuid)
            if previous is not None:
                if previous["payload_hash"] != payload_hash:
                    raise ValueError(
                        "Provider event UUID was reused with different input"
                    )
                continue
            event_inputs[event_uuid] = {
                "event": event,
                "account_uuid": sys_uuid.UUID(str(event["external_account_uuid"])),
                "project_id": sys_uuid.UUID(str(event["project_id"])),
                "payload_hash": payload_hash,
            }
        inserted_rows = session.execute(
            """
            INSERT INTO "m_external_provider_events_v1" (
                "bridge_instance_uuid", "provider_event_uuid",
                "external_account_uuid", "project_id", "provider_sequence",
                "event_kind", "payload_sha256", "status"
            )
            SELECT %s, input.provider_event_uuid, input.external_account_uuid,
                   input.project_id, input.provider_sequence, input.event_kind,
                   input.payload_sha256, 'processing'
            FROM unnest(
                %s::uuid[], %s::uuid[], %s::uuid[], %s::text[],
                %s::text[], %s::text[]
            ) AS input(
                provider_event_uuid, external_account_uuid, project_id,
                provider_sequence, event_kind, payload_sha256
            )
            ON CONFLICT ("bridge_instance_uuid", "provider_event_uuid")
            DO NOTHING
            RETURNING "provider_event_uuid"
            """,
            (
                identity.bridge_instance_uuid,
                list(event_inputs),
                [value["account_uuid"] for value in event_inputs.values()],
                [value["project_id"] for value in event_inputs.values()],
                [
                    value["event"].get("provider_sequence")
                    for value in event_inputs.values()
                ],
                [value["event"]["kind"] for value in event_inputs.values()],
                [value["payload_hash"] for value in event_inputs.values()],
            ),
        ).fetchall()
        inserted_event_uuids = {
            sys_uuid.UUID(str(row["provider_event_uuid"])) for row in inserted_rows
        }
        existing_event_uuids = set(event_inputs) - inserted_event_uuids
        existing_events: dict[sys_uuid.UUID, typing.Mapping[str, typing.Any]] = {}
        if existing_event_uuids:
            existing_rows = session.execute(
                """
                SELECT "provider_event_uuid", "payload_sha256", "status",
                       "target_uuid", "safe_error"
                FROM "m_external_provider_events_v1"
                WHERE "bridge_instance_uuid" = %s
                  AND "provider_event_uuid" = ANY(%s::uuid[])
                """,
                (identity.bridge_instance_uuid, sorted(existing_event_uuids, key=str)),
            ).fetchall()
            existing_events = {
                sys_uuid.UUID(str(row["provider_event_uuid"])): row
                for row in existing_rows
            }
            if set(existing_events) != existing_event_uuids:
                raise ValueError("Provider event ledger is incomplete")
            for event_uuid, row in existing_events.items():
                if row["payload_sha256"] != event_inputs[event_uuid]["payload_hash"]:
                    raise ValueError(
                        "Provider event UUID was reused with different input"
                    )

        results_by_uuid: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        applied_event_uuids: list[sys_uuid.UUID] = []
        applied_target_uuids: list[sys_uuid.UUID | None] = []
        results = []
        for event in events:
            event_uuid = sys_uuid.UUID(str(event["provider_event_uuid"]))
            if event_uuid in results_by_uuid:
                results.append({**results_by_uuid[event_uuid], "duplicate": True})
                continue
            if event_uuid in existing_events:
                existing = existing_events[event_uuid]
                result = {
                    "provider_event_uuid": str(event_uuid),
                    "status": existing["status"],
                    "target_uuid": _uuid_string(existing["target_uuid"]),
                    "safe_error": existing["safe_error"],
                    "duplicate": True,
                }
                results_by_uuid[event_uuid] = result
                results.append(result)
                continue
            target_uuid = apply(event, session, identity)
            normalized_target_uuid = (
                None if target_uuid is None else sys_uuid.UUID(str(target_uuid))
            )
            result = {
                "provider_event_uuid": str(event_uuid),
                "status": "applied",
                "target_uuid": _uuid_string(normalized_target_uuid),
                "safe_error": None,
                "duplicate": False,
            }
            results_by_uuid[event_uuid] = result
            results.append(result)
            applied_event_uuids.append(event_uuid)
            applied_target_uuids.append(normalized_target_uuid)
        broadcast_epochs = messenger_events.flush_buffered_resource_broadcast_events(
            session
        )
        if applied_event_uuids:
            session.execute(
                """
                WITH applied(provider_event_uuid, target_uuid) AS (
                    SELECT * FROM unnest(%s::uuid[], %s::uuid[])
                )
                UPDATE "m_external_provider_events_v1" AS event
                SET "status" = 'applied', "target_uuid" = applied.target_uuid
                FROM applied
                WHERE event."bridge_instance_uuid" = %s
                  AND event."provider_event_uuid" = applied.provider_event_uuid
                """,
                (
                    applied_event_uuids,
                    applied_target_uuids,
                    identity.bridge_instance_uuid,
                ),
            )
    except (
        KeyError,
        TypeError,
        ValueError,
        ra_exceptions.ValidationErrorException,
        storage_exceptions.ConflictRecords,
        storage_exceptions.RecordNotFound,
    ) as error:
        raise ProviderBatchError(str(error)) from error
    finally:
        if previous_cache is missing_cache:
            delattr(session, cache_attribute)
        else:
            setattr(session, cache_attribute, previous_cache)
    LOG.info(
        "Applied provider event batch: events=%d broadcasts=%d duration_seconds=%.3f",
        len(events),
        len(broadcast_epochs),
        time.monotonic() - started_at,
    )
    return {"results": results}


def enqueue_provider_operation(
    session: typing.Any,
    *,
    operation_uuid: sys_uuid.UUID,
    bridge_instance_uuid: object,
    external_account_uuid: object,
    project_id: object,
    owner_user_uuid: object,
    operation_kind: str,
    target_type: str,
    target_uuid: object,
    payload: object,
) -> tuple[external_models.ExternalOperation, sys_uuid.UUID]:
    """Create the public operation and provider outbox row atomically."""
    now = datetime.datetime.now(datetime.timezone.utc)
    operation = external_models.ExternalOperation(
        uuid=operation_uuid,
        external_account_uuid=external_account_uuid,
        owner_user_uuid=owner_user_uuid,
        action=operation_kind,
        target_type=target_type,
        target_uuid=target_uuid,
        details={"payload": payload},
        status=external_models.ExternalOperationStatus.QUEUED.value,
    )
    operation.insert(session=session)
    record_uuid = sys_uuid.uuid4()
    session.execute(
        """
        INSERT INTO "m_external_provider_operations_v1" (
            "uuid", "external_operation_uuid", "bridge_instance_uuid",
            "external_account_uuid", "project_id", "operation_kind", "payload",
            "created_at", "updated_at"
        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        """,
        (
            record_uuid,
            operation_uuid,
            bridge_instance_uuid,
            external_account_uuid,
            project_id,
            operation_kind,
            _canonical_json(payload),
            now,
            now,
        ),
    )
    publish_operation_event(
        session,
        operation,
        project_id,
        messenger_events.EXTERNAL_OPERATION_CREATED_EVENT,
    )
    return operation, record_uuid


def retry_provider_operation(
    session: typing.Any,
    *,
    external_operation_uuid: object,
    next_attempt: int,
) -> typing.Any:
    """Requeue an existing provider operation in the caller transaction."""
    row = session.execute(
        """
        UPDATE "m_external_provider_operations_v1"
        SET
            "status" = 'queued',
            "attempt" = %s - 1,
            "available_at" = NOW(),
            "lease_uuid" = NULL,
            "lease_expires_at" = NULL,
            "safe_error" = NULL,
            "completed_at" = NULL,
            "updated_at" = NOW()
        WHERE "external_operation_uuid" = %s
          AND "status" IN ('failed', 'discarded')
        RETURNING "uuid", "project_id"
        """,
        (next_attempt, external_operation_uuid),
    ).fetchone()
    if row is None:
        raise ValueError("Provider operation cannot be retried from its current state")
    return row


def discard_provider_operation(
    session: typing.Any,
    *,
    external_operation_uuid: object,
) -> typing.Any:
    """Prevent a queued provider operation from being leased before deletion."""
    row = session.execute(
        """
        UPDATE "m_external_provider_operations_v1"
        SET
            "status" = 'discarded',
            "lease_uuid" = NULL,
            "lease_expires_at" = NULL,
            "completed_at" = NOW(),
            "updated_at" = NOW()
        WHERE "external_operation_uuid" = %s
          AND "status" IN ('queued', 'failed')
        RETURNING "uuid", "project_id"
        """,
        (external_operation_uuid,),
    ).fetchone()
    if row is None:
        raise ValueError(
            "Provider operation cannot be discarded from its current state"
        )
    return row


def apply_provider_event(
    session: typing.Any,
    *,
    bridge_instance_uuid: object,
    external_account_uuid: object,
    project_id: object,
    event: dict[str, typing.Any],
    apply: typing.Callable[
        [dict[str, typing.Any], typing.Any], str | sys_uuid.UUID | None
    ],
) -> dict[str, typing.Any]:
    """Apply one inbound provider event exactly once in the caller transaction."""
    event_uuid = sys_uuid.UUID(str(event["provider_event_uuid"]))
    payload_hash = _sha256(event)
    inserted = session.execute(
        """
        INSERT INTO "m_external_provider_events_v1" (
            "bridge_instance_uuid", "provider_event_uuid",
            "external_account_uuid", "project_id", "provider_sequence",
            "event_kind", "payload_sha256", "status"
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'processing')
        ON CONFLICT ("bridge_instance_uuid", "provider_event_uuid") DO NOTHING
        RETURNING "provider_event_uuid"
        """,
        (
            bridge_instance_uuid,
            event_uuid,
            external_account_uuid,
            project_id,
            event.get("provider_sequence"),
            event["kind"],
            payload_hash,
        ),
    ).fetchone()
    if inserted is None:
        existing = session.execute(
            """
            SELECT "payload_sha256", "status", "target_uuid", "safe_error"
            FROM "m_external_provider_events_v1"
            WHERE "bridge_instance_uuid" = %s AND "provider_event_uuid" = %s
            """,
            (bridge_instance_uuid, event_uuid),
        ).fetchone()
        if existing["payload_sha256"] != payload_hash:
            raise ValueError("Provider event UUID was reused with different input")
        return {
            "provider_event_uuid": str(event_uuid),
            "status": existing["status"],
            "target_uuid": _uuid_string(existing["target_uuid"]),
            "safe_error": existing["safe_error"],
            "duplicate": True,
        }
    target_uuid = apply(event, session)
    session.execute(
        """
        UPDATE "m_external_provider_events_v1"
        SET "status" = 'applied', "target_uuid" = %s
        WHERE "bridge_instance_uuid" = %s AND "provider_event_uuid" = %s
        """,
        (target_uuid, bridge_instance_uuid, event_uuid),
    )
    return {
        "provider_event_uuid": str(event_uuid),
        "status": "applied",
        "target_uuid": _uuid_string(target_uuid),
        "safe_error": None,
        "duplicate": False,
    }
