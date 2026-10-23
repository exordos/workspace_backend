# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import datetime
import queue
import threading
import time
import uuid
from unittest import mock

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import sessions
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.bridges.zulip.dm import reactions
from workspace.common import mutation_journal_opts
from workspace.messenger_api.dm import external_models
from workspace.services import history_import
from workspace.services.messenger_workers import mutation_runtime
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_message_states as state_tests
from workspace.tests.integration import test_zulip_messages as message_tests
from workspace.tests.integration import test_zulip_realtime as realtime_tests
from workspace.tests.integration import test_zulip_reactions as reaction_tests
from workspace.tests.integration import test_zulip_task_workers as worker_tests

message_sources = state_tests.message_sources
history_account = message_tests.history_account
outgoing = worker_tests.outgoing


def test_native_observers_do_not_cross_lock_each_others_history_batches(
    outgoing, db, monkeypatch
):
    first = outgoing[1]
    owner = uuid.uuid4()
    conftest.seed_workspace_user(db, owner, f"cassi-history-lock-{owner}")
    peer = external_models.ExternalAccount(
        uuid=uuid.UUID(int=first.uuid.int + 1),
        project_id=first.project_id,
        owner_user_uuid=owner,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        settings=first.settings,
    )
    peer.insert()
    message_tests._channel(peer)
    sends = [_queued_send(outgoing), _queued_send(outgoing)]
    for identifier, send in zip((12345, 12346), sends):
        db.execute(
            "UPDATE bridge_zulip_v5_deliveries SET zulip_message_id=%s WHERE uuid=%s",
            (identifier, send.content.delivery_uuid),
        )
    for account in (first, peer):
        db.execute(
            "INSERT INTO bridge_zulip_v5_history_batches(external_account_uuid,project_id,hostname,first_id,covered_to,source_revision) VALUES(%s,%s,%s,12001,12999,'test')",
            (account.uuid, account.project_id, account.get_host()),
        )
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: True)
    process = queue.Queue()

    def observe_other():
        with contexts.Context().session_manager() as session:
            process.put(
                session.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            task = models.Task(
                project_id=peer.project_id,
                external_account_uuid=peer.uuid,
                content=models.ImportHistoryContent(
                    first_message_id=12346, last_message_id=12346
                ),
            )
            task.content._execute_import(
                task,
                peer,
                (
                    peer.realtime_revision(),
                    datetime.datetime.now(datetime.timezone.utc),
                    [message_tests._message_data(12346, reactions=[])],
                ),
            )

    try:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            with contexts.Context().session_manager() as session:
                session.execute("SET LOCAL statement_timeout='4s'")
                session.execute(
                    "SELECT first_id FROM bridge_zulip_v5_history_batches WHERE external_account_uuid=%s FOR UPDATE",
                    (first.uuid,),
                )
                pending = pool.submit(observe_other)
                pid = process.get(timeout=2)
                deadline = time.monotonic() + 3
                while (
                    db.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (pid,),
                    ).fetchone()[0]
                    != "Lock"
                ):
                    assert time.monotonic() < deadline
                    time.sleep(0.005)
                session.execute(
                    "UPDATE bridge_zulip_v5_deliveries SET content_observed_at=NOW() WHERE uuid=%s",
                    (sends[0].content.delivery_uuid,),
                )
            pending.result(timeout=4)
    finally:
        for send in sends:
            send.delete()
        peer.delete()


def test_inbox_yields_its_account_to_a_ready_first_send(outgoing):
    send = _queued_send(outgoing)
    account = outgoing[1]
    subscription = models.RealtimeSubscription.for_account(account)
    try:
        with contexts.Context().session_manager():
            models.RealtimeSubscription.append_events(
                account,
                subscription.generation,
                [
                    {
                        "id": 10,
                        "type": "message",
                        "message": message_tests._message_data(42, reactions=[]),
                        "flags": [],
                    }
                ],
            )
        with contexts.Context().session_manager():
            assert models.RealtimeEvent.dispatch_batch(16) == -1
        with contexts.Context().session_manager() as session:
            session.execute(
                "UPDATE bridge_zulip_v5_deliveries SET zulip_message_id=12345 WHERE uuid=%s",
                (send.content.delivery_uuid,),
            )
        with contexts.Context().session_manager():
            assert models.RealtimeEvent.dispatch_batch(16) == 1
    finally:
        send.delete()


