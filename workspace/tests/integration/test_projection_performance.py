# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import concurrent.futures
import itertools
import json
import threading
import time
import uuid as sys_uuid

import psycopg
import pytest
from restalchemy.common import contexts

from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/streams/"
MESSAGES = f"{V1}/messages/"
MESSAGE_REACTIONS = f"{V1}/message_reactions/"


@pytest.fixture(autouse=True)
def _production_v2_store():
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(
            sql_canonical_store.SQLCanonicalMessengerStoreFactory()
        )


@pytest.fixture(autouse=True)
def _isolate_projection_queue(_database):
    def cleanup():
        with contexts.Context().session_manager() as session:
            session.execute("DELETE FROM messenger_projection_scope_leases", ())
            session.execute("DELETE FROM messenger_domain_outbox_events", ())

    cleanup()
    try:
        yield
    finally:
        cleanup()


def _drain() -> int:
    with contexts.Context().session_manager() as session:
        return v2_projection.drain_projection_queue(
            session,
            f"integration-performance:{sys_uuid.uuid4()}",
            limit=10000,
        )


def _create_message(api, name):
    stream = api.post(
        STREAMS,
        json={
            "name": name,
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": name},
        },
    ).json()
    _drain()
    return stream, message


def _seed_partition_claim_tasks(api, specifications):
    event_uuids = [sys_uuid.uuid4() for _ in specifications]
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT input.uuid, %s, input.event_kind, input.scope_kind,
                   input.scope_key, input.payload::jsonb,
                   NOW() + input.position * INTERVAL '1 millisecond',
                   NOW() + input.position * INTERVAL '1 millisecond'
            FROM unnest(
                %s::uuid[], %s::text[], %s::text[], %s::text[],
                %s::text[], %s::integer[]
            ) AS input(
                uuid, event_kind, scope_kind, scope_key, payload, position
            )
            """,
            (
                api.project_id,
                event_uuids,
                [specification["task_kind"] for specification in specifications],
                [specification["scope_kind"] for specification in specifications],
                [specification["scope_key"] for specification in specifications],
                [
                    json.dumps(specification["payload"])
                    for specification in specifications
                ],
                list(range(len(specifications))),
            ),
        )
        assert v2_projection.derive_projection_tasks(
            session, len(specifications)
        ) == len(specifications)
    return event_uuids


def _plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


def test_reaction_snapshot_uses_message_scoped_covering_index(api, db):
    _stream, target = _create_message(api, "reaction-index-target")
    _stream, distractor = _create_message(api, "reaction-index-distractor")
    target_reaction_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT placement.message_uuid, placement.uuid
            FROM messenger_message_placements AS placement
            WHERE placement.project_id = %s AND placement.uuid = ANY(%s::uuid[])
            ORDER BY placement.uuid
            """,
            (api.project_id, [target["uuid"], distractor["uuid"]]),
        )
        placements = {
            str(placement_uuid): canonical_uuid
            for canonical_uuid, placement_uuid in cursor.fetchall()
        }
        target_canonical = placements[target["uuid"]]
        distractor_canonical = placements[distractor["uuid"]]
        cursor.execute(
            """
            INSERT INTO messenger_message_reaction_facts (
                uuid, project_id, canonical_message_uuid, placement_uuid,
                user_uuid, emoji_name
            ) VALUES (%s, %s, %s, %s, %s, 'target')
            """,
            (
                target_reaction_uuid,
                api.project_id,
                target_canonical,
                target["uuid"],
                api.user_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO messenger_message_reaction_facts (
                uuid, project_id, canonical_message_uuid, placement_uuid,
                user_uuid, emoji_name
            )
            SELECT gen_random_uuid(), %s, %s, %s, %s,
                   'noise-' || input.number::text
            FROM generate_series(1, 20000) AS input(number)
            """,
            (
                api.project_id,
                distractor_canonical,
                distractor["uuid"],
                api.user_uuid,
            ),
        )
        cursor.execute("ANALYZE messenger_message_reaction_facts")
        cursor.execute(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT canonical_message_uuid, emoji_name,
                   count(*) AS reaction_count,
                   jsonb_agg(user_uuid::text ORDER BY created_at, uuid) AS users
            FROM messenger_message_reaction_facts
            WHERE project_id = %s AND canonical_message_uuid = %s
            GROUP BY canonical_message_uuid, emoji_name
            """,
            (api.project_id, target_canonical),
        )
        scoped_plan = cursor.fetchone()[0][0]
        cursor.execute(
            """
            EXPLAIN (FORMAT JSON)
            SELECT canonical_message_uuid, emoji_name,
                   count(*) AS reaction_count,
                   jsonb_agg(user_uuid::text ORDER BY created_at, uuid) AS users
            FROM messenger_message_reaction_facts
            WHERE project_id = %s
            GROUP BY canonical_message_uuid, emoji_name
            """,
            (api.project_id,),
        )
        project_plan = cursor.fetchone()[0][0]

    nodes = list(_plan_nodes(scoped_plan["Plan"]))
    assert any(
        node.get("Index Name") == "messenger_message_reaction_facts_snapshot_idx"
        for node in nodes
    ), json.dumps(scoped_plan)
    assert not any(node["Node Type"] == "Seq Scan" for node in nodes)
    assert scoped_plan["Plan"]["Total Cost"] * 20 < project_plan["Plan"]["Total Cost"]


def test_reaction_burst_coalesces_and_preserves_realtime_events(api, db, monkeypatch):
    _stream, message = _create_message(api, "reaction-coalescing")
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(max(epoch_version), 0) FROM m_workspace_visible_events"
        )
        baseline_epoch = cursor.fetchone()[0]

    created = api.post(
        MESSAGE_REACTIONS,
        json={"message_uuid": message["uuid"], "emoji_name": "eyes"},
    )
    assert created.status_code == 201, created.text
    reaction_uuid = created.json()["uuid"]
    updated = api.put(
        f"{MESSAGE_REACTIONS}{reaction_uuid}",
        json={"emoji_name": "heart"},
    )
    assert updated.status_code == 200, updated.text
    deleted = api.delete(f"{MESSAGE_REACTIONS}{reaction_uuid}")
    assert deleted.status_code in (200, 204), deleted.text

    with contexts.Context().session_manager() as session:
        assert v2_projection.derive_projection_tasks(session, 100) >= 3
        processed = v2_projection.process_one_projection_task(
            session,
            "integration:reaction-coalescing",
        )
        assert processed is True

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE attempts > 0),
                   jsonb_agg(execution_stats ORDER BY created_at, uuid)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND payload->>'reaction_uuid' = %s
              AND status = 'completed'
            """,
            (api.project_id, reaction_uuid),
        )
        completed, actually_claimed, execution_stats = cursor.fetchone()
        cursor.execute(
            """
            SELECT object_type, action, payload
            FROM m_workspace_visible_events
            WHERE project_id = %s AND epoch_version > %s
              AND object_type IN ('message', 'message_reaction')
            ORDER BY epoch_version
            """,
            (api.project_id, baseline_epoch),
        )
        events = cursor.fetchall()

    assert completed == 3
    assert actually_claimed == 1
    claimed_stats = [stats for stats in execution_stats if stats.get("claim_count")]
    coalesced_stats = [
        stats for stats in execution_stats if stats.get("last_outcome") == "coalesced"
    ]
    assert len(claimed_stats) == 1
    assert len(coalesced_stats) == 2
    assert claimed_stats[0]["last_outcome"] == "completed"
    assert claimed_stats[0]["coalesced_task_count"] == 2
    assert claimed_stats[0]["reaction_fact_count"] == 0
    assert claimed_stats[0]["reaction_event_count"] == 3
    assert claimed_stats[0]["queue_wait_ms"] >= 0
    assert claimed_stats[0]["outbox_wait_ms"] >= claimed_stats[0]["queue_wait_ms"]
    assert claimed_stats[0]["derivation_delay_ms"] >= 0
    assert claimed_stats[0]["claim_duration_ms"] >= 0
    assert claimed_stats[0]["processing_duration_ms"] >= 0
    assert claimed_stats[0]["outbox_to_finish_ms"] >= 0
    reaction_actions = [
        action for kind, action, _payload in events if kind == "message_reaction"
    ]
    assert reaction_actions == ["created", "updated", "deleted"]
    message_events = [payload for kind, _action, payload in events if kind == "message"]
    assert len(message_events) == 1
    assert message_events[0]["reactions"] == {}
    assert api.get(f"{MESSAGES}{message['uuid']}").json()["reactions"] == {}


def test_reaction_coalescing_locks_a_bounded_sibling_batch(api, db, monkeypatch):
    _stream, message = _create_message(api, "bounded-reaction-coalescing")
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    task_count = v2_projection.REACTION_COALESCE_LIMIT + 5
    with contexts.Context().session_manager() as session:
        canonical_message_uuid = session.execute(
            """
            SELECT message_uuid
            FROM messenger_message_placements
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message["uuid"]),
        ).fetchone()["message_uuid"]
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            )
            SELECT gen_random_uuid(), %s, 'reaction_snapshot', 'message', %s,
                   jsonb_build_object(
                       'source_kind', 'message_reaction.updated',
                       'placement_uuid', %s::text,
                       'emit_reaction_event', FALSE,
                       'emit_message_updated', FALSE
                   )
            FROM generate_series(1, %s)
            """,
            (
                api.project_id,
                f"{api.project_id}:{canonical_message_uuid}",
                message["uuid"],
                task_count,
            ),
        )
        assert v2_projection.derive_projection_tasks(session, task_count) == task_count
        assert v2_projection.process_one_projection_task(
            session,
            "integration:bounded-reaction:first",
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE status = 'pending'),
                   count(*) FILTER (WHERE attempts > 0),
                   max((execution_stats->>'coalesced_task_count')::integer)
                       FILTER (WHERE attempts > 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND scope_key = %s
            """,
            (
                api.project_id,
                f"{api.project_id}:{canonical_message_uuid}",
            ),
        )
        completed, pending, claimed, first_coalesced = cursor.fetchone()

    assert completed == v2_projection.REACTION_COALESCE_LIMIT
    assert pending == task_count - v2_projection.REACTION_COALESCE_LIMIT
    assert claimed == 1
    assert first_coalesced == v2_projection.REACTION_COALESCE_LIMIT - 1

    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:bounded-reaction:second",
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE status = 'pending'),
                   count(*) FILTER (WHERE attempts > 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND scope_key = %s
            """,
            (
                api.project_id,
                f"{api.project_id}:{canonical_message_uuid}",
            ),
        )
        completed, pending, claimed = cursor.fetchone()

    assert completed == task_count
    assert pending == 0
    assert claimed == 2


def test_reaction_coalescing_honors_sibling_retry_deadlines(api, db, monkeypatch):
    _stream, message = _create_message(api, "reaction-retry-deadline")
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    future_placement_uuid = sys_uuid.uuid4()
    scope_key = None
    with contexts.Context().session_manager() as session:
        canonical_message_uuid = session.execute(
            """
            SELECT message_uuid
            FROM messenger_message_placements
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message["uuid"]),
        ).fetchone()["message_uuid"]
        scope_key = f"{api.project_id}:{canonical_message_uuid}"
        for placement_uuid in (future_placement_uuid, message["uuid"]):
            session.execute(
                """
                INSERT INTO messenger_domain_outbox_events (
                    uuid, project_id, event_kind, scope_kind, scope_key, payload
                ) VALUES (
                    gen_random_uuid(), %s, 'reaction_snapshot', 'message', %s,
                    jsonb_build_object(
                        'source_kind', 'message_reaction.updated',
                        'placement_uuid', %s::text,
                        'emit_reaction_event', FALSE,
                        'emit_message_updated', FALSE
                    )
                )
                """,
                (api.project_id, scope_key, placement_uuid),
            )
        assert v2_projection.derive_projection_tasks(session, 2) == 2
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'failed', next_retry_at = NOW() + INTERVAL '1 hour',
                last_error = 'transient provider failure'
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND payload->>'placement_uuid' = %s
            """,
            (api.project_id, str(future_placement_uuid)),
        )
        assert v2_projection.process_one_projection_task(
            session,
            "integration:reaction-retry-deadline",
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT payload->>'placement_uuid', status, attempts,
                   COALESCE(next_retry_at > NOW(), FALSE),
                   execution_stats->>'coalesced_task_count'
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND scope_key = %s
            ORDER BY payload->>'placement_uuid'
            """,
            (api.project_id, scope_key),
        )
        rows = {
            placement_uuid: (status, attempts, retry_in_future, coalesced_count)
            for (
                placement_uuid,
                status,
                attempts,
                retry_in_future,
                coalesced_count,
            ) in cursor.fetchall()
        }

    assert rows[str(future_placement_uuid)] == ("failed", 0, True, None)
    assert rows[message["uuid"]] == ("completed", 1, False, "0")


