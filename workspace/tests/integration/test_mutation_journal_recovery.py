# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import time

import psycopg
from restalchemy.storage.sql import sessions

from workspace.services.messenger_workers import counters, mutation_runtime
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal_tests

conversation = journal_tests.conversation


def test_async_request_status_is_scoped_and_reports_committed_commands(
    api, db, conversation
):
    _stream, messages, peer = conversation
    response = api.post(
        f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read_up_to/invoke", user=peer
    )
    assert response.status_code == 200, response.text
    request_uuid = response.headers["X-Workspace-Mutation-Id"]
    url = f"/v1/messenger/message_mutations/{request_uuid}"
    pending = api.get(url, user=peer)
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "pending"
    assert api.get(url).status_code == 404
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        status = api.get(url, user=peer).json()
        assert (
            status["status"] == "applied"
            and status["command_count"] == status["applied_count"] == 1
        )
    finally:
        runtime.close()


def test_restart_counts_durable_invalidations_after_command_commit(
    api, db, conversation
):
    stream, messages, peer = conversation
    response = api.post(
        f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read_up_to/invoke", user=peer
    )
    assert response.status_code == 200, response.text
    first = mutation_runtime.MutationRuntime()
    try:
        deadline = time.monotonic() + 3
        while (
            db.execute(
                "SELECT status FROM messenger_mutation_commands WHERE request_uuid=%s",
                (response.headers["X-Workspace-Mutation-Id"],),
            ).fetchone()[0]
            == "pending"
        ):
            first.tick()
            assert time.monotonic() < deadline
    finally:
        first.close()
    # Stop after mutation commit, before the independent counter process has
    # consumed the durable invalidation. A restart must not lose that work.
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_counter_updates WHERE project_id=%s AND user_uuid=%s",
            (api.project_id, peer),
        ).fetchone()[0]
        > 0
    )
    assert (
        api.get(f"{native.STREAMS}{stream['uuid']}", user=peer).json()["unread_count"]
        == 2
    )
    restarted = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(restarted, db)
        assert (
            api.get(f"{native.STREAMS}{stream['uuid']}", user=peer).json()[
                "unread_count"
            ]
            == 0
        )
    finally:
        restarted.close()


def test_admitted_flags_do_not_cross_a_membership_generation(api, db, conversation):
    stream, messages, peer = conversation
    target = messages[-1]["uuid"]
    response = api.post(f"{native.MESSAGES}{target}/actions/star/invoke", user=peer)
    assert response.status_code == 200
    db.execute(
        "UPDATE messenger_stream_bindings SET membership_generation=membership_generation+1 WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
        (api.project_id, stream["uuid"], peer),
    )
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        assert (
            db.execute(
                "SELECT EXISTS(SELECT 1 FROM messenger_user_message_stars WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s)",
                (api.project_id, target, peer),
            ).fetchone()[0]
            is False
        )
        assert (
            db.execute(
                "SELECT status FROM messenger_mutation_commands WHERE request_uuid=%s",
                (response.headers["X-Workspace-Mutation-Id"],),
            ).fetchone()[0]
            == "rejected"
        )
    finally:
        runtime.close()


def test_admitted_edit_cannot_modify_a_reused_canonical_uuid(api, db, conversation):
    _stream, messages, _peer = conversation
    target = messages[-1]["uuid"]
    response = api.put(
        f"{native.MESSAGES}{target}",
        json={"payload": {"kind": "markdown", "content": "stale edit"}},
    )
    assert response.status_code == 200
    db.execute(
        "UPDATE messenger_messages SET ingest_sequence=ingest_sequence+1000000 WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s)",
        (api.project_id, api.project_id, target),
    )
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        assert (
            api.get(f"{native.MESSAGES}{target}").json()["payload"]["content"]
            == "second"
        )
        assert (
            db.execute(
                "SELECT status FROM messenger_mutation_commands WHERE request_uuid=%s",
                (response.headers["X-Workspace-Mutation-Id"],),
            ).fetchone()[0]
            == "rejected"
        )
    finally:
        runtime.close()


def test_healthy_runtime_does_not_start_full_catalog_repair_after_five_minutes(
    api, db, conversation, monkeypatch
):
    _stream, messages, peer = conversation
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)

        execute = sessions.PgSQLSession.execute

        def forbid_recount(session, statement, *args, **kwargs):
            if statement == counters.COUNT_TOPIC_SQL:
                raise AssertionError("A star mutation must not start a catalog recount")
            return execute(session, statement, *args, **kwargs)

        monkeypatch.setattr(sessions.PgSQLSession, "execute", forbid_recount)
        response = api.post(
            f"{native.MESSAGES}{messages[-1]['uuid']}/actions/star/invoke", user=peer
        )
        assert response.status_code == 200
        original = mutation_runtime.time.monotonic
        with monkeypatch.context() as clock:
            clock.setattr(mutation_runtime.time, "monotonic", lambda: original() + 301)
            runtime.tick()
        journal_tests.drain(runtime, db)
        assert (
            api.get(f"{native.MESSAGES}{messages[-1]['uuid']}", user=peer).json()[
                "starred"
            ]
            is True
        )
    finally:
        runtime.close()


def test_known_transaction_rollback_retries_without_full_catalog_repair(
    api, db, conversation, monkeypatch
):
    _stream, messages, peer = conversation
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        original = sessions.PgSQLSession.execute
        failed = False

        def inject(session, statement, *args, **kwargs):
            nonlocal failed
            if statement == counters.COUNT_TOPIC_SQL:
                raise AssertionError("A transaction retry must not recount the catalog")
            if not failed and statement.lstrip().startswith(
                "UPDATE messenger_mutation_commands SET status="
            ):
                failed = True
                raise psycopg.errors.SerializationFailure(
                    "Injected guaranteed rollback"
                )
            return original(session, statement, *args, **kwargs)

        monkeypatch.setattr(sessions.PgSQLSession, "execute", inject)
        response = api.post(
            f"{native.MESSAGES}{messages[-1]['uuid']}/actions/star/invoke", user=peer
        )
        assert response.status_code == 200
        journal_tests.drain(runtime, db)
        assert failed
        assert (
            api.get(f"{native.MESSAGES}{messages[-1]['uuid']}", user=peer).json()[
                "starred"
            ]
            is True
        )
    finally:
        runtime.close()