def test_latency_migrations_round_trip_the_existing_journals(db):
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    own = sorted(
        path.name
        for path in conftest.MIGRATIONS_DIR.glob("02[67][0789]-*.py")
        if 267 <= int(path.name[:4]) <= 270
    )
    assert len(own) == 4
    try:
        for name in reversed(own):
            engine.rollback_migration(name)
        assert (
            db.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname LIKE '%_wake_owned' AND NOT tgisinternal"
            ).fetchone()[0]
            == 0
        )
    finally:
        engine.apply_migration(engine.get_latest_migration())
    assert (
        db.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname LIKE '%_wake_owned' AND NOT tgisinternal"
        ).fetchone()[0]
        == 4
    )


@pytest.mark.parametrize("changed", [False, True])
def test_delivery_bookkeeping_does_not_reinvalidate_all_recipient_flags(
    outgoing, db, changed
):
    send = _queued_send(outgoing)
    try:
        db.execute(
            "UPDATE bridge_zulip_v5_deliveries SET zulip_message_id=12345 WHERE uuid=%s",
            (send.content.delivery_uuid,),
        )
        before = db.execute(
            "SELECT state_uuid,revision FROM bridge_zulip_v5_flag_changes WHERE project_id=%s ORDER BY state_uuid",
            (send.project_id,),
        ).fetchall()
        assert before
        db.execute(
            "UPDATE bridge_zulip_v5_deliveries SET zulip_message_id=%s,content_observed_at=NOW() WHERE uuid=%s",
            (12346 if changed else 12345, send.content.delivery_uuid),
        )
        after = db.execute(
            "SELECT state_uuid,revision FROM bridge_zulip_v5_flag_changes WHERE project_id=%s ORDER BY state_uuid",
            (send.project_id,),
        ).fetchall()
        assert (before != after) == changed
    finally:
        send.delete()


def test_outgoing_account_guard_allows_an_observers_delivery_foreign_key_check(
    outgoing, db
):
    send = _queued_send(outgoing)
    account = outgoing[1]
    process = queue.Queue()

    def claim_delivery():
        with contexts.Context().session_manager() as session:
            process.put(
                session.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            send.content.delivery(send)

    try:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            with contexts.Context().session_manager() as session:
                session.execute("SET LOCAL statement_timeout='4s'")
                session.execute(
                    "SELECT uuid FROM bridge_zulip_v5_deliveries WHERE uuid=%s FOR UPDATE",
                    (send.content.delivery_uuid,),
                )
                pending = pool.submit(claim_delivery)
                pid = process.get(timeout=2)
                deadline = time.monotonic() + 3
                while (
                    db.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (pid,),
                    ).fetchone()[0]
                    != "Lock"
                ):
                    assert time.monotonic() < deadline
                    time.sleep(0.005)
                # Delivery observations validate the sender account FK while
                # holding this delivery row, without changing that account.
                session.execute(
                    "SELECT uuid FROM m_external_accounts_v2 WHERE uuid=%s FOR KEY SHARE",
                    (account.uuid,),
                )
            pending.result(timeout=4)
    finally:
        send.delete()


def test_provider_message_input_wakes_the_owned_worker_only_after_commit(
    message_sources,
):
    account, source = message_sources()
    with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as listener:
        listener.execute("LISTEN workspace_mutation_commands")
        with contexts.Context().session_manager():
            source.timestamp += 1
            source.update()
            assert not list(listener.notifies(timeout=0.02))
        notifications = list(listener.notifies(timeout=0.25, stop_after=1))
        assert [notice.channel for notice in notifications] == [
            "workspace_mutation_commands"
        ]