def test_concurrent_reaction_workers_keep_one_exact_snapshot(api, db, monkeypatch):
    _stream, message = _create_message(api, "reaction-concurrency")
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    reactions = []
    for emoji_name in ("eyes", "heart", "rocket", "check"):
        response = api.post(
            MESSAGE_REACTIONS,
            json={"message_uuid": message["uuid"], "emoji_name": emoji_name},
        )
        assert response.status_code == 201, response.text
        reactions.append(response.json())
    with contexts.Context().session_manager() as session:
        assert v2_projection.derive_projection_tasks(session, 100) >= len(reactions)

    ready = threading.Barrier(2)

    def process(worker_id):
        with contexts.Context().session_manager() as session:
            ready.wait(timeout=5)
            return v2_projection.process_one_projection_task(session, worker_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(process, ("integration:reaction-a", "integration:reaction-b"))
        )

    assert any(results)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE attempts > 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND payload->>'placement_uuid' = %s
              AND status = 'completed'
            """,
            (api.project_id, message["uuid"]),
        )
        completed, actually_claimed = cursor.fetchone()
    assert completed == len(reactions)
    assert actually_claimed <= 2
    snapshot = api.get(f"{MESSAGES}{message['uuid']}").json()
    assert snapshot["reactions"] == {
        "check": 1,
        "eyes": 1,
        "heart": 1,
        "rocket": 1,
    }


def test_coalesced_reaction_events_resolve_each_placement(api, db, monkeypatch):
    primary_stream, message = _create_message(api, "reaction-primary-placement")
    secondary_stream = api.post(
        STREAMS,
        json={
            "name": "reaction-secondary-placement",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    secondary_placement_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT message_uuid
            FROM messenger_message_placements
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message["uuid"]),
        )
        canonical_message_uuid = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO messenger_message_placements (
                uuid, project_id, message_uuid, stream_uuid, topic_uuid
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                secondary_placement_uuid,
                api.project_id,
                canonical_message_uuid,
                secondary_stream["uuid"],
                secondary_stream["default_topic_uuid"],
            ),
        )
    db.commit()

    emitted = []
    guarded_streams = []

    def capture_created(reaction, event_message, **_kwargs):
        emitted.append(
            (
                reaction.uuid,
                event_message.uuid,
                event_message.stream_uuid,
            )
        )
        return []

    monkeypatch.setattr(
        v2_projection.messenger_events,
        "create_message_reaction_created_event",
        capture_created,
    )
    monkeypatch.setattr(
        v2_projection,
        "_guard_emitted_events",
        lambda *_args, **kwargs: guarded_streams.append(kwargs["stream_uuid"]),
    )
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )
    placements = [
        (sys_uuid.UUID(message["uuid"]), sys_uuid.UUID(primary_stream["uuid"])),
        (secondary_placement_uuid, sys_uuid.UUID(secondary_stream["uuid"])),
    ]
    with contexts.Context().session_manager() as session:
        for index, (placement_uuid, _stream_uuid) in enumerate(placements):
            reaction_uuid = sys_uuid.uuid4()
            session.execute(
                """
                INSERT INTO messenger_domain_outbox_events (
                    uuid, project_id, event_kind, scope_kind, scope_key, payload
                ) VALUES (
                    gen_random_uuid(), %s, 'reaction_snapshot', 'message', %s,
                    %s::jsonb
                )
                """,
                (
                    api.project_id,
                    f"{api.project_id}:{canonical_message_uuid}",
                    json.dumps(
                        {
                            "source_kind": "message_reaction.created",
                            "reaction_uuid": str(reaction_uuid),
                            "placement_uuid": str(placement_uuid),
                            "reaction": {
                                "uuid": str(reaction_uuid),
                                "project_id": api.project_id,
                                "message_uuid": str(placement_uuid),
                                "user_uuid": api.user_uuid,
                                "emoji_name": f"probe-{index}",
                            },
                            "emit_reaction_event": True,
                            "emit_message_updated": False,
                        }
                    ),
                ),
            )
        assert v2_projection.derive_projection_tasks(session, 10) == 2
        assert v2_projection.process_one_projection_task(
            session,
            "integration:reaction-multi-placement",
        )

    assert {(placement_uuid, stream_uuid) for _, placement_uuid, stream_uuid in emitted} == {
        (placement_uuid, stream_uuid) for placement_uuid, stream_uuid in placements
    }
    assert set(guarded_streams) == {stream_uuid for _, stream_uuid in placements}


def test_reaction_move_rebuilds_both_message_snapshots(api, db, monkeypatch):
    stream, source = _create_message(api, "reaction-move-source")
    target_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "reaction-move-target"},
        },
    )
    assert target_response.status_code == 201, target_response.text
    target = target_response.json()
    _drain()
    created = api.post(
        MESSAGE_REACTIONS,
        json={"message_uuid": source["uuid"], "emoji_name": "eyes"},
    )
    assert created.status_code == 201, created.text
    reaction_uuid = created.json()["uuid"]
    _drain()
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("reaction_snapshot"),
    )

    moved = api.put(
        f"{MESSAGE_REACTIONS}{reaction_uuid}",
        json={"message_uuid": target["uuid"], "emoji_name": "heart"},
    )
    assert moved.status_code == 200, moved.text
    with contexts.Context().session_manager() as session:
        assert v2_projection.derive_projection_tasks(session, 100) == 2
        assert v2_projection.process_one_projection_task(
            session, "integration:reaction-move:new"
        )
        assert v2_projection.process_one_projection_task(
            session, "integration:reaction-move:old"
        )

    assert api.get(f"{MESSAGES}{source['uuid']}").json()["reactions"] == {}
    assert api.get(f"{MESSAGES}{target['uuid']}").json()["reactions"] == {"heart": 1}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT payload->>'source_kind', status
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'reaction_snapshot'
              AND payload->>'reaction_uuid' = %s
              AND payload->>'source_kind' LIKE 'message_reaction.updated%%'
            ORDER BY created_at, uuid
            """,
            (api.project_id, reaction_uuid),
        )
        assert set(cursor.fetchall()) == {
            ("message_reaction.updated", "completed"),
            ("message_reaction.updated_old", "completed"),
        }


