# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import json
from unittest import mock
import uuid

import psycopg
import pytest
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import engines
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import external_models
from workspace.services import history_import
from workspace.tests.integration import conftest


@pytest.fixture
def task_queue(_database, db, scheduled_reconciliation):
    owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"history-import-{owner_uuid}")
    account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=owner_uuid,
        provider="zulip",
        settings={"server_url": "https://zulip.example.com"},
    )
    account.insert()

    def create(**values):
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            content=models.InitializeConnectionContent(),
            **values,
        )
        task.insert()
        return task

    yield create
    account.delete()


def _stored(task):
    return models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})


def _http_error(status_code, code="BAD_REQUEST", **details):
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(
        {"result": "error", "code": code, "msg": "Provider request failed", **details}
    ).encode()
    return requests.HTTPError("Provider request failed", response=response)


def test_execution_and_queue_timings_are_persisted_across_retries(
    task_queue, db, monkeypatch
):
    now = datetime.datetime.now(datetime.timezone.utc)
    task = task_queue(
        created_at=now - datetime.timedelta(seconds=100),
        not_before_at=now - datetime.timedelta(seconds=40),
    )
    clock = {"value": 10.0}

    def execute(content, current):
        clock["value"] += 2.5
        if current.execution_time is None:
            raise RuntimeError("Retry this attempt")

    def retry(content, current, error):
        current.status = "retry"
        current.next_retry_at = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(seconds=3)

    monkeypatch.setattr(models.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(models.InitializeConnectionContent, "execute", execute)
    monkeypatch.setattr(models.InitializeConnectionContent, "on_error", retry)
    monkeypatch.setattr(
        models.InitializeConnectionContent, "on_done", lambda content, current: None
    )
    with contexts.Context().session_manager():
        task.execute()
    first = _stored(task)
    assert first.status == "retry"
    assert first.execution_time == 2.5
    assert first.queue_wait_time == pytest.approx(40.0, abs=0.5)
    db.execute(
        "UPDATE bridge_zulip_v5_tasks SET updated_at = %s, next_retry_at = %s WHERE uuid = %s",
        (
            (now - datetime.timedelta(seconds=30)).replace(tzinfo=None),
            (now - datetime.timedelta(seconds=10)).replace(tzinfo=None),
            task.uuid,
        ),
    )
    current = _stored(task)
    with contexts.Context().session_manager():
        current.execute()
    finished = _stored(task)
    assert finished.status == "done"
    assert finished.execution_time == 5.0
    assert finished.queue_wait_time == pytest.approx(50.0, abs=1.0)


def test_task_timings_roll_back_with_task_work(task_queue, monkeypatch):
    task = task_queue()
    monkeypatch.setattr(
        models.InitializeConnectionContent, "execute", lambda content, current: None
    )
    monkeypatch.setattr(
        models.InitializeConnectionContent, "on_done", lambda content, current: None
    )
    with pytest.raises(RuntimeError, match="Rollback timings"):
        with contexts.Context().session_manager():
            task.execute()
            assert task.execution_time is not None
            raise RuntimeError("Rollback timings")
    restored = _stored(task)
    assert restored.status == "new"
    assert restored.execution_time is restored.queue_wait_time is None


def test_timing_migration_preserves_unknown_history_and_round_trips(task_queue, db):
    task = task_queue(status="done")
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0238-Track-bridge-task-execution-and-queue-wait-times-5f55e9.py"
    engine.rollback_migration(migration)
    try:
        assert db.execute(
            "SELECT status FROM bridge_zulip_v5_tasks WHERE uuid = %s", (task.uuid,)
        ).fetchone() == ("done",)
    finally:
        engine.apply_migration(engine.get_latest_migration())
    restored = _stored(task)
    assert restored.status == "done"
    assert restored.execution_time is restored.queue_wait_time is None


@pytest.mark.parametrize("status", ["new", "error"])
def test_backfill_starts_only_after_realtime_queue_registration(
    task_queue, monkeypatch, status
):
    task = task_queue()
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    account.status = status
    account.error_description = (
        "Previous connection error" if status == "error" else None
    )
    account.settings = {"server_url": "https://zulip.example.com"}
    account.update()
    zulip = mock.Mock()
    zulip.check_connection.return_value = {
        "user_id": 7,
        "full_name": "Owner",
        "email": "owner@example.com",
    }
    zulip.get_last_message_id.return_value = 10005
    zulip.get_channels.return_value = []
    zulip.get_topic_notifications.return_value = []
    zulip.get_users.return_value = []
    zulip.get_messages.return_value = []
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)

    service = history_import.HistoryImportService()
    service._iteration()

    assert _stored(task).status == "done"
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    assert account.status == "backfill"
    assert account.external_account_id == "zulip.example.com:7"
    assert account.error_description is None
    pending = models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "status": filters.EQ("new"),
        }
    )
    assert pending == []
    account.settings = {
        "server_url": "https://zulip.example.com",
        "email": "owner@example.com",
        "api_key": "test",
    }
    account.update()
    with contexts.Context().session_manager():
        models.RealtimeSubscription.register(
            account,
            {
                "queue_id": "test-queue",
                "last_event_id": -1,
                "max_message_id": 10005,
                "event_queue_longpoll_timeout_seconds": 90,
            },
            datetime.datetime.now(datetime.timezone.utc),
        )
    assert (
        models.Task.objects.get_all(
            filters={"external_account_uuid": account.uuid, "status": "new"}
        )
        == []
    )
    with contexts.Context().session_manager():
        assert models.RealtimeEvent.dispatch_one()
    (full_backfill,) = models.Task.objects.get_all(
        filters={"external_account_uuid": account.uuid, "status": "new"}
    )
    assert isinstance(full_backfill.content, models.FullBackfillContent)
    assert full_backfill.priority == models.TASK_PRIORITY_INITIALIZE_CONNECTION

    service._iteration()

    assert _stored(full_backfill).status == "done"
    pending = models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "status": filters.EQ("new"),
        }
    )
    assert len(pending) == 103
    assert all(child.project_id == task.project_id for child in pending)
    (user_sync,) = [
        child
        for child in pending
        if isinstance(child.content, models.ImportUsersContent)
    ]
    assert user_sync.priority == models.TASK_PRIORITY_IMPORT_USERS
    service._iteration()
    assert _stored(user_sync).status == "done"
    (next_user_sync,) = models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "priority": filters.EQ(models.TASK_PRIORITY_IMPORT_USERS),
            "status": filters.EQ("waiting"),
        }
    )
    assert next_user_sync.not_before_at is not None
    (first_sync,) = [
        child
        for child in pending
        if isinstance(child.content, models.ImportChannelsContent)
    ]
    history = sorted(
        [
            child
            for child in pending
            if isinstance(child.content, models.ImportHistoryContent)
        ],
        key=lambda child: child.created_at,
    )
    assert [
        (child.content.first_message_id, child.content.last_message_id)
        for child in history
    ] == [(10001, 10005), *[(first, first + 99) for first in range(9901, 0, -100)]]
    assert all(
        newer.created_at < older.created_at
        for newer, older in zip(history, history[1:])
    )
    assert [child.priority for child in history] == list(range(10_000, 9_899, -1))
    assert first_sync.project_id == account.project_id
    assert first_sync.priority == models.TASK_PRIORITY_IMPORT_USERS == 500_000
    assert first_sync.not_before_at is None
    assert isinstance(first_sync.content, models.ImportChannelsContent)

    before = datetime.datetime.now(datetime.timezone.utc)
    service._iteration()
    after = datetime.datetime.now(datetime.timezone.utc)

    assert _stored(first_sync).status == "done"
    pending = models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "status": filters.In(["new", "waiting"]),
        }
    )
    assert len(pending) == 103
    (next_sync,) = [
        child
        for child in pending
        if isinstance(child.content, models.ImportChannelsContent)
    ]
    assert next_sync.uuid != first_sync.uuid
    assert next_sync.project_id == account.project_id
    assert next_sync.priority == models.TASK_PRIORITY_IMPORT_CHANNELS
    assert isinstance(next_sync.content, models.ImportChannelsContent)
    assert next_sync.status == "waiting"
    interval = datetime.timedelta(minutes=15)
    assert before + interval <= next_sync.not_before_at <= after + interval

    service._iteration()

    assert _stored(history[0]).status == "done"
    zulip.get_messages.assert_called_once_with(10001, 10005)
    assert _stored(next_sync).status == "waiting"
    for child in history[1:]:
        service._iteration()

    assert _stored(next_sync).status == "waiting"
    assert all(_stored(child).status == "done" for child in history)
    zulip.check_connection.assert_called_once_with()
    zulip.get_last_message_id.assert_not_called()
    zulip.get_channels.assert_called_once_with()
    zulip.get_users.assert_called_once_with()
    assert zulip.get_messages.call_args_list == [
        mock.call(10001, 10005),
        *[mock.call(first, first + 99) for first in range(9901, 0, -100)],
    ]
    assert zulip.close.call_count == len(history) + 3
    assert (
        external_models.ExternalAccount.objects.get_one(
            filters={"uuid": account.uuid}
        ).status
        == "active"
    )


