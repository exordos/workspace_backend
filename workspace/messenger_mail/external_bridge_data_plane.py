# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import copy
import datetime
import hashlib
import json
import re
import uuid as sys_uuid
from collections.abc import Callable
from typing import Any, cast

from restalchemy.dm import filters as dm_filters

from workspace.external_bridge_control import sql_state as bridge_sql_state
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models
from workspace.messenger_mail import external_bridge_codec as codec
from workspace.messenger_mail import repository as mail_repository


OPERATION_NAMESPACE = sys_uuid.UUID("b4f915af-4cc4-57e5-9ac2-1f8356c77592")
WORKSPACE_SENDER = "zulip-bridge-producer@bridge.workspace.invalid"
BRIDGE_ADDRESS = "zulip-bridge@messenger.workspace.invalid"
INGRESS_ADDRESS = "zulip-bridge-ingress@messenger.workspace.invalid"
INGRESS_MAILBOX = "INBOX"
_FILE_URN_PREFIX_RE = re.compile(r"urn:(?:file|image|video):")
_FILE_URN_RE = re.compile(
    r"urn:(?:file|image|video):"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
    r"(?:\?[^\s)\]<>]*)?(?=$|[\s)\]<>])"
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _iso(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _provider_stream_id(provider_chat_id: str | int) -> int:
    """Return the public Zulip stream id from a namespaced chat identifier."""
    value = provider_chat_id
    if isinstance(value, str) and value.startswith("channel:"):
        value = value.partition(":")[2]
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _inbound_delivery_class(operation: dict[str, Any]) -> str:
    delivery_class = operation.get("extensions", {}).get("delivery_class")
    if delivery_class not in {"live", "backfill"}:
        raise ValueError("invalid_record")
    return cast(str, delivery_class)


def _inbound_provider_metadata(
    record: dict[str, Any],
    target: dict[str, Any],
    account: dict[str, Any],
    *,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operation = record["operation"]
    delivery_class = _inbound_delivery_class(operation)
    notification_eligible = delivery_class == "live" and account["live_ready"] is True
    if current is not None and "notification_eligible" in current:
        notification_eligible = current["notification_eligible"]
        delivery_class = current.get("delivery_class", delivery_class)
    return {
        "kind": "zulip",
        "account_uuid": record["account_uuid"],
        "external_id": operation["provider"]["entity_id"],
        "revision": operation["provider"]["revision"],
        "capabilities": target["capabilities"],
        "delivery_class": delivery_class,
        "notification_eligible": notification_eligible,
    }


def _canonical_record(
    record: dict[str, Any],
    operation: str,
    entity_uuid: sys_uuid.UUID | str,
    payload: dict[str, Any],
    suffix: str | None = None,
) -> mail_repository.OperationRecord:
    operation_uuid = sys_uuid.UUID(record["operation_uuid"])
    if suffix is not None:
        operation_uuid = sys_uuid.uuid5(operation_uuid, suffix)
    return mail_repository.OperationRecord(
        project_uuid=sys_uuid.UUID(record["project_uuid"]),
        operation_uuid=operation_uuid,
        actor_uuid=sys_uuid.UUID(record["operation"]["actor_uuid"]),
        operation=operation,
        entity_uuid=sys_uuid.UUID(str(entity_uuid)),
        payload=payload,
        occurred_at=datetime.datetime.fromisoformat(
            record["operation"]["occurred_at"].replace("Z", "+00:00")
        ),
    )


_PUBLIC_CAPABILITY_BY_OPERATION = {
    "identity.upsert": "messenger.chat_catalog",
    "message.create": "messenger.message.send",
    "message.update": "messenger.message.edit",
    "message.delete": "messenger.message.delete",
    "read_state.set": "messenger.message.read",
    "stream.upsert": "messenger.stream.rename",
    "stream.delete": "messenger.stream.rename",
    "topic.upsert": "messenger.topic.rename",
    "topic.delete": "messenger.topic.rename",
}

_DELIVERY_STATUS_BY_OPERATION_STATUS = {
    "queued": "pending",
    "running": "pending",
    "succeeded": "delivered",
    "failed": "failed",
    "manual_reconciliation_required": "manual_reconciliation_required",
    "discarded": "discarded",
}


def _operation_delivery(operation: Any) -> dict[str, Any]:
    status = _DELIVERY_STATUS_BY_OPERATION_STATUS[operation.status]
    return {
        "external_operation_uuid": str(operation.uuid),
        "status": status,
        "safe_error": operation.safe_error,
        "can_retry": operation.can_retry,
        "can_discard": operation.can_discard,
        "updated_at": _iso(operation.updated_at),
        "duplicate_risk": operation.duplicate_risk,
        "retry_requires_confirmation": operation.retry_requires_confirmation,
        "original_url": operation.original_url,
        "reconciliation_reason": operation.reconciliation_reason,
    }


def _emit_target_updated_events(
    session: Any,
    project_uuid: sys_uuid.UUID,
    target_type: str,
    target_uuid: sys_uuid.UUID,
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
            "project_id": dm_filters.EQ(project_uuid),
            "uuid": dm_filters.EQ(target_uuid),
        },
        session=session,
    ):
        create_event(resource, session=session)


def update_operation_projection(
    session: Any,
    operation: Any,
    project_uuid: sys_uuid.UUID | str,
    *,
    event_kind: str = messenger_events.EXTERNAL_OPERATION_UPDATED_EVENT,
) -> None:
    """Publish an operation snapshot and converge its target delivery state."""
    project_uuid = sys_uuid.UUID(str(project_uuid))
    messenger_events.create_external_resource_event(
        project_uuid,
        operation.owner_user_uuid,
        operation,
        event_kind,
        hidden_fields=("owner_user_uuid",),
        session=session,
    )
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
            _json(delivery),
            legacy_status,
            operation.safe_error,
            operation.updated_at,
            project_uuid,
            operation.target_uuid,
        ),
    ).fetchone()
    if changed is not None:
        _emit_target_updated_events(
            session,
            project_uuid,
            operation.target_type,
            operation.target_uuid,
        )