def test_rolled_back_claim_is_immediately_reusable(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    event_uuid = sys_uuid.uuid4()
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                %s, %s, 'topic_state_projection', 'topic', %s,
                '{"source_kind":"topic.updated"}'::jsonb
            )
            """,
            (event_uuid, api.project_id, f"{api.project_id}:shutdown-reclaim"),
        )
        assert v2_projection.derive_projection_tasks(session, 10) == 1

    first_task_uuid = None
    try:
        with contexts.Context().session_manager() as session:
            claimed = v2_projection._claim_task(
                session, "integration:stopping-worker", 30
            )
            assert claimed is not None
            first_task_uuid = claimed["uuid"]
            raise RuntimeError("simulate graceful worker transaction rollback")
    except RuntimeError:
        pass

    with contexts.Context().session_manager() as session:
        reclaimed = v2_projection._claim_task(
            session, "integration:replacement-worker", 30
        )
        assert reclaimed is not None
        assert reclaimed["uuid"] == first_task_uuid
        assert reclaimed["attempts"] == 1


def test_projection_claims_different_users_in_one_project_concurrently(
    api,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    user_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "topic.read",
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            }
            for user_uuid in user_uuids
        ],
    )
    claimed = threading.Barrier(3)
    release = threading.Event()

    def hold_claim(worker_id):
        with contexts.Context().session_manager() as session:
            task = v2_projection._claim_task(session, worker_id, 30)
            claimed.wait(timeout=5)
            assert release.wait(timeout=5)
            session.rollback()
            return task

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(hold_claim, f"integration:user-partition:{index}")
            for index in range(2)
        ]
        claimed.wait(timeout=5)
        release.set()
        tasks = [future.result(timeout=5) for future in futures]

    assert all(task is not None for task in tasks)
    assert {task["partition_kind"] for task in tasks} == {"user"}
    assert {task["partition_key"] for task in tasks} == {
        str(user_uuid) for user_uuid in user_uuids
    }


def test_projection_claim_serializes_different_scopes_for_one_user(
    api,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    user_uuid = sys_uuid.uuid4()
    _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": scope_kind,
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": source_kind,
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            }
            for scope_kind, source_kind in (
                ("user-stream", "stream.read"),
                ("user-topic", "topic.read"),
            )
        ],
    )
    ready = threading.Event()
    release = threading.Event()

    def hold_first_claim():
        with contexts.Context().session_manager() as session:
            task = v2_projection._claim_task(session, "integration:same-user:1", 30)
            ready.set()
            assert release.wait(timeout=5)
            session.rollback()
            return task

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold_first_claim)
        assert ready.wait(timeout=5)
        with contexts.Context().session_manager() as session:
            competing = v2_projection._claim_task(
                session,
                "integration:same-user:2",
                30,
            )
        release.set()
        first = future.result(timeout=5)

    assert first is not None
    assert first["partition_kind"] == "user"
    assert competing is None


def test_user_partition_and_scope_fifo_use_the_same_ordering_key(
    api,
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    user_uuid = sys_uuid.uuid4()
    scope_key = f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}"
    event_uuids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": scope_key,
                "payload": {
                    "source_kind": "topic.read",
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            }
            for _ in range(2)
        ],
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = CASE outbox_event_uuid
                    WHEN %s THEN NOW() + INTERVAL '2 seconds'
                    ELSE NOW() + INTERVAL '1 second'
                END,
                ordering_created_at = CASE outbox_event_uuid
                    WHEN %s THEN NOW() - INTERVAL '2 seconds'
                    ELSE NOW() - INTERVAL '1 second'
                END
            WHERE project_id = %s AND outbox_event_uuid IN (%s, %s)
            """,
            (
                event_uuids[0],
                event_uuids[0],
                api.project_id,
                event_uuids[0],
                event_uuids[1],
            ),
        )
    db.commit()

    with contexts.Context().session_manager() as session:
        task = v2_projection._claim_task(session, "integration:fifo-key", 30)
        assert task is not None
        assert task["outbox_event_uuid"] == event_uuids[1]
        session.rollback()