@pytest.mark.parametrize("phase", ["fetch", "auth", "close", "queue"])
def test_failed_full_backfill_does_not_leave_partial_child_tasks(
    task_queue, monkeypatch, phase
):
    initial = task_queue(status="done")
    task = models.Task(
        project_id=initial.project_id,
        external_account_uuid=initial.external_account_uuid,
        priority=models.TASK_PRIORITY_FULL_BACKFILL,
        content=models.ActualizeBackfillContent(
            last_message_id=10005,
            since=datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=30),
        ),
    )
    task.insert()
    zulip = mock.Mock()
    zulip.check_connection.return_value = {
        "user_id": 7,
        "full_name": "Owner",
        "email": "owner@example.com",
    }
    zulip.get_first_message_id_since.return_value = 1
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    if phase == "fetch":
        zulip.get_first_message_id_since.side_effect = requests.Timeout("Timed out")
    elif phase == "auth":
        zulip.get_first_message_id_since.side_effect = _http_error(
            401, "INVALID_API_KEY"
        )
    elif phase == "close":
        zulip.close.side_effect = RuntimeError("Close failed")
    else:
        insert_task = models.Task.insert
        attempts = []

        def insert(current_task, *args, **kwargs):
            attempts.append(current_task.uuid)
            if len(attempts) == 3:
                raise RuntimeError("Queue failed")
            return insert_task(current_task, *args, **kwargs)

        monkeypatch.setattr(models.Task, "insert", insert)

    history_import.HistoryImportService()._iteration()

    stored = models.Task.objects.get_all(
        filters={"external_account_uuid": filters.EQ(task.external_account_uuid)}
    )
    assert {item.uuid for item in stored} == {initial.uuid, task.uuid}
    assert _stored(task).status == ("retry" if phase == "fetch" else "error")
    zulip.close.assert_called_once_with()