def test_import_and_delivery_share_cache_before_flag_journal_lock_order(
    message_sources, db, monkeypatch
):
    first, source = message_sources()
    other, _ = message_sources(project_id=first.project_id, sender_id=8)
    state_tests._prepare(first)
    state_tests._prepare(other)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(32, project_id=first.project_id)
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s LIMIT 1",
        (first.project_id,),
    ).fetchone()[0]
    delivery = models.MessageDelivery(
        project_id=first.project_id,
        message_uuid=canonical,
        stream_uuid=source.channel.stream_uuid,
        external_account_uuid=first.uuid,
        hostname=first.get_host(),
        external_account_id=first.external_account_id,
        recipient={"to": 42},
        topic_name="General",
        zulip_message_id=42,
    )
    delivery.insert()
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: True)
    monkeypatch.setattr(client, "ZulipClient", lambda account: mock.Mock())
    process = queue.Queue()

    def import_other():
        with contexts.Context().session_manager() as session:
            process.put(
                session.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            task = models.Task(
                project_id=other.project_id,
                external_account_uuid=other.uuid,
                content=models.ImportHistoryContent(
                    first_message_id=42, last_message_id=42
                ),
            )
            task.content._execute_import(
                task,
                other,
                (
                    other.realtime_revision(),
                    datetime.datetime.now(datetime.timezone.utc),
                    [message_tests._message_data(42, reactions=[])],
                ),
            )

    try:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            with contexts.Context().session_manager() as session:
                session.execute("SET LOCAL statement_timeout='4s'")
                reactions.prepare_messages(first, [42])
                pending = pool.submit(import_other)
                pid = process.get(timeout=2)
                deadline = time.monotonic() + 3
                while (
                    db.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (pid,),
                    ).fetchone()[0]
                    != "Lock"
                ):
                    assert time.monotonic() < deadline
                    time.sleep(0.005)
                # The delivery trigger invalidates every recipient's flags.
                session.execute(
                    "UPDATE bridge_zulip_v5_deliveries SET zulip_message_id=zulip_message_id WHERE uuid=%s",
                    (delivery.uuid,),
                )
            pending.result(timeout=4)
    finally:
        delivery.delete()


def test_import_batches_fresh_observations_without_per_message_catalog_reads(
    history_account, monkeypatch, db
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = [
        message_tests._message_data(i) for i in range(40, 50)
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: True)
    original = sessions.PgSQLSession.execute
    reads, inserts = [], []

    def capture(session, statement, *args, **kwargs):
        if (
            'FROM "bridge_zulip_v5_channels"' in statement
            or 'FROM "bridge_zulip_v5_messages"' in statement
        ):
            reads.append(statement)
        if statement.startswith(
            'INSERT INTO "bridge_zulip_v5_messages"'
        ) or statement.startswith('INSERT INTO "bridge_zulip_v5_message_contents"'):
            inserts.append(statement)
        return original(session, statement, *args, **kwargs)

    monkeypatch.setattr(sessions.PgSQLSession, "execute", capture)
    assert message_tests._import_history(account, first=40, last=49).status == "done"
    assert (
        db.execute(
            "SELECT count(*) FROM bridge_zulip_v5_messages WHERE project_id=%s",
            (account.project_id,),
        ).fetchone()[0]
        == 10
    )
    assert len(reads) <= 2
    assert not inserts


def _priority_task(content, priority):
    task = models.Task(
        queue="realtime",
        concurrency_key=str(uuid.uuid4()),
        priority=priority,
        content=content,
    )
    task.insert()
    with contexts.Context().session_manager() as session:
        session.execute(
            "UPDATE bridge_zulip_v5_tasks SET concurrency_key=%s WHERE uuid=%s",
            (str(uuid.uuid4()), task.uuid),
        )
    return task


def _flag_task():
    return _priority_task(
        models.UpdateMessageFlagsContent(
            state_uuid=uuid.uuid4(),
            flag="read",
            value=True,
            changed_at=datetime.datetime.now(datetime.timezone.utc),
            account_revision="test",
        ),
        550_000,
    )


def _queued_send(outgoing):
    before = {task.uuid for task in worker_tests.outgoing_tests.tasks(outgoing[1])}
    worker_tests.outgoing_tests.create(outgoing)
    worker_tests.outgoing_tests.dispatch()
    send = next(
        task
        for task in worker_tests.outgoing_tests.tasks(outgoing[1])
        if task.uuid not in before
    )
    send.priority = 300_000  # A task persisted before the priority change.
    send.update()
    return send


def test_first_send_overtakes_confirmation_of_an_already_posted_message(outgoing, db):
    confirmation = _queued_send(outgoing)
    confirmation.priority = 800_000
    confirmation.update()
    db.execute(
        "UPDATE bridge_zulip_v5_deliveries SET zulip_message_id=12345 WHERE uuid=%s",
        (confirmation.content.delivery_uuid,),
    )
    send = _queued_send(outgoing)
    try:
        with contexts.Context().session_manager():
            claimed = history_import.HistoryImportService(
                queue="realtime"
            )._claim_tasks(1)
        assert claimed[0][0] == send.uuid
    finally:
        send.delete()
        confirmation.delete()


@pytest.mark.parametrize("owned", [False, True])
def test_owned_first_send_does_not_wait_for_an_account_flag_http_lease(
    outgoing, db, monkeypatch, owned
):
    send = _queued_send(outgoing)
    flag = _flag_task()
    account = outgoing[1]
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: owned)
    try:
        state_uuid = db.execute(
            "SELECT uuid FROM messenger_user_message_states WHERE project_id=%s LIMIT 1",
            (account.project_id,),
        ).fetchone()[0]
        db.execute(
            "UPDATE bridge_zulip_v5_tasks SET content=jsonb_set(content,'{state_uuid}',to_jsonb(%s::text)),project_id=%s,external_account_uuid=%s,lease_owner=%s,lease_expires_at=NOW()+interval '1 minute' WHERE uuid=%s",
            (state_uuid, account.project_id, account.uuid, uuid.uuid4(), flag.uuid),
        )
        with contexts.Context().session_manager():
            claimed = history_import.HistoryImportService(
                queue="realtime", workers=4
            )._claim_tasks(1)
        assert [row[0] for row in claimed] == ([send.uuid] if owned else [])
    finally:
        send.delete()
        flag.delete()