def test_user_projection_partition_blocks_project_global_task(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    user_uuid = sys_uuid.uuid4()
    event_uuids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "folder_projection",
                "scope_kind": "user-folder",
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "folder.updated",
                    "user_uuid": str(user_uuid),
                    "folder_uuid": str(sys_uuid.uuid4()),
                },
            },
            {
                "task_kind": "folder_projection",
                "scope_kind": "stream-folders",
                "scope_key": f"{api.project_id}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "stream.updated",
                    "stream_uuid": str(sys_uuid.uuid4()),
                },
            },
        ],
    )
    ready = threading.Event()
    release = threading.Event()

    def hold_user_claim():
        with contexts.Context().session_manager() as session:
            task = v2_projection._claim_task(session, "integration:user-gate", 30)
            ready.set()
            assert release.wait(timeout=5)
            session.rollback()
            return task

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold_user_claim)
        assert ready.wait(timeout=5)
        metrics = {}
        with contexts.Context().session_manager() as session:
            competing = v2_projection._claim_task(
                session,
                "integration:global-gate",
                30,
                metrics=metrics,
            )
        release.set()
        user_task = future.result(timeout=5)

    assert user_task is not None
    assert user_task["outbox_event_uuid"] == event_uuids[0]
    assert user_task["partition_kind"] == "user"
    assert competing is None
    assert metrics["partition_contention"] >= 1
    assert metrics["partition_contention_project"] >= 1


