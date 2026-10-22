# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import datetime
import json
import os
import signal
import subprocess
import sys
import threading
import time
from unittest import mock
import uuid

import psycopg.errors
import pytest
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage import exceptions as storage_exceptions
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.services import history_import
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_messages as message_tests
from workspace.tests.integration import test_zulip_outgoing as outgoing_tests
from workspace.tests.integration import test_zulip_realtime as realtime_tests


history_account = message_tests.history_account
outgoing = outgoing_tests.outgoing
account = realtime_tests.account
DEADLOCK_CASES = json.loads(
    (
        conftest.REPO_ROOT / "workspace/tests/fixtures/zulip_history_deadlocks.json"
    ).read_text()
)


def _stored(task):
    return models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})


def _until(service, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        service._iteration()
        if predicate():
            return
        service._wake_event.wait(0.005)
        service._wake_event.clear()
    pytest.fail("Task worker did not reach the expected state")


def test_migration_routes_existing_tasks_by_origin_and_round_trips(account, db):
    subscription = realtime_tests.register(account)
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {"id": 1, "type": "message", "message": {"id": 42}},
            ],
        )
    internal, remote = realtime_tests.events(account)
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = next(conftest.MIGRATIONS_DIR.glob("0243-*.py")).name
    tasks = []
    try:
        for event, backfill, kind in (
            (remote.uuid, None, "import_history"),
            (internal.uuid, internal.uuid, "import_history"),
            (internal.uuid, None, "refresh_message_flags"),
            (None, None, "initialize_connection"),
        ):
            task = models.Task(
                project_id=account.project_id,
                external_account_uuid=account.uuid,
                realtime_event_uuid=event,
                backfill_uuid=backfill,
                content=(
                    models.ImportHistoryContent(first_message_id=42, last_message_id=42)
                    if kind == "import_history"
                    else models.RefreshMessageFlagsContent(last_message_id=42)
                    if kind == "refresh_message_flags"
                    else models.InitializeConnectionContent()
                ),
            )
            task.insert()
            tasks.append(task)
        engine.rollback_migration(migration)
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_tasks_queue_ready_idx')"
        ).fetchone() == (None,)
        engine.apply_migration(migration)
        assert [_stored(task).queue for task in tasks] == [
            "realtime",
            "background",
            "background",
            "realtime",
        ]
        assert all(
            _stored(task).concurrency_key == f"{account.project_id}:{account.uuid}"
            for task in tasks
        )
    finally:
        engine.apply_migration(engine.get_latest_migration())
        for task in tasks:
            task.delete()


def test_claims_are_exclusive_and_do_not_queue_same_account_twice(history_account):
    account = history_account
    tasks = [
        models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            content=models.ImportHistoryContent(first_message_id=i, last_message_id=i),
        )
        for i in (43, 42)
    ]
    for task in tasks:
        task.insert()
    service = history_import.HistoryImportService(queue="background", workers=4)

    def claim():
        with contexts.Context().session_manager():
            return service._claim_tasks(4)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        claimed = [task_uuid for result in results for task_uuid, _owner in result]
        assert len(claimed) == len(set(claimed)) == 1
        with contexts.Context().session_manager():
            assert service._claim_tasks(4) == []
    finally:
        for task in tasks:
            task.delete()


@pytest.mark.parametrize("held_queue", ["background", "realtime"])
def test_dispatch_transactions_do_not_block_the_other_queue(
    history_account, held_queue
):
    account = history_account
    tasks = {
        queue: models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            queue=queue,
            content=models.ImportHistoryContent(
                first_message_id=42, last_message_id=42
            ),
        )
        for queue in ("background", "realtime")
    }
    for task in tasks.values():
        task.insert()
    other_queue = "realtime" if held_queue == "background" else "background"

    def claim_other():
        with contexts.Context().session_manager():
            return history_import.HistoryImportService(queue=other_queue)._claim_tasks(
                1
            )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with contexts.Context().session_manager():
                claimed = history_import.HistoryImportService(
                    queue=held_queue
                )._claim_tasks(1)
                assert claimed[0][0] == tasks[held_queue].uuid
                other = pool.submit(claim_other).result(timeout=3)
                assert other[0][0] == tasks[other_queue].uuid
    finally:
        for task in tasks.values():
            task.delete()


