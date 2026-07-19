# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Database-authoritative Messenger writer gate used during canonical cutover."""

import datetime
import logging
import os
import socket
import threading
import typing
import uuid as sys_uuid


ALL_WRITER_CLASSES = frozenset({"api", "worker", "smtp_ingress", "external_bridge"})
LEGACY_MAIL_FREEZE = "legacy_mail_freeze"
POSTGRESQL_CANONICAL_RUNTIME = "postgresql_canonical_runtime"
REQUIRED_WRITER_CLASSES_BY_PHASE = {
    LEGACY_MAIL_FREEZE: ALL_WRITER_CLASSES,
    POSTGRESQL_CANONICAL_RUNTIME: frozenset({"api", "worker", "external_bridge"}),
}
# Compatibility alias for callers and tests that model the pre-cutover freeze.
REQUIRED_WRITER_CLASSES = REQUIRED_WRITER_CLASSES_BY_PHASE[LEGACY_MAIL_FREEZE]
DEFAULT_GATE_LEASE = datetime.timedelta(minutes=10)
DEFAULT_ACK_LEASE = datetime.timedelta(minutes=2)
DEFAULT_INSTANCE_LEASE = datetime.timedelta(seconds=45)
LOG = logging.getLogger(__name__)


class WriterGateClosed(RuntimeError):
    pass


def _utcnow(now: datetime.datetime | None = None) -> datetime.datetime:
    return now or datetime.datetime.now(datetime.timezone.utc)


def default_instance_id(writer_class: str) -> str:
    return f"{writer_class}:{socket.gethostname()}:{os.getpid()}"


def _lock_project(session: typing.Any, project_id: object) -> None:
    # Both the first-ever writer and gate creation take this transaction lock.
    # Therefore an absent sentinel row cannot create a writer-vs-close race.
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
        (str(project_id),),
    )


def _record_release(
    session: typing.Any,
    project_id: object,
    gate_uuid: object,
    acquired_at: datetime.datetime,
    lease_expires_at: datetime.datetime,
    released_at: datetime.datetime,
) -> None:
    session.execute(
        """
        INSERT INTO "m_messenger_writer_gate_releases_v1" (
            "gate_uuid", "project_id", "acquired_at",
            "lease_expires_at", "released_at"
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT ("gate_uuid") DO NOTHING
        """,
        (
            gate_uuid,
            project_id,
            acquired_at,
            lease_expires_at,
            released_at,
        ),
    )


def heartbeat_instance(
    session: typing.Any,
    writer_class: str,
    instance_id: str | None = None,
    now: datetime.datetime | None = None,
    lease: datetime.timedelta | None = None,
) -> str:
    if writer_class not in ALL_WRITER_CLASSES:
        raise ValueError(f"Unknown Messenger writer class {writer_class}")
    now = _utcnow(now)
    instance_id = instance_id or default_instance_id(writer_class)
    expires = now + (lease or DEFAULT_INSTANCE_LEASE)
    session.execute(
        """
        INSERT INTO "m_messenger_writer_instances_v1" (
            "writer_class", "instance_id", "started_at", "heartbeat_at",
            "lease_expires_at"
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT ("writer_class", "instance_id") DO UPDATE
        SET "heartbeat_at" = EXCLUDED."heartbeat_at",
            "lease_expires_at" = EXCLUDED."lease_expires_at"
        """,
        (writer_class, instance_id, now, now, expires),
    )
    return instance_id


def close_gate(
    session: typing.Any,
    project_id: object,
    gate_uuid: sys_uuid.UUID | None = None,
    now: datetime.datetime | None = None,
    lease: datetime.timedelta | None = None,
) -> sys_uuid.UUID:
    """Atomically replace an open/expired gate with a new closed generation."""
    now = _utcnow(now)
    gate_uuid = gate_uuid or sys_uuid.uuid4()
    expires = now + (lease or DEFAULT_GATE_LEASE)
    _lock_project(session, project_id)
    current = _closed_gate(session, project_id, now, for_update=True)
    if current is not None and current["state"] == "closed":
        raise ValueError(
            "Previous Messenger writer gate must be explicitly released"
        )
    if current is not None:
        if current["released_at"] is None:
            raise ValueError("Open Messenger writer gate lacks release evidence")
        _record_release(
            session,
            project_id,
            current["gate_uuid"],
            current["acquired_at"],
            current["lease_expires_at"],
            current["released_at"],
        )
    session.execute(
        'DELETE FROM "m_messenger_writer_gates_v1" WHERE "project_id" = %s',
        (project_id,),
    )
    session.execute(
        """
        INSERT INTO "m_messenger_writer_gates_v1" (
            "project_id", "gate_uuid", "state", "acquired_at",
            "lease_expires_at", "released_at"
        ) VALUES (%s, %s, 'closed', %s, %s, NULL)
        """,
        (project_id, gate_uuid, now, expires),
    )
    session.execute(
        """
        INSERT INTO "m_messenger_writer_gate_expected_v1" (
            "gate_uuid", "writer_class", "instance_id"
        )
        SELECT %s, "writer_class", "instance_id"
        FROM "m_messenger_writer_instances_v1"
        WHERE "lease_expires_at" > %s
        """,
        (gate_uuid, now),
    )
    return gate_uuid


