# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Regressions for review findings in topic-owned message mutations."""

import concurrent.futures
import importlib.util
import json
import threading
import time
import uuid

import pytest
from restalchemy.common import contexts
from oslo_config import cfg

from workspace.common import mutation_journal_opts
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import journal_store
from workspace.services.messenger_workers import keyed_dispatcher
from workspace.services.messenger_workers import mutation_runtime
from workspace.tests.integration import test_mutation_journal as journal_tests
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native






conversation = journal_tests.conversation
@pytest.fixture
def journal(conversation, db):
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        yield runtime
    finally:
        runtime.close()






def test_read_up_to_leaves_hidden_personal_states_unchanged(
    api, db, conversation, record_property
):
    stream, messages, peer = conversation
    hidden, boundary = messages
    changed = db.execute(
        "UPDATE messenger_user_message_bindings SET visibility='hidden' WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s RETURNING uuid",
        (api.project_id, hidden["uuid"], peer),
    ).fetchall()
    assert len(changed) == 1
    assert api.get(f"{native.MESSAGES}{hidden['uuid']}", user=peer).status_code == 404
    assert api.get(f"{native.MESSAGES}{boundary['uuid']}", user=peer).status_code == 200
    response = api.post(
        f"{native.MESSAGES}{boundary['uuid']}/actions/read_up_to/invoke", user=peer
    )
    assert response.status_code == 200, response.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
    finally:
        runtime.close()
    state = db.execute(
        "SELECT read_at IS NOT NULL FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s",
        (api.project_id, hidden["uuid"], peer),
    ).fetchone()[0]
    record_property("hidden_state_marked_read", state)
    assert state is False


def test_terminal_non_delete_receipts_are_not_retained_forever(
    api, db, conversation, record_property
):
    _stream, messages, peer = conversation
    runtime = mutation_runtime.MutationRuntime()
    counts = []
    try:
        for _ in range(3):
            for _ in range(100):
                response = api.post(
                    f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read/invoke",
                    user=peer,
                )
                assert response.status_code == 200, response.text
            journal_tests.drain(runtime, db)
            counts.append(
                db.execute(
                    "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND status='applied' AND operation='read'",
                    (api.project_id,),
                ).fetchone()[0]
            )
        db.execute(
            "UPDATE messenger_mutation_commands SET created_at='2000-01-01',finished_at='2000-01-01' WHERE project_id=%s",
            (api.project_id,),
        )
    finally:
        runtime.close()
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        for _ in range(100):
            runtime.tick()
            time.sleep(0.002)
    finally:
        runtime.close()
    retained = db.execute(
        "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND operation='read' AND finished_at<'2001-01-01'",
        (api.project_id,),
    ).fetchone()[0]
    record_property("terminal_row_counts", str(counts))
    record_property("old_receipts_after_restart", retained)
    assert retained < counts[-1], (
        "No terminal receipt was collected, including receipts dated over 25 years ago"
    )




def test_deferred_message_publication_hides_revoked_membership(
    api, workspace_api, db, conversation, record_property
):
    stream, messages, peer = conversation
    target = messages[-1]["uuid"]
    response = api.put(
        f"{native.MESSAGES}{target}",
        json={"payload": {"kind": "markdown", "content": "Membership-fenced edit"}},
    )
    assert response.status_code == 200, response.text
    command = db.execute(
        "SELECT uuid,scope_key,payload FROM messenger_mutation_commands WHERE request_uuid=%s",
        (response.headers["X-Workspace-Mutation-Id"],),
    ).fetchone()
    runtime = mutation_runtime.MutationRuntime()
    try:
        runtime._execute(
            keyed_dispatcher.Work(
                ("command", command[1]),
                {command[0]},
                frozenset(
                    runtime._topic_reservations(
                        uuid.UUID(api.project_id), command[2]["topics"]
                    )
                ),
            )
        )
        pending = db.execute(
            "SELECT count(*) FROM messenger_publication_outbox WHERE project_id=%s AND kind='message.updated' AND %s=ANY(recipients)",
            (api.project_id, peer),
        ).fetchone()[0]
        assert pending > 0
        binding = db.execute(
            "SELECT uuid FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
            (api.project_id, stream["uuid"], peer),
        ).fetchone()[0]
        removed = api.delete(f"{native.STREAM_BINDINGS}{binding}")
        assert removed.status_code == 204, removed.text
        journal_tests.drain(runtime, db)
    finally:
        runtime.close()
    owner_events = workspace_api.get(
        native.EVENTS,
        project=api.project_id,
        user=api.user_uuid,
        params={"page_limit": 100},
    )
    peer_events = workspace_api.get(
        native.EVENTS, project=api.project_id, user=peer, params={"page_limit": 100}
    )
    assert owner_events.status_code == peer_events.status_code == 200

    def edited(events):
        return [
            row
            for row in events.json()
            if row["payload"].get("uuid") == target
            and row["payload"].get("kind") == "message.updated"
        ]

    owner_count, peer_count = len(edited(owner_events)), len(edited(peer_events))
    record_property("deferred_events_before_revocation", pending)
    record_property("owner_visible_edit_events", owner_count)
    record_property("revoked_peer_visible_edit_events", peer_count)
    assert owner_count > 0 and peer_count == 0