def validate_workspace_operation(
    session: Any,
    *,
    project_uuid: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    operation_kind: str,
    target_stream_uuid: sys_uuid.UUID,
) -> dict[str, Any] | None:
    """Lock and validate an external target before canonical state is mutated."""
    rows = session.execute(
        """
        SELECT chat.uuid AS chat_uuid, chat.external_account_uuid,
               chat.provider_chat_id, chat.selected AS chat_selected,
               chat.status AS chat_status, chat.capabilities,
               account.provider, account.status AS account_status,
               account.live_ready AS account_live_ready
        FROM m_external_chats_v2 AS chat
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = chat.external_account_uuid
        WHERE chat.project_id = %s
          AND chat.owner_user_uuid = %s
          AND chat.projection_stream_uuid = %s
        LIMIT 2
        FOR UPDATE OF chat, account
        """,
        (project_uuid, owner_user_uuid, target_stream_uuid),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError("External stream maps to multiple chats")
    target = rows[0]
    account_ready = target["account_status"] == "backfill" or (
        target["account_status"] == "live" and target["account_live_ready"] is True
    )
    chat_ready = target["chat_selected"] is True and target["chat_status"] in {
        "syncing",
        "live",
    }
    capability_name = _PUBLIC_CAPABILITY_BY_OPERATION[operation_kind]
    capability = target["capabilities"].get(capability_name)
    if (
        not account_ready
        or not chat_ready
        or not isinstance(capability, dict)
        or capability.get("available") is not True
    ):
        raise ValueError("external_operation_unavailable")
    return cast(dict[str, Any], target)


def _append_external_chat_assignment(
    session: Any,
    chat: Any,
    bridge_instance_uuid: sys_uuid.UUID | None = None,
) -> sys_uuid.UUID | None:
    if bridge_instance_uuid is None:
        rows = session.execute(
            """
            SELECT bridge_instance_uuid
            FROM m_external_bridge_desired_resources_v1
            WHERE provider_kind = %s
              AND resource_type = 'external_chat_assignment'
              AND resource_uuid = %s
            LIMIT 2
            """,
            (chat.provider, chat.uuid),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("External chat assignment belongs to multiple bridges")
        bridge_instance_uuid = rows[0]["bridge_instance_uuid"]
    return cast(
        sys_uuid.UUID | None,
        bridge_sql_state.append_upsert(
            session,
            bridge_instance_uuid,
            chat.provider,
            bridge_sql_state.external_chat_assignment_desired(chat, session=session),
        ),
    )


def _update_stream_projection_mapping(
    session: Any,
    target: dict[str, Any],
    *,
    name: str,
    description: str,
    private: bool,
    bridge_instance_uuid: sys_uuid.UUID | None = None,
) -> None:
    chat = external_models.ExternalChat.objects.get_one(
        filters={"uuid": dm_filters.EQ(target["chat_uuid"])},
        session=session,
    )
    source = {
        **chat.source,
        "description": description,
        "private": private,
    }
    if chat.display_name == name and chat.source == source:
        return
    chat.properties["display_name"].set_value_force(name)
    chat.properties["source"].set_value_force(source)
    chat.properties["revision"].set_value_force(chat.revision + 1)
    chat.update(session=session)
    _append_external_chat_assignment(session, chat, bridge_instance_uuid)


def _ensure_topic_projection_mapping(
    session: Any,
    target: dict[str, Any],
    *,
    topic_uuid: sys_uuid.UUID,
    topic_name: str,
    bridge_instance_uuid: sys_uuid.UUID | None,
) -> str:
    chat = external_models.ExternalChat.objects.get_one(
        filters={"uuid": dm_filters.EQ(target["chat_uuid"])},
        session=session,
    )
    topics = [dict(item) for item in chat.source.get("topics", [])]
    stream_id = chat.provider_chat_id.removeprefix("channel:")
    chat_type = chat.source.get("chat_type", "channel")
    provider_topic_id = (
        f"{stream_id}:{topic_name}"
        if chat_type == "channel"
        else f"{chat.provider_chat_id}:default"
    )
    for topic in topics:
        if topic["topic_uuid"] == str(topic_uuid):
            if (
                topic["provider_topic_id"] == provider_topic_id
                and topic["name"] == topic_name
            ):
                return provider_topic_id
            topic.update(
                {
                    "provider_topic_id": provider_topic_id,
                    "name": topic_name,
                }
            )
            break
    else:
        topics.append(
            {
                "topic_uuid": str(topic_uuid),
                "provider_topic_id": provider_topic_id,
                "name": topic_name,
                "is_default": False,
            }
        )
    source = {**chat.source, "topics": topics}
    chat.properties["source"].set_value_force(source)
    chat.properties["revision"].set_value_force(chat.revision + 1)
    chat.update(session=session)
    _append_external_chat_assignment(session, chat, bridge_instance_uuid)
    return provider_topic_id


def ensure_topic_projection_mapping(
    session: Any,
    *,
    project_uuid: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
    topic_uuid: sys_uuid.UUID,
    topic_name: str,
    bridge_instance_uuid: sys_uuid.UUID,
) -> str | None:
    target = validate_workspace_operation(
        session,
        project_uuid=project_uuid,
        owner_user_uuid=owner_user_uuid,
        operation_kind="message.create",
        target_stream_uuid=stream_uuid,
    )
    if target is None:
        return None
    return _ensure_topic_projection_mapping(
        session,
        target,
        topic_uuid=topic_uuid,
        topic_name=topic_name,
        bridge_instance_uuid=bridge_instance_uuid,
    )


def queue_workspace_operation(
    session: Any,
    *,
    project_uuid: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    operation_uuid: sys_uuid.UUID,
    operation_kind: str,
    entity_uuid: sys_uuid.UUID,
    payload: dict[str, Any],
    target_type: str,
    target_stream_uuid: sys_uuid.UUID | None = None,
    provider_entity_id: str | None = None,
    provider_revision: int | None = None,
    realm_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    identity_generation: int,
    enrollment_secret: bytes,
    now: datetime.datetime | None = None,
) -> sys_uuid.UUID | None:
    """Atomically project one external mutation and its immutable mail outbox."""
    stream_uuid = sys_uuid.UUID(str(target_stream_uuid or payload["stream_uuid"]))
    target = validate_workspace_operation(
        session,
        project_uuid=project_uuid,
        owner_user_uuid=owner_user_uuid,
        operation_kind=operation_kind,
        target_stream_uuid=stream_uuid,
    )
    if target is None:
        return None
    if operation_kind == "stream.upsert":
        _update_stream_projection_mapping(
            session,
            target,
            name=payload["name"],
            description=payload["description"],
            private=payload["private"],
            bridge_instance_uuid=bridge_instance_uuid,
        )
    elif operation_kind == "topic.upsert":
        provider_entity_id = _ensure_topic_projection_mapping(
            session,
            target,
            topic_uuid=entity_uuid,
            topic_name=payload["name"],
            bridge_instance_uuid=bridge_instance_uuid,
        )
    elif operation_kind == "message.create":
        topic = session.execute(
            """
            SELECT name FROM m_workspace_stream_topics
            WHERE project_id = %s AND uuid = %s AND stream_uuid = %s
            """,
            (project_uuid, payload["topic_uuid"], stream_uuid),
        ).fetchone()
        if topic is None:
            raise ValueError("Workspace message topic is not available")
        _ensure_topic_projection_mapping(
            session,
            target,
            topic_uuid=payload["topic_uuid"],
            topic_name=topic["name"],
            bridge_instance_uuid=bridge_instance_uuid,
        )
    account_uuid = sys_uuid.UUID(str(target["external_account_uuid"]))
    entity_uuid = sys_uuid.UUID(str(entity_uuid))
    operation_uuid = sys_uuid.UUID(str(operation_uuid))
    existing = session.execute(
        """
        SELECT record_uuid FROM m_external_bridge_mail_outbox_v1
        WHERE operation_uuid = %s
        """,
        (operation_uuid,),
    ).fetchone()
    if existing is not None:
        return sys_uuid.UUID(str(existing["record_uuid"]))

    lane = f"chat:{account_uuid}:{target['chat_uuid']}"
    lane_row = session.execute(
        """
        INSERT INTO m_external_bridge_mail_lanes_v1
            (external_account_uuid, origin, causal_lane, last_sequence)
        VALUES (%s, 'workspace', %s, 0)
        ON CONFLICT (external_account_uuid, origin, causal_lane)
        DO UPDATE SET causal_lane = EXCLUDED.causal_lane
        RETURNING last_sequence, last_operation_uuid
        """,
        (account_uuid, lane),
    ).fetchone()
    sequence = int(lane_row["last_sequence"]) + 1
    predecessor = lane_row["last_operation_uuid"]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    expires_at = now + datetime.timedelta(hours=24)
    record_uuid = sys_uuid.uuid5(operation_uuid, "attempt:1")
    record = {
        "schema": codec.SCHEMA,
        "schema_version": codec.SCHEMA_VERSION,
        "record_kind": "operation",
        "record_uuid": str(record_uuid),
        "operation_uuid": str(operation_uuid),
        "attempt": 1,
        "operation_sha256": "0" * 64,
        "account_uuid": str(account_uuid),
        "project_uuid": str(sys_uuid.UUID(str(project_uuid))),
        "origin": "workspace",
        "causal_lane": lane,
        "sequence": sequence,
        "predecessor_operation_uuid": (
            None if predecessor is None else str(predecessor)
        ),
        "created_at": _iso(now),
        "expires_at": _iso(expires_at),
        "operation": {
            "kind": operation_kind,
            "entity_uuid": str(entity_uuid),
            "actor_uuid": str(sys_uuid.UUID(str(owner_user_uuid))),
            "occurred_at": _iso(now),
            "provider": {
                "kind": target["provider"],
                "chat_id": target["provider_chat_id"],
                "entity_id": provider_entity_id,
                "revision": provider_revision,
            },
            "payload": payload,
            "extensions": {},
        },
    }
    record["operation_sha256"] = codec.operation_sha256(record)
    key = codec.derive_direction_key(
        enrollment_secret,
        realm_uuid,
        bridge_instance_uuid,
        identity_generation,
        "workspace-to-zulip",
    )
    raw_message = codec.build_message(
        record,
        "workspace-to-zulip",
        key,
        WORKSPACE_SENDER,
        BRIDGE_ADDRESS,
    )
    session.execute(
        """
        INSERT INTO m_external_operations_v2 (
            uuid, external_account_uuid, owner_user_uuid, action,
            target_type, target_uuid, details, status, attempt
        ) VALUES (%s, %s, %s, %s, %s, %s,
                  %s::jsonb, 'queued', 1)
        """,
        (
            operation_uuid,
            account_uuid,
            owner_user_uuid,
            operation_kind,
            target_type,
            entity_uuid,
            _json({"record_uuid": str(record_uuid)}),
        ),
    )
    session.execute(
        """
        INSERT INTO m_external_bridge_mail_outbox_v1 (
            record_uuid, operation_uuid, record_kind, attempt,
            external_account_uuid,
            project_uuid, operation_sha256, raw_message
        ) VALUES (%s, %s, 'operation', 1, %s, %s, %s, %s)
        """,
        (
            record_uuid,
            operation_uuid,
            account_uuid,
            project_uuid,
            record["operation_sha256"],
            raw_message,
        ),
    )
    session.execute(
        """
        UPDATE m_external_bridge_mail_lanes_v1
        SET last_sequence = %s, last_operation_uuid = %s
        WHERE external_account_uuid = %s
          AND origin = 'workspace' AND causal_lane = %s
        """,
        (sequence, operation_uuid, account_uuid, lane),
    )
    operation = external_models.ExternalOperation.objects.get_one(
        filters={"uuid": dm_filters.EQ(operation_uuid)},
        session=session,
    )
    update_operation_projection(
        session,
        operation,
        project_uuid,
        event_kind=messenger_events.EXTERNAL_OPERATION_CREATED_EVENT,
    )
    return record_uuid


def queue_message_create(
    session: Any,
    *,
    project_uuid: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    message: dict[str, Any],
    realm_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    identity_generation: int,
    enrollment_secret: bytes,
    now: datetime.datetime | None = None,
) -> sys_uuid.UUID | None:
    account = session.execute(
        """
        SELECT external_account_uuid
        FROM m_external_chats_v2
        WHERE project_id = %s AND owner_user_uuid = %s
          AND projection_stream_uuid = %s AND selected
        LIMIT 1
        """,
        (project_uuid, owner_user_uuid, message["stream_uuid"]),
    ).fetchone()
    if account is None:
        return None
    message_uuid = sys_uuid.UUID(str(message["uuid"]))
    operation_uuid = sys_uuid.uuid5(
        OPERATION_NAMESPACE,
        f"message.create:{account['external_account_uuid']}:{message_uuid}",
    )
    return queue_workspace_operation(
        session,
        project_uuid=project_uuid,
        owner_user_uuid=owner_user_uuid,
        operation_uuid=operation_uuid,
        operation_kind="message.create",
        entity_uuid=message_uuid,
        payload={
            "stream_uuid": str(sys_uuid.UUID(str(message["stream_uuid"]))),
            "topic_uuid": str(sys_uuid.UUID(str(message["topic_uuid"]))),
            "author_uuid": str(sys_uuid.UUID(str(owner_user_uuid))),
            "payload": message["payload"],
            "reply_to_message_uuid": message.get("reply_to_message_uuid"),
        },
        target_type="message",
        realm_uuid=realm_uuid,
        bridge_instance_uuid=bridge_instance_uuid,
        identity_generation=identity_generation,
        enrollment_secret=enrollment_secret,
        now=now,
    )


def queue_manual_retry(
    session: Any,
    *,
    operation_uuid: sys_uuid.UUID,
    attempt: int,
    realm_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    identity_generation: int,
    enrollment_secret: bytes,
    now: datetime.datetime | None = None,
) -> sys_uuid.UUID:
    """Queue one immutable higher attempt for a failed Workspace operation."""
    operation_uuid = sys_uuid.UUID(str(operation_uuid))
    previous = session.execute(
        """
        SELECT raw_message, operation_sha256
        FROM m_external_bridge_mail_outbox_v1
        WHERE operation_uuid = %s AND record_kind = 'operation'
        ORDER BY attempt DESC
        LIMIT 1
        FOR UPDATE
        """,
        (operation_uuid,),
    ).fetchone()
    if previous is None:
        raise ValueError("External operation has no transport record")
    key = codec.derive_direction_key(
        enrollment_secret,
        realm_uuid,
        bridge_instance_uuid,
        identity_generation,
        "workspace-to-zulip",
    )
    previous_record = codec.parse_message(
        bytes(previous["raw_message"]),
        "workspace-to-zulip",
        [key],
        WORKSPACE_SENDER,
        BRIDGE_ADDRESS,
    )
    if (
        previous_record["record_kind"] != "operation"
        or previous_record["operation_uuid"] != str(operation_uuid)
        or previous_record["operation_sha256"] != previous["operation_sha256"]
        or attempt != previous_record["attempt"] + 1
    ):
        raise ValueError("External operation retry state is inconsistent")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    record = copy.deepcopy(previous_record)
    record.update(
        {
            "record_uuid": str(sys_uuid.uuid5(operation_uuid, f"attempt:{attempt}")),
            "attempt": attempt,
            "created_at": _iso(now),
            "expires_at": _iso(now + datetime.timedelta(hours=24)),
        }
    )
    raw_message = codec.build_message(
        record,
        "workspace-to-zulip",
        key,
        WORKSPACE_SENDER,
        BRIDGE_ADDRESS,
    )
    session.execute(
        """
        INSERT INTO m_external_bridge_mail_outbox_v1 (
            record_uuid, operation_uuid, record_kind, attempt,
            external_account_uuid, project_uuid, operation_sha256, raw_message
        ) VALUES (%s, %s, 'operation', %s, %s, %s, %s, %s)
        """,
        (
            record["record_uuid"],
            operation_uuid,
            attempt,
            record["account_uuid"],
            record["project_uuid"],
            record["operation_sha256"],
            raw_message,
        ),
    )
    return sys_uuid.UUID(record["record_uuid"])


def flush_outbox(session: Any, runtime_factory: Any, limit: int = 50) -> int:
    rows = session.execute(
        """
        SELECT record_uuid, external_account_uuid, raw_message
        FROM m_external_bridge_mail_outbox_v1
        WHERE status = 'queued'
        ORDER BY created_at, record_uuid
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        try:
            with runtime_factory.external_bridge_outbox(
                row["external_account_uuid"]
            ) as (client, path):
                client.append(path, bytes(row["raw_message"]))
        except Exception as exc:
            session.execute(
                """
                UPDATE m_external_bridge_mail_outbox_v1
                SET send_attempts = send_attempts + 1,
                    safe_error = %s
                WHERE record_uuid = %s
                """,
                (type(exc).__name__, row["record_uuid"]),
            )
            continue
        session.execute(
            """
            UPDATE m_external_bridge_mail_outbox_v1
            SET status = 'sent', send_attempts = send_attempts + 1,
                safe_error = NULL, sent_at = NOW()
            WHERE record_uuid = %s
            """,
            (row["record_uuid"],),
        )
    return len(rows)


def raw_sha256(raw_message: bytes) -> str:
    return hashlib.sha256(raw_message).hexdigest()


def _authorize_file_urns(
    session: Any,
    content: str,
    *,
    account_uuid: sys_uuid.UUID,
    project_uuid: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
    owner_uuid: sys_uuid.UUID,
) -> None:
    prefixes = list(_FILE_URN_PREFIX_RE.finditer(content))
    matches = list(_FILE_URN_RE.finditer(content))
    if [item.start() for item in prefixes] != [item.start() for item in matches]:
        raise ValueError("invalid_record")
    for match in matches:
        file_uuid = sys_uuid.UUID(match.group(1))
        authorized = session.execute(
            """
            SELECT 1
            FROM m_workspace_files AS file
            JOIN m_workspace_file_accesses AS access
              ON access.project_id = file.project_id
             AND access.file_uuid = file.uuid
            WHERE file.uuid = %s
              AND file.project_id = %s
              AND file.stream_uuid = %s
              AND file.external_account_uuid = %s
              AND access.user_uuid = %s
            LIMIT 1
            """,
            (
                file_uuid,
                project_uuid,
                stream_uuid,
                account_uuid,
                owner_uuid,
            ),
        ).fetchone()
        if authorized is None:
            raise ValueError("permission_denied")


def _advance_cursor(
    session: Any,
    bridge_instance_uuid: sys_uuid.UUID,
    uid_validity: int,
    uid: int,
) -> None:
    session.execute(
        """
        INSERT INTO m_external_bridge_mail_cursors_v1
            (bridge_instance_uuid, mailbox, uid_validity, last_uid)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (bridge_instance_uuid, mailbox) DO UPDATE SET
            uid_validity = EXCLUDED.uid_validity,
            last_uid = CASE
                WHEN m_external_bridge_mail_cursors_v1.uid_validity =
                     EXCLUDED.uid_validity
                THEN GREATEST(
                    m_external_bridge_mail_cursors_v1.last_uid,
                    EXCLUDED.last_uid
                )
                ELSE EXCLUDED.last_uid
            END,
            updated_at = NOW()
        """,
        (bridge_instance_uuid, INGRESS_MAILBOX, uid_validity, uid),
    )


def _quarantine(
    session: Any,
    bridge_instance_uuid: sys_uuid.UUID,
    uid_validity: int,
    fetched: Any,
    reason: str,
) -> None:
    _quarantine_raw(
        session,
        bridge_instance_uuid,
        uid_validity,
        fetched.uid,
        fetched.raw_message,
        reason,
    )


def _quarantine_raw(
    session: Any,
    bridge_instance_uuid: sys_uuid.UUID,
    uid_validity: int,
    uid: int,
    raw_message: bytes,
    reason: str,
) -> None:
    session.execute(
        """
        INSERT INTO m_external_bridge_mail_quarantine_v1 (
            bridge_instance_uuid, mailbox, uid_validity, uid,
            raw_sha256, reason
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            bridge_instance_uuid,
            INGRESS_MAILBOX,
            uid_validity,
            uid,
            raw_sha256(raw_message),
            reason,
        ),
    )
    _advance_cursor(session, bridge_instance_uuid, uid_validity, uid)


def _spool_ingress(
    session: Any,
    bridge_instance_uuid: sys_uuid.UUID,
    uid_validity: int,
    fetched: Any,
    record: dict[str, Any],
) -> None:
    session.execute(
        """
        INSERT INTO m_external_bridge_mail_pending_v1 (
            bridge_instance_uuid, mailbox, uid_validity, uid,
            record_uuid, operation_uuid, external_account_uuid,
            project_uuid, record_kind, origin, causal_lane, sequence,
            predecessor_operation_uuid, operation_sha256, record, raw_message
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s::jsonb, %s
        )
        ON CONFLICT DO NOTHING
        """,
        (
            bridge_instance_uuid,
            INGRESS_MAILBOX,
            uid_validity,
            fetched.uid,
            record["record_uuid"],
            record["operation_uuid"],
            record["account_uuid"],
            record["project_uuid"],
            record["record_kind"],
            record["origin"],
            record["causal_lane"],
            record["sequence"],
            record["predecessor_operation_uuid"],
            record["operation_sha256"],
            _json(record),
            fetched.raw_message,
        ),
    )
    _advance_cursor(session, bridge_instance_uuid, uid_validity, fetched.uid)


def _apply_result(session: Any, record: dict[str, Any]) -> None:
    outbox = session.execute(
        """
        SELECT record_uuid, operation_uuid, external_account_uuid,
               project_uuid, operation_sha256
        FROM m_external_bridge_mail_outbox_v1
        WHERE operation_uuid = %s
          AND record_kind = 'operation' AND attempt = %s
        """,
        (record["operation_uuid"], record["attempt"]),
    ).fetchone()
    if outbox is None:
        raise ValueError("unmatched_result")
    expected = {
        "in_reply_to_record_uuid": str(outbox["record_uuid"]),
        "operation_uuid": str(outbox["operation_uuid"]),
        "account_uuid": str(outbox["external_account_uuid"]),
        "project_uuid": str(outbox["project_uuid"]),
        "operation_sha256": outbox["operation_sha256"],
    }
    if any(record[name] != value for name, value in expected.items()):
        raise ValueError("result_binding_mismatch")
    result = record["result"]
    outcome = result["outcome"]
    if outcome == "committed":
        status = "succeeded"
        safe_error = None
    elif outcome == "cancelled":
        status = "discarded"
        safe_error = result["safe_error"]["message"]
    else:
        status = "failed"
        safe_error = result["safe_error"]["message"]
    session.execute(
        """
        UPDATE m_external_operations_v2
        SET status = %s,
            safe_error = %s,
            can_retry = %s,
            can_discard = %s,
            details = details || %s::jsonb,
            revision = revision + 1,
            updated_at = NOW()
        WHERE uuid = %s AND attempt = %s
        """,
        (
            status,
            safe_error,
            result["manual_retry_allowed"],
            status == "failed",
            _json(
                {
                    "result_record_uuid": record["record_uuid"],
                    "provider_entity_id": result["provider_entity_id"],
                    "provider_revision": result["provider_revision"],
                }
            ),
            record["operation_uuid"],
            record["attempt"],
        ),
    )
    operation = external_models.ExternalOperation.objects.get_one(
        filters={"uuid": dm_filters.EQ(record["operation_uuid"])},
        session=session,
    )
    update_operation_projection(
        session,
        operation,
        outbox["project_uuid"],
    )


def _queue_ingress_result(
    session: Any,
    record: dict[str, Any],
    *,
    realm_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    identity_generation: int,
    enrollment_secret: bytes,
    outcome: str,
    safe_error: dict[str, Any] | None = None,
    now: datetime.datetime | None = None,
) -> sys_uuid.UUID:
    result_uuid = sys_uuid.uuid5(
        sys_uuid.UUID(record["record_uuid"]), "workspace-result:v1"
    )
    existing = session.execute(
        "SELECT 1 FROM m_external_bridge_mail_outbox_v1 WHERE record_uuid = %s",
        (result_uuid,),
    ).fetchone()
    if existing is not None:
        return result_uuid
    now = now or datetime.datetime.now(datetime.timezone.utc)
    result_record = {
        name: record[name]
        for name in (
            "schema",
            "schema_version",
            "operation_uuid",
            "attempt",
            "operation_sha256",
            "account_uuid",
            "project_uuid",
            "origin",
            "causal_lane",
            "sequence",
            "predecessor_operation_uuid",
            "expires_at",
        )
    }
    result_record.update(
        {
            "record_kind": "result",
            "record_uuid": str(result_uuid),
            "in_reply_to_record_uuid": record["record_uuid"],
            "created_at": _iso(now),
            "result": {
                "outcome": outcome,
                "committed_at": _iso(now) if outcome == "committed" else None,
                "provider_entity_id": record["operation"]["provider"]["entity_id"],
                "provider_revision": record["operation"]["provider"]["revision"],
                "safe_error": safe_error,
                "manual_retry_allowed": False,
            },
        }
    )
    key = codec.derive_direction_key(
        enrollment_secret,
        realm_uuid,
        bridge_instance_uuid,
        identity_generation,
        "workspace-to-zulip",
    )
    raw_message = codec.build_message(
        result_record,
        "workspace-to-zulip",
        key,
        WORKSPACE_SENDER,
        BRIDGE_ADDRESS,
    )
    session.execute(
        """
        INSERT INTO m_external_bridge_mail_outbox_v1 (
            record_uuid, operation_uuid, record_kind, attempt,
            external_account_uuid,
            project_uuid, operation_sha256, raw_message
        ) VALUES (%s, %s, 'result', %s, %s, %s, %s, %s)
        """,
        (
            result_uuid,
            record["operation_uuid"],
            record["attempt"],
            record["account_uuid"],
            record["project_uuid"],
            record["operation_sha256"],
            raw_message,
        ),
    )
    return result_uuid


def _apply_inbound_operation(
    session: Any, record: dict[str, Any], runtime_factory: Any
) -> None:
    operation = record["operation"]
    if record["origin"] != "zulip":
        raise ValueError("unsupported_ingress_operation")
    kind = operation["kind"]
    account = session.execute(
        """
        SELECT owner_user_uuid, settings, capabilities, status, live_ready
        FROM m_external_accounts_v2
        WHERE uuid = %s
          AND status IN ('backfill', 'live')
        """,
        (record["account_uuid"],),
    ).fetchone()
    if account is None:
        raise ValueError("result_binding_mismatch")
    _inbound_delivery_class(operation)
    capability = account["capabilities"].get(
        _PUBLIC_CAPABILITY_BY_OPERATION.get(kind, kind)
    )
    if not isinstance(capability, dict) or capability.get("available") is not True:
        raise ValueError("capability_missing")
    owner_uuid = sys_uuid.UUID(str(account["owner_user_uuid"]))
    actor_uuid = sys_uuid.UUID(operation["actor_uuid"])
    if kind == "identity.upsert":
        payload = operation["payload"]
        identity_uuid = sys_uuid.UUID(operation["entity_uuid"])
        if actor_uuid not in {owner_uuid, identity_uuid}:
            raise ValueError("permission_denied")
        user = models.WorkspaceUser.objects.get_one_or_none(
            filters={"uuid": dm_filters.EQ(identity_uuid)}, session=session
        )
        display = payload["display_name"].strip()
        first_name, _, last_name = display.partition(" ")
        values = {
            "username": payload["email"] or f"zulip-{identity_uuid}",
            "source": "zulip",
            "status": "active" if payload["active"] else "offline",
            "first_name": first_name or None,
            "last_name": last_name or None,
            "email": payload["email"],
            "external_account_uuid": sys_uuid.UUID(record["account_uuid"]),
            "provider_external_id": operation["provider"]["entity_id"],
        }
        if payload["avatar_urn"] is not None:
            values["avatar"] = payload["avatar_urn"]
        if user is None:
            models.WorkspaceUser(uuid=identity_uuid, **values).insert(session=session)
        else:
            user.update_dm(values=values)
            user.update(session=session)
        return
    target = session.execute(
        """
        SELECT chat.uuid AS chat_uuid, chat.projection_stream_uuid,
               chat.capabilities, account.settings, topic.name AS topic_name
        FROM m_external_chats_v2 AS chat
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = chat.external_account_uuid
        LEFT JOIN m_workspace_stream_topics AS topic
          ON topic.uuid = %s AND topic.project_id = chat.project_id
        WHERE chat.external_account_uuid = %s
          AND chat.owner_user_uuid = account.owner_user_uuid
          AND chat.project_id = %s
          AND chat.provider_chat_id = %s
          AND chat.selected
          AND chat.status IN ('syncing', 'live')
          AND account.status IN ('backfill', 'live')
        LIMIT 2
        """,
        (
            operation["payload"].get("topic_uuid"),
            record["account_uuid"],
            record["project_uuid"],
            operation["provider"]["chat_id"],
        ),
    ).fetchall()
    if len(target) != 1:
        raise ValueError("result_binding_mismatch")
    target = target[0]
    payload = operation["payload"]
    expected_stream_uuid = (
        operation["entity_uuid"]
        if kind == "stream.upsert"
        else payload.get("stream_uuid")
    )
    if str(target["projection_stream_uuid"]) != expected_stream_uuid:
        raise ValueError("result_binding_mismatch")
    project_uuid = sys_uuid.UUID(record["project_uuid"])
    entity_uuid = sys_uuid.UUID(operation["entity_uuid"])
    if kind == "stream.upsert":
        wanted = {sys_uuid.UUID(value) for value in payload["participant_uuids"]}
        if actor_uuid != owner_uuid and actor_uuid not in wanted:
            raise ValueError("permission_denied")
        if payload["chat_kind"] == "personal_dm" and (
            not payload["private"] or len(wanted) != 2
        ):
            raise ValueError("invalid_record")
        if payload["chat_kind"] == "group_dm" and not payload["private"]:
            raise ValueError("invalid_record")
        if (
            payload["chat_kind"] in {"personal_dm", "group_dm"}
            and payload["default_topic_uuid"] is None
        ):
            raise ValueError("invalid_record")
        existing_bindings = {
            binding.user_uuid: binding
            for binding in models.WorkspaceStreamBinding.objects.get_all(
                filters={
                    "project_id": dm_filters.EQ(project_uuid),
                    "stream_uuid": dm_filters.EQ(entity_uuid),
                },
                session=session,
            )
        }
        binding_uuids = {
            str(user_uuid): (
                existing_bindings[user_uuid].uuid
                if user_uuid in existing_bindings
                else sys_uuid.uuid5(
                    sys_uuid.UUID(record["operation_uuid"]),
                    f"binding:{user_uuid}",
                )
            )
            for user_uuid in wanted
        }
        stream = models.WorkspaceStream.objects.get_one_or_none(
            filters={
                "uuid": dm_filters.EQ(entity_uuid),
                "project_id": dm_filters.EQ(project_uuid),
            },
            session=session,
        )
        provider_stream_id = _provider_stream_id(operation["provider"]["chat_id"])
        source = {
            "kind": "zulip",
            "stream_id": provider_stream_id,
            "server_url": account["settings"].get("server_url"),
        }
        canonical_records = []
        with runtime_factory.messenger_service(project_uuid) as service:
            service.repository.refresh()
            projection = service.repository.projection
            canonical_records.append(
                _canonical_record(
                    record,
                    (
                        "stream.update"
                        if entity_uuid in projection.streams
                        else "stream.create"
                    ),
                    entity_uuid,
                    {
                        "name": payload["name"],
                        "description": payload["description"],
                        "kind": (
                            "direct"
                            if payload["chat_kind"] == "personal_dm"
                            else "stream"
                        ),
                        "private": payload["private"],
                        "invite_only": payload["private"],
                        "default_topic_uuid": payload["default_topic_uuid"],
                        "source_name": "zulip",
                        "source": source,
                    },
                    "stream",
                )
            )
            projected_by_user = {
                sys_uuid.UUID(value["user_uuid"]): (binding_uuid, value)
                for binding_uuid, value in projection.bindings.items()
                if value["stream_uuid"] == str(entity_uuid)
            }
            for user_uuid in wanted - set(projected_by_user):
                canonical_records.append(
                    _canonical_record(
                        record,
                        "binding.create",
                        binding_uuids[str(user_uuid)],
                        {
                            "stream_uuid": str(entity_uuid),
                            "user_uuid": str(user_uuid),
                            "who_uuid": str(owner_uuid),
                            "role": "owner" if user_uuid == owner_uuid else "member",
                            "notification_mode": "all_messages",
                        },
                        f"binding-create:{user_uuid}",
                    )
                )
            for user_uuid in set(projected_by_user) - wanted:
                binding_uuid, _value = projected_by_user[user_uuid]
                canonical_records.append(
                    _canonical_record(
                        record,
                        "binding.delete",
                        binding_uuid,
                        {},
                        f"binding-delete:{user_uuid}",
                    )
                )
            default_topic_uuid = (
                None
                if payload["default_topic_uuid"] is None
                else sys_uuid.UUID(payload["default_topic_uuid"])
            )
            if (
                default_topic_uuid is not None
                and default_topic_uuid not in projection.topics
            ):
                canonical_records.append(
                    _canonical_record(
                        record,
                        "topic.create",
                        default_topic_uuid,
                        {
                            "stream_uuid": str(entity_uuid),
                            "name": "General",
                            "source_name": "zulip",
                            "source": {**source, "topic_name": "General"},
                        },
                        "default-topic",
                    )
                )
        stream_values = {
            "name": payload["name"],
            "description": payload["description"],
            "private": payload["private"],
            "invite_only": payload["private"],
            "source_name": "zulip",
            "source": models.ZulipSource(
                stream_id=provider_stream_id,
                server_url=account["settings"].get("server_url"),
            ),
            "external_account_uuid": sys_uuid.UUID(record["account_uuid"]),
            "provider_external_id": operation["provider"]["chat_id"],
            "provider_metadata": {
                **_inbound_provider_metadata(record, target, account),
                "external_id": operation["provider"]["chat_id"],
            },
        }
        if stream is None:
            helpers.get_or_create_workspace_user_stream(
                project_id=project_uuid,
                user_uuid=owner_uuid,
                session=session,
                uuid=entity_uuid,
                canonical_default_topic_uuid=default_topic_uuid,
                create_default_topic=default_topic_uuid is not None,
                default_topic_name="General",
                canonical_binding_uuids=binding_uuids,
                **stream_values,
            )
        else:
            stream.update_dm(values=stream_values)
            stream.update(session=session)
        for user_uuid in wanted - set(existing_bindings):
            helpers._get_or_create_workspace_stream_binding(
                project_id=project_uuid,
                stream_uuid=entity_uuid,
                user_uuid=user_uuid,
                who_uuid=owner_uuid,
                role="member",
                session=session,
                uuid=binding_uuids[str(user_uuid)],
            )
        for user_uuid in set(existing_bindings) - wanted:
            helpers.delete_workspace_stream_binding(
                project_uuid,
                existing_bindings[user_uuid].uuid,
                session=session,
            )
        with runtime_factory.messenger_service(project_uuid) as service:
            service.repository.refresh()
            for canonical_record in canonical_records:
                service.repository.append_operation(canonical_record)
        if stream is not None:
            _emit_target_updated_events(session, project_uuid, "stream", entity_uuid)
        _update_stream_projection_mapping(
            session,
            target,
            name=payload["name"],
            description=payload["description"],
            private=payload["private"],
        )
        return
    actor_binding = models.WorkspaceStreamBinding.objects.get_one_or_none(
        filters={
            "project_id": dm_filters.EQ(project_uuid),
            "stream_uuid": dm_filters.EQ(
                sys_uuid.UUID(str(target["projection_stream_uuid"]))
            ),
            "user_uuid": dm_filters.EQ(actor_uuid),
        },
        session=session,
    )
    if actor_uuid != owner_uuid and actor_binding is None:
        raise ValueError("permission_denied")
    if kind in {"message.create", "message.update"}:
        _authorize_file_urns(
            session,
            payload["payload"]["content"],
            account_uuid=sys_uuid.UUID(record["account_uuid"]),
            project_uuid=project_uuid,
            stream_uuid=sys_uuid.UUID(payload["stream_uuid"]),
            owner_uuid=owner_uuid,
        )
    if kind == "stream.delete":
        with runtime_factory.messenger_service(project_uuid) as service:
            service.repository.refresh()
            service.repository.append_operation(
                _canonical_record(
                    record,
                    "stream.delete",
                    entity_uuid,
                    {},
                    "stream",
                )
            )
        stream = models.WorkspaceStream.objects.get_one_or_none(
            filters={
                "uuid": dm_filters.EQ(entity_uuid),
                "project_id": dm_filters.EQ(project_uuid),
            },
            session=session,
        )
        if stream is not None:
            stream.delete(session=session)
        return
    if kind == "topic.upsert":
        topic = models.WorkspaceStreamTopic.objects.get_one_or_none(
            filters={
                "uuid": dm_filters.EQ(entity_uuid),
                "project_id": dm_filters.EQ(project_uuid),
            },
            session=session,
        )
        provider_stream_id = _provider_stream_id(operation["provider"]["chat_id"])
        topic_source = {
            "kind": "zulip",
            "stream_id": provider_stream_id,
            "server_url": account["settings"].get("server_url"),
            "topic_name": payload["name"],
        }
        with runtime_factory.messenger_service(project_uuid) as service:
            service.repository.refresh()
            service.repository.append_operation(
                _canonical_record(
                    record,
                    "topic.update" if topic is not None else "topic.create",
                    entity_uuid,
                    {
                        "stream_uuid": payload["stream_uuid"],
                        "name": payload["name"],
                        "source_name": "zulip",
                        "source": topic_source,
                    },
                    "topic",
                )
            )
        if topic is None:
            helpers.create_workspace_user_stream_topic(
                project_uuid,
                owner_uuid,
                {
                    "uuid": entity_uuid,
                    "stream_uuid": sys_uuid.UUID(payload["stream_uuid"]),
                    "name": payload["name"],
                    "source_name": "zulip",
                    "source": models.ZulipSource(
                        stream_id=provider_stream_id,
                        server_url=topic_source["server_url"],
                        topic_name=payload["name"],
                    ),
                    "external_account_uuid": sys_uuid.UUID(record["account_uuid"]),
                    "provider_external_id": operation["provider"]["entity_id"],
                    "provider_metadata": _inbound_provider_metadata(
                        record, target, account
                    ),
                },
                session=session,
            )
        else:
            topic.update_dm(
                values={
                    "name": payload["name"],
                    "source": models.ZulipSource(
                        stream_id=provider_stream_id,
                        server_url=topic_source["server_url"],
                        topic_name=payload["name"],
                    ),
                    "provider_metadata": _inbound_provider_metadata(
                        record, target, account
                    ),
                }
            )
            topic.update(session=session)
            _emit_target_updated_events(session, project_uuid, "topic", entity_uuid)
        _ensure_topic_projection_mapping(
            session,
            target,
            topic_uuid=entity_uuid,
            topic_name=payload["name"],
            bridge_instance_uuid=None,
        )
        return
    if kind == "topic.delete":
        with runtime_factory.messenger_service(project_uuid) as service:
            service.repository.refresh()
            service.repository.append_operation(
                _canonical_record(
                    record,
                    "topic.delete",
                    entity_uuid,
                    {},
                    "topic",
                )
            )
        topic = models.WorkspaceStreamTopic.objects.get_one_or_none(
            filters={
                "uuid": dm_filters.EQ(entity_uuid),
                "project_id": dm_filters.EQ(project_uuid),
            },
            session=session,
        )
        if topic is not None:
            topic.delete(session=session)
        return
    if kind in {"message.update", "message.delete"}:
        author_uuid = sys_uuid.UUID(payload["author_uuid"])
        if actor_uuid != author_uuid:
            raise ValueError("permission_denied")
        canonical_record = mail_repository.OperationRecord(
            project_uuid=project_uuid,
            operation_uuid=sys_uuid.UUID(record["operation_uuid"]),
            actor_uuid=actor_uuid,
            operation=kind,
            entity_uuid=entity_uuid,
            payload=(
                {"payload": payload["payload"]} if kind == "message.update" else {}
            ),
            occurred_at=datetime.datetime.fromisoformat(
                operation["occurred_at"].replace("Z", "+00:00")
            ),
        )
        with runtime_factory.messenger_service(project_uuid) as service:
            service.repository.refresh()
            if kind == "message.update":
                service.repository.append_operation(canonical_record)
            else:
                service.delete_message(canonical_record)
        if kind == "message.update":
            message = models.WorkspaceMessage.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(entity_uuid),
                    "project_id": dm_filters.EQ(project_uuid),
                    "user_uuid": dm_filters.EQ(author_uuid),
                },
                session=session,
            )
            message.update_dm(
                values={
                    "payload": message_payloads.MarkdownPayload(
                        content=payload["payload"]["content"]
                    ),
                    "provider_metadata": _inbound_provider_metadata(
                        record,
                        target,
                        account,
                        current=message.provider_metadata,
                    ),
                }
            )
            message.update(session=session)
            _emit_target_updated_events(session, project_uuid, "message", entity_uuid)
        else:
            helpers.delete_workspace_user_message(
                project_uuid,
                author_uuid,
                entity_uuid,
                session=session,
                enforce_visibility=False,
            )
        return
    if kind == "read_state.set":
        reader_uuid = sys_uuid.UUID(payload["reader_uuid"])
        if actor_uuid != reader_uuid:
            raise ValueError("permission_denied")
        filters = {
            "project_id": dm_filters.EQ(project_uuid),
            "stream_uuid": dm_filters.EQ(sys_uuid.UUID(payload["stream_uuid"])),
        }
        if payload.get("topic_uuid") is not None:
            filters["topic_uuid"] = dm_filters.EQ(sys_uuid.UUID(payload["topic_uuid"]))
        messages = models.WorkspaceMessage.objects.get_all(
            filters=filters,
            order_by={"created_at": "asc", "uuid": "asc"},
            session=session,
        )
        if "message_uuids" in payload:
            exact_uuids = {
                sys_uuid.UUID(message_uuid) for message_uuid in payload["message_uuids"]
            }
            messages = [message for message in messages if message.uuid in exact_uuids]
            if {message.uuid for message in messages} != exact_uuids:
                raise ValueError("result_binding_mismatch")
        else:
            through_uuid = payload["through_message_uuid"]
            boundary_uuid = sys_uuid.UUID(through_uuid)
            boundary = next(
                (message for message in messages if message.uuid == boundary_uuid),
                None,
            )
            if boundary is None:
                raise ValueError("result_binding_mismatch")
            boundary_key = (boundary.created_at, boundary.uuid)
            messages = [
                message
                for message in messages
                if (message.created_at, message.uuid) <= boundary_key
            ]
        canonical_operation = "message.read" if payload["read"] else "message.unread"
        with runtime_factory.messenger_service(project_uuid) as service:
            service.repository.refresh()
            for message in messages:
                service.repository.append_operation(
                    _canonical_record(
                        record,
                        canonical_operation,
                        message.uuid,
                        {"user_uuid": str(reader_uuid)},
                        f"read-state:{message.uuid}",
                    )
                )
        for message in messages:
            helpers.sync_workspace_user_message_flags(
                project_uuid,
                reader_uuid,
                message.uuid,
                {"read": payload["read"]},
                session=session,
            )
        return
    if kind != "message.create":
        raise ValueError("unsupported_ingress_operation")
    message_uuid = sys_uuid.UUID(operation["entity_uuid"])
    author_uuid = sys_uuid.UUID(payload["author_uuid"])
    if actor_uuid != author_uuid:
        raise ValueError("permission_denied")
    provider_entity_id = operation["provider"]["entity_id"]
    chat_id = operation["provider"]["chat_id"]
    stream_id = _provider_stream_id(chat_id)
    try:
        message_id = int(provider_entity_id)
    except (TypeError, ValueError):
        message_id = None
    source = {
        "kind": "zulip",
        "stream_id": stream_id,
        "server_url": target["settings"].get("server_url"),
        "topic_name": target["topic_name"],
        "message_id": message_id,
    }
    canonical_payload = {
        "stream_uuid": payload["stream_uuid"],
        "topic_uuid": payload["topic_uuid"],
        "author_uuid": payload["author_uuid"],
        "payload": payload["payload"],
        "reply_to_message_uuid": payload.get("reply_to_message_uuid"),
        "source_name": "zulip",
        "source": source,
    }
    canonical_record = mail_repository.OperationRecord(
        project_uuid=project_uuid,
        operation_uuid=sys_uuid.UUID(record["operation_uuid"]),
        actor_uuid=author_uuid,
        operation="message.create",
        entity_uuid=message_uuid,
        payload=canonical_payload,
        occurred_at=datetime.datetime.fromisoformat(
            operation["occurred_at"].replace("Z", "+00:00")
        ),
    )
    with runtime_factory.messenger_service(project_uuid) as service:
        service.repository.refresh()
        service.deliver_message(canonical_record)
    existing_message = models.WorkspaceMessage.objects.get_one_or_none(
        filters={
            "project_id": dm_filters.EQ(project_uuid),
            "uuid": dm_filters.EQ(message_uuid),
        },
        session=session,
    )
    if existing_message is None:
        helpers.create_workspace_user_message(
            project_id=project_uuid,
            user_uuid=author_uuid,
            uuid=message_uuid,
            stream_uuid=sys_uuid.UUID(payload["stream_uuid"]),
            topic_uuid=sys_uuid.UUID(payload["topic_uuid"]),
            payload=message_payloads.MarkdownPayload(
                content=payload["payload"]["content"]
            ),
            source_name="zulip",
            source=models.ZulipSource(
                stream_id=stream_id,
                server_url=source["server_url"],
                topic_name=source["topic_name"],
                message_id=message_id,
            ),
            external_account_uuid=sys_uuid.UUID(record["account_uuid"]),
            provider_external_id=provider_entity_id,
            provider_metadata=_inbound_provider_metadata(record, target, account),
            session=session,
            return_visible=False,
        )