def release_gate(
    session: typing.Any,
    project_id: object,
    gate_uuid: sys_uuid.UUID,
    now: datetime.datetime | None = None,
) -> None:
    now = _utcnow(now)
    _lock_project(session, project_id)
    result = session.execute(
        """
        UPDATE "m_messenger_writer_gates_v1"
        SET "state" = 'open', "released_at" = %s, "updated_at" = NOW()
        WHERE "project_id" = %s AND "gate_uuid" = %s AND "state" = 'closed'
        RETURNING "gate_uuid", "acquired_at", "lease_expires_at", "released_at"
        """,
        (now, project_id, gate_uuid),
    ).fetchone()
    if result is None:
        raise ValueError("Messenger writer gate is absent, replaced, or already open")
    _record_release(
        session,
        project_id,
        result["gate_uuid"],
        result["acquired_at"],
        result["lease_expires_at"],
        result["released_at"],
    )


def _closed_gate(
    session: typing.Any,
    project_id: object,
    now: datetime.datetime,
    *,
    for_update: bool = False,
) -> typing.Any:
    suffix = " FOR UPDATE" if for_update else ""
    return session.execute(
        f"""
        SELECT "gate_uuid", "state", "acquired_at", "lease_expires_at",
               "released_at"
        FROM "m_messenger_writer_gates_v1"
        WHERE "project_id" = %s{suffix}
        """,
        (project_id,),
    ).fetchone()


def acknowledge(
    session: typing.Any,
    project_id: object,
    writer_class: str,
    instance_id: str | None = None,
    now: datetime.datetime | None = None,
) -> object | None:
    """A writer proves it observed the authoritative closed generation."""
    if writer_class not in ALL_WRITER_CLASSES:
        raise ValueError(f"Unknown Messenger writer class {writer_class}")
    now = _utcnow(now)
    _lock_project(session, project_id)
    gate = _closed_gate(session, project_id, now, for_update=True)
    if gate is None or gate["state"] != "closed":
        return None
    if gate["lease_expires_at"] <= now:
        raise WriterGateClosed("Messenger writer gate lease expired while closed")
    instance_id = instance_id or default_instance_id(writer_class)
    expected = session.execute(
        """
        SELECT expected."instance_id"
        FROM "m_messenger_writer_gate_expected_v1" AS expected
        JOIN "m_messenger_writer_instances_v1" AS instance
          ON instance."writer_class" = expected."writer_class"
         AND instance."instance_id" = expected."instance_id"
        WHERE expected."gate_uuid" = %s
          AND expected."writer_class" = %s
          AND expected."instance_id" = %s
          AND instance."lease_expires_at" > %s
        """,
        (gate["gate_uuid"], writer_class, instance_id, now),
    ).fetchone()
    if expected is None:
        raise ValueError("Writer instance was not live before gate acquisition")
    ack_expires = min(gate["lease_expires_at"], now + DEFAULT_ACK_LEASE)
    session.execute(
        """
        INSERT INTO "m_messenger_writer_gate_acks_v1" (
            "gate_uuid", "writer_class", "instance_id",
            "acknowledged_at", "lease_expires_at"
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT ("gate_uuid", "writer_class", "instance_id") DO UPDATE
        SET "acknowledged_at" = EXCLUDED."acknowledged_at",
            "lease_expires_at" = EXCLUDED."lease_expires_at"
        """,
        (gate["gate_uuid"], writer_class, instance_id, now, ack_expires),
    )
    return gate["gate_uuid"]


def assert_writable(
    session: typing.Any,
    project_id: object,
    writer_class: str,
    instance_id: str | None = None,
    now: datetime.datetime | None = None,
) -> None:
    """Serialize with gate creation and reject writes after close commits."""
    del instance_id
    now = _utcnow(now)
    _lock_project(session, project_id)
    gate = _closed_gate(session, project_id, now, for_update=True)
    if gate is not None and gate["state"] == "closed":
        raise WriterGateClosed(
            f"Messenger writes are frozen by gate {gate['gate_uuid']}"
        )