def test_private_dispatcher_lock_directory_current_symlink_behavior(
    tmp_path, record_property
):
    directory = tmp_path / "private-run"
    directory.mkdir(mode=0o700)
    victim = tmp_path / "existing-file"
    victim.write_text("Synthetic fixture")
    (directory / "mutation-dispatcher.lock").symlink_to(victim)
    with pytest.raises(OSError):
        mutation_runtime.MutationRuntime(singleton_directory=directory)
    record_property("test_directory_mode", oct(directory.stat().st_mode & 0o777))
    assert victim.read_text() == "Synthetic fixture"


def test_pending_admission_is_bounded_and_recovers_after_drain(api, db, conversation):
    _stream, messages, peer = conversation
    cfg.CONF.set_override("max_pending_per_user", 2, group=mutation_journal_opts.DOMAIN)
    runtime = mutation_runtime.MutationRuntime()
    try:
        path = f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read/invoke"
        accepted = [api.post(path, user=peer) for _ in range(2)]
        assert all(response.status_code == 200 for response in accepted)
        rejected = api.post(path, user=peer)
        assert rejected.status_code == 429, rejected.text
        assert "X-Workspace-Mutation-Id" not in rejected.headers
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND user_uuid=%s AND status='pending'",
                (api.project_id, peer),
            ).fetchone()[0]
            == 2
        )
        # Another project member has its own capacity.
        assert api.post(path).status_code == 200
        journal_tests.drain(runtime, db)
        assert api.post(path, user=peer).status_code == 200
        journal_tests.drain(runtime, db)
    finally:
        runtime.close()
        cfg.CONF.clear_override(
            "max_pending_per_user", group=mutation_journal_opts.DOMAIN
        )


def test_concurrent_admission_cannot_overfill_a_users_pending_slots(
    api, db, conversation
):
    _stream, messages, peer = conversation
    cfg.CONF.set_override("max_pending_per_user", 4, group=mutation_journal_opts.DOMAIN)
    start = threading.Barrier(8)

    def admit():
        start.wait(timeout=10)
        try:
            with contexts.Context().session_manager():
                store = journal_store.JournalStore(api.project_id, peer)
                store.perform_action(
                    "messages", uuid.UUID(messages[-1]["uuid"]), "read", {}
                )
            return True
        except messenger_exceptions.MutationQueueFullError:
            return False

    try:
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            accepted = sum(pool.map(lambda _: admit(), range(8)))
        assert 1 <= accepted <= 4
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND user_uuid=%s AND status='pending'",
                (api.project_id, peer),
            ).fetchone()[0]
            == accepted
        )
    finally:
        cfg.CONF.clear_override(
            "max_pending_per_user", group=mutation_journal_opts.DOMAIN
        )


def test_receipt_gc_keeps_entire_unfinished_requests(api, db, conversation):
    _stream, messages, peer = conversation
    for _ in range(2):
        assert (
            api.post(
                f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read/invoke",
                user=peer,
            ).status_code
            == 200
        )
    rows = db.execute(
        "SELECT uuid,request_uuid FROM messenger_mutation_commands WHERE project_id=%s ORDER BY sequence",
        (api.project_id,),
    ).fetchall()
    db.execute(
        "UPDATE messenger_mutation_commands SET status='applied',finished_at='2000-01-01' WHERE uuid=%s",
        (rows[0][0],),
    )
    db.execute(
        "UPDATE messenger_mutation_commands SET request_uuid=%s,created_at='2000-01-01' WHERE uuid=%s",
        (rows[0][1], rows[1][0]),
    )
    runtime = mutation_runtime.MutationRuntime()
    try:
        runtime._execute(keyed_dispatcher.Work(("receipts",), set(), frozenset()))
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE request_uuid=%s",
                (rows[0][1],),
            ).fetchone()[0]
            == 2
        )
        receipt = api.get(f"/v1/messenger/message_mutations/{rows[0][1]}", user=peer)
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["status"] == "pending"
        assert receipt.json()["command_count"] == 2
    finally:
        runtime.close()