def _remember_ingress_record(session: Any, record: dict[str, Any]) -> None:
    session.execute(
        """
        INSERT INTO m_external_bridge_mail_records_v1 (
            record_uuid, operation_uuid, external_account_uuid,
            direction, operation_sha256
        ) VALUES (%s, %s, %s, 'zulip-to-workspace', %s)
        ON CONFLICT (record_uuid) DO NOTHING
        """,
        (
            record["record_uuid"],
            record["operation_uuid"],
            record["account_uuid"],
            record["operation_sha256"],
        ),
    )


def _delete_pending_ingress(session: Any, row: dict[str, Any]) -> None:
    session.execute(
        """
        DELETE FROM m_external_bridge_mail_pending_v1
        WHERE bridge_instance_uuid = %s AND mailbox = %s
          AND uid_validity = %s AND uid = %s
        """,
        (
            row["bridge_instance_uuid"],
            row["mailbox"],
            row["uid_validity"],
            row["uid"],
        ),
    )


def _pending_rejection_reason(exc: ValueError) -> str:
    reason = str(exc)
    if reason not in {
        "capability_missing",
        "permission_denied",
        "unmatched_result",
        "result_binding_mismatch",
        "unsupported_ingress_operation",
    }:
        return "invalid_record"
    return reason


def _quarantine_pending(session: Any, row: dict[str, Any], reason: str) -> None:
    _quarantine_raw(
        session,
        row["bridge_instance_uuid"],
        row["uid_validity"],
        row["uid"],
        bytes(row["raw_message"]),
        reason,
    )


