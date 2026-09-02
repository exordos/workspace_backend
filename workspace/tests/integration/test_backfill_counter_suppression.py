# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

from restalchemy.common import contexts

from workspace.external_bridge_control import provider_event_apply
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import message_payloads
from workspace.tests.integration import conftest


def test_legacy_backfill_flags_suppress_only_intermediate_counters(api, db):
    stream = api.post(
        "/v1/streams/",
        json={
            "name": "Quiet provider backfill",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    topic = api.post(
        "/v1/stream_topics/",
        json={
            "stream_uuid": stream["uuid"],
            "name": "general",
            "source": {"kind": "native"},
        },
    ).json()
    project_uuid = sys_uuid.UUID(str(api.project_id))
    owner_uuid = sys_uuid.UUID(str(api.user_uuid))
    stream_uuid = sys_uuid.UUID(stream["uuid"])
    topic_uuid = sys_uuid.UUID(topic["uuid"])
    message_uuid = sys_uuid.uuid4()

    with contexts.Context().session_manager() as session:
        message = helpers.create_workspace_user_message(
            project_id=project_uuid,
            user_uuid=owner_uuid,
            uuid=message_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            payload=message_payloads.MarkdownPayload(content="quiet history"),
            provider_metadata={"delivery_class": "backfill"},
            session=session,
            compact_events=True,
            emit_events=False,
            schedule_counters=False,
            scoped_recipient_uuids=[owner_uuid],
            return_visible=False,
        )

    member_uuid = sys_uuid.uuid4()
    conftest.seed_user_stream_binding(
        db,
        project_uuid,
        stream_uuid,
        member_uuid,
    )
    with contexts.Context().session_manager() as session:
        helpers.ensure_workspace_message_recipients(
            project_uuid,
            message,
            [owner_uuid, member_uuid],
            session,
            emit_events=False,
            schedule_counters=False,
        )
        provider_event_apply._sync_provider_read_state(
            session,
            project_uuid,
            owner_uuid,
            stream_uuid,
            topic_uuid,
            [message_uuid],
            False,
            emit_events=False,
            schedule_counters=False,
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT placement.uuid
            FROM messenger_message_placements AS placement
            WHERE placement.project_id = %s
              AND placement.legacy_public_uuid = %s
            """,
            (project_uuid, message_uuid),
        )
        placement_uuid = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM messenger_domain_outbox_events
            WHERE project_id = %s
              AND event_kind = 'read_counters'
              AND payload->>'placement_uuid' = %s
              AND payload->>'source_kind' LIKE 'legacy_message_state.%%'
            """,
            (project_uuid, str(placement_uuid)),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            """
            SELECT flags.user_uuid, flags.read, state.read_at IS NOT NULL
            FROM m_workspace_user_message_flags AS flags
            JOIN messenger_user_message_states AS state
              ON state.project_id = flags.project_id
             AND state.user_uuid = flags.user_uuid
             AND state.placement_uuid = %s
            WHERE flags.project_id = %s AND flags.uuid = %s
            ORDER BY flags.user_uuid
            """,
            (placement_uuid, project_uuid, message_uuid),
        )
        assert cursor.fetchall() == sorted(
            [
                (owner_uuid, False, False),
                (member_uuid, False, False),
            ],
            key=lambda row: str(row[0]),
        )

    with contexts.Context().session_manager() as session:
        provider_event_apply._sync_provider_read_state(
            session,
            project_uuid,
            owner_uuid,
            stream_uuid,
            topic_uuid,
            [message_uuid],
            True,
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM messenger_domain_outbox_events
            WHERE project_id = %s
              AND event_kind = 'read_counters'
              AND payload->>'placement_uuid' = %s
            """,
            (project_uuid, str(placement_uuid)),
        )
        assert cursor.fetchone()[0] > 0