@pytest.mark.parametrize("phase", ["check", "close", "status"])
def test_failed_connection_does_not_queue_full_backfill(task_queue, monkeypatch, phase):
    task = task_queue()
    zulip = mock.Mock()
    zulip.check_connection.return_value = {
        "user_id": 7,
        "full_name": "Owner",
        "email": "owner@example.com",
    }
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    if phase == "check":
        zulip.check_connection.side_effect = requests.Timeout("Timed out")
    elif phase == "close":
        zulip.close.side_effect = RuntimeError("Close failed")
    else:

        def fail(*args, **kwargs):
            raise RuntimeError("Completion failed")

        if phase == "status":
            monkeypatch.setattr(external_models.ExternalAccount, "update_status", fail)
        else:
            monkeypatch.setattr(models.Task, "insert", fail)

    history_import.HistoryImportService()._iteration()

    stored = models.Task.objects.get_all(
        filters={"external_account_uuid": filters.EQ(task.external_account_uuid)}
    )
    assert len(stored) == 1
    assert stored[0].uuid == task.uuid
    assert stored[0].status == ("retry" if phase == "check" else "error")
    if phase in ("status", "queue"):
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(task.external_account_uuid)}
        )
        assert account.status == "new"
    zulip.close.assert_called_once_with()


@pytest.mark.parametrize(
    "error",
    [None, requests.Timeout("Timed out"), _http_error(401, "INVALID_API_KEY")],
)
def test_connection_outcome_publishes_account_event_only_in_its_project(
    task_queue, db, monkeypatch, error
):
    task = task_queue()
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    projects = [account.project_id, uuid.uuid4()]
    other_owner = uuid.uuid4()
    conftest.seed_workspace_user(db, other_owner, f"other-owner-{other_owner}")
    for project_id, user_uuid in [
        *((project_id, account.owner_user_uuid) for project_id in projects),
        (projects[0], other_owner),
        (uuid.uuid4(), other_owner),
    ]:
        db.execute(
            "INSERT INTO m_workspace_event_cursors (project_id, user_uuid) "
            "VALUES (%s, %s)",
            (project_id, user_uuid),
        )
    zulip = mock.Mock()
    zulip.check_connection.return_value = {
        "user_id": 7,
        "full_name": "Owner",
        "email": "owner@example.com",
    }
    zulip.check_connection.side_effect = error
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)

    history_import.HistoryImportService()._iteration()

    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    events = db.execute(
        "SELECT project_id, user_uuid, payload FROM m_workspace_events "
        "WHERE object_type='external_account' AND action='updated' "
        "AND payload->>'uuid'=%s",
        (str(account.uuid),),
    ).fetchall()
    assert len(events) == 1
    assert events[0][0] == account.project_id
    assert account.status == ("backfill" if error is None else "error")
    assert _stored(task).status == (
        "done"
        if error is None
        else "retry"
        if isinstance(error, requests.Timeout)
        else "error"
    )
    for _, user_uuid, payload in events:
        assert user_uuid == account.owner_user_uuid
        assert payload["kind"] == "external_account.updated"
        assert payload["snapshot"]["uuid"] == str(account.uuid)
        assert payload["snapshot"]["project_id"] == str(account.project_id)
        assert payload["snapshot"]["status"] == account.status
        assert payload["snapshot"]["error_description"] == account.error_description
        assert "owner_user_uuid" not in payload["snapshot"]


def test_account_status_update_rolls_back_when_its_event_cannot_be_saved(
    task_queue, monkeypatch
):
    task = task_queue()
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )

    def fail_event(*args, **kwargs):
        raise RuntimeError("Event creation failed")

    monkeypatch.setattr(messenger_events, "create_external_resource_event", fail_event)

    with pytest.raises(RuntimeError, match="Event creation failed"):
        with contexts.Context().session_manager():
            account.update_status("backfill")

    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    assert account.status == "new"