def _process_pending_ingress_row(
    session: Any,
    row: dict[str, Any],
    runtime_factory: Any,
    *,
    realm_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    identity_generation: int,
    enrollment_secret: bytes,
    operation_handler: Callable[[Any, dict[str, Any]], None] | None,
) -> bool:
    record = row["record"]
    known_record = session.execute(
        """
        SELECT 1 FROM m_external_bridge_mail_records_v1
        WHERE record_uuid = %s
        """,
        (record["record_uuid"],),
    ).fetchone()
    if known_record is not None:
        _delete_pending_ingress(session, row)
        return True

    known_operation = session.execute(
        """
        SELECT operation_sha256
        FROM m_external_bridge_mail_records_v1
        WHERE operation_uuid = %s AND direction = 'zulip-to-workspace'
        ORDER BY processed_at
        LIMIT 1
        """,
        (record["operation_uuid"],),
    ).fetchone()
    if known_operation is not None:
        if known_operation["operation_sha256"] != record["operation_sha256"]:
            _quarantine_pending(session, row, "invalid_record")
        _delete_pending_ingress(session, row)
        return True

    if record["record_kind"] == "result":
        try:
            _apply_result(session, record)
        except ValueError as exc:
            _quarantine_pending(session, row, _pending_rejection_reason(exc))
        else:
            _remember_ingress_record(session, record)
        _delete_pending_ingress(session, row)
        return True

    account_uuid = sys_uuid.UUID(record["account_uuid"])
    origin = record["origin"]
    causal_lane = record["causal_lane"]
    session.execute(
        """
        INSERT INTO m_external_bridge_mail_lanes_v1 (
            external_account_uuid, origin, causal_lane, last_sequence
        ) VALUES (%s, %s, %s, 0)
        ON CONFLICT DO NOTHING
        """,
        (account_uuid, origin, causal_lane),
    )
    lane = session.execute(
        """
        SELECT last_sequence, last_operation_uuid
        FROM m_external_bridge_mail_lanes_v1
        WHERE external_account_uuid = %s AND origin = %s AND causal_lane = %s
        FOR UPDATE
        """,
        (account_uuid, origin, causal_lane),
    ).fetchone()
    sequence = int(record["sequence"])
    expected_sequence = int(lane["last_sequence"]) + 1
    predecessor = record["predecessor_operation_uuid"]
    expected_predecessor = lane["last_operation_uuid"]
    if sequence < expected_sequence:
        _quarantine_pending(session, row, "invalid_record")
        _delete_pending_ingress(session, row)
        return True
    if sequence != expected_sequence or (
        (predecessor is None) != (expected_predecessor is None)
        or (
            predecessor is not None
            and sys_uuid.UUID(predecessor) != expected_predecessor
        )
    ):
        return False

    try:
        if operation_handler is not None:
            operation_handler(session, record)
        else:
            _apply_inbound_operation(session, record, runtime_factory)
    except ValueError as exc:
        reason = _pending_rejection_reason(exc)
        error_code = (
            reason
            if reason in {"capability_missing", "permission_denied", "invalid_record"}
            else "conflict"
        )
        _queue_ingress_result(
            session,
            record,
            realm_uuid=realm_uuid,
            bridge_instance_uuid=bridge_instance_uuid,
            identity_generation=identity_generation,
            enrollment_secret=enrollment_secret,
            outcome="rejected",
            safe_error={
                "code": error_code,
                "message": "Workspace rejected the bridge operation",
            },
        )
        _quarantine_pending(session, row, reason)
    else:
        _queue_ingress_result(
            session,
            record,
            realm_uuid=realm_uuid,
            bridge_instance_uuid=bridge_instance_uuid,
            identity_generation=identity_generation,
            enrollment_secret=enrollment_secret,
            outcome="committed",
        )
    _remember_ingress_record(session, record)
    session.execute(
        """
        UPDATE m_external_bridge_mail_lanes_v1
        SET last_sequence = %s, last_operation_uuid = %s
        WHERE external_account_uuid = %s AND origin = %s AND causal_lane = %s
        """,
        (
            sequence,
            record["operation_uuid"],
            account_uuid,
            origin,
            causal_lane,
        ),
    )
    _delete_pending_ingress(session, row)
    return True