def test_global_projection_claim_keeps_project_event_ordering_lock(
    api,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("background"),
    )
    _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "folder_projection",
                "scope_kind": "stream-folders",
                "scope_key": f"{api.project_id}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "stream.updated",
                    "stream_uuid": str(sys_uuid.uuid4()),
                },
            }
        ],
    )

    with contexts.Context().session_manager() as session:
        task = v2_projection._claim_task(session, "integration:global-event", 30)
        assert task is not None
        assert task["partition_kind"] == "project"
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as observer:
            with observer.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s::text, 0))",
                    (api.project_id,),
                )
                assert cursor.fetchone() == (False,)
        session.rollback()


def test_user_projection_requeues_without_attempt_on_event_lock_contention(
    api,
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    user_uuid = sys_uuid.uuid4()
    event_uuids = _seed_partition_claim_tasks(
        api,
        [
            {
                "task_kind": "read_counters",
                "scope_kind": "user-topic",
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "topic.read",
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            },
            {
                "task_kind": "read_counters",
                "scope_kind": "user-stream",
                "scope_key": f"{api.project_id}:{user_uuid}:{sys_uuid.uuid4()}",
                "payload": {
                    "source_kind": "stream.read",
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                },
            },
        ],
    )
    event_uuid = event_uuids[0]

    def publish_at_tail(session, task, _batch_size):
        v2_projection._try_lock_project_event_tail(session, task["project_id"])
        return True

    monkeypatch.setattr(v2_projection, "_process_task", publish_at_tail)
    metrics = {}
    blocker = psycopg.connect(conftest.TEST_DB_URL)
    with blocker.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s::text, 0))",
            (api.project_id,),
        )
    try:
        with contexts.Context().session_manager() as session:
            assert v2_projection.process_one_projection_task(
                session,
                "integration:event-contention",
                metrics=metrics,
            )
    finally:
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s::text, 0))",
                (api.project_id,),
            )
        blocker.close()

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET next_retry_at = NOW() + INTERVAL '1 hour'
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
    db.commit()
    with contexts.Context().session_manager() as session:
        assert (
            v2_projection._claim_task(
                session,
                "integration:ordered-event-contention",
                30,
            )
            is None
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, last_error,
                   execution_stats->>'last_outcome',
                   execution_stats->>'partition_kind'
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
        assert cursor.fetchone() == (
            "pending",
            0,
            None,
            "event_lock_contention",
            "user",
        )
    assert metrics["event_lock_contention"] == 1
    assert metrics["claimed_partition_user"] == 1
    assert metrics["claim_seconds_read_counters"] >= 0
    assert metrics["processing_seconds_read_counters"] >= 0
    assert "outbox_to_finish_seconds_read_counters" not in metrics

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET next_retry_at = NOW()
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
    db.commit()
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:event-contention-retry",
            metrics=metrics,
        )
    assert metrics["claimed_partition_user"] == 2
    assert metrics["outbox_to_finish_seconds_read_counters"] >= 0


