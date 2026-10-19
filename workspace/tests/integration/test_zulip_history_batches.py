# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import concurrent.futures
import datetime
import threading
import time
import uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.tests.integration import conftest


MIGRATION = "0237-Split-pending-Zulip-history-imports-into-100-ID-batches-342a1c.py"


@pytest.fixture
def account(_database, db, scheduled_reconciliation):
    owner = uuid.uuid4()
    conftest.seed_workspace_user(db, owner, f"history-batches-{owner}")
    value = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=owner,
        provider="zulip",
        external_account_id="zulip.example.com:7",
        status="backfill",
        settings={
            "server_url": "https://zulip.example.com",
            "email": "owner@example.com",
            "api_key": "test",
        },
    )
    value.insert()
    yield value
    value.delete()


@pytest.fixture
def migration(_database, db):
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(MIGRATION)
    # Keep current task models usable while testing the earlier data migration.
    db.execute("""
        ALTER TABLE bridge_zulip_v5_tasks
            ADD COLUMN IF NOT EXISTS execution_time DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS queue_wait_time DOUBLE PRECISION;
    """)
    try:
        yield engine
    finally:
        engine.apply_migration(engine.get_latest_migration())


def _backfill(account, last_id):
    with contexts.Context().session_manager():
        subscription = models.RealtimeSubscription.register(
            account,
            {
                "queue_id": "history-batches",
                "last_event_id": -1,
                "max_message_id": last_id,
                "event_queue_longpoll_timeout_seconds": 90,
            },
            datetime.datetime.now(datetime.timezone.utc),
        )
    event = models.RealtimeEvent.objects.get_one(
        filters={"uuid": filters.EQ(uuid.uuid5(subscription.generation, "backfill"))}
    )
    event.processed_at = datetime.datetime.now(datetime.timezone.utc)
    event.update()
    return event


def _tasks(account):
    return models.Task.objects.get_all(
        filters={"external_account_uuid": filters.EQ(account.uuid)},
        order_by={"priority": "desc", "created_at": "asc", "uuid": "asc"},
    )


def _snapshot(account, db):
    return db.execute(
        "SELECT to_jsonb(task) FROM bridge_zulip_v5_tasks AS task "
        "WHERE external_account_uuid = %s ORDER BY uuid",
        (account.uuid,),
    ).fetchall()


@pytest.mark.parametrize(
    "status,first,last",
    [("new", 1, 5000), ("waiting", 17, 221), ("retry", 1001, 1101), ("error", 1, 101)],
)
def test_migration_splits_unfinished_ranges_and_preserves_metadata(
    account, migration, db, status, first, last
):
    event = _backfill(account, last)
    since = datetime.datetime(2026, 9, 19, tzinfo=datetime.timezone.utc)
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    original = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        backfill_uuid=event.uuid,
        realtime_event_uuid=event.uuid,
        priority=9876,
        status=status,
        content=models.ImportHistoryContent(
            first_message_id=first, last_message_id=last, since=since
        ),
        error_description="Previous error" if status in ("retry", "error") else None,
        not_before_at=future if status == "waiting" else None,
        next_retry_at=future if status == "retry" else None,
        lease_owner=uuid.uuid4(),
        lease_expires_at=future,
    )
    original.insert()
    migration.apply_migration(MIGRATION)
    tasks = _tasks(account)
    cancelled = next(task for task in tasks if task.uuid == original.uuid)
    assert cancelled.status == "cancel"
    assert cancelled.backfill_uuid is None
    assert cancelled.realtime_event_uuid == event.uuid
    assert (
        cancelled.next_retry_at
        is cancelled.lease_owner
        is cancelled.lease_expires_at
        is None
    )
    assert cancelled.content.as_plain_dict() == original.content.as_plain_dict()
    children = [task for task in tasks if task.uuid != original.uuid]
    assert len(children) == (last - first) // 100 + 1
    starts = [child.content.first_message_id for child in children]
    assert starts == sorted(starts, reverse=True)
    assert sorted(
        message_id
        for child in children
        for message_id in range(
            child.content.first_message_id, child.content.last_message_id + 1
        )
    ) == list(range(first, last + 1))
    for child in children:
        assert child.project_id == original.project_id
        assert child.external_account_uuid == original.external_account_uuid
        assert child.backfill_uuid == child.realtime_event_uuid == event.uuid
        assert child.priority == original.priority
        assert child.status == status
        assert child.content.since == since
        assert child.error_description == original.error_description
        assert child.not_before_at == original.not_before_at
        assert child.next_retry_at == original.next_retry_at
        assert child.lease_owner is child.lease_expires_at is None
        assert (
            1
            <= child.content.last_message_id - child.content.first_message_id + 1
            <= 100
        )
    before = _snapshot(account, db)
    migration.rollback_migration(MIGRATION)
    migration.apply_migration(MIGRATION)
    assert _snapshot(account, db) == before