def _process_pending_ingress(
    session: Any,
    runtime_factory: Any,
    *,
    realm_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    identity_generation: int,
    enrollment_secret: bytes,
    operation_handler: Callable[[Any, dict[str, Any]], None] | None,
    limit: int,
) -> int:
    processed = 0
    while processed < limit:
        keys = session.execute(
            """
            SELECT bridge_instance_uuid, mailbox, uid_validity, uid
            FROM m_external_bridge_mail_pending_v1
            WHERE bridge_instance_uuid = %s
            ORDER BY (record_kind = 'result') DESC, created_at, uid
            """,
            (bridge_instance_uuid,),
        ).fetchall()
        if not keys:
            break
        made_progress = False
        for key in keys:
            if processed >= limit:
                break
            row = session.execute(
                """
                SELECT * FROM m_external_bridge_mail_pending_v1
                WHERE bridge_instance_uuid = %s AND mailbox = %s
                  AND uid_validity = %s AND uid = %s
                FOR UPDATE SKIP LOCKED
                """,
                (
                    key["bridge_instance_uuid"],
                    key["mailbox"],
                    key["uid_validity"],
                    key["uid"],
                ),
            ).fetchone()
            if row is None:
                continue
            row_processed = _process_pending_ingress_row(
                session,
                row,
                runtime_factory,
                realm_uuid=realm_uuid,
                bridge_instance_uuid=bridge_instance_uuid,
                identity_generation=identity_generation,
                enrollment_secret=enrollment_secret,
                operation_handler=operation_handler,
            )
            if row_processed:
                processed += 1
                made_progress = True
        if not made_progress:
            break
    return processed


