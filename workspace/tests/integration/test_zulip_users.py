# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime
from unittest import mock
import uuid

import psycopg
import pytest
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import models as messenger_models
from workspace.services import history_import
from workspace.tests.integration import conftest


@pytest.fixture
def user_import_account(_database, db):
    owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"user-import-{owner_uuid}")
    host = f"{uuid.uuid4().hex}.zulip.example.com"
    account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=owner_uuid,
        provider="zulip",
        external_account_id=f"{host}:7",
        settings={
            "server_url": f"https://{host}",
            "email": "owner@example.com",
            "api_key": "owner-key",
        },
    )
    account.insert()
    yield account
    for stored in _accounts(account):
        stored.delete()


def _user_data(user_id=8, **values):
    return {
        "user_id": user_id,
        "full_name": f"User {user_id}",
        "email": f"user-{user_id}@example.com",
        "is_active": True,
        **values,
    }


def _accounts(account):
    return external_models.ExternalAccount.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)},
        order_by={"external_account_id": "asc"},
    )


def _run_user_import(account):
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        priority=models.TASK_PRIORITY_IMPORT_USERS,
        content=models.ImportUsersContent(),
    )
    task.insert()
    with contexts.Context().session_manager():
        restored = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
        assert isinstance(restored.content, models.ImportUsersContent)
        restored.execute()
    return models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})


@pytest.mark.parametrize("operation", ("create", "update"))
def test_provider_identity_cannot_be_supplied_through_the_public_api(api, operation):
    path = "/v1/messenger/external_accounts/"
    spec = {"provider": "example", "settings": {}}
    if operation == "create":
        response = api.post(
            path,
            permissions=("workspace.external_account.create",),
            json={**spec, "external_account_id": "zulip.example.com:7"},
        )
    else:
        created = api.post(
            path,
            permissions=("workspace.external_account.create",),
            json=spec,
        )
        assert created.status_code == 201, created.text
        assert created.json()["disabled"] is False
        response = api.put(
            f"{path}{created.json()['uuid']}",
            permissions=("workspace.external_account.update",),
            json={"external_account_id": "zulip.example.com:7"},
        )
    assert response.status_code == 403, response.text