def validate_closed_gate(
    session: typing.Any,
    project_id: object,
    gate_uuid: sys_uuid.UUID,
    now: datetime.datetime | None = None,
    phase: str = LEGACY_MAIL_FREEZE,
) -> dict[str, object]:
    """Return authoritative gate evidence or reject stale/forged state."""
    try:
        required_writer_classes = REQUIRED_WRITER_CLASSES_BY_PHASE[phase]
    except KeyError as exc:
        raise ValueError(f"Unknown Messenger writer-gate phase {phase}") from exc
    now = _utcnow(now)
    _lock_project(session, project_id)
    gate = session.execute(
        """
        SELECT "gate_uuid", "state", "acquired_at", "lease_expires_at"
        FROM "m_messenger_writer_gates_v1"
        WHERE "project_id" = %s AND "gate_uuid" = %s FOR UPDATE
        """,
        (project_id, gate_uuid),
    ).fetchone()
    if gate is None or gate["state"] != "closed":
        raise ValueError("Messenger writer gate is absent, replaced, or open")
    if gate["lease_expires_at"] <= now:
        raise ValueError("Messenger writer gate lease has expired")
    rows = session.execute(
        """
        SELECT expected."writer_class", expected."instance_id",
               instance."lease_expires_at" AS "instance_lease_expires_at",
               ack."lease_expires_at" AS "ack_lease_expires_at"
        FROM "m_messenger_writer_gate_expected_v1" AS expected
        LEFT JOIN "m_messenger_writer_instances_v1" AS instance
          ON instance."writer_class" = expected."writer_class"
         AND instance."instance_id" = expected."instance_id"
        LEFT JOIN "m_messenger_writer_gate_acks_v1" AS ack
          ON ack."gate_uuid" = expected."gate_uuid"
         AND ack."writer_class" = expected."writer_class"
         AND ack."instance_id" = expected."instance_id"
        WHERE expected."gate_uuid" = %s
        """,
        (gate_uuid,),
    ).fetchall()
    covered = {
        row["writer_class"]
        for row in rows
        if row["writer_class"] in required_writer_classes
    }
    missing_classes = required_writer_classes - covered
    stale = [
        f"{row['writer_class']}:{row['instance_id']}"
        for row in rows
        if row["writer_class"] in required_writer_classes
        and (
            row["instance_lease_expires_at"] is None
            or row["instance_lease_expires_at"] <= now
            or row["ack_lease_expires_at"] is None
            or row["ack_lease_expires_at"] <= now
        )
    ]
    if missing_classes or stale:
        missing = sorted(missing_classes) + stale
        raise ValueError(
            "Messenger writer gate lacks live expected acknowledgements: "
            + ", ".join(sorted(missing))
        )
    return {
        "gate_id": str(gate_uuid),
        "phase": phase,
        "acquired_at": gate["acquired_at"].isoformat(),
        "lease_expires_at": gate["lease_expires_at"].isoformat(),
        "blocked_writer_classes": sorted(required_writer_classes),
    }


def heartbeat_and_acknowledge(
    session: typing.Any,
    writer_class: str,
    instance_id: str | None = None,
    now: datetime.datetime | None = None,
) -> tuple[object, ...]:
    """Heartbeat one real process and acknowledge gates that expected it."""
    now = _utcnow(now)
    instance_id = heartbeat_instance(
        session, writer_class, instance_id=instance_id, now=now
    )
    rows = session.execute(
        """
        SELECT gate."project_id"
        FROM "m_messenger_writer_gates_v1" AS gate
        JOIN "m_messenger_writer_gate_expected_v1" AS expected
          ON expected."gate_uuid" = gate."gate_uuid"
         AND expected."writer_class" = %s
         AND expected."instance_id" = %s
        WHERE gate."state" = 'closed' AND gate."lease_expires_at" > %s
        """,
        (writer_class, instance_id, now),
    ).fetchall()
    project_ids = []
    for row in rows:
        acknowledge(
            session,
            row["project_id"],
            writer_class,
            instance_id=instance_id,
            now=now,
        )
        project_ids.append(row["project_id"])
    return tuple(project_ids)


def start_heartbeat(
    session_factory: typing.Callable[[], typing.ContextManager[typing.Any]],
    writer_class: str,
    interval: float = 10,
    instance_id: str | None = None,
) -> tuple[threading.Event, threading.Thread]:
    """Start a process-owned heartbeat; never claims another writer class."""
    instance_id = instance_id or default_instance_id(writer_class)
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                with session_factory() as session:
                    heartbeat_and_acknowledge(
                        session, writer_class, instance_id=instance_id
                    )
            except Exception:
                LOG.exception("Messenger writer-gate heartbeat failed")
            finally:
                stop.wait(interval)

    thread = threading.Thread(
        target=run,
        name=f"workspace-writer-gate-{writer_class}",
        daemon=True,
    )
    thread.start()
    return stop, thread
