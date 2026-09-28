# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""Real legacy projection load must not be amplified by baseline capture."""

import concurrent.futures
import threading
import time
import uuid as sys_uuid

import psycopg
import pytest
from restalchemy.storage.sql import migrations

from workspace.services.workspace_v3_workers import agents
from workspace.tests.integration import conftest
from workspace.tests.integration import (
    test_workspace_v3_hierarchical_counters as counters,
)
from workspace.tests.integration import test_workspace_v3_projection as support


BASELINE = "0207-Maintain-exact-hierarchical-unread-counters-77e2f5.py"
BACKPRESSURE = (
    "0208-Keep-unread-baseline-snapshots-out-of-the-legacy-projection-queue-ec0e37.py"
)
SETTINGS = (
    "workspace_v3.suppress_counter_snapshots",
    "workspace_v3.suppress_folder_item_projection",
)


def _migrations():
    return migrations.MigrationEngine(
        migrations_path=str(conftest.MIGRATIONS_DIR)
    )._load_migrations()


def _cold_corpus(db, *, topic_count=3, messages_per_topic=20, fixed=True):
    steps = _migrations()
    project, stream, topic, users = support._seed_conversation(
        db, user_count=1, clear_tasks=False
    )
    # Exercise baseline propagation through pre-existing default folder items.
    support._drain(db)
    assert (
        db.execute(
            "SELECT count(*) FROM workspace_v3.folder_items WHERE project_id=%s",
            (project,),
        ).fetchone()[0]
        > 0
    )
    with db.transaction():
        steps[BACKPRESSURE].downgrade(db)
        steps[BASELINE].downgrade(db)
    topics = [topic]
    for _ in range(topic_count - 1):
        topics.append(counters._topic(db, project, stream, users[0]))
    messages = []
    with db.transaction():
        for topic in topics:
            messages.extend(
                counters._messages(
                    db, project, stream, topic, users[0], messages_per_topic
                )
            )
    db.execute(
        "DELETE FROM workspace_v3.projection_tasks WHERE project_id=%s", (project,)
    )
    with db.transaction():
        steps[BASELINE].upgrade(db)
        if fixed:
            steps[BACKPRESSURE].upgrade(db)
    return project, stream, topics, users[0], messages


@pytest.fixture
def cold_corpus(_database, db):
    projects = []

    def seed(**kwargs):
        corpus = _cold_corpus(db, **kwargs)
        projects.append(corpus[0])
        return corpus

    yield seed
    for project in projects:
        _finish(db)
        # Remove only this test's root; schema foreign keys own source cleanup.
        db.execute("DELETE FROM workspace_v3.streams WHERE project_id=%s", (project,))
    support._drain(db, limit=100)


def _state(db):
    return db.execute(
        "SELECT ready,snapshots_complete,last_flag_uuid,snapshot_binding_uuid "
        "FROM workspace_v3.unread_counter_baseline"
    ).fetchone()


def _finish(db):
    for _ in range(2000):
        db.execute("SELECT workspace_v3.advance_unread_baseline(1000)")
        if _state(db)[1]:
            support._drain(db, limit=100)
            return
    raise AssertionError("Bounded test baseline did not finish")


def _worker():
    worker = agents.WorkspaceV3ProjectionAgent(
        batch_size=100, idle_sleep_seconds=0.001, metrics_log_interval_seconds=3600
    )
    # Event retention is a separate periodic responsibility, not this workload.
    worker._event_pruned_at = time.monotonic()
    return worker


