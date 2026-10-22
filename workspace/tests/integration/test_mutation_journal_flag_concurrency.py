# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import datetime
import threading
import time
import uuid

from restalchemy.common import contexts
from restalchemy.storage.sql import sessions

from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import models
from workspace.services.messenger_workers import keyed_dispatcher
from workspace.tests.integration import test_mutation_journal as journal_tests
from workspace.tests.integration import test_mutation_journal_provider as provider_tests
from workspace.tests.integration import test_zulip_flags as flag_tests


message_sources = flag_tests.message_sources
imported = flag_tests.imported
journal = provider_tests.journal


def test_incoming_flag_batch_and_topic_consumer_cannot_deadlock(
    db, imported, journal, monkeypatch
):
    account, source, _state, *_rest = imported
    with contexts.Context().session_manager():
        models.ZulipMessage(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            message_uuid=uuid.uuid5(source.message_uuid, "next-message"),
            message_id=source.message_id + 1,
            sender_id=source.sender_id,
            timestamp=source.timestamp + 1,
            channel=source.channel,
            topic=source.topic,
            content=source.content,
            read=True,
            starred=False,
            mentioned=False,
        ).insert()
    journal_tests.drain(journal, db)
    ids = [source.message_id, source.message_id + 1]
    with contexts.Context().session_manager():
        flags.record(
            account, ids, "read", False, datetime.datetime.now(datetime.timezone.utc)
        )
    assert (
        db.execute(
            "SELECT count(*) FROM bridge_zulip_v5_message_flags WHERE external_account_uuid=%s AND flag='read' AND NOT value AND message_id=ANY(%s::bigint[])",
            (account.uuid, ids),
        ).fetchone()[0]
        == 2
    )
    rows = db.execute(
        "SELECT state.uuid,placement.topic_uuid,observed.message_id FROM messenger_user_message_states state "
        "JOIN messenger_message_placements placement ON placement.project_id=state.project_id "
        "AND placement.uuid=state.placement_uuid JOIN bridge_zulip_v5_messages observed "
        "ON observed.project_id=placement.project_id AND observed.external_account_uuid=%s "
        "AND messenger_uuid_v5(observed.project_id,observed.message_uuid::text)=placement.message_uuid "
        "WHERE state.project_id=%s AND state.user_uuid=%s",
        (account.uuid, account.project_id, account.owner_user_uuid),
    ).fetchall()
    assert len(rows) == 2
    first_ack, release, producer_started = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    worker_ident, worker_pid, producer_pid = [], [], []
    acknowledged_states, producer_ids = [], []
    execute = sessions.PgSQLSession.execute

    def interleave(session, statement, *args, **kwargs):
        result = execute(session, statement, *args, **kwargs)
        if worker_ident and threading.get_ident() == worker_ident[0]:
            if statement.startswith("SET TRANSACTION"):
                execute(session, "SET LOCAL deadlock_timeout='100ms'")
                worker_pid.append(
                    execute(session, "SELECT pg_backend_pid() AS pid").fetchone()["pid"]
                )
            if (
                statement.lstrip().startswith(
                    "DELETE FROM bridge_zulip_v5_flag_changes"
                )
                and not first_ack.is_set()
            ):
                acknowledged_states.append(uuid.UUID(str(args[0][-2])))
                first_ack.set()
                assert release.wait(5)
        return result

    monkeypatch.setattr(sessions.PgSQLSession, "execute", interleave)

    def consume():
        worker_ident.append(threading.get_ident())
        return journal._execute(
            keyed_dispatcher.Work(
                ("provider_flags", account.project_id, rows[0][1]),
                {row[0] for row in rows},
                frozenset({("topic", account.project_id, rows[0][1])}),
            )
        )

    def produce():
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL deadlock_timeout='100ms'")
            producer_pid.append(
                session.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            producer_started.set()
            flags.record(
                account,
                producer_ids,
                "read",
                True,
                datetime.datetime.now(datetime.timezone.utc),
            )

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        consumer = pool.submit(consume)
        assert first_ack.wait(5)
        producer_ids.extend(row[2] for row in rows if row[0] != acknowledged_states[0])
        producer_ids.extend(row[2] for row in rows if row[0] == acknowledged_states[0])
        producer = pool.submit(produce)
        try:
            assert producer_started.wait(5)
            deadline = time.monotonic() + 3
            while not producer.done():
                blocked = db.execute(
                    "SELECT pg_blocking_pids(%s)", (producer_pid[0],)
                ).fetchone()[0]
                if worker_pid[0] in blocked:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.002)
        finally:
            release.set()
        producer_error = producer.exception(timeout=5)
        consumer_error = consumer.exception(timeout=5)
    assert producer_error is None
    assert (
        consumer_error is None or getattr(consumer_error, "sqlstate", None) == "40001"
    )
    journal_tests.drain(journal, db)
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_user_message_states WHERE project_id=%s AND read_at IS NULL",
            (account.project_id,),
        ).fetchone()[0]
        == 0
    )