def consume_ingress(
    session: Any,
    runtime_factory: Any,
    *,
    realm_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    identity_generation: int,
    enrollment_secret: bytes,
    operation_handler: Callable[[Any, dict[str, Any]], None] | None = None,
    limit: int = 100,
) -> int:
    """Consume signed ingress without allowing one poison UID to stop progress."""
    with runtime_factory.external_bridge_ingress() as client:
        metadata = client.select(INGRESS_MAILBOX, readonly=True)
        if metadata.uid_validity is None:
            raise RuntimeError("Ingress mailbox has no UIDVALIDITY")
        cursor = session.execute(
            """
            SELECT uid_validity, last_uid
            FROM m_external_bridge_mail_cursors_v1
            WHERE bridge_instance_uuid = %s AND mailbox = %s
            """,
            (bridge_instance_uuid, INGRESS_MAILBOX),
        ).fetchone()
        last_uid = (
            int(cursor["last_uid"])
            if cursor is not None
            and int(cursor["uid_validity"]) == metadata.uid_validity
            else 0
        )
        uids = client.search(f"UID {last_uid + 1}:*")[:limit]
        fetched_messages = sorted(client.fetch(uids), key=lambda item: item.uid)

    key = codec.derive_direction_key(
        enrollment_secret,
        realm_uuid,
        bridge_instance_uuid,
        identity_generation,
        "zulip-to-workspace",
    )
    processed = 0
    for fetched in fetched_messages:
        if fetched.uid <= last_uid:
            continue
        try:
            record = codec.parse_message(
                fetched.raw_message,
                "zulip-to-workspace",
                [key],
                BRIDGE_ADDRESS,
                INGRESS_ADDRESS,
            )
        except (codec.InvalidExternalBridgeRecord, ValueError):
            _quarantine(
                session,
                bridge_instance_uuid,
                metadata.uid_validity,
                fetched,
                "invalid_record",
            )
            last_uid = fetched.uid
            processed += 1
            continue
        _spool_ingress(
            session,
            bridge_instance_uuid,
            metadata.uid_validity,
            fetched,
            record,
        )
        last_uid = fetched.uid
        processed += 1
    _process_pending_ingress(
        session,
        runtime_factory,
        realm_uuid=realm_uuid,
        bridge_instance_uuid=bridge_instance_uuid,
        identity_generation=identity_generation,
        enrollment_secret=enrollment_secret,
        operation_handler=operation_handler,
        limit=limit,
    )
    return processed
