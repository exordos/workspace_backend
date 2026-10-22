# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import statistics
import concurrent.futures
import uuid

import psycopg
import pytest
from oslo_config import cfg
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import sessions

from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import models
from workspace.common import mutation_journal_opts
from workspace.services import history_import
from workspace.services.messenger_workers import mutation_runtime
from workspace.services.messenger_workers import keyed_dispatcher
from workspace.services.messenger_workers import owned_projection
from workspace.tests.integration import conftest
from workspace.tests.integration import test_mutation_journal as journal_tests
from workspace.tests.integration import test_zulip_flags as flag_tests
from workspace.tests.integration import test_zulip_reactions as reaction_tests


message_sources = flag_tests.message_sources
imported = flag_tests.imported


@pytest.fixture
def journal(imported, db):
    cfg.CONF.set_override("enabled", True, group=mutation_journal_opts.DOMAIN)
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        yield runtime
    finally:
        runtime.close()
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)


def test_incoming_and_outgoing_read_share_topic_owner_and_http_has_no_session(
    api, db, imported, journal
):
    account, source, state, _subscription, zulip, _constructor = imported
    at = datetime.datetime.now(datetime.timezone.utc)
    with contexts.Context().session_manager():
        flags.record(account, [source.message_id], "read", False, at)
        assert flags.FlagChange.reconcile_batch(100) == 0
    assert flag_tests.reload(state).read_at is not None
    journal_tests.drain(journal, db)
    assert flag_tests.reload(state).read_at is None
    api.project_id, api.user_uuid = (
        str(account.project_id),
        str(account.owner_user_uuid),
    )
    response = api.post(
        f"/v1/messenger/messages/{state.placement_uuid}/actions/read/invoke"
    )
    assert response.status_code == 200, response.text
    assert flag_tests.reload(state).read_at is None
    journal_tests.drain(journal, db)
    assert flag_tests.reload(state).read_at is not None
    pending = flag_tests.pending(account)
    assert len(pending) == 1
    task = pending[0]
    owner = uuid.uuid4()
    db.execute(
        "UPDATE bridge_zulip_v5_tasks SET lease_owner=%s,lease_expires_at=NOW()+interval '1 minute' WHERE uuid=%s",
        (owner, task.uuid),
    )
    called = []

    def send(*args):
        with pytest.raises(sessions.SessionNotFound):
            contexts.Context().get_session()
        called.append(args)
        return {"result": "success", "messages": [source.message_id]}

    zulip.update_message_flags.side_effect = send
    service = history_import.HistoryImportService(queue="realtime")
    service._run_task(task.uuid, owner)
    assert called == [([source.message_id], "read", True)]
    journal_tests.drain(journal, db)
    assert flag_tests.pending(account) == []
    assert flag_tests.reload(state).read_at is not None


def test_provider_reconciliation_uses_dispatcher_without_explicit_data_locks(
    db, imported, journal, monkeypatch
):
    account, source, state, _subscription, _zulip, _constructor = imported
    queries = []
    original = sessions.PgSQLSession.execute

    def capture(session, statement, values=None):
        queries.append(statement)
        return original(session, statement, values)

    with contexts.Context().session_manager():
        flags.record(
            account,
            [source.message_id],
            "starred",
            False,
            datetime.datetime.now(datetime.timezone.utc),
        )
    monkeypatch.setattr(sessions.PgSQLSession, "execute", capture)
    journal_tests.drain(journal, db)
    assert flag_tests.reload(state).starred is False

    explicit = [
        q
        for q in queries
        if "FOR UPDATE" in q.upper()
        or "PG_ADVISORY" in q.upper()
        or "PG_TRY_ADVISORY" in q.upper()
    ]
    assert explicit == []


def test_owned_provider_timestamp_update_preserves_utc_in_non_utc_session(
    db, imported, journal
):
    account, _source, state, *_rest = imported
    before = db.execute(
        "SELECT read_updated_at,starred_updated_at FROM messenger_user_message_states WHERE project_id=%s AND uuid=%s",
        (account.project_id, state.uuid),
    ).fetchone()
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
        (account.project_id, state.placement_uuid),
    ).fetchone()[0]
    db.execute(
        "UPDATE messenger_user_message_states SET zulip_flags_updated_at=NULL WHERE project_id=%s AND uuid=%s",
        (account.project_id, state.uuid),
    )
    with contexts.Context().session_manager() as session:
        session.execute("SET LOCAL timezone='Europe/Moscow'", ())
        with owned_projection.capture():
            projection = models.ZulipUserMessageState.objects.get_one(
                filters={
                    "project_id": filters.EQ(account.project_id),
                    "message_uuid": filters.EQ(canonical),
                    "user_uuid": filters.EQ(account.owner_user_uuid),
                }
            )
            projection.do()
    after = db.execute(
        "SELECT read_updated_at,starred_updated_at FROM messenger_user_message_states WHERE project_id=%s AND uuid=%s",
        (account.project_id, state.uuid),
    ).fetchone()
    assert after == before