def test_baseline_migration_removes_real_legacy_projection_feedback(
    cold_corpus, db, record_property
):
    project, _stream, _topics, _user, _messages = cold_corpus(
        topic_count=120, messages_per_topic=100, fixed=False
    )
    db.execute("SELECT workspace_v3.advance_unread_baseline(1000)")
    assert not _state(db)[0]
    pending = db.execute(
        "SELECT uuid FROM workspace_v3.projection_tasks "
        "WHERE status='pending' ORDER BY uuid"
    ).fetchall()
    # One unmodified 0207 capture page manufactures an entire worker batch.
    assert len(pending) >= 100
    record_property("legacy_generated_tasks", len(pending))
    scopes = db.execute(
        "SELECT project_id,topic_uuid,user_uuid FROM workspace_v3.topic_bindings "
        "WHERE project_id=%s ORDER BY uuid LIMIT 5",
        (project,),
    ).fetchall()
    started = time.monotonic()
    legacy_timed_out = False
    try:
        with db.transaction():
            db.execute("SET LOCAL statement_timeout='2s'")
            support.projections._update_topic_counters(db, scopes)
    except psycopg.errors.QueryCanceled:
        # Keep the pathological legacy workload bounded in the regression.
        # The full worker reference is captured by the diagnostic run.
        legacy_timed_out = True
    record_property("legacy_five_scope_projection_seconds", time.monotonic() - started)
    record_property("legacy_five_scope_projection_timed_out", legacy_timed_out)
    before = _state(db)
    with db.transaction():
        _migrations()[BACKPRESSURE].upgrade(db)
    assert _state(db) == before
    pending_before = db.execute(
        "SELECT uuid FROM workspace_v3.projection_tasks "
        "WHERE status='pending' ORDER BY uuid"
    ).fetchall()
    started = time.monotonic()
    for _ in range(5):
        assert (
            db.execute("SELECT workspace_v3.advance_unread_baseline(1000)").fetchone()[
                0
            ]
            == 1000
        )
    elapsed = time.monotonic() - started
    assert not _state(db)[0]
    assert _state(db)[2] > before[2]
    assert (
        db.execute(
            "SELECT uuid FROM workspace_v3.projection_tasks "
            "WHERE status='pending' ORDER BY uuid"
        ).fetchall()
        == pending_before
    )
    record_property("fixed_five_capture_pages_seconds", elapsed)
    record_property("fixed_generated_tasks", 0)
    record_property("fixed_capture_flags_per_second", 5000 / elapsed)

    # A new database session models restart after a committed cursor page.
    with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as resumed:
        assert _state(resumed) == _state(db)
        _finish(resumed)
    assert _state(db)[:2] == (True, True)
    counters._oracle(db, project)
    assert (
        db.execute(
            "SELECT unread_count FROM workspace_v3.stream_bindings WHERE project_id=%s",
            (project,),
        ).fetchone()[0]
        == 120
    )


def _settings(db):
    return tuple(
        db.execute("SELECT current_setting(%s,true)", (setting,)).fetchone()[0]
        for setting in SETTINGS
    )


def _caller_settings(db):
    for setting in SETTINGS:
        db.execute("SELECT set_config(%s,'off',true)", (setting,))


def test_baseline_setting_restores_on_success_error_and_skip_locked(cold_corpus, db):
    project, stream, topics, user, _messages = cold_corpus()
    with db.transaction():
        _caller_settings(db)
        assert (
            db.execute("SELECT workspace_v3.advance_unread_baseline(7)").fetchone()[0]
            == 7
        )
        assert _settings(db) == ("off", "off")
        with pytest.raises(psycopg.errors.InvalidRowCountInLimitClause):
            with db.transaction():
                db.execute("SELECT workspace_v3.advance_unread_baseline(-1)")
        assert _settings(db) == ("off", "off")
        assert (
            db.execute(
                "SELECT count(*) FROM workspace_v3.projection_tasks "
                "WHERE project_id=%s AND status='pending'",
                (project,),
            ).fetchone()[0]
            == 0
        )
        # Same outer transaction, after the function's local settings returned:
        # a real new canonical flag must still schedule its public events.
        counters._messages(db, project, stream, topics[0], user, 1)
        assert (
            db.execute(
                "SELECT count(*) FROM workspace_v3.projection_tasks "
                "WHERE project_id=%s AND status='pending'",
                (project,),
            ).fetchone()[0]
            > 0
        )
    assert "on" not in _settings(db)
    with psycopg.connect(conftest.TEST_DB_URL) as blocker:
        blocker.execute("SELECT * FROM workspace_v3.unread_counter_baseline FOR UPDATE")
        with db.transaction():
            _caller_settings(db)
            assert (
                db.execute("SELECT workspace_v3.advance_unread_baseline(7)").fetchone()[
                    0
                ]
                == 0
            )
            assert _settings(db) == ("off", "off")
    _finish(db)
    counters._oracle(db, project)