@pytest.mark.parametrize("held_queue", ["background", "realtime"])
def test_combined_dispatcher_respects_each_queue_lock(history_account, held_queue):
    task = models.Task(
        project_id=history_account.project_id,
        external_account_uuid=history_account.uuid,
        content=models.ImportHistoryContent(first_message_id=42, last_message_id=42),
    )
    task.insert()

    def claim_combined():
        with contexts.Context().session_manager():
            return history_import.HistoryImportService(
                queue="all", workers=4
            )._claim_tasks(1)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with contexts.Context().session_manager() as session:
                session.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"zulip-task-dispatch:{held_queue}",),
                )
                assert pool.submit(claim_combined).result(timeout=3) == []
        with contexts.Context().session_manager():
            claimed = history_import.HistoryImportService(
                queue="all", workers=4
            )._claim_tasks(1)
            assert claimed[0][0] == task.uuid
    finally:
        task.delete()


def test_realtime_publishes_converted_message_while_background_is_blocked(
    history_account,
    db,
    monkeypatch,
    scheduled_reconciliation,
    api,
    tmp_path,
):
    account = history_account
    started, release = threading.Event(), threading.Event()
    background = models.Task(content=models.ConvertMessageContent())
    background.insert()
    message = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        queue="realtime",
        priority=models.TASK_PRIORITY_REALTIME_MESSAGE,
        content=models.ImportHistoryContent(first_message_id=42, last_message_id=42),
    )
    message.insert()
    zulip = mock.Mock()
    zulip.get_messages.return_value = [
        message_tests._message_data(
            content="@**Owner|7** :sunset: ![photo](/user_uploads/1/hash/photo.png)"
        )
    ]
    zulip.download_file.return_value = b"photo", "image/png"
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)

    def block(content, task):
        started.set()
        assert release.wait(10)

    monkeypatch.setattr(models.ConvertMessageContent, "execute", block)
    bg = history_import.HistoryImportService(queue="background", workers=4)
    rt = history_import.HistoryImportService(queue="realtime", workers=1)
    try:
        bg._iteration()
        assert started.wait(3)
        _until(rt, lambda: _stored(message).status == "done")

        def published():
            return bool(
                db.execute(
                    "SELECT 1 FROM messenger_api_user_messages_v1 WHERE project_id=%s AND user_uuid=%s AND visible",
                    (account.project_id, account.owner_user_uuid),
                ).fetchone()
            )

        _until(rt, published)
        assert not release.is_set()
        row = db.execute(
            "SELECT payload->>'content' FROM messenger_messages WHERE project_id=%s",
            (account.project_id,),
        ).fetchone()
        assert f"urn:user:{account.owner_user_uuid}" in row[0]
        assert "🌆" in row[0]
        assert "urn:image:" in row[0]
        api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
        file_uuid = db.execute(
            "SELECT uuid FROM m_workspace_files WHERE project_id=%s",
            (account.project_id,),
        ).fetchone()[0]
        response = api.get(f"/v1/messenger/files/{file_uuid}/actions/download")
        assert response.status_code == 200, response.text
        assert response.content == b"photo"
    finally:
        release.set()
        bg.stop()
        rt.stop()
        background.delete()


def test_different_accounts_run_concurrently_with_separate_sessions(
    account,
    db,
    monkeypatch,
):
    other = realtime_tests.external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=account.owner_user_uuid,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        settings=account.settings,
    )
    other.insert()
    tasks = []
    barrier = threading.Barrier(2)
    sessions = []

    def execute(content, task):
        session = contexts.Context().get_session()
        sessions.append(
            session.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        )
        barrier.wait(3)

    monkeypatch.setattr(models.InitializeConnectionContent, "execute", execute)
    monkeypatch.setattr(
        models.InitializeConnectionContent, "on_done", lambda *args: None
    )
    service = history_import.HistoryImportService(queue="realtime", workers=2)
    try:
        for value in (account, other):
            task = models.Task(
                project_id=value.project_id,
                external_account_uuid=value.uuid,
                content=models.InitializeConnectionContent(),
            )
            task.insert()
            tasks.append(task)
        _until(service, lambda: all(_stored(task).status == "done" for task in tasks))
        assert len(set(sessions)) == 2
        assert all(_stored(task).lease_owner is None for task in tasks)
        assert all(_stored(task).queue_wait_time is not None for task in tasks)
    finally:
        service.stop()
        other.delete()


