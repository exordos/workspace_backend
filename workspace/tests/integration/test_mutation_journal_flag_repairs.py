# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import importlib.util

import pytest
from oslo_config import cfg
from restalchemy.common import contexts

from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import models
from workspace.common import mutation_journal_opts
from workspace.tests.integration import conftest
from workspace.tests.integration import test_mutation_journal as journal_tests
from workspace.tests.integration import test_mutation_journal_provider as provider_tests
from workspace.tests.integration import test_zulip_flags as flag_tests


message_sources = flag_tests.message_sources
imported = flag_tests.imported
journal = provider_tests.journal


def full_repairs(db, project_id):
    return db.execute(
        "SELECT (SELECT count(*) FROM bridge_zulip_v5_message_changes WHERE project_id=%s)"
        "+(SELECT count(*) FROM messenger_message_changes WHERE project_id=%s)",
        (project_id, project_id),
    ).fetchone()[0]


@pytest.mark.parametrize("flag", flags.FLAGS)
def test_owned_flag_observation_keeps_flag_journal_without_full_message_repairs(
    db, imported, journal, flag
):
    account, source, state, *_rest = imported
    with contexts.Context().session_manager() as session:
        before = session.execute(
            "SELECT current_setting('workspace.owned_mutations',true) AS value"
        ).fetchone()["value"]
        flags.record(
            account,
            [source.message_id],
            flag,
            False,
            datetime.datetime.now(datetime.timezone.utc),
        )
        assert session.execute(
            "SELECT current_setting('workspace.owned_mutations',true) AS value"
        ).fetchone()["value"] in {before, ""}
    assert (
        db.execute(
            "SELECT count(*) FROM bridge_zulip_v5_flag_changes WHERE project_id=%s AND state_uuid=%s",
            (account.project_id, state.uuid),
        ).fetchone()[0]
        == 1
    )
    assert full_repairs(db, account.project_id) == 0
    journal_tests.drain(journal, db)
    current = flag_tests.reload(state)
    assert (current.read_at is not None if flag == "read" else current.starred) is False


@pytest.mark.parametrize("flag,action", [("read", "read"), ("starred", "star")])
def test_owned_user_flag_mutation_does_not_run_full_message_repairs(
    api, db, imported, journal, monkeypatch, flag, action
):
    account, source, state, *_rest = imported
    with contexts.Context().session_manager():
        flags.record(
            account,
            [source.message_id],
            flag,
            False,
            datetime.datetime.now(datetime.timezone.utc),
        )
    journal_tests.drain(journal, db)
    calls = []
    reconcile = models.ZulipMessageState.reconcile_batch

    def counted(*args, **kwargs):
        calls.append(1)
        return reconcile(*args, **kwargs)

    monkeypatch.setattr(models.ZulipMessageState, "reconcile_batch", counted)
    api.project_id, api.user_uuid = (
        str(account.project_id),
        str(account.owner_user_uuid),
    )
    response = api.post(
        f"/v1/messenger/messages/{state.placement_uuid}/actions/{action}/invoke"
    )
    assert response.status_code == 200, response.text
    journal_tests.drain(journal, db)
    current = flag_tests.reload(state)
    assert (current.read_at is not None if flag == "read" else current.starred) is True
    assert calls == []


def test_flag_optimisation_preserves_legacy_and_content_repairs(db, imported, journal):
    account, source, _state, *_rest = imported
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    with contexts.Context().session_manager():
        flags.record(
            account,
            [source.message_id],
            "read",
            False,
            datetime.datetime.now(datetime.timezone.utc),
        )
    assert full_repairs(db, account.project_id) > 0
    cfg.CONF.set_override("enabled", True, group=mutation_journal_opts.DOMAIN)
    journal_tests.drain(journal, db)
    with contexts.Context().session_manager() as session:
        session.execute("SELECT set_config('workspace.owned_mutations','on',true)")
        source.content.workspace_content = "An actual content change"
        source.content.update()
    assert full_repairs(db, account.project_id) > 0
    journal_tests.drain(journal, db)


@pytest.mark.parametrize("target", ["source", "state"])
def test_owned_structural_changes_still_schedule_full_message_repair(
    db, imported, journal, target
):
    account, source, state, *_rest = imported
    with contexts.Context().session_manager() as session:
        session.execute("SELECT set_config('workspace.owned_mutations','on',true)")
        model = source if target == "source" else state
        model.mentioned = not model.mentioned
        model.update()
    assert full_repairs(db, account.project_id) > 0
    journal_tests.drain(journal, db)


def test_owned_flag_observation_preserves_an_existing_content_repair(
    db, imported, journal
):
    account, source, state, *_rest = imported
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
        (account.project_id, state.placement_uuid),
    ).fetchone()[0]
    revision = db.execute(
        "INSERT INTO bridge_zulip_v5_message_changes(project_id,message_uuid) VALUES (%s,%s) RETURNING revision",
        (account.project_id, canonical),
    ).fetchone()[0]
    with contexts.Context().session_manager():
        flags.record(
            account,
            [source.message_id],
            "read",
            False,
            datetime.datetime.now(datetime.timezone.utc),
        )
    assert (
        db.execute(
            "SELECT revision FROM bridge_zulip_v5_message_changes WHERE project_id=%s AND message_uuid=%s",
            (account.project_id, canonical),
        ).fetchone()[0]
        == revision
    )
    journal_tests.drain(journal, db)
    assert flag_tests.reload(state).read_at is None


def test_flag_repair_migration_rollback_and_reapply_preserve_pending_work(
    db, imported, journal
):
    account, source, _state, *_rest = imported
    path = (
        conftest.MIGRATIONS_DIR
        / "0256-Avoid-full-message-repairs-for-owned-personal-flag-updates-1ca744.py"
    )
    spec = importlib.util.spec_from_file_location("owned_flag_repairs_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with contexts.Context().session_manager() as session:
        module.migration_step.downgrade(session)
        flags.record(
            account,
            [source.message_id],
            "read",
            False,
            datetime.datetime.now(datetime.timezone.utc),
        )
        pending = session.execute(
            "SELECT message_uuid,revision FROM bridge_zulip_v5_message_changes WHERE project_id=%s",
            (account.project_id,),
        ).fetchall()
        assert pending
        module.migration_step.upgrade(session)
        flags.record(
            account,
            [source.message_id],
            "read",
            True,
            datetime.datetime.now(datetime.timezone.utc),
        )
        assert (
            session.execute(
                "SELECT message_uuid,revision FROM bridge_zulip_v5_message_changes WHERE project_id=%s",
                (account.project_id,),
            ).fetchall()
            == pending
        )
    journal_tests.drain(journal, db)


def test_owned_snapshot_batch_and_global_read_keep_their_flag_semantics(
    db, imported, journal
):
    account, source, state, *_rest = imported
    at = datetime.datetime.now(datetime.timezone.utc)
    with contexts.Context().session_manager():
        observed = flags.observe_snapshot(
            account, [{"id": source.message_id, "flags": []}], at
        )
    assert observed[source.message_id] == {"read": False, "starred": False}
    journal_tests.drain(journal, db)
    current = flag_tests.reload(state)
    assert current.read_at is None and current.starred is False
    with contexts.Context().session_manager():
        flags.record(account, [0], "read", True, at + datetime.timedelta(seconds=1))
    journal_tests.drain(journal, db)
    current = flag_tests.reload(state)
    assert current.read_at is not None and current.starred is False