def test_realtime_claim_orders_sends_receives_edits_before_existing_flags(outgoing, db):
    send = _queued_send(outgoing)
    tasks = [
        _flag_task(),
        _priority_task(
            models.EditMessageContent(
                delivery_uuid=send.content.delivery_uuid, content="edit"
            ),
            250_000,
        ),
        _priority_task(
            models.ImportHistoryContent(first_message_id=42, last_message_id=42),
            300_000,
        ),
        send,
    ]
    try:
        with contexts.Context().session_manager():
            claimed = history_import.HistoryImportService(
                queue="realtime"
            )._claim_tasks(4)
        assert [item[0] for item in claimed] == [task.uuid for task in reversed(tasks)]
    finally:
        for task in tasks:
            task.delete()


def test_realtime_claim_keeps_one_send_slot_when_flags_are_already_running(
    outgoing, db
):
    tasks = [_flag_task() for _ in range(6)]
    try:
        service = history_import.HistoryImportService(queue="realtime", workers=4)
        with contexts.Context().session_manager():
            claimed = service._claim_tasks(4)
        assert len(claimed) == 3
        send = _queued_send(outgoing)
        tasks.append(send)
        with contexts.Context().session_manager():
            assert service._claim_tasks(1)[0][0] == send.uuid
    finally:
        for task in tasks:
            task.delete()


def test_send_already_running_does_not_waste_an_additional_slot(outgoing, db):
    send = _queued_send(outgoing)
    flag = _flag_task()
    try:
        service = history_import.HistoryImportService(queue="realtime", workers=2)
        with contexts.Context().session_manager():
            assert service._claim_tasks(1)[0][0] == send.uuid
        with contexts.Context().session_manager():
            assert service._claim_tasks(1)[0][0] == flag.uuid
    finally:
        send.delete()
        flag.delete()


@pytest.mark.parametrize("owned", [False, True])
def test_malformed_inbox_message_does_not_block_its_batch(
    history_account, db, monkeypatch, owned, scheduled_reconciliation
):
    account = history_account
    account.settings = {
        **account.settings,
        "email": "owner@example.com",
        "api_key": "test",
    }
    account.status = "active"
    account.update()
    subscription = realtime_tests.register(account)
    for event in realtime_tests.events(account):
        event.processed_at = datetime.datetime.now(datetime.timezone.utc)
        event.update()
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: owned)
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {
                    "id": 10,
                    "type": "message",
                    "message": {"reactions": []},
                    "flags": [],
                },
                {
                    "id": 11,
                    "type": "message",
                    "message": {"id": 42, "reactions": []},
                    "flags": [],
                },
            ],
        )
    with contexts.Context().session_manager():
        assert models.RealtimeEvent.dispatch_batch(16) == 2
    rows = db.execute(
        "SELECT event_id,processed_at IS NOT NULL,attempts FROM bridge_zulip_v5_events WHERE external_account_uuid=%s AND source='zulip' ORDER BY event_id",
        (account.uuid,),
    ).fetchall()
    assert rows == [(10, False, 1), (11, True, 0)]


