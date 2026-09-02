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
from workspace.messenger_api.dm import read_state
from workspace.messenger_api.dm import v2_models


LEASE_MIN_SECONDS = 10
LEASE_MAX_SECONDS = 300
LEASE_MAX_ITEMS = 100
RESULT_MAX_ITEMS = 500
EVENT_MAX_ITEMS = 500
PROVIDER_READ_MAX_MESSAGES = 500
PROVIDER_READ_LEGACY_MAX_PAGES = LEASE_MAX_ITEMS
PROVIDER_READ_MAX_EMPTY_BATCHES_PER_PAGE = 4
PROVIDER_READ_PAGING_CAPABILITY = "messenger.message.read.paging"
PROVIDER_READ_PAGING_REVISION = 1
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


class ProviderReadProjectMoveConflictError(RuntimeError):
    """An in-flight provider read page cannot change project safely."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _database_now(session: typing.Any) -> datetime.datetime:
    """Use PostgreSQL as the queue clock across independently clocked VMs."""
    return session.execute(
        "SELECT statement_timestamp() AS current_time",
        (),
    ).fetchone()["current_time"]


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


def _capability_revision(capabilities: object, name: str) -> int:
    descriptor = capabilities.get(name) if isinstance(capabilities, dict) else None
    value = descriptor.get("revision") if isinstance(descriptor, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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
            and account.status != external_models.ExternalAccountStatus.BACKFILL.value
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
    payload = row["payload"]
    response_revision = (
        payload.get("_workspace_response_revision", 0)
        if isinstance(payload, dict)
        else 0
    )
    is_physical_read_page = (
        row["operation_kind"] == "read_state.set"
        and isinstance(response_revision, int)
        and not isinstance(response_revision, bool)
        and response_revision >= 2
    )
    public_payload = (
        {
            key: value
            for key, value in payload.items()
            if key != "_workspace_response_revision"
        }
        if isinstance(payload, dict)
        else payload
    )
    return {
        "provider_operation_uuid": str(row["uuid"]),
        "external_operation_uuid": str(
            row["uuid"] if is_physical_read_page else row["external_operation_uuid"]
        ),
        "lease_uuid": str(row["lease_uuid"]),
        "lease_expires_at": _timestamp(row["lease_expires_at"]),
        "external_account_uuid": str(row["external_account_uuid"]),
        "project_id": str(row["project_id"]),
        "operation_kind": row["operation_kind"],
        "required_capability": required_capability,
        "attempt": row["attempt"],
        "payload": public_payload,
    }


def _provider_causal_lane(payload: object, causal_lane: object | None) -> object | None:
    if causal_lane is not None:
        return sys_uuid.UUID(str(causal_lane))
    if isinstance(payload, dict) and payload.get("stream_uuid") is not None:
        return sys_uuid.UUID(str(payload["stream_uuid"]))
    return None


def _lock_provider_causal_lane(
    session: typing.Any,
    *,
    bridge_instance_uuid: object,
    external_account_uuid: object,
    causal_lane: object,
) -> None:
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (
            "provider-causal-lane-v1:"
            f"{bridge_instance_uuid}:{external_account_uuid}:{causal_lane}",
        ),
    )


def lock_provider_causal_lane(
    session: typing.Any,
    *,
    bridge_instance_uuid: object,
    external_account_uuid: object,
    causal_lane: object,
) -> None:
    """Expose the provider lane lock to callers that lock before mutation."""
    _lock_provider_causal_lane(
        session,
        bridge_instance_uuid=bridge_instance_uuid,
        external_account_uuid=external_account_uuid,
        causal_lane=causal_lane,
    )


def rebind_provider_read_lane_project(
    session: typing.Any,
    *,
    bridge_instance_uuid: object,
    external_account_uuid: object,
    causal_lane: object,
    old_project_id: object,
    new_project_id: object,
) -> None:
    """Move idle provider read work before its projection changes project."""
    read_state.lock_read_state_schema_shared(session)
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"provider-read-materialize-v1:{bridge_instance_uuid}",),
    )
    _lock_provider_causal_lane(
        session,
        bridge_instance_uuid=bridge_instance_uuid,
        external_account_uuid=external_account_uuid,
        causal_lane=causal_lane,
    )
    pages = session.execute(
        """
        SELECT page.uuid, page.status, page.attempt
        FROM m_external_provider_operations_v1 AS page
        JOIN m_external_operations_v2 AS public_operation
          ON public_operation.uuid = page.external_operation_uuid
         AND public_operation.status IN (
                'queued', 'running', 'failed',
                'manual_reconciliation_required'
         )
        WHERE page.bridge_instance_uuid = %s
          AND page.external_account_uuid = %s
          AND page.causal_lane = %s
          AND page.project_id = %s
          AND page.operation_kind = 'read_state.set'
        ORDER BY page.sequence
        FOR UPDATE OF page
        """,
        (
            bridge_instance_uuid,
            external_account_uuid,
            causal_lane,
            old_project_id,
        ),
    ).fetchall()
    if any(
        page["status"] == "leased"
        or (page["status"] == "queued" and page["attempt"] > 0)
        for page in pages
    ):
        raise ProviderReadProjectMoveConflictError()
    session.execute(
        """
        UPDATE m_external_provider_read_snapshots_v1 AS snapshot
        SET project_id = %s, updated_at = NOW()
        FROM m_external_operations_v2 AS public_operation
        WHERE public_operation.uuid = snapshot.external_operation_uuid
          AND public_operation.status IN (
                'queued', 'running', 'failed',
                'manual_reconciliation_required'
          )
          AND snapshot.bridge_instance_uuid = %s
          AND snapshot.external_account_uuid = %s
          AND snapshot.causal_lane = %s
          AND snapshot.project_id = %s
        """,
        (
            new_project_id,
            bridge_instance_uuid,
            external_account_uuid,
            causal_lane,
            old_project_id,
        ),
    )
    session.execute(
        """
        UPDATE m_external_provider_operations_v1 AS page
        SET project_id = %s, updated_at = NOW()
        FROM m_external_operations_v2 AS public_operation
        WHERE public_operation.uuid = page.external_operation_uuid
          AND public_operation.status IN (
                'queued', 'running', 'failed',
                'manual_reconciliation_required'
          )
          AND page.bridge_instance_uuid = %s
          AND page.external_account_uuid = %s
          AND page.causal_lane = %s
          AND page.project_id = %s
          AND page.operation_kind = 'read_state.set'
        """,
        (
            new_project_id,
            bridge_instance_uuid,
            external_account_uuid,
            causal_lane,
            old_project_id,
        ),
    )


def _provider_message_ids_for_read_page(
    session: typing.Any,
    *,
    external_account_uuid: object,
    project_id: object,
    message_uuids: typing.Sequence[object],
) -> list[str] | None:
    """Resolve an exact read page to immutable Zulip message identifiers."""
    if not message_uuids:
        return None
    rows = session.execute(
        """
        WITH candidate AS (
            SELECT message_uuid, ordinal_position
            FROM unnest(%s::uuid[]) WITH ORDINALITY
                 AS value(message_uuid, ordinal_position)
        ), target_account AS (
            SELECT provider_realm_uuid
            FROM m_external_accounts_v2
            WHERE uuid = %s AND provider = 'zulip'
        )
        SELECT candidate.message_uuid,
               COALESCE(direct.provider_external_id,
                        legacy.provider_external_id) AS provider_message_id
        FROM candidate
        CROSS JOIN target_account
        LEFT JOIN m_workspace_messages AS direct
          ON direct.project_id = %s
         AND direct.uuid = candidate.message_uuid
         AND direct.provider_external_id IS NOT NULL
         AND EXISTS (
                SELECT 1
                FROM m_external_accounts_v2 AS source_account
                WHERE source_account.uuid = direct.external_account_uuid
                  AND source_account.provider = 'zulip'
                  AND source_account.provider_realm_uuid =
                      target_account.provider_realm_uuid
         )
        LEFT JOIN messenger_message_placements AS placement
          ON placement.project_id = %s
         AND placement.uuid = candidate.message_uuid
        LEFT JOIN m_workspace_messages AS legacy
          ON legacy.project_id = placement.project_id
         AND legacy.uuid = placement.legacy_public_uuid
         AND legacy.provider_external_id IS NOT NULL
         AND EXISTS (
                SELECT 1
                FROM m_external_accounts_v2 AS source_account
                WHERE source_account.uuid = legacy.external_account_uuid
                  AND source_account.provider = 'zulip'
                  AND source_account.provider_realm_uuid =
                      target_account.provider_realm_uuid
         )
        ORDER BY candidate.ordinal_position
        """,
        (list(message_uuids), external_account_uuid, project_id, project_id),
    ).fetchall()
    if len(rows) != len(message_uuids):
        return None
    provider_ids = [row["provider_message_id"] for row in rows]
    if any(
        value is None
        or not str(value).isdecimal()
        or (len(str(value)) > 1 and str(value).startswith("0"))
        for value in provider_ids
    ):
        return None
    return [str(value) for value in provider_ids]


def _delivered_provider_read_page_bindings(
    session: typing.Any,
    *,
    external_account_uuid: object,
    project_id: object,
    message_uuids: typing.Sequence[object],
) -> list[tuple[object, str]] | None:
    """Bind only messages that were delivered to the target Zulip account."""
    if not message_uuids:
        return []
    rows = session.execute(
        """
        WITH candidate AS (
            SELECT message_uuid, ordinal_position
            FROM unnest(%s::uuid[]) WITH ORDINALITY
                 AS value(message_uuid, ordinal_position)
        ), target_account AS (
            SELECT provider_realm_uuid, owner_user_uuid
            FROM m_external_accounts_v2
            WHERE uuid = %s AND provider = 'zulip'
        ), target_delivery AS (
            SELECT EXISTS (
                SELECT 1
                FROM m_external_provider_events_v1 AS event
                WHERE event.project_id = %s
                  AND event.external_account_uuid = %s
                  AND event.status = 'applied'
                  AND event.event_kind = 'message.upsert'
                  AND event.target_uuid IS NOT NULL
            ) AS available
        ), resolved AS (
            SELECT candidate.message_uuid, candidate.ordinal_position,
                   COALESCE(direct.uuid, legacy.uuid) AS legacy_message_uuid,
                   COALESCE(direct.stream_uuid,
                            legacy.stream_uuid) AS stream_uuid,
                   COALESCE(direct.provider_external_id,
                            legacy.provider_external_id) AS provider_message_id,
                   COALESCE(direct.source_name,
                            legacy.source_name) AS source_name,
                   COALESCE(direct.external_account_uuid,
                            legacy.external_account_uuid)
                       AS source_external_account_uuid,
                   target_account.owner_user_uuid
            FROM candidate
            CROSS JOIN target_account
            LEFT JOIN m_workspace_messages AS direct
              ON direct.project_id = %s
             AND direct.uuid = candidate.message_uuid
             AND direct.provider_external_id IS NOT NULL
             AND EXISTS (
                    SELECT 1
                    FROM m_external_accounts_v2 AS source_account
                    WHERE source_account.uuid = direct.external_account_uuid
                      AND source_account.provider = 'zulip'
                      AND source_account.provider_realm_uuid =
                          target_account.provider_realm_uuid
             )
            LEFT JOIN messenger_message_placements AS placement
              ON placement.project_id = %s
             AND placement.uuid = candidate.message_uuid
            LEFT JOIN m_workspace_messages AS legacy
              ON legacy.project_id = placement.project_id
             AND legacy.uuid = placement.legacy_public_uuid
             AND legacy.provider_external_id IS NOT NULL
             AND EXISTS (
                    SELECT 1
                    FROM m_external_accounts_v2 AS source_account
                    WHERE source_account.uuid = legacy.external_account_uuid
                      AND source_account.provider = 'zulip'
                      AND source_account.provider_realm_uuid =
                          target_account.provider_realm_uuid
             )
        )
        SELECT resolved.message_uuid, resolved.provider_message_id,
               target_delivery.available,
               EXISTS (
                SELECT 1
                FROM m_external_provider_events_v1 AS event
                WHERE event.project_id = %s
                  AND event.external_account_uuid = %s
                  AND event.status = 'applied'
                  AND event.event_kind = 'message.upsert'
                  AND event.target_uuid = resolved.legacy_message_uuid
               ) OR (
                resolved.source_name = 'native'
                AND resolved.source_external_account_uuid = %s
               ) OR EXISTS (
                SELECT 1
                FROM m_workspace_stream_bindings AS binding
                JOIN m_confirmed_external_stream_access AS access
                  ON access.project_id = binding.project_id
                 AND access.stream_uuid = binding.stream_uuid
                 AND access.user_uuid = binding.user_uuid
                WHERE binding.project_id = %s
                  AND binding.stream_uuid = resolved.stream_uuid
                  AND binding.user_uuid = resolved.owner_user_uuid
               ) AS delivered
        FROM resolved
        CROSS JOIN target_delivery
        ORDER BY resolved.ordinal_position
        """,
        (
            list(message_uuids),
            external_account_uuid,
            project_id,
            external_account_uuid,
            project_id,
            project_id,
            project_id,
            external_account_uuid,
            external_account_uuid,
            project_id,
        ),
    ).fetchall()
    if rows and not rows[0]["available"]:
        return None
    return [
        (row["message_uuid"], str(row["provider_message_id"]))
        for row in rows
        if row["delivered"]
        and row["provider_message_id"] is not None
        and str(row["provider_message_id"]).isdecimal()
        and not (
            len(str(row["provider_message_id"])) > 1
            and str(row["provider_message_id"]).startswith("0")
        )
    ]


def _materialize_provider_read_pages(
    session: typing.Any,
    *,
    bridge_instance_uuid: object,
    limit: int,
    now: datetime.datetime,
) -> None:
    """Materialize only the read pages that one lease can expose."""
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"provider-read-materialize-v1:{bridge_instance_uuid}",),
    )
    outstanding = session.execute(
        """
        SELECT COUNT(*) AS page_count
        FROM m_external_provider_operations_v1 AS page
        JOIN m_external_provider_read_snapshots_v1 AS snapshot
          ON snapshot.external_operation_uuid = page.external_operation_uuid
        WHERE page.bridge_instance_uuid = %s
          AND page.operation_kind = 'read_state.set'
          AND page.status IN ('queued', 'leased')
        """,
        (bridge_instance_uuid,),
    ).fetchone()["page_count"]
    available_pages = max(0, limit - outstanding)
    if not available_pages:
        return
    candidate_batches_remaining = (
        available_pages * PROVIDER_READ_MAX_EMPTY_BATCHES_PER_PAGE
    )
    snapshot_limit = candidate_batches_remaining
    snapshots = session.execute(
        """
        SELECT snapshot.*
        FROM m_external_provider_read_snapshots_v1 AS snapshot
        JOIN m_external_operations_v2 AS public_operation
          ON public_operation.uuid = snapshot.external_operation_uuid
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = snapshot.external_account_uuid
        JOIN m_external_provider_policies_v1 AS policy
          ON policy.provider = account.provider
         AND policy.enabled = TRUE
         AND policy.emergency_suspended = FALSE
        WHERE snapshot.bridge_instance_uuid = %s
          AND snapshot.exhausted = FALSE
          AND public_operation.status IN ('queued', 'running')
          AND NOT EXISTS (
                SELECT 1
                FROM m_external_provider_operations_v1 AS failed_page
                WHERE failed_page.external_operation_uuid =
                        snapshot.external_operation_uuid
                  AND failed_page.status = 'failed'
          )
          AND NOT EXISTS (
                SELECT 1
                FROM m_external_provider_read_snapshots_v1 AS earlier
                WHERE earlier.external_account_uuid =
                        snapshot.external_account_uuid
                  AND earlier.causal_lane = snapshot.causal_lane
                  AND earlier.queue_sequence < snapshot.queue_sequence
          )
        ORDER BY snapshot.updated_at, snapshot.queue_sequence,
                 snapshot.external_operation_uuid
        LIMIT %s
        FOR SHARE OF policy
        FOR UPDATE OF snapshot SKIP LOCKED
        """,
        (bridge_instance_uuid, snapshot_limit),
    ).fetchall()
    active_snapshot_uuids = {
        snapshot["external_operation_uuid"] for snapshot in snapshots
    }
    while available_pages and active_snapshot_uuids:
        made_progress = False
        for snapshot in snapshots:
            if not candidate_batches_remaining:
                break
            operation_uuid = snapshot["external_operation_uuid"]
            if operation_uuid not in active_snapshot_uuids:
                continue
            candidate_pack = session.execute(
                """
                SELECT pack_number, candidate_count, cursor_position
                FROM m_external_provider_read_candidate_packs_v1
                WHERE external_operation_uuid = %s
                ORDER BY pack_number
                LIMIT 1
                """,
                (operation_uuid,),
            ).fetchone()
            candidates = []
            consumed_candidates = False
            if candidate_pack is not None:
                candidates = session.execute(
                    """
                    SELECT candidate.message_uuid,
                           pack.cursor_position + candidate.ordinal_position
                               AS position
                    FROM m_external_provider_read_candidate_packs_v1 AS pack
                    CROSS JOIN LATERAL unnest(
                        pack.candidate_uuids[
                            pack.cursor_position + 1:
                            LEAST(
                                pack.cursor_position + %s,
                                pack.candidate_count
                            )
                        ]
                    ) WITH ORDINALITY AS candidate(
                        message_uuid, ordinal_position
                    )
                    WHERE pack.external_operation_uuid = %s
                      AND pack.pack_number = %s
                    ORDER BY candidate.ordinal_position
                    """,
                    (
                        PROVIDER_READ_MAX_MESSAGES,
                        operation_uuid,
                        candidate_pack["pack_number"],
                    ),
                ).fetchall()
                if not candidates:
                    raise RuntimeError("Provider read candidate pack has no remainder")
                next_cursor = candidates[-1]["position"]
                if next_cursor == candidate_pack["candidate_count"]:
                    session.execute(
                        """
                        DELETE FROM m_external_provider_read_candidate_packs_v1
                        WHERE external_operation_uuid = %s AND pack_number = %s
                        """,
                        (operation_uuid, candidate_pack["pack_number"]),
                    )
                else:
                    session.execute(
                        """
                        UPDATE m_external_provider_read_candidate_packs_v1
                        SET cursor_position = %s
                        WHERE external_operation_uuid = %s AND pack_number = %s
                        """,
                        (next_cursor, operation_uuid, candidate_pack["pack_number"]),
                    )
                consumed_candidates = True
            else:
                candidate_chunk = session.execute(
                    """
                    SELECT chunk_number
                    FROM m_external_provider_read_candidate_chunks_v1
                    WHERE external_operation_uuid = %s
                    ORDER BY chunk_number
                    LIMIT 1
                    """,
                    (operation_uuid,),
                ).fetchone()
                if candidate_chunk is not None:
                    candidates = session.execute(
                        f"""
                        WITH selected AS MATERIALIZED (
                            SELECT chunk.chunk_number, bit_offset
                            FROM m_external_provider_read_candidate_chunks_v1
                                AS chunk
                            CROSS JOIN LATERAL generate_series(
                                0, {read_state.READ_CHUNK_BITS - 1}
                            ) AS bit_offset
                            WHERE chunk.external_operation_uuid = %s
                              AND get_bit(chunk.candidate_bits, bit_offset) = 1
                            ORDER BY chunk.chunk_number, bit_offset
                            LIMIT %s
                        ), clear_masks AS (
                            SELECT
                                chunk_number,
                                bit_or(
                                    set_bit(
                                        B'0'::bit({read_state.READ_CHUNK_BITS}),
                                        bit_offset,
                                        1
                                    )
                                ) AS clear_bits
                            FROM selected
                            GROUP BY chunk_number
                        ), remaining AS MATERIALIZED (
                            SELECT
                                chunk.chunk_number,
                                chunk.candidate_bits & ~clear_mask.clear_bits
                                    AS candidate_bits
                            FROM m_external_provider_read_candidate_chunks_v1
                                AS chunk
                            JOIN clear_masks AS clear_mask
                              ON clear_mask.chunk_number = chunk.chunk_number
                            WHERE chunk.external_operation_uuid = %s
                        ), deleted AS (
                            DELETE FROM
                                m_external_provider_read_candidate_chunks_v1
                                AS chunk
                            USING remaining
                            WHERE chunk.external_operation_uuid = %s
                              AND chunk.chunk_number = remaining.chunk_number
                              AND bit_count(remaining.candidate_bits) = 0
                            RETURNING chunk.chunk_number
                        ), cleared AS (
                            UPDATE m_external_provider_read_candidate_chunks_v1
                                AS chunk
                            SET candidate_bits = remaining.candidate_bits
                            FROM remaining
                            WHERE chunk.external_operation_uuid = %s
                              AND chunk.chunk_number = remaining.chunk_number
                              AND bit_count(remaining.candidate_bits) > 0
                            RETURNING chunk.chunk_number
                        )
                        SELECT message.uuid AS message_uuid
                        FROM selected
                        JOIN m_workspace_messages AS message
                          ON message.ingest_sequence =
                                selected.chunk_number
                                    * {read_state.READ_CHUNK_BITS}
                                + selected.bit_offset
                        ORDER BY selected.chunk_number, selected.bit_offset
                        """,
                        (
                            operation_uuid,
                            PROVIDER_READ_MAX_MESSAGES,
                            operation_uuid,
                            operation_uuid,
                            operation_uuid,
                        ),
                    ).fetchall()
                    consumed_candidates = True
            if consumed_candidates:
                candidate_batches_remaining -= 1
            exhausted = session.execute(
                """
                SELECT NOT EXISTS (
                    SELECT 1
                    FROM m_external_provider_read_candidate_packs_v1
                    WHERE external_operation_uuid = %s
                    UNION ALL
                    SELECT 1
                    FROM m_external_provider_read_candidate_chunks_v1
                    WHERE external_operation_uuid = %s
                ) AS exhausted
                """,
                (operation_uuid, operation_uuid),
            ).fetchone()["exhausted"]
            session.execute(
                """
                UPDATE m_external_provider_read_snapshots_v1
                SET exhausted = %s, updated_at = %s
                WHERE external_operation_uuid = %s
                """,
                (exhausted, now, operation_uuid),
            )
            if candidates:
                candidate_message_uuids = [
                    candidate["message_uuid"] for candidate in candidates
                ]
                delivered_bindings = _delivered_provider_read_page_bindings(
                    session,
                    external_account_uuid=snapshot["external_account_uuid"],
                    project_id=snapshot["project_id"],
                    message_uuids=candidate_message_uuids,
                )
            else:
                candidate_message_uuids = []
                delivered_bindings = []
            if delivered_bindings is None:
                message_uuids = candidate_message_uuids
                provider_message_ids = _provider_message_ids_for_read_page(
                    session,
                    external_account_uuid=snapshot["external_account_uuid"],
                    project_id=snapshot["project_id"],
                    message_uuids=message_uuids,
                )
            else:
                message_uuids = [binding[0] for binding in delivered_bindings]
                provider_message_ids = [binding[1] for binding in delivered_bindings]
            if message_uuids:
                _insert_provider_operation(
                    session,
                    external_operation_uuid=operation_uuid,
                    bridge_instance_uuid=snapshot["bridge_instance_uuid"],
                    external_account_uuid=snapshot["external_account_uuid"],
                    project_id=snapshot["project_id"],
                    operation_kind="read_state.set",
                    causal_lane=snapshot["causal_lane"],
                    payload={
                        **snapshot["payload"],
                        "_workspace_response_revision": 2,
                        "message_uuids": [str(value) for value in message_uuids],
                        **(
                            {}
                            if provider_message_ids is None
                            else {"provider_message_ids": provider_message_ids}
                        ),
                    },
                    now=now,
                )
                available_pages -= 1
            elif exhausted:
                terminal = session.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE status IN ('queued', 'leased')
                        ) AS nonterminal_count,
                        COALESCE(MAX(attempt), 0) AS attempt,
                        (array_agg(terminal_result ORDER BY sequence DESC)
                            FILTER (WHERE status = 'succeeded'))[1]
                            AS terminal_result
                    FROM m_external_provider_operations_v1
                    WHERE external_operation_uuid = %s
                    """,
                    (operation_uuid,),
                ).fetchone()
                if not terminal["nonterminal_count"]:
                    terminal_result = terminal["terminal_result"] or {
                        "status": "succeeded",
                        "safe_error": None,
                    }
                    completed = session.execute(
                        """
                        UPDATE m_external_operations_v2
                        SET status = 'succeeded', attempt = %s,
                            safe_error = NULL, can_retry = FALSE,
                            can_discard = FALSE,
                            details = details || jsonb_build_object(
                                'provider_result', %s::jsonb
                            ),
                            attempt_history = array_append(
                                attempt_history, %s::jsonb
                            ),
                            revision = revision + 1, updated_at = %s
                        WHERE uuid = %s AND status IN ('queued', 'running')
                        RETURNING uuid
                        """,
                        (
                            terminal["attempt"],
                            _canonical_json(terminal_result),
                            _canonical_json(
                                {
                                    "attempt": terminal["attempt"],
                                    "status": "succeeded",
                                    "completed_at": _timestamp(now),
                                    "safe_error": None,
                                }
                            ),
                            now,
                            operation_uuid,
                        ),
                    ).fetchone()
                    if completed is not None:
                        session.execute(
                            """
                            DELETE FROM m_external_provider_read_snapshots_v1
                            WHERE external_operation_uuid = %s
                            """,
                            (operation_uuid,),
                        )
                        _emit_operation_event(
                            session,
                            operation_uuid,
                            snapshot["project_id"],
                            messenger_events.EXTERNAL_OPERATION_UPDATED_EVENT,
                        )
            made_progress = made_progress or consumed_candidates
            if exhausted:
                active_snapshot_uuids.remove(operation_uuid)
            if not available_pages:
                break
        if not made_progress:
            break


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
    if target_type in {"stream", "topic"} and read_state.uses_compact_state(
        session, project_id
    ):
        if target_type == "stream":
            stream_uuid = target_uuid
        else:
            topic = session.execute(
                """
                SELECT stream_uuid
                FROM m_workspace_stream_topics
                WHERE project_id = %s AND uuid = %s
                """,
                (project_id, target_uuid),
            ).fetchone()
            if topic is None:
                return
            stream_uuid = topic["stream_uuid"]
        project_uuid = sys_uuid.UUID(str(project_id))
        canonical_stream_uuid = sys_uuid.UUID(str(stream_uuid))
        recipients = models.get_stream_recipients(
            project_uuid,
            canonical_stream_uuid,
            session=session,
        )
        create_event: typing.Callable[..., typing.Any]
        if target_type == "stream":
            resources = messenger_helpers.get_compact_workspace_user_stream_snapshots(
                project_uuid,
                canonical_stream_uuid,
                recipients,
                session=session,
            )
            create_event = messenger_events.create_stream_updated_events
        else:
            resources = messenger_helpers.get_compact_workspace_user_topic_snapshots(
                project_uuid,
                target_uuid,
                recipients,
                session=session,
            )
            create_event = messenger_events.create_topic_updated_events
        create_event(project_id, resources, session=session, compact=True)
        return
    model_and_event = {
        "stream": (
            models.WorkspaceUserStream,
            messenger_events.create_stream_updated_events,
        ),
        "topic": (
            models.WorkspaceUserTopic,
            messenger_events.create_topic_updated_events,
        ),
    }.get(target_type)
    if model_and_event is None:
        return
    model, create_event = model_and_event
    resources = model.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "uuid": dm_filters.EQ(target_uuid),
        },
        session=session,
    )
    create_event(project_id, resources, session=session, compact=True)


