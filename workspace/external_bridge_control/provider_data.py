# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Transactional PostgreSQL data plane for external provider services."""

import datetime
import hashlib
import json
import typing
import uuid as sys_uuid

from restalchemy.common import exceptions as ra_exceptions
from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exceptions

from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import models


LEASE_MIN_SECONDS = 10
LEASE_MAX_SECONDS = 300
LEASE_MAX_ITEMS = 100
RESULT_MAX_ITEMS = 500
EVENT_MAX_ITEMS = 500
HEARTBEAT_MAX_AGE = datetime.timedelta(seconds=60)

_OPERATION_CAPABILITIES = {
    "message.create": "messenger.message.send",
    "message.update": "messenger.message.edit",
    "message.delete": "messenger.message.delete",
    "read_state.set": "messenger.message.read",
    "reaction.create": "messenger.reaction.write",
    "reaction.update": "messenger.reaction.write",
    "reaction.delete": "messenger.reaction.write",
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


class ProviderDataError(RuntimeError):
    status = 400
    error = "provider_request_invalid"


class ProviderUnavailableError(ProviderDataError):
    status = 409
    error = "provider_bridge_unavailable"


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
    """Resolve one live selected chat and its bridge in the caller transaction."""
    account = external_models.ExternalAccount.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(external_account_uuid),
            "owner_user_uuid": dm_filters.EQ(owner_user_uuid),
        },
        session=session,
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
        not account.live_ready
        or not _effective_capability_available(account.capabilities, capability_name)
        or not _effective_capability_available(chat.capabilities, capability_name)
    ):
        raise ProviderUnavailableError("External chat capability is unavailable")
    bridges = external_models.ExternalBridgeInstance.objects.get_all(
        filters={
            "provider": dm_filters.EQ(account.provider),
            "status": dm_filters.In(
                (
                    external_models.ExternalBridgeInstanceStatus.ACTIVE.value,
                    external_models.ExternalBridgeInstanceStatus.DEGRADED.value,
                )
            ),
        },
        order_by={"created_at": "desc", "uuid": "desc"},
        session=session,
        limit=1,
    )
    if not bridges:
        raise ProviderUnavailableError("External provider bridge is unavailable")
    return account, chat, bridges[0]


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
    model_and_event = {
        "stream": (
            models.WorkspaceUserStream,
            messenger_events.create_stream_updated_event,
        ),
        "topic": (
            models.WorkspaceUserTopic,
            messenger_events.create_topic_updated_event,
        ),
        "message": (
            models.WorkspaceUserMessage,
            messenger_events.create_message_updated_event,
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
            SELECT "uuid"
            FROM "m_external_provider_operations_v1"
            WHERE "bridge_instance_uuid" = %s AND "status" = 'queued'
              AND "available_at" <= %s
              AND "operation_kind" = ANY(%s::text[])
            ORDER BY "sequence"
            LIMIT %s
            FOR UPDATE SKIP LOCKED
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
    if not isinstance(events, list) or not 1 <= len(events) <= EVENT_MAX_ITEMS:
        raise ProviderBatchError("Provider event batch size is invalid")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    _bridge_capabilities(session, identity, now)
    results = []
    try:
        for event in events:
            account_uuid = sys_uuid.UUID(str(event["external_account_uuid"]))
            chat_uuid = sys_uuid.UUID(str(event["external_chat_uuid"]))
            project_id = sys_uuid.UUID(str(event["project_id"]))
            account = session.execute(
                """
                SELECT 1
                FROM "m_external_accounts_v2" AS account
                WHERE account."uuid" = %s AND account."provider" = %s
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
                      AND desired."resource_uuid" = %s
                      AND desired."operation" = 'upsert'
                      AND desired."resource"->>'external_account_uuid' = %s
                      AND desired."resource"->>'project_id' = %s
                      AND desired."resource"->>'selected' = 'true'
                  )
                """,
                (
                    account_uuid,
                    identity.provider_kind,
                    identity.bridge_instance_uuid,
                    identity.provider_kind,
                    identity.bridge_instance_uuid,
                    identity.provider_kind,
                    chat_uuid,
                    str(account_uuid),
                    str(project_id),
                ),
            ).fetchone()
            if account is None:
                raise ValueError(
                    "External account and chat are not assigned to this bridge"
                )
            results.append(
                apply_provider_event(
                    session,
                    bridge_instance_uuid=identity.bridge_instance_uuid,
                    external_account_uuid=account_uuid,
                    project_id=project_id,
                    event=event,
                    apply=lambda item, current_session: apply(
                        item,
                        current_session,
                        identity,
                    ),
                )
            )
    except (
        KeyError,
        TypeError,
        ValueError,
        ra_exceptions.ValidationErrorException,
        storage_exceptions.RecordNotFound,
    ) as error:
        raise ProviderBatchError(str(error)) from error
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