def test_upgrade_with_backlog_and_concurrent_live_writes_preserves_final_snapshots(
    cold_corpus, db, record_property
):
    project, stream, topics, user, messages = cold_corpus(
        topic_count=12, messages_per_topic=1200, fixed=False
    )
    # Simulate the already deployed 0207: one committed baseline page has
    # populated the legacy queue, and canonical import is still running.
    db.execute("SELECT workspace_v3.advance_unread_baseline(1000)")
    counters._messages(db, project, stream, topics[0], user, 3)
    pending_before = db.execute(
        "SELECT uuid FROM workspace_v3.projection_tasks "
        "WHERE status='pending' ORDER BY uuid"
    ).fetchall()
    assert pending_before
    assert not _state(db)[0]
    state_before = _state(db)
    with db.transaction():
        _migrations()[BACKPRESSURE].upgrade(db)
    assert _state(db) == state_before
    assert (
        db.execute(
            "SELECT uuid FROM workspace_v3.projection_tasks "
            "WHERE status='pending' ORDER BY uuid"
        ).fetchall()
        == pending_before
    )
    record_property("pending_tasks_at_upgrade", len(pending_before))
    before_events = db.execute(
        "SELECT coalesce(max(epoch_version),0) FROM workspace_v3.events WHERE project_id=%s",
        (project,),
    ).fetchone()[0]
    barrier = threading.Barrier(3)

    def run_worker():
        worker = _worker()
        baseline = agents.WorkspaceV3BaselineAgent()
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as observer:
            barrier.wait(timeout=5)
            for iteration in range(2000):
                baseline._iteration()
                worker._iteration()
                if _state(observer)[1]:
                    return iteration + 1
        raise AssertionError("Concurrent worker exceeded bounded iterations")

    def live_writer():
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as writer:
            barrier.wait(timeout=5)
            for index, message in enumerate(messages[:12]):
                writer.execute(
                    "UPDATE workspace_v3.message_flags SET read=true "
                    "WHERE project_id=%s AND message_uuid=%s AND user_uuid=%s",
                    (project, message, user),
                )
                counters._messages(writer, project, stream, topics[index], user, 3)
                time.sleep(0.01)
            assert "on" not in _settings(writer)

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        workers = [pool.submit(run_worker) for _ in range(2)]
        writer = pool.submit(live_writer)
        writer.result(timeout=60)
        iterations = [future.result(timeout=90) for future in workers]
    support._drain(db, limit=100)
    record_property("concurrent_recovery_seconds", time.monotonic() - started)
    record_property("concurrent_worker_iterations", iterations)
    assert _state(db)[:2] == (True, True)
    counters._oracle(db, project)
    assert (
        db.execute(
            "SELECT count(*) FROM workspace_v3.projection_tasks "
            "WHERE project_id=%s AND status<>'completed'",
            (project,),
        ).fetchone()[0]
        == 0
    )
    events = db.execute(
        "SELECT object_type,action,entity_uuid FROM workspace_v3.events "
        "WHERE project_id=%s AND epoch_version>%s",
        (project, before_events),
    ).fetchall()
    actions = {(kind, action) for kind, action, _uuid in events}
    assert ("message", "read") in actions
    assert ("stream", "updated") in actions
    assert ("folder", "updated") in actions
    assert {
        entity
        for kind, action, entity in events
        if (kind, action) == ("topic", "updated")
    } == set(topics)
    affected_folders = {
        row[0]
        for row in db.execute(
            "SELECT folder_uuid FROM workspace_v3.folder_items WHERE project_id=%s",
            (project,),
        ).fetchall()
    }
    assert {
        entity
        for kind, action, entity in events
        if (kind, action) == ("folder", "updated")
    } == affected_folders
    snapshots = db.execute(
        """SELECT DISTINCT ON (event.object_type,event.entity_uuid)
            event.object_type,event.payload || coalesce(recipient.payload,'{}'::jsonb)
        FROM workspace_v3.events AS event
        LEFT JOIN workspace_v3.event_recipient_payloads AS recipient
          ON recipient.event_uuid=event.uuid AND recipient.consumer_uuid=%s
        WHERE event.project_id=%s AND event.epoch_version>%s
          AND event.object_type IN ('stream','folder') AND event.action='updated'
        ORDER BY event.object_type,event.entity_uuid,event.epoch_version DESC""",
        (user, project, before_events),
    ).fetchall()
    for kind, payload in snapshots:
        assert payload["unread_count"] == (len(topics) if kind == "stream" else 1)
        assert "counter_version" not in payload
        assert "unread_topic_count" not in payload