def test_user_import_creates_linked_profiles_without_credentials_and_updates_them(
    user_import_account, monkeypatch
):
    account = user_import_account
    zulip = mock.Mock()
    zulip.get_users.return_value = [
        _user_data(7, email="hidden-owner@example.com"),
        _user_data(),
        _user_data(9, is_bot=True, is_active=False),
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)

    before = datetime.datetime.now(datetime.timezone.utc)
    task = _run_user_import(account)
    after = datetime.datetime.now(datetime.timezone.utc)
    assert task.status == "done"
    accounts = _accounts(account)
    assert len(accounts) == 3
    owner, *imported = accounts
    assert owner.uuid == account.uuid
    assert owner.owner_user_uuid == account.owner_user_uuid
    assert owner.settings == account.settings
    assert owner.external_account_id == f"{account.get_host()}:7"
    assert owner.disabled is False
    for external_account, user_id in zip(imported, (8, 9)):
        assert external_account.external_account_id == f"{account.get_host()}:{user_id}"
        assert external_account.settings == {
            "server_url": account.settings["server_url"],
            "email": f"user-{user_id}@example.com",
        }
        assert external_account.status == "new"
        assert external_account.disabled is (user_id == 9)
        profile = messenger_models.WorkspaceUser.objects.get_one(
            filters={"uuid": filters.EQ(external_account.owner_user_uuid)}
        )
        assert profile.first_name == f"User {user_id}"
        assert profile.email == f"user-{user_id}@example.com"
        assert profile.source == "zulip"
        assert profile.disabled is (user_id == 9)
        assert profile.status == "offline"

    pending = models.Task.objects.get_all(
        filters={
            "project_id": filters.EQ(account.project_id),
            "status": filters.EQ("waiting"),
        }
    )
    assert len(pending) == 1
    assert isinstance(pending[0].content, models.ImportUsersContent)
    assert pending[0].external_account_uuid == account.uuid
    assert pending[0].priority == models.TASK_PRIORITY_IMPORT_USERS
    assert (
        before + datetime.timedelta(minutes=15)
        <= pending[0].not_before_at
        <= after + datetime.timedelta(minutes=15)
    )
    account_timestamps = {row.uuid: row.updated_at for row in _accounts(account)}
    user_filter = {"uuid": filters.In([row.owner_user_uuid for row in accounts])}
    user_timestamps = {
        row.uuid: row.updated_at
        for row in messenger_models.WorkspaceUser.objects.get_all(filters=user_filter)
    }
    assert _run_user_import(account).status == "done"
    assert {
        row.uuid: row.updated_at for row in _accounts(account)
    } == account_timestamps
    assert {
        row.uuid: row.updated_at
        for row in messenger_models.WorkspaceUser.objects.get_all(filters=user_filter)
    } == user_timestamps

    zulip.get_users.return_value = [
        _user_data(7),
        _user_data(full_name="Renamed", email="renamed@example.com"),
    ]
    assert _run_user_import(account).status == "done"
    updated = _accounts(account)
    assert [(item.uuid, item.owner_user_uuid) for item in updated] == [
        (item.uuid, item.owner_user_uuid) for item in accounts
    ]
    renamed = messenger_models.WorkspaceUser.objects.get_one(
        filters={"uuid": filters.EQ(imported[0].owner_user_uuid)}
    )
    assert renamed.first_name == "Renamed"
    assert renamed.email == "renamed@example.com"
    assert updated[1].settings["email"] == "renamed@example.com"
    assert updated[0].settings == account.settings
    assert zulip.close.call_count == 3


def test_user_import_repairs_native_profiles_and_tracks_deactivation_and_reactivation(
    user_import_account, db, monkeypatch
):
    account = user_import_account
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
    user_uuid = uuid.uuid5(namespace, "user:8")
    messenger_models.WorkspaceUser(
        uuid=user_uuid,
        username=f"zulip-{user_uuid}",
        source="native",
    ).insert()
    imported = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=user_uuid,
        provider="zulip",
        external_account_id=f"{account.get_host()}:8",
        settings={
            "server_url": account.settings["server_url"],
            "email": "old@example.com",
        },
    )
    imported.insert()
    zulip = mock.Mock()
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)

    for is_active in (False, True, False):
        zulip.get_users.return_value = [_user_data(is_active=is_active)]
        assert _run_user_import(account).status == "done"
        restored = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(imported.uuid)}
        )
        profile = messenger_models.WorkspaceUser.objects.get_one(
            filters={"uuid": filters.EQ(user_uuid)}
        )
        assert len(_accounts(account)) == 2
        assert restored.owner_user_uuid == user_uuid
        assert restored.disabled is (not is_active)
        assert profile.disabled is (not is_active)
        assert profile.source == "zulip"
        assert db.execute(
            "SELECT source, disabled FROM m_workspace_directory_users_v1 WHERE uuid=%s",
            (user_uuid,),
        ).fetchone() == ("zulip", not is_active)

        unchanged = restored.updated_at, profile.updated_at
        assert _run_user_import(account).status == "done"
        assert (
            external_models.ExternalAccount.objects.get_one(
                filters={"uuid": filters.EQ(imported.uuid)}
            ).updated_at
            == unchanged[0]
        )
        assert (
            messenger_models.WorkspaceUser.objects.get_one(
                filters={"uuid": filters.EQ(user_uuid)}
            ).updated_at
            == unchanged[1]
        )