def test_broadcast_guard_does_not_deadlock_with_existing_project_user_update(api):
    stream, message = _create_message(api, "broadcast-project-user-lock-order")
    audience_uuid = sys_uuid.uuid4()
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO m_workspace_event_audience_snapshots_v1 (
                uuid, project_id, membership_digest
            ) VALUES (%s, %s, %s)
            """,
            (audience_uuid, api.project_id, f"lock-order:{audience_uuid}"),
        )
        session.execute(
            """
            INSERT INTO m_workspace_event_audience_members_v1 (
                audience_snapshot_uuid, user_uuid
            ) VALUES (%s, %s)
            """,
            (audience_uuid, api.user_uuid),
        )

    ready = threading.Barrier(2)

    def emit_while_holding_project_lock():
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL lock_timeout = '5s'", ())
            session.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (api.project_id,),
            )
            ready.wait(timeout=5)
            session.execute(
                """
                INSERT INTO m_workspace_broadcast_message_events_v1 (
                    uuid, project_id, entity_uuid, audience_snapshot_uuid,
                    object_type, action, payload
                ) VALUES (
                    gen_random_uuid(), %s, %s, %s, 'message', 'created',
                    jsonb_build_object(
                        'uuid', %s::text,
                        'stream_uuid', %s::text,
                        'topic_uuid', %s::text,
                        'source_name', 'native'
                    )
                )
                """,
                (
                    api.project_id,
                    message["uuid"],
                    audience_uuid,
                    message["uuid"],
                    stream["uuid"],
                    stream["default_topic_uuid"],
                ),
            )
        return "event"

    def update_user_then_take_project_lock():
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL lock_timeout = '5s'", ())
            session.execute(
                """
                UPDATE messenger_project_users
                SET updated_at = NOW()
                WHERE project_id = %s AND user_uuid = %s
                """,
                (api.project_id, api.user_uuid),
            )
            ready.wait(timeout=5)
            session.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (api.project_id,),
            )
        return "user"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(emit_while_holding_project_lock),
            executor.submit(update_user_then_take_project_lock),
        )
        assert {future.result(timeout=10) for future in futures} == {
            "event",
            "user",
        }


def test_fanout_is_derived_before_old_read_outbox(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_DERIVATION_CYCLE",
        itertools.repeat("fanout"),
    )
    fanout_uuid = sys_uuid.uuid4()
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT gen_random_uuid(), %s, 'read_counters', 'user-topic',
                   %s || ':derive-read:' || input.number::text,
                   '{"source_kind":"legacy_message_state.updated"}'::jsonb,
                   NOW() - interval '1 day', NOW() - interval '1 day'
            FROM generate_series(1, 10000) AS input(number)
            """,
            (api.project_id, str(api.project_id)),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                %s, %s, 'fanout', 'topic', %s,
                jsonb_build_object('placement_uuid', gen_random_uuid())
            )
            """,
            (fanout_uuid, api.project_id, f"{api.project_id}:derive-fanout"),
        )
        assert v2_projection.derive_projection_tasks(session, 1) == 1
        derived = session.execute(
            """
            SELECT task_kind, outbox_event_uuid
            FROM messenger_projection_tasks
            """,
            (),
        ).fetchall()

    assert [(row["task_kind"], row["outbox_event_uuid"]) for row in derived] == [
        ("fanout", fanout_uuid)
    ]


def test_fair_scheduler_bounds_fanout_under_large_read_backlog(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.cycle(v2_projection.FAIR_SCHEDULER_LANES),
    )
    project_id = api.project_id
    special_events = (
        ("fanout", "topic", "fanout", {"placement_uuid": str(sys_uuid.uuid4())}),
        (
            "read_counters",
            "user-topic",
            "interactive",
            {"source_kind": "topic.read"},
        ),
        (
            "reaction_snapshot",
            "message",
            "reaction",
            {"placement_uuid": str(sys_uuid.uuid4())},
        ),
        (
            "topic_state_projection",
            "topic",
            "background",
            {"source_kind": "topic.updated"},
        ),
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            "DELETE FROM messenger_domain_outbox_events WHERE project_id = %s",
            (project_id,),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT gen_random_uuid(), %s, 'read_counters', 'user-topic',
                   %s || ':read:' || input.number::text,
                   jsonb_build_object(
                       'source_kind', 'legacy_message_state.updated',
                       'user_uuid', %s::text
                   ),
                   NOW() - interval '1 day', NOW() - interval '1 day'
            FROM generate_series(1, 30000) AS input(number)
            """,
            (project_id, str(project_id), api.user_uuid),
        )
        for event_kind, scope_kind, suffix, payload in special_events:
            session.execute(
                """
                INSERT INTO messenger_domain_outbox_events (
                    uuid, project_id, event_kind, scope_kind, scope_key, payload
                ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    project_id,
                    event_kind,
                    scope_kind,
                    f"{project_id}:{suffix}",
                    json.dumps(payload),
                ),
            )
        assert v2_projection.derive_projection_tasks(session, 40000) == 30004
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = event.updated_at
            FROM messenger_domain_outbox_events AS event
            WHERE task.project_id = %s
              AND event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
            """,
            (project_id,),
        )
        session.execute("ANALYZE messenger_projection_tasks", ())
        plan = session.execute(
            """
            EXPLAIN (FORMAT JSON)
            SELECT project_id, uuid
            FROM messenger_projection_tasks
            WHERE status NOT IN ('completed', 'dead_letter')
              AND task_kind = 'fanout'
            ORDER BY created_at, ordering_created_at, outbox_event_uuid
            LIMIT 128
            """,
            (),
        ).fetchone()["QUERY PLAN"][0]
        started_at = time.monotonic()
        claimed = []
        for index in range(len(v2_projection.FAIR_SCHEDULER_LANES)):
            task = v2_projection._claim_task(
                session,
                f"integration:fair:{index}",
                30,
            )
            assert task is not None
            claimed.append((task["task_kind"], task["payload"].get("source_kind")))
            session.execute(
                """
                UPDATE messenger_projection_tasks
                SET status = 'completed', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (project_id, task["uuid"]),
            )
            session.execute(
                """
                UPDATE messenger_projection_scope_leases
                SET owner = NULL, lease_expires_at = NOW(), updated_at = NOW()
                WHERE project_id = %s AND scope_kind = %s AND scope_key = %s
                """,
                (project_id, task["scope_kind"], task["scope_key"]),
            )
        elapsed = time.monotonic() - started_at
        metrics = v2_projection.projection_queue_metrics(session)
        session.execute(
            "DELETE FROM messenger_domain_outbox_events WHERE project_id = %s",
            (project_id,),
        )

    assert claimed[0][0] == "fanout"
    assert ("read_counters", "topic.read") in claimed
    assert any(task_kind == "reaction_snapshot" for task_kind, _source in claimed)
    assert any(task_kind == "topic_state_projection" for task_kind, _source in claimed)
    assert any(
        node.get("Index Name") == "messenger_projection_tasks_fair_claim_idx"
        for node in _plan_nodes(plan["Plan"])
    ), json.dumps(plan)
    # The indexed, bounded plan is the stable regression check. Keep only a
    # coarse end-to-end guard here so shared-runner I/O and autovacuum timing
    # cannot turn an otherwise bounded query into a flaky test.
    assert elapsed < 30
    assert metrics["unfinished"] >= 29994
    assert metrics["oldest_pending_task_seconds"] >= 23 * 60 * 60


def test_fair_scheduler_scans_past_dense_locked_project(api, monkeypatch):
    locked_project_id = sys_uuid.UUID(api.project_id)
    available_project_id = sys_uuid.uuid4()
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT gen_random_uuid(), %s, 'fanout', 'message',
                   %s || ':locked:' || input.number::text,
                   jsonb_build_object('placement_uuid', gen_random_uuid()),
                   NOW() - interval '2 days' +
                       input.number * interval '1 microsecond',
                   NOW() - interval '2 days'
            FROM generate_series(1, %s) AS input(number)
            """,
            (
                locked_project_id,
                str(locked_project_id),
                v2_projection.CLAIM_CANDIDATE_LIMIT + 1,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), %s, 'fanout', 'message', %s,
                jsonb_build_object('placement_uuid', gen_random_uuid()),
                NOW() - interval '1 day', NOW() - interval '1 day'
            )
            """,
            (available_project_id, f"{available_project_id}:available"),
        )
        assert v2_projection.derive_projection_tasks(session, 1000) == (
            v2_projection.CLAIM_CANDIDATE_LIMIT + 2
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = event.updated_at
            FROM messenger_domain_outbox_events AS event
            WHERE event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
            """,
            (),
        )

    locked = threading.Event()
    release = threading.Event()

    def hold_project_lock():
        with contexts.Context().session_manager() as session:
            session.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (locked_project_id,),
            )
            locked.set()
            assert release.wait(timeout=10)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_project_lock)
        assert locked.wait(timeout=5)
        try:
            with contexts.Context().session_manager() as session:
                claimed = v2_projection._claim_task(
                    session,
                    "integration:dense-project-fairness",
                    30,
                )
                assert claimed is not None
                assert claimed["project_id"] == available_project_id
        finally:
            release.set()
        holder.result(timeout=10)


