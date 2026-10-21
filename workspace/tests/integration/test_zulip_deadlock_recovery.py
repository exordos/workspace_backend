# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import uuid

from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_messages as message_tests
from workspace.tests.integration import test_zulip_task_workers as worker_tests


history_account = message_tests.history_account


def test_migration_requeues_captured_history_deadlocks_without_changing_other_work(
    history_account, db
):
    account = history_account
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = next(conftest.MIGRATIONS_DIR.glob("0245-*.py")).name
    tasks = []
    recovered = []
    unchanged = []
    now = datetime.datetime.now(datetime.timezone.utc)

    def create(case, **values):
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            queue=case["queue"],
            content=models.ImportHistoryContent(
                **{
                    key: value
                    for key, value in case["content"].items()
                    if key != "kind"
                }
            ),
            status="error",
            error_description=case["message"],
            priority=9814,
            execution_time=1.25,
            queue_wait_time=300.0,
        )
        task.update_dm(values=values)
        task.insert()
        tasks.append(task)
        return task

    def stored(task):
        return db.execute(
            "SELECT row_to_json(task) FROM bridge_zulip_v5_tasks AS task WHERE uuid=%s",
            (task.uuid,),
        ).fetchone()[0]

    try:
        engine.rollback_migration(migration)
        for case in worker_tests.DEADLOCK_CASES:
            handler_message = case.get("handler_message", "deadlock detected")
            for suffix in ("", f"\nError handler failed: {handler_message}"):
                task = create(
                    case,
                    error_description=case["message"] + suffix,
                    lease_owner=uuid.uuid4(),
                    lease_expires_at=now + datetime.timedelta(minutes=1),
                )
                recovered.append((task, stored(task)))
        for status in ("done", "cancel", "new", "waiting", "retry"):
            task = create(
                worker_tests.DEADLOCK_CASES[0],
                status=status,
                next_retry_at=now + datetime.timedelta(minutes=10),
            )
            unchanged.append((task, stored(task)))
        for message in (
            "Download timed out",
            "HTTP 403 FORBIDDEN: Access denied",
            "Could not parse deadlock detected",
            "deadlock detected in a filename",
            "RuntimeError\ndeadlock detected",
        ):
            task = create(worker_tests.DEADLOCK_CASES[0], error_description=message)
            unchanged.append((task, stored(task)))
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            content=models.InitializeConnectionContent(),
            status="error",
            error_description=worker_tests.DEADLOCK_CASES[0]["message"],
        )
        task.insert()
        tasks.append(task)
        unchanged.append((task, stored(task)))

        before = datetime.datetime.now(datetime.timezone.utc)
        engine.apply_migration(migration)
        after = datetime.datetime.now(datetime.timezone.utc)
        changed = {
            "status",
            "next_retry_at",
            "lease_owner",
            "lease_expires_at",
            "updated_at",
        }
        for task, original in recovered:
            current = stored(task)
            assert current["status"] == "retry"
            assert current["lease_owner"] is None
            assert current["lease_expires_at"] is None
            next_retry = datetime.datetime.fromisoformat(
                current["next_retry_at"]
            ).replace(tzinfo=datetime.timezone.utc)
            assert before <= next_retry <= after
            assert {
                key: value for key, value in current.items() if key not in changed
            } == {key: value for key, value in original.items() if key not in changed}
        for task, original in unchanged:
            assert stored(task) == original

        finished = recovered[0][0]
        db.execute(
            "UPDATE bridge_zulip_v5_tasks SET status='done' WHERE uuid=%s",
            (finished.uuid,),
        )
        retried = {task.uuid: stored(task) for task, _original in recovered}
        engine.rollback_migration(migration)
        engine.apply_migration(migration)
        assert {task.uuid: stored(task) for task, _original in recovered} == retried
    finally:
        engine.apply_migration(engine.get_latest_migration())
        for task in tasks:
            task.delete()