def test_cancelled_or_reassigned_claim_cannot_execute(account, db, monkeypatch):
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.InitializeConnectionContent(),
    )
    task.insert()
    service = history_import.HistoryImportService(queue="realtime")
    with contexts.Context().session_manager():
        [(task_uuid, owner)] = service._claim_tasks(1)
    db.execute(
        "UPDATE bridge_zulip_v5_tasks SET lease_owner=%s WHERE uuid=%s",
        (uuid.uuid4(), task_uuid),
    )
    execute = mock.Mock()
    monkeypatch.setattr(models.InitializeConnectionContent, "execute", execute)
    service._run_task(task_uuid, owner)
    execute.assert_not_called()
    assert _stored(task).lease_owner != owner


def test_realtime_send_keeps_committed_attempt_and_does_not_repeat_post(outgoing):
    _api, account, _channel, _remote, zulip, _constructor = outgoing
    outgoing_tests.create(outgoing)
    outgoing_tests.dispatch()
    service = history_import.HistoryImportService(queue="realtime")
    try:
        _until(service, lambda: zulip.send_message.called)
        service.stop()
        task = outgoing_tests.tasks(account)[0]
        assert _stored(task).status == "retry"
        outgoing_tests.ready(task)
        service._iteration()
        _until(service, lambda: _stored(task).status == "done")
        zulip.send_message.assert_called_once()
    finally:
        service.stop()


def test_slow_backfill_http_does_not_lock_out_realtime_on_the_same_account(
    history_account,
    db,
    monkeypatch,
    scheduled_reconciliation,
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
    internal = realtime_tests.events(account)[0]
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {
                    "id": 1,
                    "type": "message",
                    "message": message_tests._message_data(42),
                },
            ],
        )
    remote = realtime_tests.events(account)[1]
    entered, release = threading.Event(), threading.Event()

    def constructor(account):
        zulip = mock.Mock()

        def fetch(first, last):
            if first == 1:
                entered.set()
                assert release.wait(10)
                return []
            return [message_tests._message_data(42, content="@**Owner|7** :sunset:")]

        zulip.get_messages.side_effect = fetch
        return zulip

    monkeypatch.setattr(client, "ZulipClient", constructor)
    backfill = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        backfill_uuid=internal.uuid,
        realtime_event_uuid=internal.uuid,
        content=models.ImportHistoryContent(first_message_id=1, last_message_id=1),
    )
    backfill.insert()
    bg = history_import.HistoryImportService(queue="background", workers=4)
    rt = history_import.HistoryImportService(queue="realtime")
    try:
        bg._iteration()
        assert entered.wait(3)
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            realtime_event_uuid=remote.uuid,
            queue="realtime",
            priority=models.TASK_PRIORITY_REALTIME_MESSAGE,
            content=models.ImportHistoryContent(
                first_message_id=42, last_message_id=42
            ),
        )
        task.insert()
        _until(
            rt,
            lambda: bool(
                db.execute(
                    "SELECT 1 FROM messenger_api_user_messages_v1 WHERE project_id=%s AND user_uuid=%s AND visible",
                    (account.project_id, account.owner_user_uuid),
                ).fetchone()
            ),
            timeout=3,
        )
        assert not release.is_set()
    finally:
        release.set()
        bg.stop()
        rt.stop()


