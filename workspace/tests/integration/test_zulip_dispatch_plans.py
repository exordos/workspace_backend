# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Keep dispatcher reads bounded when history dwarfs the realtime queue."""

import json
import os
import uuid

from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models
from workspace.services import history_import
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_messages as message_tests


history_account = message_tests.history_account


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def _explain(session, query, values=None):
    return session.execute(
        "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + query, values
    ).fetchone()["QUERY PLAN"][0]


def test_dispatch_and_recurring_checks_use_bounded_generic_plans(
    history_account, db, monkeypatch
):
    account = history_account
    count = int(os.environ.get("WORKSPACE_ZULIP_DISPATCH_BACKLOG", "30000"))
    db.execute(
        """INSERT INTO bridge_zulip_v5_tasks
           (uuid,project_id,external_account_uuid,content,status,priority)
           SELECT md5(%s || i::text)::uuid,%s,%s,
               jsonb_build_object('kind','import_history','first_message_id',i*100-99,
                   'last_message_id',i*100), 'new',10000
           FROM generate_series(1,%s) AS i""",
        (str(uuid.uuid4()), account.project_id, account.uuid, count),
    )
    db.execute(
        """INSERT INTO bridge_zulip_v5_tasks
           (uuid,project_id,external_account_uuid,content,status,priority)
           SELECT md5(%s || i::text)::uuid,%s,%s,
               jsonb_build_object('kind','reconciliation','batch_size',16,'interval',10),
               'done',200000 FROM generate_series(1,%s) AS i""",
        (str(uuid.uuid4()), account.project_id, account.uuid, count // 2),
    )
    db.execute("ANALYZE bridge_zulip_v5_tasks")
    service = history_import.HistoryImportService(queue="realtime")
    before = {
        task.uuid
        for task in models.Task.objects.get_all(
            filters={"project_id": filters.Is(None)}
        )
    }
    try:
        with contexts.Context().session_manager() as session:
            execute = session.execute
            queries = []

            def capture(query, values=None):
                queries.append((query, values))
                return execute(query, values)

            with monkeypatch.context() as patch:
                patch.setattr(session, "execute", capture)
                assert service._claim_tasks(1) == []
                service._ensure_reconciliation_task()
            claim, values = next(
                row for row in queries if row[0].startswith("WITH candidate")
            )
            recurring = next(
                query for query, _ in queries if "SELECT DISTINCT" in query
            )
            session.execute("SET LOCAL plan_cache_mode=force_generic_plan")
            prepared = claim.replace("%s", "$1", 1)
            session.execute("PREPARE zulip_dispatch_bounded(uuid) AS " + prepared)
            dispatch_plan = _explain(
                session, f"EXECUTE zulip_dispatch_bounded('{values[0]}')"
            )
            recurring_plan = _explain(session, recurring)
            for plan, index in (
                (dispatch_plan, "bridge_zulip_v5_tasks_queue_ready_idx"),
                (recurring_plan, "bridge_zulip_v5_tasks_recurring_idx"),
            ):
                assert index in {
                    node.get("Index Name") for node in _nodes(plan["Plan"])
                }, json.dumps(plan)
                assert (
                    plan["Plan"]["Shared Hit Blocks"]
                    + plan["Plan"]["Shared Read Blocks"]
                    < 100
                )

            # Reproduce the old optional queue filter under the same generic
            # planner, including the real UPDATE/row-locking part of the claim.
            baseline = claim.replace(
                "AND task.queue='realtime'", "AND (%s='all' OR task.queue=%s)"
            )
            for number in range(1, 4):
                baseline = baseline.replace("%s", f"${number}", 1)
            session.execute(
                "PREPARE zulip_dispatch_optional(text,text,uuid) AS " + baseline
            )
            old_dispatch = _explain(
                session,
                f"EXECUTE zulip_dispatch_optional('realtime','realtime','{values[0]}')",
            )
            assert (
                max(
                    node.get("Rows Removed by Filter", 0)
                    for node in _nodes(old_dispatch["Plan"])
                )
                >= count
            )
            session.execute("""PREPARE zulip_recurring_optional(text,text,text,text) AS
                SELECT * FROM bridge_zulip_v5_tasks WHERE content->>'kind'=$1
                AND status IN ($2,$3,$4) LIMIT 1""")
            old_recurring = _explain(
                session,
                "EXECUTE zulip_recurring_optional('reconciliation','new','waiting','retry')",
            )
            print(
                json.dumps(
                    {
                        "pending_history": count,
                        "completed_tasks": count // 2,
                        "dispatch_before_ms": old_dispatch["Execution Time"],
                        "dispatch_after_ms": dispatch_plan["Execution Time"],
                        "recurring_before_ms": old_recurring["Execution Time"],
                        "recurring_after_ms": recurring_plan["Execution Time"],
                    }
                )
            )
    finally:
        for task in models.Task.objects.get_all(
            filters={"project_id": filters.Is(None)}
        ):
            if task.uuid not in before:
                task.delete()


def test_recurring_index_migration_round_trips_without_changing_tasks(
    _database, db, scheduled_reconciliation
):
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = next(conftest.MIGRATIONS_DIR.glob("0244-*.py")).name
    before = db.execute(
        "SELECT uuid,status,content FROM bridge_zulip_v5_tasks ORDER BY uuid"
    ).fetchall()
    try:
        engine.rollback_migration(migration)
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_tasks_recurring_idx')"
        ).fetchone() == (None,)
        engine.apply_migration(migration)
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_tasks_recurring_idx') IS NOT NULL"
        ).fetchone() == (True,)
        assert (
            db.execute(
                "SELECT uuid,status,content FROM bridge_zulip_v5_tasks ORDER BY uuid"
            ).fetchall()
            == before
        )
    finally:
        engine.apply_migration(engine.get_latest_migration())