def test_empty_reaction_snapshot_batch_is_bounded_and_keeps_newer_events(
    message_sources, db, monkeypatch
):
    account, _source = message_sources()
    before = datetime.datetime.now(datetime.timezone.utc)
    at = before + datetime.timedelta(seconds=1)
    after = at + datetime.timedelta(seconds=1)
    with contexts.Context().session_manager():
        for identifier, observed_at in [(42, before), (43, after)]:
            message = reactions.message_for(account, identifier)
            reactions._record(message, reaction_tests.reaction(), True, observed_at)
    original = sessions.PgSQLSession.execute
    queries = []

    def capture(session, statement, *args, **kwargs):
        queries.append(statement)
        return original(session, statement, *args, **kwargs)

    monkeypatch.setattr(sessions.PgSQLSession, "execute", capture)
    with contexts.Context().session_manager():
        reactions.observe_snapshot(
            account,
            [{"id": identifier, "reactions": []} for identifier in range(42, 58)],
            at,
        )
    assert db.execute(
        "SELECT message.message_id,reaction.present FROM bridge_zulip_v5_reactions reaction JOIN bridge_zulip_v5_reaction_messages message ON message.uuid=reaction.reaction_message_uuid WHERE message.project_id=%s ORDER BY message.message_id",
        (account.project_id,),
    ).fetchall() == [(42, False), (43, True)]
    assert len(queries) <= 8


@pytest.mark.parametrize("consumer", ["inbox", "import", "source_task"])
def test_poll_cursor_can_advance_while_the_account_is_processing(
    history_account, db, consumer, scheduled_reconciliation, monkeypatch
):
    account = history_account
    account.settings = {
        **account.settings,
        "email": "owner@example.com",
        "api_key": "test",
    }
    account.status = "active"
    account.update()
    subscription = realtime_tests.register(account)
    for event in realtime_tests.events(account):
        event.processed_at = datetime.datetime.now(datetime.timezone.utc)
        event.update()
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [{"id": 10, "type": "message", "message": {"id": 42}}],
        )
        owner = uuid.uuid4()
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            realtime_event_uuid=(
                realtime_tests.events(account)[-1].uuid
                if consumer == "source_task"
                else None
            ),
            content=models.ImportHistoryContent(
                first_message_id=42, last_message_id=42
            ),
            lease_owner=owner,
            lease_expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=1),
        )
        task.insert()
    processing = queue.Queue()
    release = threading.Event()

    if consumer == "source_task":

        def hold(content, task):
            processing.put(True)
            assert release.wait(4)

        monkeypatch.setattr(models.ImportHistoryContent, "execute", hold)

    def consume():
        with contexts.Context().session_manager():
            if consumer == "source_task":
                task.execute()
                return
            if consumer == "import":
                assert (
                    history_import.HistoryImportService._locked_task(task.uuid, owner)
                    is not None
                )
            else:
                assert models.RealtimeEvent.dispatch_batch(16) == 1
            processing.put(True)
            assert release.wait(4)

    def append():
        with contexts.Context().session_manager():
            return models.RealtimeSubscription.append_events(
                account,
                subscription.generation,
                [{"id": 11, "type": "message", "message": {"id": 43}}],
            )

    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        pending = executor.submit(consume)
        try:
            assert processing.get(timeout=2)
            assert executor.submit(append).result(timeout=1) == 11
        finally:
            release.set()
        pending.result(timeout=2)


def test_owned_ingress_waits_for_its_topic_projection(message_sources, db, monkeypatch):
    account, _source = message_sources()
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: True)
    runtime = mutation_runtime.MutationRuntime()
    try:
        with mock.patch.object(runtime._dispatcher, "trigger") as trigger:
            with contexts.Context().session_manager() as session:
                runtime._scan_provider_inputs(session)
            assert not [
                call
                for call in trigger.call_args_list
                if call.args[0][0] == "provider_message"
            ]
            state_tests._prepare(account)
            with contexts.Context().session_manager() as session:
                runtime._scan_provider_inputs(session)
            assert [
                call
                for call in trigger.call_args_list
                if call.args[0][0] == "provider_message"
            ]
    finally:
        runtime.close()