def test_backpressure_downgrade_preserves_baseline_state_and_query_settings(
    _database, db
):
    before = _state(db)
    with db.transaction():
        _migrations()[BACKPRESSURE].downgrade(db)
    assert _state(db) == before
    options = db.execute(
        "SELECT proconfig FROM pg_proc "
        "WHERE oid='workspace_v3.advance_unread_baseline(integer)'::regprocedure"
    ).fetchone()[0]
    assert sorted(options) == ["enable_sort=off", "jit=off"]
    definition = db.execute(
        "SELECT pg_get_functiondef('workspace_v3.enqueue_exact_counter_snapshot()'::regprocedure)"
    ).fetchone()[0]
    assert "suppress_counter_snapshots" not in definition
    with db.transaction():
        _migrations()[BACKPRESSURE].upgrade(db)
    assert _state(db) == before


def test_baseline_upgrade_runtime_and_downgrade_without_superuser(cold_corpus, db):
    if not db.execute(
        "SELECT rolcreaterole FROM pg_roles WHERE rolname=current_user"
    ).fetchone()[0]:
        pytest.skip("requires CREATEROLE to provision the temporary runtime role")
    project, _stream, _topics, _user, _messages = cold_corpus(fixed=False)
    shape_sql = (
        "SELECT proargnames,proargtypes::text,prorettype::regtype::text,"
        "prosecdef,provolatile,proparallel,proisstrict,proconfig FROM pg_proc "
        "WHERE oid='workspace_v3.advance_unread_baseline(integer)'::regprocedure"
    )
    original_shape = db.execute(shape_sql).fetchone()
    role = "cassi_counter_runtime_" + sys_uuid.uuid4().hex
    role_id = psycopg.sql.Identifier(role)
    owner = psycopg.sql.Identifier(db.execute("SELECT current_user").fetchone()[0])
    db.execute(
        psycopg.sql.SQL(
            "CREATE ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOLOGIN"
        ).format(role_id)
    )
    try:
        db.execute(
            psycopg.sql.SQL("GRANT USAGE,CREATE ON SCHEMA workspace_v3 TO {}").format(
                role_id
            )
        )
        db.execute(
            psycopg.sql.SQL(
                "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA workspace_v3 TO {}"
            ).format(role_id)
        )
        db.execute(
            psycopg.sql.SQL(
                "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA workspace_v3 TO {}"
            ).format(role_id)
        )
        for function in (
            "advance_unread_baseline(integer)",
            "enqueue_exact_counter_snapshot()",
        ):
            db.execute(
                psycopg.sql.SQL(
                    "ALTER FUNCTION workspace_v3." + function + " OWNER TO {}"
                ).format(role_id)
            )
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as runtime:
            runtime.execute(psycopg.sql.SQL("SET ROLE {}").format(role_id))
            assert not runtime.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone()[0]
            # Reproduce the rejected mechanism on a fresh non-superuser session.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with runtime.transaction():
                    runtime.execute(
                        "ALTER FUNCTION workspace_v3.advance_unread_baseline(integer) "
                        "SET workspace_v3.suppress_counter_snapshots='on'"
                    )
            before = _state(runtime)
            with runtime.transaction():
                _migrations()[BACKPRESSURE].upgrade(runtime)
            assert _state(runtime) == before
            assert runtime.execute(shape_sql).fetchone() == original_shape
        # A separate fresh session must execute the function without first
        # initializing custom GUC placeholders as an administrative connection.
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as runtime:
            runtime.execute(psycopg.sql.SQL("SET ROLE {}").format(role_id))
            assert (
                runtime.execute(
                    "SELECT workspace_v3.advance_unread_baseline(7)"
                ).fetchone()[0]
                == 7
            )
            # PostgreSQL keeps an empty placeholder after resetting a newly
            # introduced custom setting; NULL restores its effective unset state.
            assert _settings(runtime) == ("", "")
            assert (
                runtime.execute(
                    "SELECT count(*) FROM workspace_v3.projection_tasks "
                    "WHERE project_id=%s AND status='pending'",
                    (project,),
                ).fetchone()[0]
                == 0
            )
            with runtime.transaction():
                _caller_settings(runtime)
                with pytest.raises(psycopg.errors.InvalidRowCountInLimitClause):
                    with runtime.transaction():
                        runtime.execute(
                            "SELECT workspace_v3.advance_unread_baseline(-1)"
                        )
                assert _settings(runtime) == ("off", "off")
            _finish(runtime)
            counters._oracle(runtime, project)
            completed = _state(runtime)
            with runtime.transaction():
                _migrations()[BACKPRESSURE].downgrade(runtime)
            assert _state(runtime) == completed
            assert (
                runtime.execute(
                    "SELECT to_regprocedure('workspace_v3.advance_unread_baseline_page(integer)')"
                ).fetchone()[0]
                is None
            )
            assert runtime.execute(
                "SELECT proconfig FROM pg_proc "
                "WHERE oid='workspace_v3.advance_unread_baseline(integer)'::regprocedure"
            ).fetchone()[0] == ["enable_sort=off", "jit=off"]
            with runtime.transaction():
                _migrations()[BACKPRESSURE].upgrade(runtime)
            assert _state(runtime) == completed
    finally:
        # Restore object ownership before removing this test-only role's grants.
        db.execute(psycopg.sql.SQL("REASSIGN OWNED BY {} TO {}").format(role_id, owner))
        db.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(role_id))
        db.execute(psycopg.sql.SQL("DROP ROLE {}").format(role_id))