@pytest.mark.parametrize("is_active", (True, False))
def test_user_import_reuses_connected_accounts_preserving_credentials_and_iam_profiles(
    user_import_account, db, monkeypatch, is_active
):
    account = user_import_account
    owner_uuid = uuid.uuid4()
    username = f"existing-peer-{owner_uuid}"
    conftest.seed_workspace_user(db, owner_uuid, username)
    peer = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=owner_uuid,
        provider="zulip",
        external_account_id=f"{account.get_host()}:8",
        status="active",
        settings={
            "server_url": account.settings["server_url"] + "/",
            "email": "user-8@example.com",
            "api_key": "peer-key",
        },
    )
    peer.insert()
    zulip = mock.Mock()
    zulip.get_users.return_value = [
        _user_data(email="hidden@example.com", is_active=is_active)
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)

    assert _run_user_import(account).status == "done"
    assert {row.uuid for row in _accounts(account)} == {account.uuid, peer.uuid}
    restored = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(peer.uuid)}
    )
    assert restored.external_account_id == f"{account.get_host()}:8"
    assert restored.owner_user_uuid == owner_uuid
    assert restored.settings == peer.settings
    assert restored.status == "active"
    assert restored.disabled is (not is_active)
    profile = messenger_models.WorkspaceUser.objects.get_one(
        filters={"uuid": filters.EQ(owner_uuid)}
    )
    assert profile.username == username
    assert profile.source == "iam"
    assert profile.first_name is None
    assert profile.disabled is False


@pytest.mark.parametrize("pending_account", ("self", "peer", "other_host"))
def test_user_import_defers_before_reading_or_writing_users_with_unknown_identity(
    user_import_account, db, monkeypatch, pending_account
):
    account = user_import_account
    if pending_account == "self":
        pending = account
        pending.properties["external_account_id"].set_value_force(None)
        pending.update()
    else:
        owner_uuid = uuid.uuid4()
        conftest.seed_workspace_user(db, owner_uuid, f"pending-{owner_uuid}")
        host = account.get_host()
        if pending_account == "other_host":
            host = f"other.{host}"
        pending = external_models.ExternalAccount(
            project_id=account.project_id,
            owner_user_uuid=owner_uuid,
            provider="zulip",
            settings={"server_url": f"https://{host}", "email": "user-8@example.com"},
        )
        pending.insert()
    original = [(row.uuid, row.updated_at, row.settings) for row in _accounts(account)]
    factory = mock.Mock()
    monkeypatch.setattr(client, "ZulipClient", factory)

    before = datetime.datetime.now(datetime.timezone.utc)
    task = _run_user_import(account)
    after = datetime.datetime.now(datetime.timezone.utc)

    assert task.status == "retry"
    assert before <= task.next_retry_at <= after
    assert str(pending.uuid) in task.error_description
    assert "waiting for account verification" in task.error_description
    factory.assert_not_called()
    assert [
        (row.uuid, row.updated_at, row.settings) for row in _accounts(account)
    ] == original
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
    assert (
        messenger_models.WorkspaceUser.objects.get_one_or_none(
            filters={"uuid": filters.EQ(uuid.uuid5(namespace, "user:8"))}
        )
        is None
    )
    assert (
        models.Task.objects.get_all(
            filters={
                "project_id": filters.EQ(account.project_id),
                "status": filters.EQ("new"),
            }
        )
        == []
    )