def test_changed_account_discards_prepared_history(
    history_account, db, monkeypatch, scheduled_reconciliation
):
    account = history_account
    with contexts.Context().session_manager():
        account.update_status("backfill")
    entered, release = threading.Event(), threading.Event()
    zulip = mock.Mock()

    def fetch(first, last):
        entered.set()
        assert release.wait(5)
        return [message_tests._message_data()]

    zulip.get_messages.side_effect = fetch
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.ImportHistoryContent(first_message_id=42, last_message_id=42),
    )
    task.insert()
    service = history_import.HistoryImportService(queue="background", workers=4)
    try:
        service._iteration()
        assert entered.wait(3)
        account.settings = {**account.settings, "api_key": "replacement"}
        account.update()
        release.set()
        _until(service, lambda: _stored(task).status == "cancel")
        assert not message_tests._messages(account)
    finally:
        release.set()
        service.stop()


@pytest.mark.parametrize("missing_identity", [False, True])
def test_prepared_import_keeps_retry_semantics(
    history_account, monkeypatch, scheduled_reconciliation, missing_identity
):
    account = history_account
    if missing_identity:
        account.properties["external_account_id"].set_value_force(None)
        account.update()
    zulip = mock.Mock()
    zulip.get_messages.side_effect = requests.Timeout("Slow history endpoint")
    constructor = mock.Mock(return_value=zulip)
    monkeypatch.setattr(client, "ZulipClient", constructor)
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.ImportHistoryContent(first_message_id=42, last_message_id=42),
    )
    task.insert()
    service = history_import.HistoryImportService(queue="background", workers=4)
    try:
        _until(service, lambda: _stored(task).status == "retry")
        assert _stored(task).lease_owner is None
        if missing_identity:
            constructor.assert_not_called()
        else:
            assert "Slow history endpoint" in _stored(task).error_description
    finally:
        service.stop()


@pytest.mark.parametrize("queue,workers", [("background", 4), ("realtime", 1)])
@pytest.mark.parametrize("deadlock", DEADLOCK_CASES, ids=lambda case: case["name"])
def test_history_import_retries_database_deadlock_after_rolling_back_partial_writes(
    history_account,
    db,
    monkeypatch,
    scheduled_reconciliation,
    queue,
    workers,
    deadlock,
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = [message_tests._message_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    insert_message = models.ZulipMessage.insert
    attempts = []

    def fail(message, wrapped):
        try:
            contexts.Context().get_session().execute(
                "DO $$ BEGIN RAISE EXCEPTION 'deadlock detected' USING ERRCODE='40P01'; END $$"
            )
        except psycopg.errors.DeadlockDetected:
            if wrapped:
                raise storage_exceptions.DeadLock(
                    msg=message.removeprefix(
                        "Deadlock found when trying to get lock. Original message: "
                    )
                )
            raise psycopg.errors.DeadlockDetected(message)

    def insert(message):
        result = insert_message(message)
        attempts.append(message.message_id)
        if len(attempts) == 1:
            fail(deadlock["message"], deadlock["wrapped"])
        return result

    monkeypatch.setattr(models.ZulipMessage, "insert", insert)
    status_update = mock.Mock(
        side_effect=AssertionError("A database deadlock must not change account status")
    )
    monkeypatch.setattr(external_models.ExternalAccount, "update_status", status_update)
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        queue=queue,
        content=models.ImportHistoryContent(first_message_id=42, last_message_id=42),
    )
    task.insert()
    service = history_import.HistoryImportService(queue=queue, workers=workers)
    before = datetime.datetime.now(datetime.timezone.utc)
    try:
        _until(service, lambda: _stored(task).status == "retry")
        retried = _stored(task)
        after = datetime.datetime.now(datetime.timezone.utc)
        assert before + models.TASK_RETRY_DELAY <= retried.next_retry_at
        assert retried.next_retry_at <= after + models.TASK_RETRY_DELAY
        status_update.assert_not_called()
        assert retried.error_description == deadlock["message"]
        assert retried.lease_owner is None
        assert retried.lease_expires_at is None
        assert not message_tests._messages(account)
        service._iteration()
        service.stop()
        assert attempts == [42]

        db.execute(
            "UPDATE bridge_zulip_v5_tasks SET next_retry_at=%s WHERE uuid=%s",
            (before.replace(tzinfo=None), task.uuid),
        )
        _until(service, lambda: _stored(task).status == "done")
        finished = _stored(task)
        assert finished.error_description is None
        assert finished.next_retry_at is None
        assert attempts == [42, 42]
        assert [message.message_id for message in message_tests._messages(account)] == [
            42
        ]
    finally:
        service.stop()


def test_execution_lock_fences_other_tasks_even_after_the_lease_expires(
    _database, monkeypatch
):
    tasks = [models.Task(content=models.ConvertMessageContent()) for _ in range(2)]
    owners = [uuid.uuid4(), uuid.uuid4()]
    for task, owner in zip(tasks, owners):
        task.lease_owner = owner
        task.lease_expires_at = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(seconds=1)
        task.insert()
    entered, release = threading.Event(), threading.Event()
    seen = []

    def execute(content, task):
        seen.append(task.uuid)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(models.ConvertMessageContent, "execute", execute)
    service = history_import.HistoryImportService(queue="background")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service._run_task, tasks[0].uuid, owners[0])
            assert entered.wait(3)
            second = pool.submit(service._run_task, tasks[1].uuid, owners[1])
            second.result(timeout=3)
            assert seen == [tasks[0].uuid]
            assert _stored(tasks[1]).status == "new"
            release.set()
            first.result(timeout=3)
    finally:
        release.set()
        for task in tasks:
            task.delete()