def test_capture_commits_while_projection_batch_waits_for_its_lease(
    cold_corpus, db, record_property
):
    project, stream, topics, user, _messages = cold_corpus(
        topic_count=24, messages_per_topic=500
    )
    # Keep actual live work in the queue. Capture must neither wait for its
    # completion nor add historical snapshot work before ready.
    counters._messages(db, project, stream, topics[0], user, 3)
    tasks = support.projections.claim_projection_tasks(db, "cassi-blocked-projection")
    assert tasks
    task_ids = [task["uuid"] for task in tasks]
    entered = threading.Event()
    worker_pid = []

    def project_batch():
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as worker:
            worker_pid.append(worker.info.backend_pid)
            entered.set()
            with worker.transaction():
                worker.execute("SET LOCAL statement_timeout='15s'")
                return support.projections.process_claimed_projection_tasks(
                    worker, "cassi-blocked-projection", tasks
                )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as blocker:
            with blocker.transaction():
                blocker.execute(
                    "SELECT uuid FROM workspace_v3.projection_tasks "
                    "WHERE uuid=ANY(%s) FOR UPDATE",
                    (task_ids,),
                )
                projection = pool.submit(project_batch)
                assert entered.wait(timeout=3)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    waiting = db.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (worker_pid[0],),
                    ).fetchone()
                    if waiting == ("Lock",):
                        break
                    time.sleep(0.01)
                else:
                    raise AssertionError("Projection did not reach the lease lock")
                queue_before = db.execute(
                    "SELECT uuid FROM workspace_v3.projection_tasks ORDER BY uuid"
                ).fetchall()
                started = time.monotonic()
                baseline = agents.WorkspaceV3BaselineAgent()
                for _ in range(6):
                    baseline._iteration()
                assert not projection.done()
                cursor = _state(db)[2]
                assert not _state(db)[0]
                # A replacement worker resumes already committed pages.
                baseline = agents.WorkspaceV3BaselineAgent()
                for _ in range(20):
                    baseline._iteration()
                    if _state(db)[0]:
                        break
                elapsed = time.monotonic() - started
                assert _state(db)[0]
                assert not _state(db)[1]
                assert _state(db)[2] > cursor
                assert not projection.done()
                assert (
                    db.execute(
                        "SELECT uuid FROM workspace_v3.projection_tasks ORDER BY uuid"
                    ).fetchall()
                    == queue_before
                )
                record_property("capture_flags", 12003)
                record_property("capture_seconds_with_blocked_projection", elapsed)
                record_property("capture_flags_per_second", 12003 / elapsed)
                counters._oracle(db, project)
        metrics = projection.result(timeout=15)
        assert metrics["failed"] == 0
        assert metrics["completed"] == len(tasks)
    # Capture stops at ready; the projection workers own the snapshot sweep.
    before = _state(db)
    baseline._iteration()
    assert _state(db) == before
    worker = _worker()
    for _ in range(10):
        worker._iteration()
        if _state(db)[1]:
            break
    assert _state(db)[:2] == (True, True)
    support._drain(db)
    counters._oracle(db, project)