def test_priority_fifo_and_ready_tasks(task_queue, monkeypatch):
    monkeypatch.setattr(
        models.InitializeConnectionContent, "on_done", lambda content, task: None
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    low = task_queue(priority=models.TASK_PRIORITY_IMPORT_HISTORY)
    newer = task_queue(
        priority=models.TASK_PRIORITY_INITIALIZE_CONNECTION, created_at=now
    )
    older = task_queue(
        priority=models.TASK_PRIORITY_INITIALIZE_CONNECTION,
        created_at=now - datetime.timedelta(seconds=1),
    )
    realtime = task_queue(priority=models.TASK_PRIORITY_REALTIME_MESSAGE)
    delayed = task_queue(
        priority=models.TASK_PRIORITY_REALTIME_MESSAGE,
        status="retry",
        next_retry_at=now + datetime.timedelta(minutes=5),
    )
    done = task_queue(priority=models.TASK_PRIORITY_REALTIME_MESSAGE, status="done")
    failed = task_queue(priority=models.TASK_PRIORITY_REALTIME_MESSAGE, status="error")
    cancelled = task_queue(
        priority=models.TASK_PRIORITY_REALTIME_MESSAGE, status="cancel"
    )
    executed = []
    monkeypatch.setattr(
        models.InitializeConnectionContent,
        "execute",
        lambda content, task: executed.append(task.uuid),
    )
    service = history_import.HistoryImportService()

    service._iteration()
    assert executed == [older.uuid]
    service._iteration()
    assert executed == [older.uuid, newer.uuid]
    service._iteration()
    service._iteration()
    service._iteration()

    assert executed == [older.uuid, newer.uuid, realtime.uuid, low.uuid]
    assert _stored(delayed).status == "retry"
    assert _stored(done).status == "done"
    assert _stored(failed).status == "error"
    assert _stored(cancelled).status == "cancel"


@pytest.mark.parametrize("exclude_current", [False, True])
def test_cancel_pending_tasks_is_account_scoped(task_queue, db, exclude_current):
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=5
    )
    lease_owner = uuid.uuid4()
    current = task_queue(
        status="retry",
        next_retry_at=future,
        lease_owner=lease_owner,
        lease_expires_at=future,
    )
    pending = [
        task_queue(
            status=status,
            not_before_at=future,
            next_retry_at=future,
            lease_owner=lease_owner,
            lease_expires_at=future,
        )
        for status in ("new", "waiting", "retry")
    ]
    terminal = [task_queue(status=status) for status in ("done", "error", "cancel")]
    other_owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(
        db, other_owner_uuid, f"other-owner-{other_owner_uuid}"
    )
    other_account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=other_owner_uuid,
        provider="zulip",
        settings={},
    )
    other_account.insert()
    try:
        other_tasks = [
            models.Task(
                project_id=other_account.project_id,
                external_account_uuid=other_account.uuid,
                status=status,
                content=models.InitializeConnectionContent(),
            )
            for status in ("new", "waiting", "retry")
        ]
        for task in other_tasks:
            task.insert()

        with contexts.Context().session_manager():
            count = current.cancel_pending_tasks(exclude_current=exclude_current)
            assert current.cancel_pending_tasks(exclude_current=exclude_current) == 0

        assert count == len(pending) + (not exclude_current)
        for task in pending:
            stored = _stored(task)
            assert stored.status == "cancel"
            assert stored.next_retry_at is None
            assert stored.lease_owner is None
            assert stored.lease_expires_at is None
            assert stored.not_before_at == future
        for task in terminal + other_tasks:
            stored = _stored(task)
            assert stored.status == task.status
            assert stored.updated_at == task.updated_at
        stored_current = _stored(current)
        assert (
            stored_current.status
            == current.status
            == ("retry" if exclude_current else "cancel")
        )
        assert (
            stored_current.next_retry_at
            == current.next_retry_at
            == (future if exclude_current else None)
        )
        assert (
            stored_current.lease_owner
            == current.lease_owner
            == (lease_owner if exclude_current else None)
        )
        assert stored_current.updated_at == current.updated_at
    finally:
        other_account.delete()


def test_cancelling_pending_tasks_uses_the_callers_transaction(task_queue):
    current = task_queue()
    retry = task_queue(status="retry")

    with pytest.raises(RuntimeError, match="Roll back cancellation"):
        with contexts.Context().session_manager():
            assert current.cancel_pending_tasks() == 2
            raise RuntimeError("Roll back cancellation")

    assert _stored(current).status == "new"
    assert _stored(retry).status == "retry"