@pytest.mark.parametrize("different_scope", ("project", "provider"))
def test_unknown_identity_outside_zulip_project_does_not_block_user_import(
    user_import_account, monkeypatch, different_scope
):
    account = user_import_account
    other = external_models.ExternalAccount(
        project_id=uuid.uuid4() if different_scope == "project" else account.project_id,
        owner_user_uuid=account.owner_user_uuid,
        provider="example" if different_scope == "provider" else "zulip",
        settings=account.settings,
    )
    other.insert()
    zulip = mock.Mock()
    zulip.get_users.return_value = [_user_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    try:
        assert _run_user_import(account).status == "done"
        zulip.get_users.assert_called_once_with()
        (imported,) = [
            row
            for row in _accounts(account)
            if row.external_account_id == f"{account.get_host()}:8"
        ]
        assert imported.owner_user_uuid != account.owner_user_uuid
    finally:
        other.delete()


def test_connection_checks_run_before_immediate_user_import_retry(
    user_import_account, db, monkeypatch
):
    account = user_import_account
    peer_owner = uuid.uuid4()
    conftest.seed_workspace_user(db, peer_owner, f"pending-peer-{peer_owner}")
    peer = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=peer_owner,
        provider="zulip",
        settings={
            "server_url": account.settings["server_url"],
            "email": "user-8@example.com",
            "api_key": "peer-key",
        },
    )
    peer.insert()
    users_client = mock.Mock()
    users_client.get_users.return_value = [_user_data()]
    check_client = mock.Mock()
    check_client.check_connection.return_value = {"user_id": 8}
    clients = {account.uuid: users_client, peer.uuid: check_client}
    monkeypatch.setattr(client, "ZulipClient", lambda current: clients[current.uuid])

    sync_task = _run_user_import(account)
    assert sync_task.status == "retry"
    assert sync_task.next_retry_at <= datetime.datetime.now(datetime.timezone.utc)
    check_task = models.Task(
        project_id=account.project_id,
        external_account_uuid=peer.uuid,
        priority=models.TASK_PRIORITY_INITIALIZE_CONNECTION,
        content=models.InitializeConnectionContent(),
    )
    check_task.insert()
    assert check_task.priority == models.TASK_PRIORITY_INITIALIZE_CONNECTION
    assert sync_task.priority == models.TASK_PRIORITY_IMPORT_USERS
    assert check_task.priority > sync_task.priority
    service = history_import.HistoryImportService()
    service._iteration()
    assert (
        models.Task.objects.get_one(
            filters={"uuid": filters.EQ(check_task.uuid)}
        ).status
        == "done"
    )
    assert (
        models.Task.objects.get_one(filters={"uuid": filters.EQ(sync_task.uuid)}).status
        == "retry"
    )
    users_client.get_users.assert_not_called()
    assert (
        external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(peer.uuid)}
        ).external_account_id
        == f"{account.get_host()}:8"
    )

    service._iteration()
    assert (
        models.Task.objects.get_one(filters={"uuid": filters.EQ(sync_task.uuid)}).status
        == "done"
    )
    assert {row.uuid for row in _accounts(account)} == {account.uuid, peer.uuid}
    check_client.check_connection.assert_called_once_with()
    users_client.get_users.assert_called_once_with()


@pytest.mark.parametrize("different_scope", ("project", "host"))
def test_user_import_scopes_external_id_to_host_and_project(
    user_import_account, monkeypatch, different_scope
):
    account = user_import_account
    host = (
        account.get_host()
        if different_scope == "project"
        else f"other.{account.get_host()}"
    )
    other = external_models.ExternalAccount(
        project_id=uuid.uuid4() if different_scope == "project" else account.project_id,
        owner_user_uuid=account.owner_user_uuid
        if different_scope == "project"
        else uuid.uuid4(),
        provider="zulip",
        external_account_id=f"{host}:8",
        settings={
            "server_url": f"https://{host}",
            "email": "user-8@example.com",
            "api_key": "other-key",
        },
    )
    if different_scope == "host":
        messenger_models.WorkspaceUser(
            uuid=other.owner_user_uuid, username=f"other-{other.uuid}"
        ).insert()
    other.insert()
    zulip = mock.Mock()
    zulip.get_users.return_value = [_user_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    try:
        assert _run_user_import(account).status == "done"
        (imported,) = [
            row
            for row in _accounts(account)
            if row.external_account_id == f"{account.get_host()}:8"
        ]
        assert imported.uuid != other.uuid
        assert imported.owner_user_uuid != other.owner_user_uuid
        assert imported.settings.get("api_key") is None
        restored = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(other.uuid)}
        )
        assert restored.settings == other.settings
    finally:
        other.delete()


