# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""One-time read-only conversion of legacy mail outbox operations.

The legacy tables remain immutable. Conversion either validates every required
operation before inserting any Provider API queue row, or returns a blocking
report without changing the new queue.
"""

import datetime
import uuid as sys_uuid
from typing import Any

from workspace.messenger_mail import external_bridge_codec
from workspace.messenger_mail import external_bridge_data_plane


_PROVIDER_OPERATION_KIND = {
    "message.create": "message.create",
    "message.update": "message.update",
    "message.delete": "message.delete",
    "stream.upsert": "stream.update",
    "stream.delete": "stream.delete",
    "topic.upsert": "topic.update",
    "topic.delete": "topic.delete",
}
_RETRYABLE_PUBLIC_STATUSES = frozenset({"failed", "manual_reconciliation_required"})


def _required_rows(
    session: Any,
    project_id: sys_uuid.UUID,
) -> list[dict[str, Any]]:
    return session.execute(
        """
        SELECT public."uuid" AS "external_operation_uuid",
               public."status" AS "public_status",
               public."attempt" AS "public_attempt",
               public."can_retry", public."safe_error",
               public."external_account_uuid", public."owner_user_uuid",
               public."action", public."target_type", public."target_uuid",
               provider."uuid" AS "provider_operation_uuid",
               provider."bridge_instance_uuid" AS "provider_bridge_instance_uuid",
               provider."external_account_uuid" AS "provider_external_account_uuid",
               provider."project_id" AS "provider_project_id",
               provider."operation_kind" AS "provider_operation_kind",
               provider."payload" AS "provider_payload",
               provider."status" AS "provider_status",
               provider."attempt" AS "provider_attempt",
               provider."safe_error" AS "provider_safe_error",
               provider."created_at" AS "provider_created_at",
               provider."completed_at" AS "provider_completed_at",
               legacy."record_uuid", legacy."attempt" AS "legacy_attempt",
               legacy."project_uuid", legacy."operation_sha256",
               legacy."raw_message", legacy."status" AS "legacy_status",
               legacy."created_at", legacy."sent_at"
        FROM "m_external_operations_v2" AS public
        JOIN "m_external_accounts_v2" AS account
          ON account."uuid" = public."external_account_uuid"
        LEFT JOIN "m_external_provider_operations_v1" AS provider
          ON provider."external_operation_uuid" = public."uuid"
        LEFT JOIN LATERAL (
            SELECT outbox.*
            FROM "m_external_bridge_mail_outbox_v1" AS outbox
            WHERE outbox."operation_uuid" = public."uuid"
              AND outbox."record_kind" = 'operation'
            ORDER BY outbox."attempt" DESC, outbox."created_at" DESC,
                     outbox."record_uuid" DESC
            LIMIT 1
        ) AS legacy ON TRUE
        WHERE (
                public."status" IN ('queued', 'running')
                OR public."can_retry"
              )
          AND (
                provider."project_id" = %s
                OR legacy."project_uuid" = %s
                OR NULLIF(
                    account."settings"->>'default_project_id', ''
                )::uuid = %s
              )
        ORDER BY public."created_at", public."uuid"
        """,
        (project_id, project_id, project_id),
    ).fetchall()


def _provider_payload(record: dict[str, Any]) -> dict[str, Any]:
    operation = record["operation"]
    payload = dict(operation["payload"])
    payload["uuid"] = operation["entity_uuid"]
    payload.setdefault("user_uuid", operation["actor_uuid"])
    timestamp_field = (
        "created_at" if operation["kind"].endswith(".create") else "updated_at"
    )
    payload.setdefault(timestamp_field, operation["occurred_at"])
    return payload


def _blocker(row: dict[str, Any], code: str) -> dict[str, str]:
    return {
        "external_operation_uuid": str(row["external_operation_uuid"]),
        "code": code,
    }


def _provider_row_matches(
    row: dict[str, Any],
    item: dict[str, Any],
) -> bool:
    return all(
        (
            row["provider_operation_uuid"] == item["uuid"],
            row["provider_bridge_instance_uuid"] == item["bridge_instance_uuid"],
            row["provider_external_account_uuid"] == item["external_account_uuid"],
            row["provider_project_id"] == item["project_id"],
            row["provider_operation_kind"] == item["operation_kind"],
            row["provider_payload"] == item["payload"],
            row["provider_status"] == item["status"],
            row["provider_attempt"] == item["attempt"],
            row["provider_safe_error"] == item["safe_error"],
            row["provider_created_at"] == item["created_at"],
            row["provider_completed_at"] == item["completed_at"],
        )
    )


def _conversion_plan(
    rows: list[dict[str, Any]],
    *,
    realm_uuid: str | sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    identity_generation: int,
    enrollment_secret: str | bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    key = external_bridge_codec.derive_direction_key(
        enrollment_secret,
        realm_uuid,
        bridge_instance_uuid,
        identity_generation,
        "workspace-to-zulip",
    )
    plan: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    already_provider = 0
    for row in rows:
        if row["provider_operation_uuid"] is not None and row["raw_message"] is None:
            already_provider += 1
            continue
        if row["raw_message"] is None:
            blockers.append(_blocker(row, "legacy_transport_record_missing"))
            continue
        try:
            record = external_bridge_codec.parse_message(
                bytes(row["raw_message"]),
                "workspace-to-zulip",
                [key],
                external_bridge_data_plane.WORKSPACE_SENDER,
                external_bridge_data_plane.BRIDGE_ADDRESS,
            )
        except (TypeError, ValueError):
            blockers.append(_blocker(row, "legacy_transport_record_invalid"))
            continue
        if (
            record["record_kind"] != "operation"
            or record["record_uuid"] != str(row["record_uuid"])
            or record["operation_uuid"] != str(row["external_operation_uuid"])
            or record["account_uuid"] != str(row["external_account_uuid"])
            or record["project_uuid"] != str(row["project_uuid"])
            or record["attempt"] != row["legacy_attempt"]
            or record["operation_sha256"] != row["operation_sha256"]
        ):
            blockers.append(_blocker(row, "legacy_transport_record_mismatch"))
            continue
        provider_kind = _PROVIDER_OPERATION_KIND.get(record["operation"]["kind"])
        if provider_kind is None:
            blockers.append(_blocker(row, "legacy_operation_kind_unsupported"))
            continue
        operation = record["operation"]
        expected_target_type = operation["kind"].split(".", maxsplit=1)[0]
        if (
            operation["kind"] != row["action"]
            or operation["entity_uuid"] != str(row["target_uuid"])
            or operation["actor_uuid"] != str(row["owner_user_uuid"])
            or expected_target_type != row["target_type"]
        ):
            blockers.append(_blocker(row, "legacy_public_operation_mismatch"))
            continue
        if row["public_attempt"] != row["legacy_attempt"]:
            blockers.append(_blocker(row, "legacy_attempt_mismatch"))
            continue
        public_status = row["public_status"]
        if public_status in {"queued", "running"} and row["legacy_status"] != "queued":
            blockers.append(_blocker(row, "legacy_delivery_state_ambiguous"))
            continue
        if public_status == "running":
            blockers.append(_blocker(row, "legacy_delivery_state_ambiguous"))
            continue
        if public_status in _RETRYABLE_PUBLIC_STATUSES or row["can_retry"]:
            provider_status = "failed"
            provider_attempt = row["legacy_attempt"]
            completed_at = row["sent_at"] or row["created_at"]
        else:
            provider_status = "queued"
            provider_attempt = row["legacy_attempt"] - 1
            completed_at = None
        item = {
            "uuid": sys_uuid.UUID(record["record_uuid"]),
            "external_operation_uuid": row["external_operation_uuid"],
            "bridge_instance_uuid": bridge_instance_uuid,
            "external_account_uuid": row["external_account_uuid"],
            "project_id": row["project_uuid"],
            "operation_kind": provider_kind,
            "payload": _provider_payload(record),
            "status": provider_status,
            "attempt": provider_attempt,
            "safe_error": row["safe_error"],
            "created_at": row["created_at"],
            "completed_at": completed_at,
        }
        if row["provider_operation_uuid"] is not None:
            if _provider_row_matches(row, item):
                already_provider += 1
            else:
                blockers.append(_blocker(row, "provider_operation_mismatch"))
            continue
        plan.append(item)
    return plan, blockers, already_provider


def convert_required_operations(
    session: Any,
    *,
    project_id: str | sys_uuid.UUID,
    realm_uuid: str | sys_uuid.UUID,
    bridge_instance_uuid: str | sys_uuid.UUID,
    identity_generation: int,
    enrollment_secret: str | bytes,
) -> dict[str, Any]:
    """Convert all safe rows or return an atomic fail-closed report."""
    project_id = sys_uuid.UUID(str(project_id))
    bridge_instance_uuid = sys_uuid.UUID(str(bridge_instance_uuid))
    rows = _required_rows(session, project_id)
    plan, blockers, already_provider = _conversion_plan(
        rows,
        realm_uuid=realm_uuid,
        bridge_instance_uuid=bridge_instance_uuid,
        identity_generation=identity_generation,
        enrollment_secret=enrollment_secret,
    )
    report: dict[str, Any] = {
        "project_id": str(project_id),
        "required": len(rows),
        "already_provider": already_provider,
        "converted": 0,
        "blockers": blockers,
        "ok": not blockers,
    }
    if blockers:
        return report
    now = datetime.datetime.now(datetime.timezone.utc)
    for item in plan:
        inserted = session.execute(
            """
            INSERT INTO "m_external_provider_operations_v1" (
                "uuid", "external_operation_uuid", "bridge_instance_uuid",
                "external_account_uuid", "project_id", "operation_kind",
                "payload", "status", "attempt", "available_at",
                "safe_error", "created_at", "updated_at", "completed_at"
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT ("external_operation_uuid") DO NOTHING
            RETURNING "uuid"
            """,
            (
                item["uuid"],
                item["external_operation_uuid"],
                item["bridge_instance_uuid"],
                item["external_account_uuid"],
                item["project_id"],
                item["operation_kind"],
                external_bridge_codec.canonical_json(item["payload"]).decode(),
                item["status"],
                item["attempt"],
                now,
                item["safe_error"],
                item["created_at"],
                now,
                item["completed_at"],
            ),
        ).fetchone()
        if inserted is None:
            existing = session.execute(
                """
                SELECT "uuid" FROM "m_external_provider_operations_v1"
                WHERE "external_operation_uuid" = %s
                """,
                (item["external_operation_uuid"],),
            ).fetchone()
            if existing is None or existing["uuid"] != item["uuid"]:
                raise ValueError("Provider queue changed during legacy conversion")
            continue
        report["converted"] += 1
    parity_rows = _required_rows(session, project_id)
    pending, parity_blockers, provider_ready = _conversion_plan(
        parity_rows,
        realm_uuid=realm_uuid,
        bridge_instance_uuid=bridge_instance_uuid,
        identity_generation=identity_generation,
        enrollment_secret=enrollment_secret,
    )
    report["provider_ready"] = provider_ready
    if (
        len(parity_rows) != report["required"]
        or pending
        or parity_blockers
        or provider_ready != report["required"]
    ):
        # The CLI owns the outer transaction. Raising here rolls back every
        # insert instead of committing a partial conversion report.
        raise ValueError("Provider queue parity failed after legacy conversion")
    return report