def test_bad_prefetched_identity_does_not_block_other_content(
    history_account, db, monkeypatch
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = [message_tests._message_data(i) for i in (42, 43)]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert message_tests._import_history(account).status == "done"
    db.execute(
        "UPDATE bridge_zulip_v5_messages SET sender_id=999 WHERE project_id=%s AND message_id=42",
        (account.project_id,),
    )
    db.execute(
        "UPDATE bridge_zulip_v5_message_contents SET conversion_version=0 WHERE uuid IN (SELECT content FROM bridge_zulip_v5_messages WHERE project_id=%s)",
        (account.project_id,),
    )
    before = {
        row.uuid
        for row in models.Task.objects.get_all(filters={"project_id": filters.Is(None)})
    }
    task = models.Task(content=models.ConvertMessageContent())
    task.insert()
    try:
        with contexts.Context().session_manager():
            task.execute()
            assert task.status == "done"
        assert db.execute(
            "SELECT observed.message_id,content.conversion_version FROM bridge_zulip_v5_messages observed JOIN bridge_zulip_v5_message_contents content ON content.uuid=observed.content WHERE observed.project_id=%s ORDER BY observed.message_id",
            (account.project_id,),
        ).fetchall() == [(42, 0), (43, 1)]
    finally:
        for pending in models.Task.objects.get_all(
            filters={"project_id": filters.Is(None)}
        ):
            if pending.uuid not in before:
                pending.delete()


def test_parallel_account_imports_preserve_shared_message_identities(
    history_account, db, monkeypatch, scheduled_reconciliation
):
    account = history_account
    owner = uuid.uuid4()
    conftest.seed_workspace_user(db, owner, f"parallel-import-{owner}")
    other = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=owner,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        settings=account.settings,
    )
    other.insert()
    message_tests._channel(other)
    barrier = threading.Barrier(2)

    def constructor(account):
        zulip = mock.Mock()

        def fetch(first, last):
            barrier.wait(3)
            return [message_tests._message_data(i) for i in range(first, last + 1)]

        zulip.get_messages.side_effect = fetch
        return zulip

    monkeypatch.setattr(client, "ZulipClient", constructor)
    tasks = []
    service = history_import.HistoryImportService(queue="background", workers=4)
    try:
        for value, last in ((account, 100), (other, 80)):
            task = models.Task(
                project_id=value.project_id,
                external_account_uuid=value.uuid,
                content=models.ImportHistoryContent(
                    first_message_id=1, last_message_id=last
                ),
            )
            task.insert()
            tasks.append(task)
        _until(
            service,
            lambda: all(
                _stored(task).status in ("done", "error", "retry") for task in tasks
            ),
        )
        assert [_stored(task).status for task in tasks] == ["done", "done"], [
            _stored(task).error_description for task in tasks
        ]
        rows = db.execute(
            "SELECT message_uuid,count(*) FROM bridge_zulip_v5_messages WHERE project_id=%s GROUP BY message_uuid",
            (account.project_id,),
        ).fetchall()
        assert len(rows) == 100
        assert sum(count == 2 for _message, count in rows) == 80
        assert sum(count == 1 for _message, count in rows) == 20
        assert db.execute(
            "SELECT max_id FROM bridge_zulip_v5_topics WHERE uuid=ANY(SELECT topic FROM bridge_zulip_v5_messages WHERE project_id=%s)",
            (account.project_id,),
        ).fetchone() == (100,)
    finally:
        service.stop()
        other.delete()


