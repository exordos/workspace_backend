# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import psycopg
import pytest
from restalchemy.storage import exceptions as storage_exceptions
from restalchemy.storage.sql import sessions

from workspace.services.messenger_workers import mutation_runtime
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import test_mutation_journal as journal_tests
from workspace.tests.integration import test_messenger_v2 as native


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


def test_restart_rebuilds_parents_from_durable_topics_without_history_scan(
    api, db, conversation, monkeypatch
):
    stream, _messages, peer = conversation
    first = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(first, db)
    finally:
        first.close()
    assert db.execute(
        "SELECT initialized FROM messenger_mutation_projection_state"
    ).fetchone()[0]
    db.execute(
        "UPDATE messenger_stream_bindings SET unread_count=999,active_unread_count=999 WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
        (api.project_id, stream["uuid"], peer),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Normal restart must not scan raw message states")

    monkeypatch.setattr(v2_projection, "_refresh_recipient_counters", forbidden)
    restarted = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(restarted, db)
        assert (
            api.get(f"{native.STREAMS}{stream['uuid']}", user=peer).json()[
                "unread_count"
            ]
            == 2
        )
    finally:
        restarted.close()


def test_admitted_flags_do_not_cross_a_membership_generation(api, db, conversation):
    _stream, messages, peer = conversation
    target = messages[-1]["uuid"]
    response = api.post(f"{native.MESSAGES}{target}/actions/star/invoke", user=peer)
    assert response.status_code == 200
    db.execute(
        "UPDATE messenger_user_message_states SET membership_generation=membership_generation+1 WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s",
        (api.project_id, target, peer),
    )
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        assert (
            db.execute(
                "SELECT starred FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s",
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

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "Healthy live mutations must not wait behind full-catalog repair"
            )

        monkeypatch.setattr(runtime, "_recover", forbidden)
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


@pytest.mark.parametrize("wrapped", [False, True])
def test_known_transaction_rollback_retries_without_full_catalog_repair(
    api, db, conversation, monkeypatch, wrapped
):
    _stream, messages, peer = conversation
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        original = sessions.PgSQLSession.execute
        failed = False

        def inject(session, statement, *args, **kwargs):
            nonlocal failed
            if not failed and statement.lstrip().startswith(
                "UPDATE messenger_mutation_commands SET status="
            ):
                failed = True
                error = psycopg.errors.SerializationFailure(
                    "Injected guaranteed rollback"
                )
                if wrapped:
                    error = storage_exceptions.UnknownStorageException(caused=error)
                raise error
            return original(session, statement, *args, **kwargs)

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "A known rollback needs retry, not whole-catalog repair"
            )

        monkeypatch.setattr(sessions.PgSQLSession, "execute", inject)
        monkeypatch.setattr(runtime, "_recover", forbidden)
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