@pytest.mark.parametrize("status", ["new", "waiting", "retry"])
def test_not_before_at_defers_tasks_until_the_scheduled_time(
    task_queue, monkeypatch, status
):
    monkeypatch.setattr(
        models.InitializeConnectionContent, "on_done", lambda content, task: None
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    scheduled_at = now + datetime.timedelta(minutes=5)
    scheduled = task_queue(
        priority=models.TASK_PRIORITY_REALTIME_MESSAGE,
        status=status,
        not_before_at=scheduled_at,
    )
    expected_status = "waiting" if status == "new" else status
    assert scheduled.status == expected_status
    retry = task_queue(
        priority=models.TASK_PRIORITY_REALTIME_MESSAGE,
        status="retry",
        not_before_at=now,
        next_retry_at=scheduled_at + datetime.timedelta(minutes=1),
    )
    ready = task_queue()
    executed = []
    monkeypatch.setattr(
        models.InitializeConnectionContent,
        "execute",
        lambda content, task: executed.append(task.uuid),
    )
    service = history_import.HistoryImportService()

    with mock.patch.object(history_import, "datetime") as clock:
        clock.datetime.now.return_value = now
        service._iteration()
        service._iteration()
        assert executed == [ready.uuid]
        assert _stored(scheduled).not_before_at == scheduled_at
        assert _stored(scheduled).status == expected_status

        clock.datetime.now.return_value = scheduled_at
        service._iteration()
        assert executed == [ready.uuid, scheduled.uuid]
        assert _stored(retry).status == "retry"

        clock.datetime.now.return_value = retry.next_retry_at
        service._iteration()

    assert executed == [ready.uuid, scheduled.uuid, retry.uuid]
    assert _stored(scheduled).status == "done"
    assert _stored(retry).status == "done"


@pytest.mark.parametrize(
    "error",
    [
        requests.Timeout("Timed out"),
        requests.ConnectionError("Disconnected"),
        _http_error(408),
        _http_error(429, "RATE_LIMIT_HIT"),
        _http_error(500),
        _http_error(502),
        _http_error(503),
        _http_error(504),
    ],
)
def test_execution_errors_and_delayed_retry(task_queue, monkeypatch, error):
    task = task_queue()
    attempts = []

    def execute(content, task):
        attempts.append(task.uuid)
        raise error

    monkeypatch.setattr(models.InitializeConnectionContent, "execute", execute)
    service = history_import.HistoryImportService()
    before = datetime.datetime.now(datetime.timezone.utc)
    service._iteration()
    stored = _stored(task)

    assert stored.status == "retry"
    assert stored.error_description == str(error)
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    assert account.status == "error"
    assert account.error_description == (
        f"{error}\nNext retry at: {stored.next_retry_at.isoformat()}"
    )
    service._iteration()
    assert attempts == [task.uuid]
    after = datetime.datetime.now(datetime.timezone.utc)
    assert before + models.TASK_RETRY_DELAY <= stored.next_retry_at
    assert stored.next_retry_at <= after + models.TASK_RETRY_DELAY
    stored.next_retry_at = before - datetime.timedelta(seconds=1)
    stored.update()
    monkeypatch.setattr(
        models.InitializeConnectionContent, "execute", lambda content, task: None
    )
    service._iteration()
    stored = _stored(task)
    assert stored.status == "done"
    assert stored.error_description is None
    assert stored.next_retry_at is None
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    assert account.status == "backfill"
    assert account.error_description is None


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (400, "BAD_REQUEST", False),
        (401, "INVALID_API_KEY", False),
        (401, "UNAUTHORIZED", False),
        (401, "USER_DEACTIVATED", False),
        (401, "REALM_DEACTIVATED", False),
        (403, "BAD_REQUEST", False),
        (404, "NONEXISTENT_SUBDOMAIN", False),
        (408, "REQUEST_TIMEOUT", True),
        (429, "RATE_LIMIT_HIT", True),
        (500, "BAD_REQUEST", True),
        (502, "BAD_REQUEST", True),
        (503, "SERVER_NOT_READY", True),
        (504, "BAD_REQUEST", True),
    ],
)
def test_connection_response_controls_task_and_account_status(
    task_queue, monkeypatch, status, code, retryable
):
    task = task_queue()
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    account.settings = {
        "server_url": "https://zulip.example.com",
        "email": "user@example.com",
        "api_key": "invalid-test-key",
    }
    account.update()
    message = "Provider request failed"
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(
        {"result": "error", "code": code, "msg": message}
    ).encode()
    session = mock.Mock()
    session.request.return_value = response
    monkeypatch.setattr(client.requests, "Session", lambda: session)

    service = history_import.HistoryImportService()
    service._iteration()
    stored = _stored(task)
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )

    if retryable:
        assert stored.status == "retry"
        assert stored.next_retry_at is not None
        assert account.status == "error"
        assert account.error_description == (
            f"{stored.error_description}\nNext retry at: {stored.next_retry_at.isoformat()}"
        )
    else:
        description = f"HTTP {status} {code}: {message}"
        assert stored.status == "error"
        assert stored.error_description == description
        assert stored.next_retry_at is None
        assert account.status == "error"
        assert account.error_description == description
    service._iteration()
    session.request.assert_called_once_with(
        "GET",
        "https://zulip.example.com/api/v1/users/me",
        params=None,
        timeout=(5, 30),
    )
    session.close.assert_called_once()


def test_error_hook_can_stop_retries_without_http(task_queue, monkeypatch):
    task = task_queue()
    message = "Task input is invalid"

    def execute(content, task):
        raise ValueError("Invalid task")

    def on_error(content, task, error):
        task.status = "error"
        task.error_description = message

    monkeypatch.setattr(models.InitializeConnectionContent, "execute", execute)
    monkeypatch.setattr(models.InitializeConnectionContent, "on_error", on_error)
    service = history_import.HistoryImportService()

    service._iteration()
    service._iteration()
    stored = _stored(task)

    assert stored.status == "error"
    assert stored.error_description == message
    assert stored.next_retry_at is None


@pytest.mark.parametrize(
    "error", [RuntimeError("Invalid task"), NotImplementedError("Not implemented")]
)
def test_unknown_errors_are_terminal(task_queue, monkeypatch, error):
    task = task_queue()
    attempts = []

    def execute(content, current_task):
        attempts.append(current_task.uuid)
        raise error

    monkeypatch.setattr(models.InitializeConnectionContent, "execute", execute)
    service = history_import.HistoryImportService()
    service._iteration()
    service._iteration()

    stored = _stored(task)
    assert attempts == [task.uuid]
    assert stored.status == "error"
    assert stored.error_description == str(error)
    assert stored.next_retry_at is None


