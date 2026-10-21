# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

from unittest import mock
import uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models
from workspace.messenger_api.api import v2_store
from workspace.messenger_api.dm import external_models
from workspace.tests.integration import conftest


@pytest.fixture
def project_user_accounts(_database, db):
    accounts = []

    def create(**values):
        owner_uuid = values.pop("owner_user_uuid", uuid.uuid4())
        conftest.seed_workspace_user(db, owner_uuid, f"project-user-{owner_uuid}")
        account = external_models.ExternalAccount(
            owner_user_uuid=owner_uuid,
            **{
                "project_id": uuid.uuid4(),
                "provider": "zulip",
                "settings": {"server_url": "https://zulip.example.com"},
                **values,
            },
        )
        account.insert()
        accounts.append(account)
        return account

    yield create
    for account in accounts:
        account.delete()
    for task in models.Task.objects.get_all(filters={"project_id": filters.Is(None)}):
        task.delete()


def _states(account):
    return models.ZulipProjectUserState.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)},
        order_by={"user_uuid": "asc"},
    )


def _run(task):
    with contexts.Context().session_manager():
        task.execute()
    return models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})


@pytest.mark.parametrize(
    "provider,registered,disabled,missing",
    [
        ("zulip", False, False, True),
        ("zulip", False, True, True),
        ("zulip", True, False, False),
        ("other", False, False, False),
    ],
)
def test_project_user_states_show_missing_zulip_account_owners(
    project_user_accounts, db, provider, registered, disabled, missing
):
    account = project_user_accounts(provider=provider, disabled=disabled)
    if registered:
        db.execute(
            "INSERT INTO messenger_project_users (project_id, user_uuid) VALUES (%s, %s)",
            (account.project_id, account.owner_user_uuid),
        )

    states = _states(account)

    if missing:
        (state,) = states
        assert state.as_plain_dict() == dict(
            project_id=account.project_id, user_uuid=account.owner_user_uuid
        )
        assert state.get_id() == (account.project_id, account.owner_user_uuid)
    else:
        assert states == []
    assert set(models.ZulipProjectUserState.get_id_property()) == {
        "project_id",
        "user_uuid",
    }


def test_project_user_states_keep_projects_separate(project_user_accounts, db):
    first = project_user_accounts()
    second = project_user_accounts(owner_user_uuid=first.owner_user_uuid)
    db.execute(
        "INSERT INTO messenger_project_users (project_id, user_uuid) VALUES (%s, %s)",
        (first.project_id, first.owner_user_uuid),
    )

    assert _states(first) == []
    (state,) = _states(second)
    with contexts.Context().session_manager():
        state.do()
        state.do()

    assert _states(first) == _states(second) == []
    assert db.execute(
        "SELECT count(*) FROM messenger_project_users WHERE user_uuid = %s",
        (first.owner_user_uuid,),
    ).fetchone() == (2,)
    second.delete()
    assert db.execute(
        "SELECT count(*) FROM messenger_project_users "
        "WHERE project_id = %s AND user_uuid = %s",
        (second.project_id, second.owner_user_uuid),
    ).fetchone() == (1,)


def test_project_user_state_registers_system_folders(project_user_accounts, db):
    account = project_user_accounts()
    (state,) = _states(account)
    with contexts.Context().session_manager():
        state.do()

    assert _states(account) == []
    assert db.execute(
        "SELECT folder.title, binding.rule "
        "FROM messenger_user_folder_bindings AS binding "
        "JOIN messenger_folders AS folder "
        "ON folder.project_id = binding.project_id AND folder.uuid = binding.folder_uuid "
        "WHERE binding.project_id = %s AND binding.user_uuid = %s ORDER BY folder.uuid",
        (account.project_id, account.owner_user_uuid),
    ).fetchall() == [
        ("All chats", "all_chats"),
        ("Personal", "personal"),
        ("Channels", "channels"),
    ]
    assert db.execute(
        "SELECT count(*) FROM messenger_stream_bindings WHERE project_id = %s",
        (account.project_id,),
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    "table",
    ["messenger_project_users", "messenger_folders", "messenger_user_folder_bindings"],
)
def test_repeated_registration_does_not_wait_for_import_writes(
    project_user_accounts, db, table
):
    account = project_user_accounts()
    db.execute(
        "SELECT messenger_v2_register_project_user(%s, %s)",
        (account.project_id, account.owner_user_uuid),
    )
    before = db.execute(
        "SELECT updated_at FROM messenger_project_users "
        "WHERE project_id = %s AND user_uuid = %s",
        (account.project_id, account.owner_user_uuid),
    ).fetchone()
    store = v2_store.MessengerV2Store(account.project_id, account.owner_user_uuid)
    with psycopg.connect(conftest.TEST_DB_URL) as importer:
        importer.execute(
            f"UPDATE {table} SET updated_at = NOW() WHERE project_id = %s",
            (account.project_id,),
        )
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL lock_timeout = '100ms'")
            store._ensure_project_user(account.owner_user_uuid)
        importer.rollback()
    assert (
        db.execute(
            "SELECT updated_at FROM messenger_project_users "
            "WHERE project_id = %s AND user_uuid = %s",
            (account.project_id, account.owner_user_uuid),
        ).fetchone()
        == before
    )