def test_migration_leaves_terminal_and_small_history_tasks_unchanged(
    account, migration, db
):
    for status, last in (("done", 5000), ("cancel", 5000), ("new", 100), ("retry", 1)):
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            status=status,
            content=models.ImportHistoryContent(
                first_message_id=1, last_message_id=last
            ),
        )
        task.insert()
    before = _snapshot(account, db)
    migration.apply_migration(MIGRATION)
    assert _snapshot(account, db) == before


def test_migration_updates_only_unfinished_reconciliation_priorities(
    account, migration
):
    for status in ("new", "waiting", "retry", "error", "done", "cancel"):
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            priority=400_000,
            status=status,
            content=models.ReconciliationContent(),
        )
        task.insert()
    migration.apply_migration(MIGRATION)
    for task in _tasks(account):
        assert task.priority == (
            400_000 if task.status in ("done", "cancel") else 200_000
        )


def test_cancelled_parent_does_not_block_backfill_completion(account, migration):
    event = _backfill(account, 205)
    parent = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        backfill_uuid=event.uuid,
        realtime_event_uuid=event.uuid,
        content=models.ImportHistoryContent(first_message_id=1, last_message_id=205),
    )
    parent.insert()
    migration.apply_migration(MIGRATION)
    children = [task for task in _tasks(account) if task.uuid != parent.uuid]
    assert len(children) == 3
    for index, child in enumerate(children):
        with contexts.Context().session_manager():
            child.on_done()
        current = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(account.uuid)}
        )
        assert current.status == ("active" if index == 2 else "backfill")
    assert current.has_completed_backfill


def test_migration_waits_for_running_history_and_does_not_reimport_it(
    account, migration, db
):
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.ImportHistoryContent(first_message_id=1, last_message_id=5000),
    )
    task.insert()
    with psycopg.connect(conftest.TEST_DB_URL) as running:
        running.execute(
            "SELECT uuid FROM bridge_zulip_v5_tasks WHERE uuid = %s", (task.uuid,)
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(migration.apply_migration, MIGRATION)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if db.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE relation = 'bridge_zulip_v5_tasks'::regclass "
                        "AND mode = 'AccessExclusiveLock' AND NOT granted)"
                    ).fetchone()[0]:
                        break
                    threading.Event().wait(0.01)
                else:
                    pytest.fail("Migration did not wait for the running task")
                running.execute(
                    "UPDATE bridge_zulip_v5_tasks SET status = 'done' WHERE uuid = %s",
                    (task.uuid,),
                )
                running.commit()
            finally:
                running.rollback()
            future.result(timeout=10)
    (completed,) = _tasks(account)
    assert completed.uuid == task.uuid
    assert completed.status == "done"


def test_failed_split_rolls_back_children_parent_cancellation_and_priority(
    account, migration, db
):
    for content, priority in (
        (models.ImportHistoryContent(first_message_id=1, last_message_id=205), 10_000),
        (models.ReconciliationContent(), 400_000),
    ):
        models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            priority=priority,
            content=content,
        ).insert()
    before = _snapshot(account, db)
    db.execute("""
        CREATE FUNCTION test_reject_history_split() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.content->>'kind' = 'import_history'
               AND (NEW.content->>'first_message_id')::bigint = 101 THEN
                RAISE EXCEPTION 'Cannot split history';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER test_reject_history_split BEFORE INSERT ON bridge_zulip_v5_tasks
        FOR EACH ROW EXECUTE FUNCTION test_reject_history_split();
    """)
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="Cannot split history"):
            migration.apply_migration(MIGRATION)
        assert _snapshot(account, db) == before
    finally:
        db.execute("""
            DROP TRIGGER test_reject_history_split ON bridge_zulip_v5_tasks;
            DROP FUNCTION test_reject_history_split();
        """)