def test_error_hook_controls_the_next_retry_time(task_queue, monkeypatch):
    task = task_queue()
    next_retry_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=10
    )
    attempts = []

    def execute(content, current_task):
        attempts.append(current_task.uuid)
        raise RuntimeError("Try again")

    def on_error(content, current_task, error):
        current_task.status = "retry"
        current_task.next_retry_at = next_retry_at

    monkeypatch.setattr(models.InitializeConnectionContent, "execute", execute)
    monkeypatch.setattr(models.InitializeConnectionContent, "on_error", on_error)
    service = history_import.HistoryImportService()
    service._iteration()
    service._iteration()

    stored = _stored(task)
    assert attempts == [task.uuid]
    assert stored.status == "retry"
    assert stored.error_description == "Try again"
    assert stored.next_retry_at == next_retry_at


def test_worker_skips_leased_tasks(task_queue, monkeypatch):
    monkeypatch.setattr(
        models.InitializeConnectionContent, "on_done", lambda content, task: None
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    first = task_queue(priority=100)
    leased = task_queue(
        priority=90,
        lease_owner=uuid.uuid4(),
        lease_expires_at=now + datetime.timedelta(minutes=5),
    )
    ready = task_queue(priority=1)
    executed = []
    monkeypatch.setattr(
        models.InitializeConnectionContent,
        "execute",
        lambda content, task: executed.append(task.uuid),
    )
    history_import.HistoryImportService()._iteration()
    history_import.HistoryImportService()._iteration()
    assert executed == [first.uuid, ready.uuid]
    assert _stored(leased).status == "new"


@pytest.mark.parametrize("phase", ["execute", "on_done"])
@pytest.mark.parametrize("database_error", [False, True])
def test_failed_execution_or_done_handler_rolls_back_partial_changes(
    task_queue, monkeypatch, database_error, phase
):
    task = task_queue()

    def fail(content, task):
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(task.external_account_uuid)}
        )
        account.settings = {"partial": True}
        account.update()
        if database_error:
            with engines.engine_factory.get_engine().session_manager() as session:
                session.execute("SELECT 1 / 0")
        raise RuntimeError("Execution failed")

    monkeypatch.setattr(
        models.InitializeConnectionContent, "execute", lambda content, task: None
    )
    monkeypatch.setattr(models.InitializeConnectionContent, phase, fail)
    history_import.HistoryImportService()._iteration()

    assert _stored(task).status == "error"
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    assert account.settings == {"server_url": "https://zulip.example.com"}


@pytest.mark.parametrize("database_error", [False, True])
def test_error_handler_failure_rolls_back_and_saves_terminal_error(
    task_queue, monkeypatch, database_error
):
    task = task_queue()

    def execute(content, current_task):
        raise RuntimeError("Execution failed")

    def on_error(content, current_task, error):
        current_task.status = "retry"
        current_task.next_retry_at = datetime.datetime.now(datetime.timezone.utc)
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(current_task.external_account_uuid)}
        )
        account.settings = {"partial": True}
        account.update()
        if database_error:
            with engines.engine_factory.get_engine().session_manager() as session:
                session.execute("SELECT 1 / 0")
        raise RuntimeError("Callback failed")

    monkeypatch.setattr(models.InitializeConnectionContent, "execute", execute)
    monkeypatch.setattr(models.InitializeConnectionContent, "on_error", on_error)
    history_import.HistoryImportService()._iteration()

    stored = _stored(task)
    assert stored.status == "error"
    assert stored.error_description.startswith(
        "Execution failed\nError handler failed:"
    )
    assert ("division by zero" if database_error else "Callback failed") in (
        stored.error_description
    )
    assert stored.next_retry_at is None
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(task.external_account_uuid)}
    )
    assert account.settings == {"server_url": "https://zulip.example.com"}