def test_repeated_registration_repairs_missing_system_folders(
    project_user_accounts, db
):
    account = project_user_accounts()
    store = v2_store.MessengerV2Store(account.project_id, account.owner_user_uuid)
    with contexts.Context().session_manager():
        store._ensure_project_user(account.owner_user_uuid)
    db.execute(
        "DELETE FROM messenger_folders WHERE project_id = %s "
        "AND uuid = '00000000-0000-0000-0000-000000000001'",
        (account.project_id,),
    )
    with contexts.Context().session_manager():
        store._ensure_project_user(account.owner_user_uuid)
    assert db.execute(
        "SELECT count(*) FROM messenger_user_folder_bindings "
        "WHERE project_id = %s AND user_uuid = %s",
        (account.project_id, account.owner_user_uuid),
    ).fetchone() == (3,)


@pytest.mark.pre_retirement_schema
def test_contention_migration_bounds_existing_default_batches_and_is_reversible(
    project_user_accounts, db
):
    project_user_accounts()
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = (
        "0230-Avoid-redundant-project-user-writes-and-bound-Zulip-"
        "reconciliation-batches-fc01aa.py"
    )
    engine.rollback_migration(migration)
    try:
        tasks = [
            models.Task(content=models.ReconciliationContent(batch_size=batch_size))
            for batch_size in (1000, 17)
        ]
        for task in tasks:
            task.insert()
        assert (
            "DO UPDATE SET updated_at = NOW()"
            in db.execute(
                "SELECT pg_get_functiondef('messenger_v2_register_project_user(uuid,uuid)'::regprocedure)"
            ).fetchone()[0]
        )
        engine.apply_migration(migration)
        assert [
            models.Task.objects.get_one(
                filters={"uuid": filters.EQ(task.uuid)}
            ).content.batch_size
            for task in tasks
        ] == [16, 17]
        assert (
            "DO UPDATE SET updated_at = NOW()"
            not in db.execute(
                "SELECT pg_get_functiondef('messenger_v2_register_project_user(uuid,uuid)'::regprocedure)"
            ).fetchone()[0]
        )
    finally:
        engine.apply_migration(engine.get_latest_migration())


def test_reconciliation_batches_project_users_before_other_stages(
    project_user_accounts, db, monkeypatch
):
    first = project_user_accounts()
    project_user_accounts(project_id=first.project_id)
    task = models.Task(content=models.ReconciliationContent(batch_size=1))
    task.insert()

    def unexpected_stage(*args, **kwargs):
        pytest.fail("Project users must exhaust the batch before other stages")

    with monkeypatch.context() as patched:
        for model in (
            models.ZulipStreamState,
            models.ZulipStreamBindingState,
            models.ZulipTopicState,
            models.ZulipMessageState,
            models.ZulipMessageChange,
        ):
            patched.setattr(model, "objects", mock.Mock(get_all=unexpected_stage))
        for remaining in (1, 0):
            assert _run(task).status == "done"
            assert len(_states(first)) == remaining
            (task,) = models.Task.objects.get_all(
                filters={"project_id": filters.Is(None), "status": filters.EQ("new")}
            )
            assert task.not_before_at is None
    assert _run(task).status == "done"
    (waiting,) = models.Task.objects.get_all(
        filters={"project_id": filters.Is(None), "status": filters.EQ("waiting")}
    )
    assert waiting.not_before_at is not None


def test_reconciliation_rolls_back_project_users_with_later_stages(
    project_user_accounts, db, monkeypatch
):
    account = project_user_accounts()
    models.ZulipChannel(
        external_account_uuid=account.uuid,
        stream_uuid=uuid.uuid4(),
        stream_id=42,
        name="General",
        description="",
        rendered_description="",
        invite_only=False,
        is_web_public=False,
        history_public_to_subscribers=True,
        is_archived=False,
    ).insert()
    task = models.Task(content=models.ReconciliationContent())
    task.insert()

    def fail_stream(state):
        raise RuntimeError("Stream reconciliation failed")

    monkeypatch.setattr(models.ZulipStreamState, "do", fail_stream)

    assert _run(task).status == "retry"
    assert len(_states(account)) == 1
    assert db.execute(
        "SELECT count(*) FROM messenger_project_users WHERE project_id = %s",
        (account.project_id,),
    ).fetchone() == (0,)
    assert db.execute(
        "SELECT count(*) FROM messenger_user_folder_bindings WHERE project_id = %s",
        (account.project_id,),
    ).fetchone() == (0,)


@pytest.mark.pre_retirement_schema
def test_project_user_state_view_migration_is_reversible(project_user_accounts, db):
    account = project_user_accounts()
    (state,) = _states(account)
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0218-Add-Zulip-project-user-reconciliation-view-e80455.py"
    engine.rollback_migration(migration)
    try:
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_project_user_states')"
        ).fetchone() == (None,)
    finally:
        engine.apply_migration(engine.get_latest_migration())
    (restored,) = _states(account)
    assert restored.as_plain_dict() == state.as_plain_dict()