def test_owned_message_creation_does_not_wait_for_later_topic_observations(
    message_sources, db, monkeypatch
):
    account, _source = message_sources()
    state_tests._prepare(account)
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: True)
    runtime = mutation_runtime.MutationRuntime()
    try:
        with contexts.Context().session_manager() as session:
            runtime._scan_provider_inputs(session)
        work = next(
            work
            for work in runtime._dispatcher._pending.values()
            if work.key[0] == "provider_message"
        )
        with psycopg.connect(conftest.TEST_DB_URL) as incoming:
            incoming.execute(
                "UPDATE bridge_zulip_v5_topic_changes SET revision=revision WHERE project_id=%s",
                (account.project_id,),
            )
            # _prepare acknowledges its topic input. Recreate the next
            # observation before holding the row, just as another importer does.
            incoming.execute(
                "INSERT INTO bridge_zulip_v5_topic_changes(project_id,topic_uuid) VALUES(%s,%s) ON CONFLICT(project_id,topic_uuid) DO UPDATE SET revision=EXCLUDED.revision",
                (account.project_id, work.key[2][0]),
            )
            with concurrent.futures.ThreadPoolExecutor(1) as executor:
                future = executor.submit(runtime._execute, work)
                try:
                    future.result(timeout=1)
                finally:
                    incoming.rollback()
    finally:
        runtime.close()


def test_newer_input_during_owned_commit_does_not_roll_back_message_visibility(
    message_sources, db, monkeypatch
):
    account, _source = message_sources()
    state_tests._prepare(account)
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: True)
    runtime = mutation_runtime.MutationRuntime()
    original = models.ZulipMessageChange.acknowledge_many

    def incoming_before_acknowledgement(changes):
        db.execute(
            "UPDATE bridge_zulip_v5_message_changes SET revision=revision+1 WHERE project_id=%s",
            (account.project_id,),
        )
        original(changes)

    try:
        with contexts.Context().session_manager() as session:
            runtime._scan_provider_inputs(session)
        work = next(
            work
            for work in runtime._dispatcher._pending.values()
            if work.key[0] == "provider_message"
        )
        with monkeypatch.context() as concurrent_input:
            concurrent_input.setattr(
                models.ZulipMessageChange,
                "acknowledge_many",
                incoming_before_acknowledgement,
            )
            runtime._execute(work)
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
                (account.project_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT count(*) FROM bridge_zulip_v5_message_changes WHERE project_id=%s",
                (account.project_id,),
            ).fetchone()[0]
            == 1
        )
        runtime._execute(work)
        assert (
            db.execute(
                "SELECT count(*) FROM bridge_zulip_v5_message_changes WHERE project_id=%s",
                (account.project_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        runtime.close()


def test_inbox_batches_flags_without_changing_their_event_versions(
    history_account, db, monkeypatch, scheduled_reconciliation
):
    account = history_account
    account.settings = {
        **account.settings,
        "email": "owner@example.com",
        "api_key": "test",
    }
    account.status = "active"
    account.update()
    subscription = realtime_tests.register(account)
    for event in realtime_tests.events(account):
        event.processed_at = datetime.datetime.now(datetime.timezone.utc)
        event.update()
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: True)
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {
                    "id": 10,
                    "type": "message",
                    "flags": ["starred"],
                    "message": {"id": 42},
                },
                {
                    "id": 11,
                    "type": "update_message_flags",
                    "flag": "starred",
                    "op": "remove",
                    "messages": [42],
                },
                {"id": 12, "type": "message", "flags": ["read"], "message": {"id": 43}},
            ],
        )

    original = sessions.PgSQLSession.execute
    inserts = []

    def capture(session, statement, *args, **kwargs):
        if statement.lstrip().startswith("INSERT INTO bridge_zulip_v5_message_flags"):
            inserts.append(statement)
        return original(session, statement, *args, **kwargs)

    monkeypatch.setattr(sessions.PgSQLSession, "execute", capture)
    with contexts.Context().session_manager():
        assert models.RealtimeEvent.dispatch_batch(16) == 3
    rows = db.execute(
        "SELECT message_id,flag,value,event_id FROM bridge_zulip_v5_message_flags WHERE external_account_uuid=%s ORDER BY message_id,flag",
        (account.uuid,),
    ).fetchall()
    assert rows == [
        (42, "read", False, 10),
        (42, "starred", False, 11),
        (43, "read", True, 12),
        (43, "starred", False, 12),
    ]
    assert len(inserts) == 2