@pytest.mark.pre_retirement_schema
def test_priority_migration_updates_pending_tasks_and_preserves_completed_tasks(
    _database,
):
    migration = "0208-Reserve-descending-priorities-for-history-import-tasks-bc5850.py"
    with conftest.isolated_migration_database(
        "0207-Add-waiting-status-for-scheduled-bridge-tasks-e1b0e4.py"
    ) as database_url:
        with psycopg.connect(database_url, autocommit=True) as db:
            project_id = uuid.uuid4()
            accounts = [
                (uuid.uuid4(), project_id),
                (uuid.uuid4(), project_id),
                (uuid.uuid4(), uuid.uuid4()),
            ]
            for account_uuid, account_project in accounts:
                owner_uuid = uuid.uuid4()
                conftest.seed_workspace_user(db, owner_uuid, f"priority-{owner_uuid}")
                db.execute(
                    "INSERT INTO m_external_accounts_v2 "
                    "(uuid, project_id, owner_user_uuid, provider, settings) "
                    "VALUES (%s, %s, %s, 'zulip', '{}'::jsonb)",
                    (account_uuid, account_project, owner_uuid),
                )
            cases = [
                (0, status, {"kind": kind}, old_priority, priority)
                for kind, old_priority, priority in (
                    ("initialize_connection", 300, 300_000),
                    ("import_users", 200, 200_000),
                    ("full_backfill", 100, 100_000),
                    ("import_channels", 50, 50_000),
                )
                for status in ("new", "waiting", "retry")
            ]
            cases.append((0, "done", {"kind": "import_users"}, 200, 200))
            cases.extend(
                (
                    account,
                    status,
                    {
                        "kind": "import_history",
                        "first_message_id": first,
                        "last_message_id": first + 4999,
                    },
                    20,
                    priority,
                )
                for account, status, first, priority in (
                    (0, "done", 50_000_001, 20),
                    (0, "new", 49_995_001, 9_999),
                    (0, "retry", 49_990_001, 9_998),
                    (0, "waiting", 50_001, 10),
                    (0, "new", 45_001, 10),
                    (0, "new", 1, 10),
                    (0, "error", 1, 20),
                    (0, "cancel", 1, 20),
                    (1, "new", 5_001, 10_000),
                    (1, "new", 1, 9_999),
                    (2, "new", 1, 10_000),
                )
            )
            tasks = {uuid.uuid4(): case for case in cases}
            for task_uuid, (account, status, content, old_priority, _) in tasks.items():
                account_uuid, account_project = accounts[account]
                db.execute(
                    "INSERT INTO bridge_zulip_v5_tasks "
                    "(uuid, project_id, external_account_uuid, status, content, priority) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
                    (
                        task_uuid,
                        account_project,
                        account_uuid,
                        status,
                        json.dumps(content),
                        old_priority,
                    ),
                )
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(migration)
            for task_uuid, (_, status, content, _, expected) in tasks.items():
                assert db.execute(
                    "SELECT priority, status, content FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                    (task_uuid,),
                ).fetchone() == (expected, status, content)

            engine.rollback_migration(migration)
            for task_uuid, (_, status, content, old_priority, _) in tasks.items():
                assert db.execute(
                    "SELECT priority, status, content FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                    (task_uuid,),
                ).fetchone() == (old_priority, status, content)
            engine.apply_migration(migration)


@pytest.mark.pre_retirement_schema
def test_waiting_status_migration_preserves_schedules_and_rolls_back(_database):
    migration = "0207-Add-waiting-status-for-scheduled-bridge-tasks-e1b0e4.py"
    with conftest.isolated_migration_database(
        "0206-Add-provider-account-identity-to-external-accounts-812e0e.py"
    ) as database_url:
        with psycopg.connect(database_url, autocommit=True) as db:
            owner_uuid, account_uuid, project_id = (uuid.uuid4() for _ in range(3))
            conftest.seed_workspace_user(db, owner_uuid, f"waiting-{owner_uuid}")
            db.execute(
                "INSERT INTO m_external_accounts_v2 "
                "(uuid, project_id, owner_user_uuid, provider, settings) "
                "VALUES (%s, %s, %s, 'zulip', '{}'::jsonb)",
                (account_uuid, project_id, owner_uuid),
            )
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            future = now + datetime.timedelta(minutes=15)
            cases = [
                ("new", future, "waiting"),
                ("new", now - datetime.timedelta(minutes=1), "new"),
                ("new", None, "new"),
                *(
                    (status, future, status)
                    for status in ("retry", "error", "done", "cancel")
                ),
            ]
            tasks = {uuid.uuid4(): case for case in cases}
            for task_uuid, (status, scheduled_at, _) in tasks.items():
                db.execute(
                    "INSERT INTO bridge_zulip_v5_tasks "
                    "(uuid, project_id, external_account_uuid, status, not_before_at, content) "
                    'VALUES (%s, %s, %s, %s, %s, \'{"kind": "initialize_connection"}\'::jsonb)',
                    (task_uuid, project_id, account_uuid, status, scheduled_at),
                )
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(migration)
            for task_uuid, (_, scheduled_at, expected) in tasks.items():
                assert db.execute(
                    "SELECT status, not_before_at FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                    (task_uuid,),
                ).fetchone() == (expected, scheduled_at)
            index = db.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname='bridge_zulip_v5_tasks_ready_idx'"
            ).fetchone()[0]
            assert "waiting" in index

            engine.rollback_migration(migration)
            for task_uuid, (status, scheduled_at, _) in tasks.items():
                assert db.execute(
                    "SELECT status, not_before_at FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                    (task_uuid,),
                ).fetchone() == (status, scheduled_at)
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_tasks SET status='waiting' WHERE uuid=%s",
                    (next(iter(tasks)),),
                )
            engine.apply_migration(migration)