def test_fair_scheduler_skips_project_with_retry_blocked_predecessor(
    api,
    monkeypatch,
):
    blocked_project_id = sys_uuid.UUID(api.project_id)
    available_project_id = sys_uuid.uuid4()
    blocked_placement_uuid = sys_uuid.uuid4()
    blocked_scope_key = f"{blocked_project_id}:retry-blocked"
    available_scope_key = f"{available_project_id}:available"
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES
                (
                    gen_random_uuid(), %s, 'fanout', 'message', %s,
                    jsonb_build_object('placement_uuid', %s::text),
                    NOW() - interval '2 days', NOW() - interval '2 days'
                ),
                (
                    gen_random_uuid(), %s, 'fanout', 'message', %s,
                    jsonb_build_object('placement_uuid', %s::text),
                    NOW() - interval '1 day', NOW() - interval '1 day'
                ),
                (
                    gen_random_uuid(), %s, 'fanout', 'message', %s,
                    jsonb_build_object('placement_uuid', gen_random_uuid()),
                    NOW() - interval '1 hour', NOW() - interval '1 hour'
                )
            """,
            (
                blocked_project_id,
                blocked_scope_key,
                blocked_placement_uuid,
                blocked_project_id,
                blocked_scope_key,
                blocked_placement_uuid,
                available_project_id,
                available_scope_key,
            ),
        )
        assert v2_projection.derive_projection_tasks(session, 10) == 3
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = event.updated_at
            FROM messenger_domain_outbox_events AS event
            WHERE event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
            """,
            (),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'failed', next_retry_at = NOW() + interval '5 minutes'
            WHERE project_id = %s AND uuid = (
                SELECT uuid
                FROM messenger_projection_tasks
                WHERE project_id = %s AND scope_key = %s
                ORDER BY created_at, ordering_created_at, outbox_event_uuid
                LIMIT 1
            )
            """,
            (blocked_project_id, blocked_project_id, blocked_scope_key),
        )

        claimed = v2_projection._claim_task(
            session,
            "integration:retry-blocked-project",
            30,
        )

        assert claimed is not None
        assert claimed["project_id"] == available_project_id


def test_fair_scheduler_filters_blocked_rows_before_candidate_limit(
    api,
    monkeypatch,
):
    project_id = sys_uuid.UUID(api.project_id)
    blocked_prefix = f"{project_id}:leased:"
    available_scope_key = f"{project_id}:available-after-leases"
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT gen_random_uuid(), %s, 'fanout', 'message',
                   %s || input.number::text,
                   jsonb_build_object('placement_uuid', gen_random_uuid()),
                   NOW() - interval '2 days' +
                       input.number * interval '1 microsecond',
                   NOW() - interval '2 days'
            FROM generate_series(1, %s) AS input(number)
            """,
            (
                project_id,
                blocked_prefix,
                v2_projection.CLAIM_CANDIDATE_LIMIT,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), %s, 'fanout', 'message', %s,
                jsonb_build_object('placement_uuid', gen_random_uuid()),
                NOW() - interval '1 day', NOW() - interval '1 day'
            )
            """,
            (project_id, available_scope_key),
        )
        assert v2_projection.derive_projection_tasks(session, 1000) == (
            v2_projection.CLAIM_CANDIDATE_LIMIT + 1
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = event.updated_at
            FROM messenger_domain_outbox_events AS event
            WHERE event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
            """,
            (),
        )
        session.execute(
            """
            INSERT INTO messenger_projection_scope_leases (
                uuid, project_id, scope_kind, scope_key, owner,
                fencing_token, lease_expires_at
            )
            SELECT gen_random_uuid(), task.project_id, task.scope_kind,
                   task.scope_key, 'integration:blocker', 1,
                   NOW() + interval '5 minutes'
            FROM messenger_projection_tasks AS task
            WHERE task.project_id = %s AND task.scope_key LIKE %s
            """,
            (project_id, f"{blocked_prefix}%"),
        )

        claimed = v2_projection._claim_task(
            session,
            "integration:scan-after-candidate-window",
            30,
        )

        assert claimed is not None
        assert claimed["scope_key"] == available_scope_key
