# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import json
import uuid as sys_uuid

import psycopg
from restalchemy.common import contexts

from workspace.services.messenger_workers import projection_wakeup
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest


def test_outbox_insert_atomically_enqueues_tasks_and_notifies(_database):
    project_id = sys_uuid.uuid4()
    placement_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    event_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    payloads = [
        {
            "source_kind": "message.created",
            "placement_uuid": str(placement_uuid),
            "message_created_at": "2026-09-07T12:00:00+00:00",
        },
        {
            "source_kind": "stream.read",
            "user_uuid": str(user_uuid),
            "stream_uuid": str(sys_uuid.uuid4()),
        },
    ]
    listener = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
    writer = psycopg.connect(conftest.TEST_DB_URL)
    observer = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
    try:
        listener.execute(f"LISTEN {projection_wakeup.CHANNEL}")
        writer.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            )
            SELECT input.uuid, %s, input.event_kind, input.scope_kind,
                   input.scope_key, input.payload::jsonb
            FROM unnest(
                %s::uuid[], %s::text[], %s::text[], %s::text[], %s::text[]
            ) AS input(uuid, event_kind, scope_kind, scope_key, payload)
            """,
            (
                project_id,
                event_uuids,
                ["fanout", "read_counters"],
                ["stream", "user-stream"],
                [str(placement_uuid), f"{project_id}:{user_uuid}"],
                [json.dumps(payload) for payload in payloads],
            ),
        )

        assert observer.execute(
            """
            SELECT count(*)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s::uuid[])
            """,
            (project_id, event_uuids),
        ).fetchone() == (0,)
        assert next(listener.notifies(timeout=0.05, stop_after=1), None) is None

        writer.commit()

        notification = next(listener.notifies(timeout=1, stop_after=1), None)
        assert notification is not None
        assert notification.payload == "2"
        rows = observer.execute(
            """
            SELECT outbox_event_uuid, task_kind, ordering_key,
                   ordering_created_at, payload
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s::uuid[])
            ORDER BY outbox_event_uuid
            """,
            (project_id, event_uuids),
        ).fetchall()
        assert len(rows) == 2
        by_event = {row[0]: row for row in rows}
        assert by_event[event_uuids[0]][1:3] == (
            "fanout",
            str(placement_uuid),
        )
        assert by_event[event_uuids[0]][3] == datetime.datetime(
            2026,
            9,
            7,
            12,
            tzinfo=datetime.timezone.utc,
        )
        assert by_event[event_uuids[1]][1:3] == (
            "read_counters",
            str(payloads[1]["stream_uuid"]),
        )
        assert by_event[event_uuids[1]][4] == payloads[1]
    finally:
        writer.rollback()
        writer.close()
        observer.close()
        listener.close()


def test_rolled_back_outbox_insert_enqueues_nothing(_database):
    project_id = sys_uuid.uuid4()
    event_uuid = sys_uuid.uuid4()
    listener = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
    writer = psycopg.connect(conftest.TEST_DB_URL)
    try:
        listener.execute(f"LISTEN {projection_wakeup.CHANNEL}")
        writer.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                %s, %s, 'fanout', 'stream', %s,
                '{"source_kind":"message.created"}'::jsonb
            )
            """,
            (event_uuid, project_id, str(sys_uuid.uuid4())),
        )
        writer.rollback()

        assert next(listener.notifies(timeout=0.05, stop_after=1), None) is None
        assert listener.execute(
            """
            SELECT count(*)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (project_id, event_uuid),
        ).fetchone() == (0,)
    finally:
        writer.rollback()
        writer.close()
        listener.close()


def test_trigger_and_repair_derivation_keep_the_same_task_contract(_database):
    project_id = sys_uuid.uuid4()
    event_uuids = [sys_uuid.uuid4() for _index in range(9)]
    values = [str(sys_uuid.uuid4()) for _index in range(8)]
    payloads = [
        {
            "placement_uuid": values[0],
            "message_created_at": "2026-09-07T12:00:00+00:00",
        },
        {
            "placement": {"uuid": values[1]},
            "audience_created_before": "2026-09-07T12:01:00+00:00",
        },
        {
            "resource_uuid": values[2],
            "membership_started_at": "2026-09-07T12:02:00+00:00",
        },
        {"canonical_message_uuid": values[3]},
        {"topic_uuid": values[4]},
        {"stream_uuid": values[5]},
        {"folder_uuid": values[6]},
        {"user_uuid": values[7]},
        {},
    ]

    def read_tasks(session):
        rows = session.execute(
            """
            SELECT uuid, outbox_event_uuid, task_kind, scope_kind, scope_key,
                   ordering_key, ordering_created_at, payload
            FROM messenger_projection_tasks
            WHERE project_id = %s
              AND outbox_event_uuid = ANY(%s::uuid[])
            ORDER BY outbox_event_uuid
            """,
            (project_id, event_uuids),
        ).fetchall()
        return [dict(row) for row in rows]

    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT input.uuid, %s, 'folder_projection', 'repair-contract',
                   input.uuid::text, input.payload::jsonb,
                   '2026-09-07T12:03:00+00:00'::timestamptz,
                   '2026-09-07T12:03:00+00:00'::timestamptz
            FROM unnest(%s::uuid[], %s::text[]) AS input(uuid, payload)
            """,
            (
                project_id,
                event_uuids,
                [json.dumps(payload) for payload in payloads],
            ),
        )
        trigger_tasks = read_tasks(session)
        session.execute(
            """
            DELETE FROM messenger_projection_tasks
            WHERE project_id = %s
              AND outbox_event_uuid = ANY(%s::uuid[])
            """,
            (project_id, event_uuids),
        )
        assert v2_projection.derive_projection_tasks(session, len(event_uuids)) == len(
            event_uuids
        )
        repair_tasks = read_tasks(session)

    assert trigger_tasks == repair_tasks