def test_realtime_worker_entrypoint_consumes_only_realtime(
    history_account, db, monkeypatch, tmp_path
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = [message_tests._message_data(42)]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert message_tests._import_history(account).status == "done"
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.ReconcileRealtimeContent(message_ids=[42]),
    )
    task.insert()
    background = models.Task(priority=900000, content=models.ConvertMessageContent())
    background.insert()
    config = tmp_path / "worker.conf"
    config.write_text(f"[db]\nconnection_url = {conftest.TEST_DB_URL}\n")
    try:
        with (tmp_path / "worker.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "workspace.cmd.zulip_task_worker",
                    "--config-file",
                    str(config),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    assert process.poll() is None
                    if db.execute(
                        "SELECT 1 FROM messenger_api_user_messages_v1 WHERE project_id=%s AND user_uuid=%s AND visible",
                        (account.project_id, account.owner_user_uuid),
                    ).fetchone():
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("Realtime worker process did not publish the message")
                assert _stored(background).status == "new"
            finally:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                    raise
    finally:
        background.delete()


def test_catalog_updates_share_an_execution_key_across_queues(
    history_account, monkeypatch, scheduled_reconciliation
):
    account = history_account
    entered, release = threading.Event(), threading.Event()
    seen = []

    def execute(content, task):
        seen.append(task.uuid)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(models.ImportUsersContent, "execute", execute)
    monkeypatch.setattr(models.ImportChannelsContent, "execute", execute)
    background = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.ImportUsersContent(periodic=False),
    )
    background.insert()
    realtime = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        queue="realtime",
        content=models.ImportChannelsContent(periodic=False),
    )
    realtime.insert()
    assert (
        _stored(background).concurrency_key
        == _stored(realtime).concurrency_key
        == f"{account.project_id}:catalog"
    )
    bg = history_import.HistoryImportService(queue="background", workers=4)
    rt = history_import.HistoryImportService(queue="realtime")
    try:
        bg._iteration()
        assert entered.wait(3)
        rt._iteration()
        for future in rt._futures:
            future.result(timeout=3)
        assert seen == [background.uuid]
        release.set()
        _until(rt, lambda: _stored(realtime).status == "done")
        assert seen == [background.uuid, realtime.uuid]
    finally:
        release.set()
        bg.stop()
        rt.stop()