def sync_operation_target_delivery(
    session: typing.Any,
    operation: external_models.ExternalOperation,
    project_id: object,
    *,
    _event_order_locked: bool = False,
) -> None:
    """Project one public operation status onto its canonical target."""
    # A direct caller may not have entered the normal messenger event path.
    # Fence downgrade and serialize the target snapshot before it is prepared.
    if not _event_order_locked:
        read_state.lock_projects(session, (project_id,))
    target = {
        "stream": ("m_workspace_streams", "uuid"),
        "topic": ("m_workspace_stream_topics", "uuid"),
        "message": ("m_workspace_messages", "uuid"),
    }.get(operation.target_type)
    if target is None or operation.target_uuid is None:
        return
    delivery = _operation_delivery(operation)
    if operation.target_type == "message":
        canonical = session.execute(
            """
            SELECT placement.message_uuid,
                   (
                       SELECT count(*)
                       FROM messenger_message_placements AS sibling
                       WHERE sibling.project_id = placement.project_id
                         AND sibling.message_uuid = placement.message_uuid
                   ) AS placement_count
            FROM messenger_message_placements AS placement
            WHERE placement.project_id = %s
              AND (
                  placement.legacy_public_uuid = %s
                  OR placement.uuid = %s
              )
            ORDER BY (placement.legacy_public_uuid = %s) DESC, placement.uuid
            LIMIT 1
            """,
            (
                project_id,
                operation.target_uuid,
                operation.target_uuid,
                operation.target_uuid,
            ),
        ).fetchone()
        if canonical is not None and canonical["placement_count"] > 1:
            changed = session.execute(
                """
                UPDATE messenger_messages
                SET delivery = %s::jsonb
                WHERE project_id = %s AND uuid = %s
                  AND delivery IS DISTINCT FROM %s::jsonb
                RETURNING uuid
                """,
                (
                    _canonical_json(delivery),
                    project_id,
                    canonical["message_uuid"],
                    _canonical_json(delivery),
                ),
            ).fetchone()
            if changed is not None:
                resources = v2_models.WorkspaceUserMessage.objects.get_all(
                    filters={
                        "project_id": dm_filters.EQ(project_id),
                        "canonical_message_uuid": dm_filters.EQ(
                            canonical["message_uuid"]
                        ),
                    },
                    order_by={"uuid": "asc", "user_uuid": "asc"},
                    session=session,
                )
                messenger_events.create_message_updated_events(
                    project_id,
                    resources,
                    session=session,
                    compact=True,
                )
            return
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
    # Event insertion takes the project advisory lock. Keep the global lock
    # order schema -> project so a concurrent downgrade cannot deadlock here.
    read_state.lock_read_state_schema_shared(session)
    messenger_events.create_external_resource_event(
        project_id,
        operation.owner_user_uuid,
        operation,
        event_kind,
        hidden_fields=("owner_user_uuid",),
        session=session,
    )
    sync_operation_target_delivery(
        session,
        operation,
        project_id,
        _event_order_locked=True,
    )


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
    uses_database_clock = now is None
    now = now if now is not None else _database_now(session)
    request_uuid = sys_uuid.UUID(str(request_uuid))
    limit = int(limit)
    lease_seconds = int(lease_seconds)
    if not 1 <= limit <= LEASE_MAX_ITEMS:
        raise ValueError("Lease limit is outside the supported range")
    if not LEASE_MIN_SECONDS <= lease_seconds <= LEASE_MAX_SECONDS:
        raise ValueError("Lease duration is outside the supported range")
    read_state.lock_read_state_schema_shared(session)
    capabilities = _bridge_capabilities(session, identity, now)
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"provider-read-materialize-v1:{identity.bridge_instance_uuid}",),
    )
    if uses_database_clock:
        now = _database_now(session)
    capabilities = _bridge_capabilities(session, identity, now)
    provider_read_revision = _capability_revision(
        capabilities,
        PROVIDER_READ_PAGING_CAPABILITY,
    )
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
    supports_provider_read_paging = (
        provider_read_revision >= PROVIDER_READ_PAGING_REVISION
    )
    if "read_state.set" in allowed_kinds and supports_provider_read_paging:
        _materialize_provider_read_pages(
            session,
            bridge_instance_uuid=identity.bridge_instance_uuid,
            limit=limit,
            now=now,
        )
    session.execute(
        """
        SELECT
            set_config(
                'workspace.provider_read_snapshot_lease_v2',
                %s,
                TRUE
            )
        """,
        ("on" if supports_provider_read_paging else "off",),
    )
    rows = session.execute(
        """
        WITH bridge_capabilities AS MATERIALIZED (
            SELECT CASE
                     WHEN jsonb_typeof(
                        bridge."capabilities"
                            ->'messenger.message.read.paging'->'revision'
                     ) = 'number'
                     THEN (
                         bridge."capabilities"
                             ->'messenger.message.read.paging'->>'revision'
                     )::integer >= %s
                     ELSE FALSE
                   END AS provider_read_paging
            FROM "m_external_bridge_instances_v2" AS bridge
            WHERE bridge."uuid" = %s
        ), candidates AS (
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
              AND (
                    operation."operation_kind" <> 'read_state.set'
                    OR COALESCE(
                        (SELECT provider_read_paging FROM bridge_capabilities),
                        FALSE
                    )
                    OR NOT EXISTS (
                        SELECT 1
                        FROM m_external_provider_read_snapshots_v1 AS page_snapshot
                        WHERE page_snapshot.external_operation_uuid =
                                operation.external_operation_uuid
                    )
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM m_external_provider_read_snapshots_v1 AS barrier
                    WHERE barrier.bridge_instance_uuid =
                            operation.bridge_instance_uuid
                      AND barrier.external_account_uuid =
                            operation.external_account_uuid
                      AND barrier.queue_sequence < COALESCE(
                            (
                                SELECT page_snapshot.queue_sequence
                                FROM m_external_provider_read_snapshots_v1
                                    AS page_snapshot
                                WHERE page_snapshot.external_operation_uuid =
                                        operation.external_operation_uuid
                            ),
                            operation.sequence
                      )
                      AND (
                            -- Fail closed for rows written by an old process
                            -- during a rolling migration.
                            operation.causal_lane IS NULL
                            OR barrier.causal_lane = operation.causal_lane
                      )
                      AND barrier.external_operation_uuid <>
                            operation.external_operation_uuid
              )
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
            PROVIDER_READ_PAGING_REVISION,
            identity.bridge_instance_uuid,
            identity.bridge_instance_uuid,
            now,
            list(allowed_kinds),
            limit,
            request_uuid,
            now + datetime.timedelta(seconds=lease_seconds),
            now,
        ),
    ).fetchall()
    # UPDATE ... RETURNING does not preserve the ORDER BY of the candidate
    # CTE. Keep the lease response in causal queue order just like replaying an
    # existing request above; otherwise concurrently materialized read pages
    # can be delivered out of order after PostgreSQL changes its update plan.
    rows = sorted(rows, key=lambda row: row["sequence"])
    if rows:
        newly_running = session.execute(
            """
            UPDATE "m_external_operations_v2" AS public_operation
            SET "status" = 'running', "attempt" = provider_operation."attempt",
                "can_retry" = FALSE, "can_discard" = FALSE,
                "revision" = public_operation."revision" + 1,
                "updated_at" = %s
            FROM "m_external_provider_operations_v1" AS provider_operation
            WHERE public_operation."uuid" = provider_operation."external_operation_uuid"
              AND provider_operation."lease_uuid" = %s
              AND public_operation."status" <> 'running'
            RETURNING public_operation."uuid"
            """,
            (now, request_uuid),
        ).fetchall()
        newly_running_uuids = {row["uuid"] for row in newly_running}
        emitted_operation_uuids = set()
        for row in rows:
            if (
                row["external_operation_uuid"] not in newly_running_uuids
                or row["external_operation_uuid"] in emitted_operation_uuids
            ):
                continue
            emitted_operation_uuids.add(row["external_operation_uuid"])
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
    provider_entity_id = result.get("provider_entity_id")
    if provider_entity_id is not None and (
        not isinstance(provider_entity_id, str)
        or not provider_entity_id
        or len(provider_entity_id) > 2048
    ):
        raise ValueError("Provider entity identifier is invalid")
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
        "provider_entity_id": provider_entity_id,
        "reconciliation": reconciliation,
        "manual": manual,
    }


def _bind_native_message_provider_identity(
    session: typing.Any,
    identity: typing.Any,
    operation: typing.Mapping[str, typing.Any],
    provider_entity_id: str | None,
) -> None:
    """Bind a committed native send before its provider echo can be imported.

    Provider Data v1 uses the operation result as the only place where a
    provider-assigned message ID becomes available.  Persist it on the native
    target in the same transaction as the terminal result so every account in
    the same provider realm resolves a later echo to that one message.
    """
    if (
        operation["operation_kind"] != "message.create"
        or identity.provider_kind != "zulip"
    ):
        return
    if provider_entity_id is None:
        raise ValueError(
            "Successful Zulip message result requires a provider identifier"
        )
    if (
        not provider_entity_id.isdecimal()
        or len(provider_entity_id) > 32
        or (len(provider_entity_id) > 1 and provider_entity_id.startswith("0"))
    ):
        raise ValueError("Zulip provider message identifier is invalid")
    public_operation = session.execute(
        """
        SELECT "target_type", "target_uuid", "external_account_uuid"
        FROM "m_external_operations_v2"
        WHERE "uuid" = %s
        FOR UPDATE
        """,
        (operation["external_operation_uuid"],),
    ).fetchone()
    if (
        public_operation is None
        or public_operation["target_type"] != "message"
        or public_operation["target_uuid"] is None
        or public_operation["external_account_uuid"] is None
    ):
        return
    account = session.execute(
        """
        SELECT "provider_realm_uuid"
        FROM "m_external_accounts_v2"
        WHERE "uuid" = %s AND "provider" = 'zulip'
        FOR SHARE
        """,
        (public_operation["external_account_uuid"],),
    ).fetchone()
    if account is None or account["provider_realm_uuid"] is None:
        return
    realm_uuid = account["provider_realm_uuid"]
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"provider-message-identity-v1:{realm_uuid}:{provider_entity_id}",),
    )
    message = session.execute(
        """
        SELECT "source_name", "provider_uuid", "external_account_uuid",
               "provider_external_id"
        FROM "m_workspace_messages"
        WHERE "project_id" = %s AND "uuid" = %s
        FOR UPDATE
        """,
        (operation["project_id"], public_operation["target_uuid"]),
    ).fetchone()
    if message is None:
        # The native message may have been deleted while its provider request
        # was in flight.  The result remains terminal, but must not create a
        # new local projection for the now-deleted target.
        return
    if message["source_name"] != "native":
        raise ValueError("Provider message result target is not native")
    if _reconcile_native_message_provider_echo(
        session,
        operation["project_id"],
        public_operation["target_uuid"],
        realm_uuid,
        provider_entity_id,
        identity.bridge_instance_uuid,
        public_operation["external_account_uuid"],
    ):
        return
    existing_identity = (
        message["provider_uuid"],
        message["external_account_uuid"],
        message["provider_external_id"],
    )
    expected_identity = (
        identity.bridge_instance_uuid,
        public_operation["external_account_uuid"],
        provider_entity_id,
    )
    if any(value is not None for value in existing_identity):
        if existing_identity != expected_identity:
            raise ValueError("Native message provider identity conflicts")
        return
    session.execute(
        """
        UPDATE "m_workspace_messages"
        SET "provider_uuid" = %s,
            "external_account_uuid" = %s,
            "provider_external_id" = %s,
            "provider_metadata" = jsonb_build_object(
                'kind', 'zulip',
                'account_uuid', %s::text,
                'external_id', %s::text,
                'provider_realm_uuid', %s::text,
                'capabilities', '{}'::jsonb
            )
        WHERE "project_id" = %s AND "uuid" = %s
        """,
        (
            identity.bridge_instance_uuid,
            public_operation["external_account_uuid"],
            provider_entity_id,
            public_operation["external_account_uuid"],
            provider_entity_id,
            realm_uuid,
            operation["project_id"],
            public_operation["target_uuid"],
        ),
    )


def _reconcile_native_message_provider_echo(
    session: typing.Any,
    project_id: object,
    native_public_uuid: object,
    provider_realm_uuid: object,
    provider_message_id: str,
    provider_uuid: object,
    external_account_uuid: object,
) -> bool:
    """Merge a provider echo that committed before its outbound result.

    Provider command canonicalization and the result path share the same
    realm/message advisory lock. If the echo won the race, move its distinct
    placements onto the native canonical row, merge semantic state for the
    duplicate placement, then delete the echo canonical root. Existing foreign
    keys own the cascade for every remaining placement dependency.
    """
    native = session.execute(
        """
        SELECT placement.uuid AS placement_uuid,
               placement.legacy_public_uuid,
               placement.stream_uuid, placement.topic_uuid,
               message.uuid AS canonical_uuid
        FROM messenger_message_placements AS placement
        JOIN messenger_messages AS message
          ON message.project_id = placement.project_id
         AND message.uuid = placement.message_uuid
        WHERE placement.project_id = %s
          AND COALESCE(placement.legacy_public_uuid, placement.uuid) = %s
        FOR UPDATE OF placement, message
        """,
        (project_id, native_public_uuid),
    ).fetchone()
    if native is None:
        return False
    echo = session.execute(
        """
        SELECT uuid, project_id, author_uuid, source_name, source
        FROM messenger_messages
        WHERE provider_realm_uuid = %s AND provider_message_id = %s
        FOR UPDATE
        """,
        (provider_realm_uuid, provider_message_id),
    ).fetchone()
    if echo is None or echo["uuid"] == native["canonical_uuid"]:
        return False
    if echo["project_id"] != project_id:
        raise ValueError("Provider echo belongs to another Workspace project")

    # The successful native send owns authorship, content, and creation time.
    # The echo owns the realm-global provider identity and may already own
    # projections for other selected accounts.
    session.execute(
        """
        UPDATE messenger_messages AS echo
        SET author_uuid = native.author_uuid,
            payload = native.payload,
            source_name = native.source_name,
            source = native.source,
            delivery = COALESCE(native.delivery, echo.delivery),
            created_at = LEAST(native.created_at, echo.created_at),
            updated_at = GREATEST(native.updated_at, echo.updated_at)
        FROM messenger_messages AS native
        WHERE native.project_id = %s AND native.uuid = %s
          AND echo.project_id = %s AND echo.uuid = %s
        """,
        (
            project_id,
            native["canonical_uuid"],
            project_id,
            echo["uuid"],
        ),
    )

    duplicate_placement = session.execute(
        """
        SELECT uuid, legacy_public_uuid, stream_uuid, topic_uuid
        FROM messenger_message_placements
        WHERE project_id = %s AND message_uuid = %s
          AND stream_uuid = %s AND topic_uuid = %s
          AND uuid <> %s
        ORDER BY created_at, uuid
        LIMIT 1
        FOR UPDATE
        """,
        (
            project_id,
            echo["uuid"],
            native["stream_uuid"],
            native["topic_uuid"],
            native["placement_uuid"],
        ),
    ).fetchone()

    if duplicate_placement is not None:
        source_placement_uuid = duplicate_placement["uuid"]
        target_placement_uuid = native["placement_uuid"]

        session.execute(
            """
            INSERT INTO messenger_user_message_bindings (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, relation_role, visibility,
                permissions, created_at, updated_at
            )
            SELECT messenger_uuid_v5(%s, source.user_uuid::text),
                   source.project_id, %s, source.user_uuid,
                   membership.membership_generation,
                   CASE WHEN source.user_uuid = message.author_uuid
                        THEN 'author' ELSE source.relation_role END,
                   source.visibility, source.permissions,
                   source.created_at, source.updated_at
            FROM messenger_user_message_bindings AS source
            JOIN messenger_stream_bindings AS membership
              ON membership.project_id = source.project_id
             AND membership.stream_uuid = %s
             AND membership.user_uuid = source.user_uuid
             AND membership.active
            JOIN messenger_messages AS message
              ON message.project_id = source.project_id AND message.uuid = %s
            WHERE source.project_id = %s AND source.placement_uuid = %s
            ON CONFLICT (project_id, placement_uuid, user_uuid) DO UPDATE SET
                membership_generation = EXCLUDED.membership_generation,
                relation_role = EXCLUDED.relation_role,
                visibility = EXCLUDED.visibility,
                permissions = EXCLUDED.permissions,
                created_at = LEAST(
                    messenger_user_message_bindings.created_at,
                    EXCLUDED.created_at
                ),
                updated_at = GREATEST(
                    messenger_user_message_bindings.updated_at,
                    EXCLUDED.updated_at
                )
            """,
            (
                target_placement_uuid,
                target_placement_uuid,
                native["stream_uuid"],
                echo["uuid"],
                project_id,
                source_placement_uuid,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_user_message_states (
                uuid, project_id, placement_uuid, user_uuid,
                membership_generation, read_at, mentioned, starred, pinned,
                created_at, updated_at
            )
            SELECT messenger_uuid_v5(%s, source.user_uuid::text),
                   source.project_id, %s, source.user_uuid,
                   membership.membership_generation,
                   source.read_at, source.mentioned, source.starred,
                   source.pinned, source.created_at, source.updated_at
            FROM messenger_user_message_states AS source
            JOIN messenger_stream_bindings AS membership
              ON membership.project_id = source.project_id
             AND membership.stream_uuid = %s
             AND membership.user_uuid = source.user_uuid
             AND membership.active
            WHERE source.project_id = %s AND source.placement_uuid = %s
            ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE SET
                membership_generation = EXCLUDED.membership_generation,
                read_at = CASE
                    WHEN messenger_user_message_states.read_at IS NULL
                    THEN EXCLUDED.read_at
                    WHEN EXCLUDED.read_at IS NULL
                    THEN messenger_user_message_states.read_at
                    ELSE GREATEST(
                        messenger_user_message_states.read_at,
                        EXCLUDED.read_at
                    )
                END,
                mentioned = messenger_user_message_states.mentioned
                            OR EXCLUDED.mentioned,
                starred = messenger_user_message_states.starred
                          OR EXCLUDED.starred,
                pinned = messenger_user_message_states.pinned
                         OR EXCLUDED.pinned,
                created_at = LEAST(
                    messenger_user_message_states.created_at,
                    EXCLUDED.created_at
                ),
                updated_at = GREATEST(
                    messenger_user_message_states.updated_at,
                    EXCLUDED.updated_at
                )
            """,
            (
                target_placement_uuid,
                target_placement_uuid,
                native["stream_uuid"],
                project_id,
                source_placement_uuid,
            ),
        )

        # Save a bounded audience snapshot before removing the duplicate
        # placement, then schedule exact counter snapshots for both scopes.
        session.execute(
            """
            WITH recipients AS (
                SELECT COALESCE(jsonb_agg(binding.user_uuid), '[]'::jsonb)
                           AS user_uuids,
                       COALESCE(
                           jsonb_object_agg(
                               binding.user_uuid::text,
                               binding.membership_generation
                           ),
                           '{}'::jsonb
                       ) AS generations
                FROM messenger_user_message_bindings AS binding
                WHERE binding.project_id = %s
                  AND binding.placement_uuid = %s
            )
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            )
            SELECT gen_random_uuid(), %s, 'delivery_snapshot_event',
                   'message', %s::text || ':' || %s::text,
                   jsonb_build_object(
                       'source_kind', 'message.deleted',
                       'placement', jsonb_build_object(
                           'uuid', %s::text,
                           'stream_uuid', %s::text,
                           'topic_uuid', %s::text
                       ),
                       'recipients', recipients.user_uuids,
                       'membership_generations', recipients.generations,
                       'author_uuid', %s::text,
                       'source_name', %s::text,
                       'source', %s::jsonb,
                       'emit_public_event', true
                   )
            FROM recipients
            WHERE jsonb_array_length(recipients.user_uuids) > 0
            """,
            (
                project_id,
                source_placement_uuid,
                project_id,
                project_id,
                echo["uuid"],
                source_placement_uuid,
                native["stream_uuid"],
                native["topic_uuid"],
                echo["author_uuid"],
                echo["source_name"],
                json.dumps(echo["source"]),
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            )
            SELECT gen_random_uuid(), binding.project_id, 'read_counters',
                   scope.kind,
                   binding.project_id::text || ':' || binding.user_uuid::text
                       || ':' || scope.uuid::text,
                   jsonb_build_object(
                       'source_kind', 'message.deleted',
                       'user_uuid', binding.user_uuid,
                       'stream_uuid', %s::uuid,
                       'topic_uuid', %s::uuid
                   )
            FROM messenger_stream_bindings AS binding
            CROSS JOIN LATERAL (
                VALUES ('user-stream'::varchar, %s::uuid),
                       ('user-topic'::varchar, %s::uuid)
            ) AS scope(kind, uuid)
            WHERE binding.project_id = %s AND binding.stream_uuid = %s
              AND binding.active
            """,
            (
                native["stream_uuid"],
                native["topic_uuid"],
                native["stream_uuid"],
                native["topic_uuid"],
                project_id,
                native["stream_uuid"],
            ),
        )

        session.execute(
            """
            UPDATE messenger_message_reaction_facts AS provider
            SET placement_uuid = %s, updated_at = NOW()
            WHERE provider.project_id = %s
              AND provider.canonical_message_uuid = %s
              AND provider.placement_uuid = %s
              AND NOT EXISTS (
                    SELECT 1
                    FROM messenger_message_reaction_facts AS native
                    WHERE native.project_id = provider.project_id
                      AND native.canonical_message_uuid = %s
                      AND native.user_uuid = provider.user_uuid
                      AND native.emoji_name = provider.emoji_name
              )
            """,
            (
                target_placement_uuid,
                project_id,
                echo["uuid"],
                source_placement_uuid,
                native["canonical_uuid"],
            ),
        )
    # Preserve native reaction identities that clients and queued provider
    # operations already reference. Duplicate echo facts remain owned by the
    # echo root and are removed by its cascade below.
    session.execute(
        """
        UPDATE messenger_message_reaction_facts AS native
        SET created_at = LEAST(native.created_at, provider.created_at),
            updated_at = GREATEST(native.updated_at, provider.updated_at)
        FROM messenger_message_reaction_facts AS provider
        WHERE native.project_id = %s
          AND native.canonical_message_uuid = %s
          AND provider.project_id = native.project_id
          AND provider.canonical_message_uuid = %s
          AND provider.user_uuid = native.user_uuid
          AND provider.emoji_name = native.emoji_name
        """,
        (project_id, native["canonical_uuid"], echo["uuid"]),
    )
    session.execute(
        """
        UPDATE messenger_message_reaction_facts AS provider
        SET canonical_message_uuid = %s,
            updated_at = NOW()
        WHERE provider.project_id = %s
          AND provider.canonical_message_uuid = %s
          AND NOT EXISTS (
                SELECT 1
                FROM messenger_message_reaction_facts AS native
                WHERE native.project_id = provider.project_id
                  AND native.canonical_message_uuid = %s
                  AND native.user_uuid = provider.user_uuid
                  AND native.emoji_name = provider.emoji_name
          )
        """,
        (
            native["canonical_uuid"],
            project_id,
            echo["uuid"],
            native["canonical_uuid"],
        ),
    )
    session.execute(
        """
        WITH moved AS (
            UPDATE messenger_message_placements AS source
            SET message_uuid = %s, updated_at = NOW()
            WHERE source.project_id = %s AND source.message_uuid = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM messenger_message_placements AS native
                  WHERE native.project_id = source.project_id
                    AND native.message_uuid = %s
                    AND native.stream_uuid = source.stream_uuid
                    AND native.topic_uuid = source.topic_uuid
              )
            RETURNING source.uuid
        )
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload
        )
        SELECT gen_random_uuid(), %s, 'delivery_snapshot_event',
               'message', %s::text || ':' || moved.uuid::text,
               jsonb_build_object(
                   'source_kind', 'message.updated',
                   'placement_uuid', moved.uuid::text
               )
        FROM moved
        """,
        (
            native["canonical_uuid"],
            project_id,
            echo["uuid"],
            native["canonical_uuid"],
            project_id,
            project_id,
        ),
    )
    # Any placement left under the echo root duplicates a native placement.
    # Deleting the canonical root delegates cleanup to the schema ownership
    # chain: placements, bindings, states, reactions, and fanout roots all use
    # ON DELETE CASCADE. Immutable outbox evidence remains and safely no-ops
    # because its deleted placement can no longer be resolved.
    session.execute(
        """
        DELETE FROM messenger_messages
        WHERE project_id = %s AND uuid = %s
        """,
        (project_id, echo["uuid"]),
    )
    session.execute(
        """
        UPDATE messenger_messages AS message
        SET provider_uuid = %s,
            external_account_uuid = %s,
            provider_external_id = %s,
            provider_realm_uuid = %s,
            provider_message_id = %s,
            provider = jsonb_build_object(
                'kind', 'zulip',
                'account_uuid', %s::text,
                'external_id', %s::text,
                'provider_realm_uuid', %s::text,
                'capabilities', '{}'::jsonb
            ),
            reactions = COALESCE(snapshot.reactions, '{}'::jsonb),
            reaction_users = COALESCE(snapshot.reaction_users, '{}'::jsonb),
            updated_at = NOW()
        FROM (
            SELECT jsonb_object_agg(grouped.emoji_name, grouped.total)
                       AS reactions,
                   jsonb_object_agg(grouped.emoji_name, grouped.users)
                       AS reaction_users
            FROM (
                SELECT emoji_name, count(*) AS total,
                       jsonb_agg(user_uuid::text ORDER BY created_at, uuid)
                           AS users
                FROM messenger_message_reaction_facts
                WHERE project_id = %s AND canonical_message_uuid = %s
                GROUP BY emoji_name
            ) AS grouped
        ) AS snapshot
        WHERE message.project_id = %s AND message.uuid = %s
        """,
        (
            provider_uuid,
            external_account_uuid,
            provider_message_id,
            provider_realm_uuid,
            provider_message_id,
            external_account_uuid,
            provider_message_id,
            provider_realm_uuid,
            project_id,
            native["canonical_uuid"],
            project_id,
            native["canonical_uuid"],
        ),
    )
    return True


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
    provider_entity_id = validated["provider_entity_id"]
    canonical_hash = _sha256(result)
    read_state.lock_read_state_schema_shared(session)
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"provider-read-materialize-v1:{identity.bridge_instance_uuid}",),
    )
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
               "attempt", "operation_kind"
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
    if queue_status == "succeeded":
        _bind_native_message_provider_identity(
            session,
            identity,
            operation,
            provider_entity_id,
        )
    snapshot = None
    if operation["operation_kind"] == "read_state.set":
        snapshot = session.execute(
            """
            SELECT exhausted
            FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = %s
            FOR UPDATE
            """,
            (operation["external_operation_uuid"],),
        ).fetchone()
    session.execute(
        """
        SELECT uuid
        FROM m_external_operations_v2
        WHERE uuid = %s
        FOR UPDATE
        """,
        (operation["external_operation_uuid"],),
    ).fetchone()
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
            "safe_error" = %s, "public_result_status" = %s,
            "terminal_result" = %s::jsonb,
            "payload" = CASE
                WHEN %s = 'succeeded' AND "operation_kind" = 'read_state.set'
                THEN jsonb_set("payload", '{message_uuids}', '[]'::jsonb)
                ELSE "payload"
            END,
            "completed_at" = %s, "updated_at" = %s
        WHERE "uuid" = %s
        """,
        (
            queue_status,
            safe_error,
            public_status,
            _canonical_json(result),
            queue_status,
            now,
            now,
            provider_operation_uuid,
        ),
    )
    aggregate = session.execute(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE status IN ('queued', 'leased')
            ) AS nonterminal_count,
            MAX(attempt) AS attempt
        FROM m_external_provider_operations_v1
        WHERE external_operation_uuid = %s
        """,
        (operation["external_operation_uuid"],),
    ).fetchone()
    if aggregate["nonterminal_count"]:
        return {"result_uuid": str(result_uuid), "status": "applied"}
    representative = session.execute(
        """
        SELECT terminal_result
        FROM m_external_provider_operations_v1
        WHERE external_operation_uuid = %s
          AND public_result_status IN (
                'failed', 'manual_reconciliation_required'
          )
        ORDER BY
            CASE public_result_status
                WHEN 'manual_reconciliation_required' THEN 0
                ELSE 1
            END,
            sequence
        LIMIT 1
        """,
        (operation["external_operation_uuid"],),
    ).fetchone()
    if snapshot is not None and not snapshot["exhausted"] and representative is None:
        return {"result_uuid": str(result_uuid), "status": "applied"}
    terminal_result = (
        result if representative is None else representative["terminal_result"]
    )
    validated = _validated_result(terminal_result)
    public_status = validated["public_status"]
    safe_error = validated["safe_error"]
    manual = validated["manual"]
    reconciliation = validated["reconciliation"]
    cancelled_read_snapshot = (
        snapshot is not None and public_status == "failed" and safe_error == "cancelled"
    )
    if cancelled_read_snapshot:
        # The bridge uses ``cancelled`` only when the leased page belongs to a
        # desired-state assignment that is no longer current. Retrying that
        # page against a replacement assignment is unsafe, and retaining its
        # snapshot would permanently block every later read in the same lane.
        public_status = "discarded"
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
            aggregate["attempt"],
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
            _canonical_json(terminal_result),
            _canonical_json(
                {
                    "attempt": aggregate["attempt"],
                    "status": public_status,
                    "completed_at": _timestamp(now),
                    "safe_error": safe_error,
                }
            ),
            now,
            operation["external_operation_uuid"],
        ),
    )
    if cancelled_read_snapshot:
        session.execute(
            """
            UPDATE m_external_provider_operations_v1
            SET status = 'discarded', updated_at = %s
            WHERE external_operation_uuid = %s AND status = 'failed'
            """,
            (now, operation["external_operation_uuid"]),
        )
    if snapshot is not None and public_status in {"succeeded", "discarded"}:
        session.execute(
            """
            DELETE FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = %s
            """,
            (operation["external_operation_uuid"],),
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


def _lock_provider_event_projects(
    session: typing.Any,
    project_ids: list[sys_uuid.UUID],
    message_uuids: list[sys_uuid.UUID],
    *,
    structural_batch: bool,
) -> list[sys_uuid.UUID]:
    for message_uuid in sorted(set(message_uuids), key=str):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"workspace-message-resource-v1:{message_uuid}",),
        )
    read_state.lock_read_state_schema_shared(session)
    affected_projects = set(project_ids)
    while True:
        existing_projects = session.execute(
            """
            SELECT DISTINCT project_id
            FROM m_workspace_messages
            WHERE uuid = ANY(%s::uuid[])
            ORDER BY project_id
            """,
            (message_uuids,),
        ).fetchall()
        affected_projects.update(row["project_id"] for row in existing_projects)
        ordered_projects = sorted(affected_projects, key=str)
        session.execute("SAVEPOINT provider_project_discovery")
        try:
            # Existing message upserts can change topic/stream scope. Pure
            # creation only needs project serialization and stays concurrent
            # with an optimistic mark-read scan.
            if structural_batch or existing_projects:
                read_state.lock_message_structure(session, ordered_projects)
            read_state.lock_projects(session, ordered_projects)
            refreshed_projects = session.execute(
                """
                SELECT DISTINCT project_id
                FROM m_workspace_messages
                WHERE uuid = ANY(%s::uuid[])
                ORDER BY project_id
                """,
                (message_uuids,),
            ).fetchall()
            expanded_projects = affected_projects | {
                row["project_id"] for row in refreshed_projects
            }
            if expanded_projects != affected_projects:
                session.execute("ROLLBACK TO SAVEPOINT provider_project_discovery")
                session.execute("RELEASE SAVEPOINT provider_project_discovery")
                affected_projects = expanded_projects
                continue
            session.execute("RELEASE SAVEPOINT provider_project_discovery")
            return ordered_projects
        except Exception:
            session.execute("ROLLBACK TO SAVEPOINT provider_project_discovery")
            session.execute("RELEASE SAVEPOINT provider_project_discovery")
            raise


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
    read_state.lock_read_state_schema_shared(session)
    _bridge_capabilities(session, identity, now)
    read_state.lock_external_account_resources(
        session,
        (sys_uuid.UUID(str(event["external_account_uuid"])) for event in events),
        shared=True,
    )
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
                or str(event.get("kind", "")).startswith("message.")
            },
            key=str,
        )
        message_uuids = []
        message_upsert_uuids = []
        for event in events:
            if not str(event.get("kind", "")).startswith("message."):
                continue
            payload = event.get("payload")
            resource = payload.get("resource") if isinstance(payload, dict) else None
            if isinstance(resource, dict) and resource.get("uuid") is not None:
                message_uuid = sys_uuid.UUID(str(resource["uuid"]))
                message_uuids.append(message_uuid)
                if event.get("kind") == "message.upsert":
                    message_upsert_uuids.append(message_uuid)
        structural_batch = any(
            event.get("kind") in {"message.delete", "stream.delete", "topic.delete"}
            for event in events
        ) or len(message_upsert_uuids) != len(set(message_upsert_uuids))
        locked_project_ids = _lock_provider_event_projects(
            session,
            project_ids,
            message_uuids,
            structural_batch=structural_batch,
        )
        batch_cache = getattr(session, cache_attribute)
        batch_cache[("project_event_locks",)] = {
            str(project_id) for project_id in locked_project_ids
        }
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
                    account.provider_realm_uuid,
                    NULL::integer AS assignment_generation
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
                    account.provider_realm_uuid,
                    assignment.generation AS assignment_generation
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
                JOIN "m_external_bridge_desired_resources_v1" AS assignment
                  ON assignment."bridge_instance_uuid" = %s
                 AND assignment."provider_kind" = %s
                 AND assignment."resource_type" =
                     'external_chat_assignment'
                 AND assignment."resource_uuid" = chat."uuid"
                 AND assignment."operation" = 'upsert'
                 AND assignment."resource"->>'external_account_uuid' =
                     requested.account_uuid::text
                 AND assignment."resource"->>'project_id' =
                     requested.project_id::text
                 AND assignment."resource"->>'selected' = 'true'
                 AND assignment."resource"#>>'{workspace_projection,stream,uuid}' =
                     chat."projection_stream_uuid"::text
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
                               authorized.provider_realm_uuid,
                           'assignment_generation',
                               authorized.assignment_generation
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
    event_count = len(events)
    broadcast_count = len(broadcast_epochs)
    apply_duration = time.monotonic() - started_at
    LOG.info(
        "Applied provider event batch: events=%d broadcasts=%d duration_seconds=%.3f",
        event_count,
        broadcast_count,
        apply_duration,
        extra={
            "provider_batch_event_count": event_count,
            "provider_batch_broadcast_count": broadcast_count,
            "provider_batch_apply_duration_seconds": apply_duration,
        },
    )
    return {"results": results}


def _insert_provider_operation(
    session: typing.Any,
    *,
    external_operation_uuid: sys_uuid.UUID,
    bridge_instance_uuid: object,
    external_account_uuid: object,
    project_id: object,
    operation_kind: str,
    payload: object,
    causal_lane: object | None = None,
    now: datetime.datetime | None = None,
) -> sys_uuid.UUID:
    """Append one immutable provider delivery row for a public operation."""
    record_uuid = sys_uuid.uuid4()
    causal_lane = _provider_causal_lane(payload, causal_lane)
    session.execute(
        """
        INSERT INTO "m_external_provider_operations_v1" (
            "uuid", "external_operation_uuid", "bridge_instance_uuid",
            "external_account_uuid", "project_id", "operation_kind", "payload",
            "causal_lane", "available_at", "created_at", "updated_at"
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
            COALESCE(%s, statement_timestamp()),
            COALESCE(%s, statement_timestamp()),
            COALESCE(%s, statement_timestamp())
        )
        """,
        (
            record_uuid,
            external_operation_uuid,
            bridge_instance_uuid,
            external_account_uuid,
            project_id,
            operation_kind,
            _canonical_json(payload),
            causal_lane,
            now,
            now,
            now,
        ),
    )
    return record_uuid


def _enqueue_provider_operation(
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
    causal_lane: object | None = None,
) -> tuple[external_models.ExternalOperation, sys_uuid.UUID]:
    """Create the public operation and provider outbox row atomically."""
    read_state.lock_read_state_schema_shared(session)
    causal_lane = _provider_causal_lane(payload, causal_lane)
    if causal_lane is not None:
        _lock_provider_causal_lane(
            session,
            bridge_instance_uuid=bridge_instance_uuid,
            external_account_uuid=external_account_uuid,
            causal_lane=causal_lane,
        )
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
    record_uuid = _insert_provider_operation(
        session,
        external_operation_uuid=operation_uuid,
        bridge_instance_uuid=bridge_instance_uuid,
        external_account_uuid=external_account_uuid,
        project_id=project_id,
        operation_kind=operation_kind,
        payload=payload,
        causal_lane=causal_lane,
    )
    publish_operation_event(
        session,
        operation,
        project_id,
        messenger_events.EXTERNAL_OPERATION_CREATED_EVENT,
    )
    return operation, record_uuid


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
    """Create one provider operation through the stable queue boundary."""
    return _enqueue_provider_operation(
        session,
        operation_uuid=operation_uuid,
        bridge_instance_uuid=bridge_instance_uuid,
        external_account_uuid=external_account_uuid,
        project_id=project_id,
        owner_user_uuid=owner_user_uuid,
        operation_kind=operation_kind,
        target_type=target_type,
        target_uuid=target_uuid,
        payload=payload,
    )


def enqueue_provider_operation_in_lane(
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
    causal_lane: object,
) -> tuple[external_models.ExternalOperation, sys_uuid.UUID]:
    """Queue with the authoritative stream lane known by the caller."""
    return _enqueue_provider_operation(
        session,
        operation_uuid=operation_uuid,
        bridge_instance_uuid=bridge_instance_uuid,
        external_account_uuid=external_account_uuid,
        project_id=project_id,
        owner_user_uuid=owner_user_uuid,
        operation_kind=operation_kind,
        target_type=target_type,
        target_uuid=target_uuid,
        payload=payload,
        causal_lane=causal_lane,
    )


def enqueue_provider_read_operation(
    session: typing.Any,
    *,
    operation_uuid: sys_uuid.UUID,
    bridge_instance_uuid: object,
    external_account_uuid: object,
    project_id: object,
    owner_user_uuid: object,
    target_type: str,
    target_uuid: object,
    payload: dict[str, object],
    candidate_sql: str,
    candidate_values: typing.Sequence[object],
    candidate_chunks: typing.Sequence[typing.Mapping[str, object]] | None = None,
    use_candidate_chunks: bool | None = None,
) -> external_models.ExternalOperation | None:
    """Queue eager compatibility pages or one lazy exact snapshot.

    ``None`` stores bounded UUID packs when compact bitmap coordinates are
    unavailable (for example while a project is still in legacy mode).
    ``False`` eagerly creates revision-1 compatibility operations without a
    lazy snapshot. Revision 2 uses bitmap chunks or bounded UUID packs.
    """
    read_state.lock_read_state_schema_shared(session)
    causal_lane = sys_uuid.UUID(str(payload["stream_uuid"]))
    _lock_provider_causal_lane(
        session,
        bridge_instance_uuid=bridge_instance_uuid,
        external_account_uuid=external_account_uuid,
        causal_lane=causal_lane,
    )
    if use_candidate_chunks is False:
        candidate_limit = PROVIDER_READ_LEGACY_MAX_PAGES * PROVIDER_READ_MAX_MESSAGES
        pages = session.execute(
            f"""
            WITH bounded_candidates AS MATERIALIZED (
                SELECT candidate.uuid, candidate.created_at
                FROM ({candidate_sql}) AS candidate
                ORDER BY candidate.created_at, candidate.uuid
                LIMIT %s
            ), ordered_candidates AS MATERIALIZED (
                SELECT
                    candidate.uuid,
                    row_number() OVER (
                        ORDER BY candidate.created_at, candidate.uuid
                    ) - 1 AS snapshot_position
                FROM bounded_candidates AS candidate
            )
            SELECT array_agg(uuid ORDER BY snapshot_position) AS candidate_uuids
            FROM ordered_candidates
            GROUP BY snapshot_position / {PROVIDER_READ_MAX_MESSAGES}
            ORDER BY snapshot_position / {PROVIDER_READ_MAX_MESSAGES}
            """,
            (*candidate_values, candidate_limit + 1),
        ).fetchall()
        if len(pages) > PROVIDER_READ_LEGACY_MAX_PAGES:
            raise ProviderUnavailableError(
                "Provider capability messenger.message.read.paging revision 1 "
                "is required"
            )
        first_operation = None
        for index, page in enumerate(pages):
            provider_message_ids = _provider_message_ids_for_read_page(
                session,
                external_account_uuid=external_account_uuid,
                project_id=project_id,
                message_uuids=page["candidate_uuids"],
            )
            operation, _record_uuid = _enqueue_provider_operation(
                session,
                operation_uuid=(operation_uuid if index == 0 else sys_uuid.uuid4()),
                bridge_instance_uuid=bridge_instance_uuid,
                external_account_uuid=external_account_uuid,
                project_id=project_id,
                owner_user_uuid=owner_user_uuid,
                operation_kind="read_state.set",
                target_type=target_type,
                target_uuid=target_uuid,
                payload={
                    **payload,
                    "message_uuids": [
                        str(message_uuid) for message_uuid in page["candidate_uuids"]
                    ],
                    **(
                        {}
                        if provider_message_ids is None
                        else {"provider_message_ids": provider_message_ids}
                    ),
                },
                causal_lane=causal_lane,
            )
            if first_operation is None:
                first_operation = operation
        return first_operation
    if use_candidate_chunks and not candidate_chunks:
        return None
    operation = external_models.ExternalOperation(
        uuid=operation_uuid,
        external_account_uuid=external_account_uuid,
        owner_user_uuid=owner_user_uuid,
        action="read_state.set",
        target_type=target_type,
        target_uuid=target_uuid,
        details={"payload": {**payload, "message_uuids": []}},
        status=external_models.ExternalOperationStatus.QUEUED.value,
    )
    operation.insert(session=session)
    now = datetime.datetime.now(datetime.timezone.utc)
    session.execute(
        """
        INSERT INTO m_external_provider_read_snapshots_v1 (
            external_operation_uuid, bridge_instance_uuid,
            external_account_uuid, project_id, causal_lane, payload,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        """,
        (
            operation_uuid,
            bridge_instance_uuid,
            external_account_uuid,
            project_id,
            causal_lane,
            _canonical_json(payload),
            now,
            now,
        ),
    )
    if use_candidate_chunks:
        session.execute(
            """
            INSERT INTO m_external_provider_read_candidate_chunks_v1 (
                external_operation_uuid, chunk_number, candidate_bits
            )
            SELECT %s, chunk_number, candidate_bits::bit(4096)
            FROM unnest(%s::bigint[], %s::text[])
                AS candidate(chunk_number, candidate_bits)
            """,
            (
                operation_uuid,
                [chunk["chunk_number"] for chunk in candidate_chunks or ()],
                [chunk["read_bits"] for chunk in candidate_chunks or ()],
            ),
        )
    else:
        inserted_packs = session.execute(
            f"""
            INSERT INTO m_external_provider_read_candidate_packs_v1 (
                external_operation_uuid, pack_number, candidate_count,
                candidate_uuids
            )
            WITH ordered_candidates AS MATERIALIZED (
                SELECT
                    candidate.uuid,
                    row_number() OVER (
                        ORDER BY candidate.created_at, candidate.uuid
                    ) - 1 AS snapshot_position
                FROM ({candidate_sql}) AS candidate
            ), packed_candidates AS (
                SELECT
                    snapshot_position / 4000 AS pack_number,
                    array_agg(uuid ORDER BY snapshot_position) AS candidate_uuids
                FROM ordered_candidates
                GROUP BY snapshot_position / 4000
            )
            SELECT
                %s, pack_number, cardinality(candidate_uuids), candidate_uuids
            FROM packed_candidates
            ORDER BY pack_number
            """,
            (*candidate_values, operation_uuid),
        ).rowcount
        if not inserted_packs:
            session.execute(
                "DELETE FROM m_external_operations_v2 WHERE uuid = %s",
                (operation_uuid,),
            )
            return None
    publish_operation_event(
        session,
        operation,
        project_id,
        messenger_events.EXTERNAL_OPERATION_CREATED_EVENT,
    )
    return operation


def retry_provider_operation(
    session: typing.Any,
    *,
    external_operation_uuid: object,
    next_attempt: int,
) -> typing.Any:
    """Requeue an existing provider operation in the caller transaction."""
    read_state.lock_read_state_schema_shared(session)
    snapshot_identity = session.execute(
        """
        SELECT bridge_instance_uuid
        FROM m_external_provider_read_snapshots_v1
        WHERE external_operation_uuid = %s
        """,
        (external_operation_uuid,),
    ).fetchone()
    if snapshot_identity is not None:
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                f"provider-read-materialize-v1:"
                f"{snapshot_identity['bridge_instance_uuid']}",
            ),
        )
    retryable = session.execute(
        """
        SELECT "uuid", "operation_kind"
        FROM "m_external_provider_operations_v1"
        WHERE "external_operation_uuid" = %s
          AND (
                "status" = 'failed'
                OR (
                    "status" = 'discarded'
                    AND "operation_kind" <> 'read_state.set'
                )
          )
        ORDER BY "sequence"
        FOR UPDATE
        """,
        (external_operation_uuid,),
    ).fetchall()
    if not retryable:
        raise ValueError("Provider operation cannot be retried from its current state")

    rows = session.execute(
        """
        WITH retry_source AS MATERIALIZED (
            SELECT uuid, bridge_instance_uuid, external_account_uuid,
                   project_id, operation_kind, causal_lane, payload
            FROM m_external_provider_operations_v1
            WHERE external_operation_uuid = %s
              AND operation_kind = 'read_state.set'
              AND status = 'failed'
            FOR UPDATE
        ), neutralized AS (
            UPDATE m_external_provider_operations_v1 AS failed_page
            SET status = 'discarded', public_result_status = NULL,
                payload = jsonb_set(
                    failed_page.payload, '{message_uuids}', '[]'::jsonb
                ),
                updated_at = NOW()
            FROM retry_source
            WHERE failed_page.uuid = retry_source.uuid
            RETURNING failed_page.uuid
        )
        INSERT INTO m_external_provider_operations_v1 (
            uuid, external_operation_uuid, bridge_instance_uuid,
            external_account_uuid, project_id, operation_kind, causal_lane, payload,
            status, attempt, available_at, created_at, updated_at
        )
        SELECT
            gen_random_uuid(), %s, bridge_instance_uuid,
            external_account_uuid, project_id, operation_kind, causal_lane, payload,
            'queued', %s - 1, NOW(), NOW(), NOW()
        FROM retry_source
        CROSS JOIN (SELECT COUNT(*) FROM neutralized) AS completed_update
        RETURNING uuid, project_id
        """,
        (external_operation_uuid, external_operation_uuid, next_attempt),
    ).fetchall()
    rows.extend(
        session.execute(
            """
        UPDATE "m_external_provider_operations_v1"
        SET
            "status" = 'queued',
            "attempt" = %s - 1,
            "available_at" = NOW(),
            "lease_uuid" = NULL,
            "lease_expires_at" = NULL,
            "safe_error" = NULL,
            "public_result_status" = NULL,
            "terminal_result" = NULL,
            "completed_at" = NULL,
            "updated_at" = NOW()
        WHERE "external_operation_uuid" = %s
          AND "status" IN ('failed', 'discarded')
          AND "operation_kind" <> 'read_state.set'
        RETURNING "uuid", "project_id"
        """,
            (next_attempt, external_operation_uuid),
        ).fetchall()
    )
    return rows[0]


def discard_provider_operation(
    session: typing.Any,
    *,
    external_operation_uuid: object,
) -> typing.Any:
    """Prevent a queued provider operation from being leased before deletion."""
    read_state.lock_read_state_schema_shared(session)
    snapshot_identity = session.execute(
        """
        SELECT bridge_instance_uuid
        FROM m_external_provider_read_snapshots_v1
        WHERE external_operation_uuid = %s
        """,
        (external_operation_uuid,),
    ).fetchone()
    if snapshot_identity is not None:
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                f"provider-read-materialize-v1:"
                f"{snapshot_identity['bridge_instance_uuid']}",
            ),
        )
    snapshot = session.execute(
        """
        SELECT project_id
        FROM m_external_provider_read_snapshots_v1
        WHERE external_operation_uuid = %s
        FOR UPDATE
        """,
        (external_operation_uuid,),
    ).fetchone()
    if snapshot is not None:
        public_operation = session.execute(
            """
            SELECT status
            FROM m_external_operations_v2
            WHERE uuid = %s
            FOR UPDATE
            """,
            (external_operation_uuid,),
        ).fetchone()
        if public_operation is None or public_operation["status"] not in {
            "queued",
            "failed",
        }:
            raise ValueError(
                "Provider operation cannot be discarded from its current state"
            )
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
    if snapshot is not None:
        session.execute(
            """
            DELETE FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = %s
            """,
            (external_operation_uuid,),
        )
        return snapshot
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