@pytest.mark.parametrize("phase", ("fetch", "validation", "close"))
def test_failed_user_import_rolls_back_new_profiles_and_accounts(
    user_import_account, monkeypatch, phase
):
    account = user_import_account
    zulip = mock.Mock()
    zulip.get_users.return_value = [_user_data(), _user_data(9, full_name="x" * 129)]
    if phase == "fetch":
        zulip.get_users.side_effect = requests.Timeout("Timed out")
    elif phase == "close":
        zulip.get_users.return_value = [_user_data()]
        zulip.close.side_effect = RuntimeError("Close failed")
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())

    task = _run_user_import(account)
    assert task.status == ("retry" if phase == "fetch" else "error")
    assert [row.uuid for row in _accounts(account)] == [account.uuid]
    assert (
        messenger_models.WorkspaceUser.objects.get_one_or_none(
            filters={"uuid": filters.EQ(uuid.uuid5(namespace, "user:8"))}
        )
        is None
    )
    assert (
        models.Task.objects.get_all(
            filters={
                "project_id": filters.EQ(account.project_id),
                "status": filters.EQ("new"),
            }
        )
        == []
    )
    zulip.close.assert_called_once_with()


def test_external_account_identity_migration_and_lookup_index(_database):
    migration = "0206-Add-provider-account-identity-to-external-accounts-812e0e.py"
    with conftest.isolated_migration_database(
        "0205-Store-Zulip-message-content-timestamps-as-UTC-for-ORM-ee88ee.py"
    ) as database_url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        with psycopg.connect(database_url, autocommit=True) as db:
            owner_uuid, project_id, account_uuid = (
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
            )
            conftest.seed_workspace_user(db, owner_uuid, f"identity-{owner_uuid}")
            db.execute(
                "INSERT INTO m_external_accounts_v2 (uuid, project_id, owner_user_uuid, provider, settings, created_at, updated_at) VALUES (%s, %s, %s, 'zulip', '{}'::jsonb, NOW(), NOW())",
                (account_uuid, project_id, owner_uuid),
            )
            engine.apply_migration(migration)
            assert db.execute(
                "SELECT external_account_id FROM m_external_accounts_v2 WHERE uuid=%s",
                (account_uuid,),
            ).fetchone() == (None,)
            assert db.execute(
                "SELECT disabled FROM m_external_accounts_v2 WHERE uuid=%s",
                (account_uuid,),
            ).fetchone() == (False,)
            db.execute(
                "UPDATE m_external_accounts_v2 SET disabled=TRUE WHERE uuid=%s",
                (account_uuid,),
            )
            assert (
                external_models.ExternalAccount.objects.get_one(
                    filters={"uuid": filters.EQ(account_uuid)}
                ).disabled
                is True
            )
            assert (
                messenger_models.WorkspaceDirectoryUser.objects.get_one(
                    filters={"uuid": filters.EQ(owner_uuid)}
                ).disabled
                is False
            )
            db.execute(
                "UPDATE m_workspace_users SET disabled=TRUE WHERE uuid=%s",
                (owner_uuid,),
            )
            assert (
                messenger_models.WorkspaceDirectoryUser.objects.get_one(
                    filters={"uuid": filters.EQ(owner_uuid)}
                ).disabled
                is True
            )
            db.execute(
                "UPDATE m_external_accounts_v2 SET external_account_id='zulip.example.com:7' WHERE uuid=%s",
                (account_uuid,),
            )
            index = db.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename='m_external_accounts_v2' AND indexname='m_external_accounts_v2_external_account_id_idx'"
            ).fetchone()[0]
            assert "(external_account_id)" in index
            engine.rollback_migration(migration)
            assert db.execute(
                "SELECT uuid FROM m_external_accounts_v2 WHERE uuid=%s", (account_uuid,)
            ).fetchone() == (account_uuid,)
            engine.apply_migration(migration)