@pytest.mark.parametrize("claim_state", ["new", "unknown_http", "reclaimed"])
def test_file_send_retries_when_account_is_locked_after_preparation(
    outgoing, db, tmp_path, monkeypatch, claim_state
):
    api, account, channel, remote, zulip, _constructor = outgoing
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    uploaded = api.post(
        "/v1/messenger/files/",
        data={"stream_uuid": str(channel.stream_uuid)},
        files={"file": ("claim-race.txt", b"claim-race", "text/plain")},
    )
    assert uploaded.status_code == 201
    remote["content"] = f"[claim-race](urn:file:{uploaded.json()['uuid']})"
    zulip.upload_file.return_value = "/user_uploads/2/claim-race.txt"
    outgoing_tests.create(outgoing)
    outgoing_tests.dispatch()
    (task,) = outgoing_tests.tasks(account)
    if claim_state == "unknown_http":
        db.execute(
            "UPDATE bridge_zulip_v5_deliveries SET sending=TRUE WHERE uuid=%s",
            (task.content.delivery_uuid,),
        )
    service = history_import.HistoryImportService(queue="realtime", workers=1)
    with contexts.Context().session_manager():
        [(task_uuid, owner)] = service._claim_tasks(1)
    original = service._locked_task
    calls = 0
    blocker = psycopg.connect(conftest.TEST_DB_URL)

    def lock_after_prepare(claimed_uuid, claimed_owner):
        nonlocal calls
        calls += 1
        if calls == 2:
            if claim_state == "reclaimed":
                db.execute(
                    "UPDATE bridge_zulip_v5_tasks SET lease_owner=%s WHERE uuid=%s",
                    (uuid.uuid4(), task_uuid),
                )
            blocker.execute(
                "SELECT uuid FROM m_external_accounts_v2 WHERE uuid=%s FOR UPDATE",
                (account.uuid,),
            )
        return original(claimed_uuid, claimed_owner)

    try:
        with mock.patch.object(service, "_locked_task", side_effect=lock_after_prepare):
            service._run_task(task_uuid, owner)
    finally:
        blocker.close()
    assert zulip.upload_file.call_count == (0 if claim_state == "unknown_http" else 1)
    zulip.send_message.assert_not_called()
    (delivery,) = models.MessageDelivery.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    # No POST started: this known local skip must not become an unknown send.
    assert delivery.sending is (claim_state != "new")
    if claim_state != "new":
        return
    outgoing_tests.ready(_stored(task))
    with contexts.Context().session_manager():
        [(task_uuid, owner)] = service._claim_tasks(1)
    service._run_task(task_uuid, owner)
    zulip.upload_file.assert_called_once()
    zulip.send_message.assert_called_once()


def test_realtime_send_rechecks_local_confirmation_within_250_milliseconds(outgoing):
    _api, account, _channel, _remote, zulip, _constructor = outgoing
    outgoing_tests.create(outgoing)
    outgoing_tests.dispatch()
    service = history_import.HistoryImportService(queue="realtime", workers=1)
    with contexts.Context().session_manager():
        [(task_uuid, owner)] = service._claim_tasks(1)
    service._run_task(task_uuid, owner)
    (task,) = outgoing_tests.tasks(account)
    assert task.status == "retry"
    assert (
        datetime.timedelta(0)
        < task.next_retry_at - task.updated_at
        <= datetime.timedelta(milliseconds=250)
    )
    # An unacknowledged HTTP attempt may be checked quickly, never reposted.
    outgoing_tests.ready(task)
    with contexts.Context().session_manager():
        [(task_uuid, owner)] = service._claim_tasks(1)
    service._run_task(task_uuid, owner)
    zulip.send_message.assert_called_once()
    zulip.get_messages.assert_called_once()


def test_send_lock_error_before_http_does_not_leave_an_unknown_attempt(outgoing):
    _api, account, _channel, _remote, zulip, _constructor = outgoing
    outgoing_tests.create(outgoing)
    outgoing_tests.dispatch()
    service = history_import.HistoryImportService(queue="realtime", workers=1)
    with contexts.Context().session_manager():
        [(task_uuid, owner)] = service._claim_tasks(1)
    original = service._locked_task
    calls = 0

    def fail_second_lock(claimed_uuid, claimed_owner):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise psycopg.errors.DeadlockDetected("deadlock before HTTP")
        return original(claimed_uuid, claimed_owner)

    with mock.patch.object(service, "_locked_task", side_effect=fail_second_lock):
        with pytest.raises(psycopg.errors.DeadlockDetected):
            service._run_task(task_uuid, owner)
    zulip.send_message.assert_not_called()
    task = models.Task.objects.get_one(filters={"uuid": filters.EQ(task_uuid)})
    delivery = models.MessageDelivery.objects.get_one(
        filters={"uuid": filters.EQ(task.content.delivery_uuid)}
    )
    assert not delivery.sending
    with contexts.Context().session_manager():
        [(task_uuid, owner)] = service._claim_tasks(1)
    service._run_task(task_uuid, owner)
    zulip.send_message.assert_called_once()
