# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Persist event payloads with mutations; allocate browser epochs separately."""

import json
import dataclasses
import typing
import uuid as sys_uuid

from restalchemy.storage.sql import sessions

from workspace.messenger_api.api import resource_projection


ENQUEUE_SQL = """
    WITH queued AS (
        INSERT INTO messenger_publication_outbox
            (uuid, project_id, entity_uuid, kind, recipients, payload, recipient_payloads, priority)
        VALUES (%s, %s, %s, %s, %s::uuid[], %s::jsonb, %s::jsonb,
                CASE WHEN current_setting('workspace.zulip_history',true)='on' THEN 1 ELSE 0 END)
        RETURNING uuid
    )
    SELECT uuid, pg_notify('workspace_projection_tasks', '') FROM queued
"""

SELECT_PENDING_SQL = """
    WITH selected AS MATERIALIZED (
        SELECT project_id,entity_uuid,priority,sequence
        FROM messenger_publication_outbox WHERE project_id=%s
        ORDER BY priority,sequence LIMIT 256
    ), heads AS (
        SELECT project_id,entity_uuid,min(priority) AS priority,max(sequence) AS last_sequence
        FROM selected GROUP BY project_id,entity_uuid
    )
    SELECT pending.* FROM heads
    CROSS JOIN LATERAL (
        SELECT candidate.* FROM messenger_publication_outbox candidate
        WHERE candidate.project_id=heads.project_id
          AND candidate.entity_uuid=heads.entity_uuid
          AND candidate.sequence<=heads.last_sequence
        ORDER BY sequence LIMIT 256
    ) pending
    ORDER BY heads.priority,pending.sequence LIMIT 256
"""


@dataclasses.dataclass(frozen=True, order=True)
class PendingEvent:
    uuid: sys_uuid.UUID
    user_uuid: sys_uuid.UUID


def enqueue(
    session: sessions.PgSQLSession,
    project_id: object,
    entity_uuid: object,
    recipients: typing.Iterable[object],
    kind: str,
    payload: typing.Any,
    overrides: typing.Any = None,
) -> list[PendingEvent]:
    event_uuid = sys_uuid.uuid4()
    recipients = sorted({sys_uuid.UUID(str(value)) for value in recipients})
    session.execute(
        ENQUEUE_SQL,
        (
            event_uuid,
            project_id,
            entity_uuid,
            kind,
            recipients,
            json.dumps(resource_projection.simple(payload)),
            json.dumps(resource_projection.simple(overrides)),
        ),
    )
    # Membership guards refer to the final event UUID, not a temporary epoch.
    return [PendingEvent(uuid=event_uuid, user_uuid=user) for user in recipients]


def publish(session: sessions.PgSQLSession, project_id: object) -> int:
    """Allocate ordered client epochs only for already committed snapshots."""
    from workspace.messenger_api import events

    locked = session.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text, 0)) AS locked",
        (project_id,),
    ).fetchone()["locked"]
    if not locked:
        return 0
    rows = session.execute(
        SELECT_PENDING_SQL,
        (project_id,),
    ).fetchall()
    for row in rows:
        events.create_broadcast_event(
            project_id,
            row["entity_uuid"],
            row["recipients"],
            row["kind"],
            row["payload"],
            recipient_payloads=row["recipient_payloads"],
            session=session,
            event_uuid=row["uuid"],
            created_at=row["created_at"],
            owned_publication=True,
        )
        session.execute(
            "DELETE FROM messenger_publication_outbox WHERE uuid=%s", (row["uuid"],)
        )
    return len(rows)


def publish_pending(session: sessions.PgSQLSession) -> int:
    return sum(
        publish(session, row["project_id"])
        for row in session.execute(
            "SELECT DISTINCT project_id FROM messenger_publication_outbox LIMIT 128",
            (),
        ).fetchall()
    )