def test_outgoing_reaction_http_is_outside_transaction_and_observation_converges(
    api, db, imported, journal
):
    account, source, state, _subscription, zulip, _constructor = imported
    api.project_id, api.user_uuid = (
        str(account.project_id),
        str(account.owner_user_uuid),
    )
    response = api.post(
        "/v1/messenger/message_reactions/",
        json={"message_uuid": str(state.placement_uuid), "emoji_name": "thumbs_up"},
    )
    assert response.status_code == 201, response.text
    journal_tests.drain(journal, db)
    pending = reaction_tests.pending(account)
    assert len(pending) == 1
    task = pending[0]
    owner = uuid.uuid4()
    db.execute(
        "UPDATE bridge_zulip_v5_tasks SET lease_owner=%s,lease_expires_at=NOW()+interval '1 minute' WHERE uuid=%s",
        (owner, task.uuid),
    )
    called = []

    def send(*args):
        with pytest.raises(sessions.SessionNotFound):
            contexts.Context().get_session()
        called.append(args)
        return {"result": "success"}

    zulip.update_reaction.side_effect = send
    history_import.HistoryImportService(queue="realtime")._run_task(task.uuid, owner)
    assert (
        len(called) == 1 and called[0][0] == source.message_id and called[0][-1] is True
    )
    journal_tests.drain(journal, db)
    assert reaction_tests.pending(account) == []
    facts = reaction_tests.facts(account)
    assert len(facts) == 1 and facts[0].user_uuid == account.owner_user_uuid


def test_owned_flag_update_does_not_upgrade_lock_through_immutable_key_columns(
    db, imported, journal
):
    account, source, state, *_rest = imported
    with contexts.Context().session_manager():
        flags.record(
            account,
            [source.message_id],
            "starred",
            False,
            datetime.datetime.now(datetime.timezone.utc),
        )
    topic = db.execute(
        "SELECT topic_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
        (account.project_id, state.placement_uuid),
    ).fetchone()[0]
    finished_while_fk_check_held = False
    with psycopg.connect(conftest.TEST_DB_URL) as blocker:
        blocker.execute(
            "SELECT uuid FROM messenger_user_message_states WHERE project_id=%s AND uuid=%s FOR KEY SHARE",
            (account.project_id, state.uuid),
        )
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(
                journal._execute,
                keyed_dispatcher.Work(
                    ("provider_flags", account.project_id, topic),
                    {state.uuid},
                    frozenset(
                        journal._topic_reservations(
                            account.project_id, [{"topic_uuid": topic}]
                        )
                    ),
                ),
            )
            try:
                future.result(timeout=1)
                finished_while_fk_check_held = True
            except concurrent.futures.TimeoutError:
                pass
            finally:
                blocker.rollback()
                future.result(timeout=3)
    assert finished_while_fk_check_held
    assert flag_tests.reload(state).starred is False


def test_late_provider_observation_cannot_resurrect_an_applied_delete(
    api, db, imported, journal
):
    account, source, state, *_rest = imported
    api.project_id, api.user_uuid = (
        str(account.project_id),
        str(account.owner_user_uuid),
    )
    target = str(state.placement_uuid)
    deleted = api.delete("/v1/messenger/messages/" + target)
    assert deleted.status_code == 204, deleted.text
    journal_tests.drain(journal, db)
    assert api.get("/v1/messenger/messages/" + target).status_code == 404
    source.content.workspace_content = "Late provider observation"
    source.content.workspace_content_updated_at += datetime.timedelta(days=1)
    source.content.update()
    source.update()
    journal_tests.drain(journal, db)
    assert api.get("/v1/messenger/messages/" + target).status_code == 404


def test_flag_probe_preserves_rows_without_join_search_explosion(
    db, imported, monkeypatch
):
    account, source, state, *_rest = imported
    with contexts.Context().session_manager() as session:
        flags.record(
            account,
            [source.message_id],
            "read",
            False,
            datetime.datetime.now(datetime.timezone.utc),
        )
        original = sessions.PgSQLSession.execute
        captured = []

        def measure(current, statement, *args, **kwargs):
            if statement.lstrip().startswith("SELECT * FROM bridge_zulip_v5_"):
                plan = original(
                    current,
                    "EXPLAIN (ANALYZE,FORMAT JSON,TIMING OFF) " + statement,
                    *args,
                    **kwargs,
                ).fetchone()["QUERY PLAN"][0]
                captured.append(plan["Planning Time"] + plan["Execution Time"])
            return original(current, statement, *args, **kwargs)

        monkeypatch.setattr(sessions.PgSQLSession, "execute", measure)
        old = []
        sql = "SELECT * FROM bridge_zulip_v5_flag_states WHERE project_id=%s AND state_uuid=%s AND (%s::uuid IS NULL OR external_account_uuid=%s) AND (%s::text IS NULL OR flag=%s) ORDER BY external_account_uuid,flag"
        params = (account.project_id, state.uuid, None, None, None, None)
        setting = session.execute("SHOW join_collapse_limit").fetchone()[
            "join_collapse_limit"
        ]
        for _ in range(3):
            plan = original(
                session, "EXPLAIN (ANALYZE,FORMAT JSON,TIMING OFF) " + sql, params
            ).fetchone()["QUERY PLAN"][0]
            old.append(plan["Planning Time"] + plan["Execution Time"])
            expected = original(session, sql, params).fetchall()
            assert expected
            assert flags.flag_states(account.project_id, state.uuid) == expected
        assert (
            session.execute("SHOW join_collapse_limit").fetchone()[
                "join_collapse_limit"
            ]
            == setting
        )
        assert statistics.median(captured) < statistics.median(old) / 2