def test_tombstone_migration_backfills_and_guards_unsafe_downgrade():
    path = (
        conftest.MIGRATIONS_DIR
        / "0258-Retain-message-delete-tombstones-and-bound-mutation-receipts-92730c.py"
    )
    spec = importlib.util.spec_from_file_location("journal_retention_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with conftest.isolated_migration_database(
        "0257-Order-batched-flag-journal-writes-and-suppress-consumer-echoes-c445d0.py"
    ) as database_url:
        with contexts.Context().session_manager() as session:
            module.migration_step.upgrade(session)
            module.migration_step.downgrade(session)
        import psycopg

        with psycopg.connect(database_url, autocommit=True) as connection:
            project, user, canonical = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conftest.seed_workspace_user(connection, user, f"user-{user}")
            native._register_project_user(connection, project, user)
            connection.execute(
                """INSERT INTO messenger_mutation_commands
                   (uuid,request_uuid,project_id,user_uuid,scope_key,operation,resource_uuid,payload,status,finished_at)
                   VALUES (%s,%s,%s,%s,%s,'delete',%s,%s::jsonb,'applied',NOW())""",
                (
                    uuid.uuid4(),
                    uuid.uuid4(),
                    project,
                    user,
                    f"{project}:message:{canonical}",
                    canonical,
                    json.dumps(
                        {
                            "topics": [
                                {
                                    "canonical_message_uuid": str(canonical),
                                    "ingest_sequence": 123,
                                }
                            ]
                        }
                    ),
                ),
            )
            with contexts.Context().session_manager() as session:
                module.migration_step.upgrade(session)
            connection.execute(
                "DELETE FROM messenger_project_users WHERE project_id=%s AND user_uuid=%s",
                (project, user),
            )
            assert connection.execute(
                "SELECT ingest_sequence FROM messenger_message_delete_tombstones WHERE project_id=%s AND canonical_message_uuid=%s",
                (project, canonical),
            ).fetchone() == (123,)
            assert (
                connection.execute(
                    "SELECT count(*) FROM messenger_mutation_commands"
                ).fetchone()[0]
                == 0
            )
            with pytest.raises(RuntimeError, match="consistent pre-upgrade snapshot"):
                with contexts.Context().session_manager() as session:
                    module.migration_step.downgrade(session)


@pytest.mark.parametrize("resource", ["streams", "stream_topics"])
def test_scope_read_uses_a_visible_boundary(api, db, conversation, resource):
    stream, messages, peer = conversation
    db.execute(
        "UPDATE messenger_user_message_bindings SET visibility='hidden' WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        (api.project_id, peer, messages[-1]["uuid"]),
    )
    resource_uuid = (
        stream["uuid"] if resource == "streams" else stream["default_topic_uuid"]
    )
    response = api.post(
        f"/v1/messenger/{resource}/{resource_uuid}/actions/read/invoke", user=peer
    )
    assert response.status_code == 200, response.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
    finally:
        runtime.close()
    receipt = api.get(
        f"/v1/messenger/message_mutations/{response.headers['X-Workspace-Mutation-Id']}",
        user=peer,
    )
    assert receipt.json()["status"] == "applied", receipt.text
    for message, visible in zip(messages, [True, False]):
        assert (
            db.execute(
                "SELECT read_at IS NOT NULL FROM messenger_user_message_states WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
                (api.project_id, peer, message["uuid"]),
            ).fetchone()[0]
            is visible
        )


def test_multi_topic_admission_rolls_back_when_queue_capacity_is_exhausted(
    api, db, conversation
):
    stream, _messages, peer = conversation
    topic = api.post(
        native.STREAM_TOPICS,
        json={"stream_uuid": stream["uuid"], "name": "Second topic"},
    )
    assert topic.status_code == 201, topic.text
    message = api.post(
        native.MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic.json()["uuid"],
            "payload": {"kind": "markdown", "content": "Second topic message"},
        },
    )
    assert message.status_code == 201, message.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        cfg.CONF.set_override(
            "max_pending_per_user", 1, group=mutation_journal_opts.DOMAIN
        )
        response = api.post(
            f"{native.STREAMS}{stream['uuid']}/actions/read/invoke", user=peer
        )
        assert response.status_code == 429, response.text
        assert "X-Workspace-Mutation-Id" not in response.headers
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND user_uuid=%s",
                (api.project_id, peer),
            ).fetchone()[0]
            == 0
        )
    finally:
        cfg.CONF.clear_override(
            "max_pending_per_user", group=mutation_journal_opts.DOMAIN
        )
        runtime.close()