@pytest.mark.pre_retirement_schema
def test_task_metadata_and_cancel_migrations_round_trip(_database):
    metadata_migration = "0197-Move-external-account-reference-to-task-5af16f.py"
    cancel_migration = "0198-Add-task-cancellation-status-452cb8.py"
    with conftest.isolated_migration_database(
        "0196-Remove-external-account-revision-fcddce.py"
    ) as database_url:
        with psycopg.connect(database_url, autocommit=True) as db:
            owner_uuid = uuid.uuid4()
            account_uuid = uuid.uuid4()
            conftest.seed_workspace_user(db, owner_uuid, "task-migration-owner")
            db.execute(
                "INSERT INTO m_external_accounts_v2 "
                "(uuid, owner_user_uuid, provider, settings) "
                "VALUES (%s, %s, 'zulip', '{}'::jsonb)",
                (account_uuid, owner_uuid),
            )
            tasks = {
                uuid.uuid4(): {
                    "kind": "initialize_connection",
                    "external_account_uuid": str(account_uuid),
                },
                uuid.uuid4(): {
                    "kind": "import_history",
                    "external_account_uuid": str(account_uuid),
                    "first_message_id": 10,
                    "last_message_id": 20,
                },
            }
            for task_uuid, content in tasks.items():
                db.execute(
                    "INSERT INTO bridge_zulip_v5_tasks (uuid, content) "
                    "VALUES (%s, %s::jsonb)",
                    (task_uuid, json.dumps(content)),
                )

            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(cancel_migration)
            for task_uuid, content in tasks.items():
                assert db.execute(
                    "SELECT external_account_uuid, content "
                    "FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                    (task_uuid,),
                ).fetchone() == (
                    account_uuid,
                    {
                        name: value
                        for name, value in content.items()
                        if name != "external_account_uuid"
                    },
                )

            cancelled_uuid = next(iter(tasks))
            db.execute(
                "UPDATE bridge_zulip_v5_tasks SET status='cancel' WHERE uuid=%s",
                (cancelled_uuid,),
            )
            engine.rollback_migration(cancel_migration)
            assert (
                db.execute(
                    "SELECT status FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                    (cancelled_uuid,),
                ).fetchone()[0]
                == "error"
            )
            engine.rollback_migration(metadata_migration)
            for task_uuid, content in tasks.items():
                assert db.execute(
                    "SELECT account_uuid, content "
                    "FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                    (task_uuid,),
                ).fetchone() == (account_uuid, content)

            engine.apply_migration(cancel_migration)
            db.execute(
                "DELETE FROM m_external_accounts_v2 WHERE uuid=%s", (account_uuid,)
            )
            assert (
                db.execute("SELECT COUNT(*) FROM bridge_zulip_v5_tasks").fetchone()[0]
                == 0
            )


@pytest.mark.pre_retirement_schema
@pytest.mark.parametrize("source", ["created_event", "single_project", "ambiguous"])
def test_account_and_task_project_migration_preserves_data(_database, source):
    migration = "0199-Scope-external-accounts-and-Zulip-tasks-to-projects-4fb70b.py"
    with conftest.isolated_migration_database(
        "0198-Add-task-cancellation-status-452cb8.py"
    ) as database_url:
        with psycopg.connect(database_url, autocommit=True) as db:
            owner_uuid, account_uuid, task_uuid, project_id = (
                uuid.uuid4() for _ in range(4)
            )
            conftest.seed_workspace_user(db, owner_uuid, f"project-owner-{owner_uuid}")
            db.execute(
                "INSERT INTO m_external_accounts_v2 "
                "(uuid, owner_user_uuid, provider, settings) "
                "VALUES (%s, %s, 'zulip', '{}'::jsonb)",
                (account_uuid, owner_uuid),
            )
            content = {"kind": "initialize_connection"}
            db.execute(
                "INSERT INTO bridge_zulip_v5_tasks "
                "(uuid, external_account_uuid, content) VALUES (%s, %s, %s::jsonb)",
                (task_uuid, account_uuid, json.dumps(content)),
            )
            db.execute(
                "INSERT INTO m_workspace_event_cursors (project_id, user_uuid) "
                "VALUES (%s, %s)",
                (project_id, owner_uuid),
            )
            if source != "single_project":
                db.execute(
                    "INSERT INTO m_workspace_event_cursors (project_id, user_uuid) "
                    "VALUES (%s, %s)",
                    (uuid.uuid4(), owner_uuid),
                )
            if source == "created_event":
                db.execute(
                    "INSERT INTO m_workspace_events "
                    "(uuid, project_id, user_uuid, object_type, action, payload) "
                    "VALUES (%s, %s, %s, 'external_account', 'created', %s::jsonb)",
                    (
                        uuid.uuid4(),
                        project_id,
                        owner_uuid,
                        json.dumps(
                            {
                                "kind": "external_account.created",
                                "uuid": str(account_uuid),
                            }
                        ),
                    ),
                )
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            if source == "ambiguous":
                with pytest.raises(
                    psycopg.errors.RaiseException, match="Cannot infer project_id"
                ):
                    engine.apply_migration(migration)
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_name='m_external_accounts_v2' AND column_name='project_id'"
                    ).fetchone()[0]
                    == 0
                )
                return

            engine.apply_migration(migration)
            assert db.execute(
                "SELECT project_id FROM m_external_accounts_v2 WHERE uuid=%s",
                (account_uuid,),
            ).fetchone() == (project_id,)
            assert db.execute(
                "SELECT project_id, external_account_uuid, content FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                (task_uuid,),
            ).fetchone() == (project_id, account_uuid, content)
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_tasks SET project_id=%s WHERE uuid=%s",
                    (uuid.uuid4(), task_uuid),
                )
            engine.rollback_migration(migration)
            assert db.execute(
                "SELECT external_account_uuid, content FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                (task_uuid,),
            ).fetchone() == (account_uuid, content)
            engine.apply_migration(migration)
            db.execute(
                "DELETE FROM m_external_accounts_v2 WHERE uuid=%s", (account_uuid,)
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                    (task_uuid,),
                ).fetchone()[0]
                == 0
            )