def test_owned_ingress_coalesces_messages_sharing_a_topic(
    message_sources, db, monkeypatch
):
    account, _source = message_sources()
    for index in range(1, 10):
        message_sources(
            project_id=account.project_id, message_id=42 + index, sender_id=7 + index
        )
    state_tests._prepare(account)
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: True)
    runtime = mutation_runtime.MutationRuntime()
    try:
        with contexts.Context().session_manager() as session:
            runtime._scan_provider_inputs(session)
        work = [
            work
            for work in runtime._dispatcher._pending.values()
            if work.key[0] == "provider_message"
        ]
        assert len(work) == 1
        assert len(work[0].values) == 10
        original = sessions.PgSQLSession.execute
        recipient_reads = []

        def capture(session, statement, *args, **kwargs):
            if (
                'FROM "messenger_user_message_states"' in statement
                or 'FROM "messenger_user_message_bindings"' in statement
            ):
                recipient_reads.append(statement)
            return original(session, statement, *args, **kwargs)

        monkeypatch.setattr(sessions.PgSQLSession, "execute", capture)
        runtime._execute(work[0])
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
                (account.project_id,),
            ).fetchone()[0]
            == 10
        )
        assert len(recipient_reads) <= 2
    finally:
        runtime.close()


def test_inbox_batch_uses_the_same_reaction_cache_order_as_imports(
    history_account, db, scheduled_reconciliation
):
    account = history_account
    account.settings = {
        **account.settings,
        "email": "owner@example.com",
        "api_key": "test",
    }
    account.status = "active"
    account.update()
    subscription = realtime_tests.register(account)
    for event in realtime_tests.events(account):
        event.processed_at = datetime.datetime.now(datetime.timezone.utc)
        event.update()
    with contexts.Context().session_manager():
        for identifier in (42, 43):
            reactions.message_for(account, identifier)
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {
                    "id": identifier,
                    "type": "message",
                    "flags": [],
                    "message": message_tests._message_data(identifier, reactions=[]),
                }
                for identifier in (42, 43)
            ],
        )
    processes = queue.Queue()

    def dispatch():
        with contexts.Context().session_manager() as session:
            processes.put(
                session.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            return models.RealtimeEvent.dispatch_batch(16)

    with psycopg.connect(conftest.TEST_DB_URL) as importing:
        importing.execute("SET LOCAL statement_timeout='4s'")
        importing.execute(
            "UPDATE bridge_zulip_v5_reaction_messages SET snapshot_at=snapshot_at "
            "WHERE project_id=%s AND message_id=43",
            (account.project_id,),
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(dispatch)
            process = processes.get(timeout=3)
            deadline = time.monotonic() + 3
            try:
                while (
                    importing.info.backend_pid
                    not in db.execute(
                        "SELECT pg_blocking_pids(%s)", (process,)
                    ).fetchone()[0]
                ):
                    assert time.monotonic() < deadline
                    assert not future.done()
                    time.sleep(0.005)
                importing.execute(
                    "UPDATE bridge_zulip_v5_reaction_messages SET snapshot_at=snapshot_at "
                    "WHERE project_id=%s AND message_id=42",
                    (account.project_id,),
                )
                importing.commit()
            finally:
                importing.rollback()
            assert future.result(timeout=3) == 2
    assert (
        db.execute(
            "SELECT count(*) FROM bridge_zulip_v5_events WHERE external_account_uuid=%s "
            "AND processed_at IS NULL",
            (account.uuid,),
        ).fetchone()[0]
        == 0
    )


def test_shared_topic_ingress_does_not_deadlock_message_acknowledgement(
    message_sources, db
):
    account, source = message_sources(message_id=42)
    other, second = message_sources(
        project_id=account.project_id, message_id=43, sender_id=8
    )
    canonical = uuid.uuid5(account.project_id, str(source.message_uuid))
    revision = db.execute(
        "SELECT revision FROM bridge_zulip_v5_message_changes "
        "WHERE project_id=%s AND message_uuid=%s",
        (account.project_id, canonical),
    ).fetchone()[0]
    topic = db.execute(
        "SELECT topic_uuid FROM bridge_zulip_v5_topic_changes WHERE project_id=%s",
        (account.project_id,),
    ).fetchone()[0]
    incoming = uuid.uuid4()
    processes = queue.Queue()

    def import_observation():
        with psycopg.connect(conftest.TEST_DB_URL) as connection:
            processes.put(connection.info.backend_pid)
            connection.execute(
                """INSERT INTO bridge_zulip_v5_messages
                   (uuid,project_id,external_account_uuid,message_uuid,message_id,
                    sender_id,timestamp,channel,topic,content,read,starred,mentioned)
                   SELECT %s,project_id,%s,message_uuid,message_id,sender_id,timestamp,
                          %s,topic,content,read,starred,mentioned
                   FROM bridge_zulip_v5_messages WHERE uuid=%s""",
                (incoming, other.uuid, second.channel.uuid, source.uuid),
            )

    with psycopg.connect(conftest.TEST_DB_URL) as reconciliation:
        reconciliation.execute("SET LOCAL statement_timeout='4s'")
        reconciliation.execute(
            "UPDATE bridge_zulip_v5_topic_changes SET revision=revision "
            "WHERE project_id=%s AND topic_uuid=%s",
            (account.project_id, topic),
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(import_observation)
            process = processes.get(timeout=3)
            deadline = time.monotonic() + 3
            try:
                while (
                    reconciliation.info.backend_pid
                    not in db.execute(
                        "SELECT pg_blocking_pids(%s)", (process,)
                    ).fetchone()[0]
                ):
                    assert time.monotonic() < deadline
                    assert not future.done()
                    time.sleep(0.005)
                # This is the same journal acknowledgement that deadlocked with
                # new observations after topic reconciliation in the live worker.
                reconciliation.execute(
                    "DELETE FROM bridge_zulip_v5_message_changes "
                    "WHERE project_id=%s AND message_uuid=%s AND revision=%s",
                    (account.project_id, canonical, revision),
                )
                reconciliation.commit()
            finally:
                reconciliation.rollback()
            future.result(timeout=3)
    assert (
        db.execute(
            "SELECT count(*) FROM bridge_zulip_v5_messages WHERE uuid=%s", (incoming,)
        ).fetchone()[0]
        == 1
    )
    assert (
        db.execute(
            "SELECT revision FROM bridge_zulip_v5_message_changes "
            "WHERE project_id=%s AND message_uuid=%s",
            (account.project_id, canonical),
        ).fetchone()[0]
        > revision
    )


def test_live_burst_does_not_fetch_every_message_separately(
    history_account, db, monkeypatch, scheduled_reconciliation
):
    account = history_account
    account.settings = {
        **account.settings,
        "email": "owner@example.com",
        "api_key": "test",
    }
    account.status = "active"
    account.update()
    subscription = realtime_tests.register(account)
    for event in realtime_tests.events(account):
        event.processed_at = datetime.datetime.now(datetime.timezone.utc)
        event.update()
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {"id": index, "type": "message", "message": {"id": index}}
                for index in range(1, 51)
            ],
        )
    while True:
        with contexts.Context().session_manager():
            if not models.RealtimeEvent.dispatch_one():
                break
    zulip = mock.Mock()
    zulip.get_messages.side_effect = lambda first, last: [
        message_tests._message_data(index) for index in range(first, last + 1)
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    service = history_import.HistoryImportService(queue="realtime", workers=1)
    try:
        worker_tests._until(
            service,
            lambda: (
                db.execute(
                    "SELECT count(*) FROM messenger_api_user_messages_v1 "
                    "WHERE project_id=%s AND user_uuid=%s AND visible",
                    (account.project_id, account.owner_user_uuid),
                ).fetchone()[0]
                == 50
            ),
        )
        assert zulip.get_messages.call_count <= 4
    finally:
        service.stop()
