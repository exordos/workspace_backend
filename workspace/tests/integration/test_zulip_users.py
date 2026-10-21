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
from workspace.messenger_api import events as messenger_events
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


@pytest.fixture
def pending_user_connection(user_import_account, db, monkeypatch):
    account = user_import_account
    zulip = mock.Mock()
    zulip.get_users.return_value = [_user_data()]
    zulip.check_connection.return_value = _user_data()
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    assert _run_user_import(account).status == "done"
    imported = external_models.ExternalAccount.objects.get_one(
        filters={
            "project_id": filters.EQ(account.project_id),
            "external_account_id": filters.EQ(f"{account.get_host()}:8"),
        }
    )
    owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"connected-{owner_uuid}")
    pending = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=owner_uuid,
        provider="zulip",
        settings={
            "server_url": account.settings["server_url"],
            "email": "user-8@example.com",
            "api_key": "connected-key",
        },
    )
    pending.insert()
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=pending.uuid,
        priority=models.TASK_PRIORITY_INITIALIZE_CONNECTION,
        content=models.InitializeConnectionContent(),
    )
    task.insert()
    return imported, pending, task


def test_connection_rebinds_imported_account_preserving_identity_and_references(
    user_import_account, pending_user_connection, db, api
):
    imported, pending, task = pending_user_connection
    channel = models.ZulipChannel(
        external_account_uuid=user_import_account.uuid,
        channel_type="private",
        direct_user_uuid=imported.uuid,
        stream_uuid=uuid.uuid4(),
        name="Imported peer",
        description="",
        rendered_description="",
        invite_only=True,
        is_web_public=False,
        history_public_to_subscribers=False,
        is_archived=False,
    )
    channel.insert()

    with contexts.Context().session_manager():
        task.execute()

    restored = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(imported.uuid)}
    )
    assert restored.owner_user_uuid == pending.owner_user_uuid
    assert restored.external_account_id == imported.external_account_id
    assert restored.settings == pending.settings
    assert restored.status == "backfill"
    assert restored.error_description is None
    assert (
        messenger_models.WorkspaceUser.objects.get_one(
            filters={"uuid": filters.EQ(imported.owner_user_uuid)}
        ).source
        == "hidden"
    )
    assert (
        messenger_models.WorkspaceDirectoryUser.objects.get_one_or_none(
            filters={"uuid": filters.EQ(imported.owner_user_uuid)}
        )
        is None
    )
    assert api.get(f"/v1/users/{imported.owner_user_uuid}").status_code == 200
    listed = api.get("/v1/users/", params={"uuid": str(imported.owner_user_uuid)})
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    assert (
        external_models.ExternalAccount.objects.get_one_or_none(
            filters={"uuid": filters.EQ(pending.uuid)}
        )
        is None
    )
    stored_task = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
    assert stored_task.status == "done"
    assert stored_task.external_account_uuid == imported.uuid
    assert (
        models.ZulipChannel.objects.get_one(
            filters={"uuid": filters.EQ(channel.uuid)}
        ).direct_user_uuid
        == imported.uuid
    )
    assert (
        models.Task.objects.get_all(
            filters={
                "external_account_uuid": filters.EQ(imported.uuid),
                "status": "new",
            }
        )
        == []
    )
    events = db.execute(
        "SELECT action, payload->>'uuid' FROM m_workspace_events "
        "WHERE project_id=%s AND user_uuid=%s AND object_type='external_account' "
        "ORDER BY epoch_version",
        (pending.project_id, pending.owner_user_uuid),
    ).fetchall()
    assert events == [
        ("deleted", str(pending.uuid)),
        ("created", str(imported.uuid)),
        ("updated", str(imported.uuid)),
    ]
    models.InitializeConnectionContent().execute(stored_task)
    assert _run_user_import(user_import_account).status == "done"
    assert {row.uuid for row in _accounts(user_import_account)} == {
        user_import_account.uuid,
        imported.uuid,
    }
    assert (
        external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(imported.uuid)}
        ).owner_user_uuid
        == pending.owner_user_uuid
    )


@pytest.mark.parametrize("registered", (False, True))
def test_project_user_reconciliation_follows_account_rebinding(
    pending_user_connection, db, registered
):
    imported, pending, task = pending_user_connection
    if registered:
        db.execute(
            "SELECT messenger_v2_register_project_user(%s, %s)",
            (imported.project_id, imported.owner_user_uuid),
        )

    with contexts.Context().session_manager():
        task.execute()

    assert (
        models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)}).status
        == "done"
    )
    states = {
        state.user_uuid: state
        for state in models.ZulipProjectUserState.objects.get_all(
            filters={"project_id": filters.EQ(imported.project_id)}
        )
    }
    assert imported.owner_user_uuid not in states
    with contexts.Context().session_manager():
        states[pending.owner_user_uuid].do()

    assert db.execute(
        "SELECT user_uuid FROM messenger_project_users "
        "WHERE project_id = %s AND user_uuid = ANY(%s) ORDER BY user_uuid",
        (
            imported.project_id,
            [imported.owner_user_uuid, pending.owner_user_uuid],
        ),
    ).fetchall() == sorted(
        [(pending.owner_user_uuid,)]
        + ([(imported.owner_user_uuid,)] if registered else [])
    )
    assert (
        models.ZulipProjectUserState.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(imported.project_id),
                "user_uuid": filters.EQ(pending.owner_user_uuid),
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "source,has_credentials",
    [("iam", False), ("iam", True), ("native", False), ("zulip", True)],
)
def test_connection_does_not_take_over_an_account_of_another_real_user(
    pending_user_connection, source, has_credentials
):
    imported, pending, task = pending_user_connection
    owner = messenger_models.WorkspaceUser.objects.get_one(
        filters={"uuid": filters.EQ(imported.owner_user_uuid)}
    )
    owner.source = source
    owner.email = f"{owner.uuid}@example.com"
    owner.update()
    if has_credentials:
        imported.settings = {**imported.settings, "api_key": "existing-key"}
        imported.update()

    with contexts.Context().session_manager():
        task.execute()

    restored = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(imported.uuid)}
    )
    assert restored.owner_user_uuid == imported.owner_user_uuid
    assert restored.settings == imported.settings
    rejected = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(pending.uuid)}
    )
    assert rejected.external_account_id is None
    assert rejected.status == "error"
    stored = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
    assert stored.status == "error"
    assert stored.next_retry_at is None
    assert "already connected to another Workspace user" in stored.error_description
    assert rejected.error_description == stored.error_description
    assert [
        row.uuid
        for row in models.Task.objects.get_all(
            filters={"external_account_uuid": filters.EQ(pending.uuid)}
        )
    ] == [task.uuid]


@pytest.mark.parametrize("phase", ("close", "account_update", "event"))
def test_failed_rebinding_preserves_both_accounts_and_original_task_reference(
    pending_user_connection, monkeypatch, phase, db
):
    imported, pending, task = pending_user_connection
    if phase == "close":
        zulip = mock.Mock()
        zulip.check_connection.return_value = _user_data()
        zulip.close.side_effect = RuntimeError("Rebinding failed")
        monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    elif phase == "account_update":
        update = external_models.ExternalAccount.update

        def fail(current, *args, **kwargs):
            if current.uuid == imported.uuid:
                raise RuntimeError("Rebinding failed")
            return update(current, *args, **kwargs)

        monkeypatch.setattr(external_models.ExternalAccount, "update", fail)
    else:

        def fail(*args, **kwargs):
            raise RuntimeError("Rebinding failed")

        monkeypatch.setattr(messenger_events, "create_external_resource_event", fail)

    with contexts.Context().session_manager():
        task.execute()

    restored = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(imported.uuid)}
    )
    assert restored.owner_user_uuid == imported.owner_user_uuid
    assert restored.settings == imported.settings
    assert (
        external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(pending.uuid)}
        ).external_account_id
        is None
    )
    stored = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
    assert stored.status == "error"
    assert stored.external_account_uuid == pending.uuid
    assert "Rebinding failed" in stored.error_description
    assert db.execute(
        "SELECT COUNT(*) FROM m_workspace_events WHERE project_id=%s AND user_uuid=%s",
        (pending.project_id, pending.owner_user_uuid),
    ).fetchone() == (0,)
    assert (
        messenger_models.WorkspaceUser.objects.get_one(
            filters={"uuid": filters.EQ(imported.owner_user_uuid)}
        ).source
        == "zulip"
    )


def test_unbinding_restores_imported_profile_and_preserves_external_account(
    pending_user_connection, api
):
    imported, pending, task = pending_user_connection
    with contexts.Context().session_manager():
        task.execute()
    channel = models.ZulipChannel(
        external_account_uuid=imported.uuid,
        stream_uuid=uuid.uuid4(),
        stream_id=42,
        name="Imported channel",
        description="",
        rendered_description="",
        invite_only=False,
        is_web_public=False,
        history_public_to_subscribers=True,
        is_archived=False,
    )
    channel.insert()

    response = api.delete(
        f"/v1/messenger/external_accounts/{imported.uuid}",
        user=pending.owner_user_uuid,
        project=pending.project_id,
        permissions=("workspace.external_account.delete",),
    )

    assert response.status_code == 204, response.text
    restored = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(imported.uuid)}
    )
    assert restored.owner_user_uuid == imported.owner_user_uuid
    assert restored.external_account_id == imported.external_account_id
    assert restored.settings == imported.settings
    assert restored.status == "new"
    assert (
        models.ZulipChannel.objects.get_one(
            filters={"uuid": filters.EQ(channel.uuid)}
        ).external_account_uuid
        == imported.uuid
    )
    assert (
        messenger_models.WorkspaceDirectoryUser.objects.get_one(
            filters={"uuid": filters.EQ(imported.owner_user_uuid)}
        ).source
        == "zulip"
    )
    assert (
        models.Task.objects.get_all(
            filters={
                "external_account_uuid": filters.EQ(imported.uuid),
                "status": "cancel",
            }
        )
        == []
    )
    assert (
        api.get(
            f"/v1/messenger/external_accounts/{imported.uuid}",
            user=pending.owner_user_uuid,
            project=pending.project_id,
            permissions=("workspace.external_account.read",),
        ).status_code
        == 404
    )
    created = api.post(
        "/v1/messenger/external_accounts/",
        user=pending.owner_user_uuid,
        project=pending.project_id,
        permissions=("workspace.external_account.create",),
        json={"provider": "zulip", "settings": pending.settings},
    )
    assert created.status_code == 201, created.text
    (check,) = models.Task.objects.get_all(
        filters={"external_account_uuid": uuid.UUID(created.json()["uuid"])}
    )
    with contexts.Context().session_manager():
        check.execute()
    assert check.status == "done"
    assert check.external_account_uuid == imported.uuid
    assert (
        messenger_models.WorkspaceUser.objects.get_one(
            filters={"uuid": filters.EQ(imported.owner_user_uuid)}
        ).source
        == "hidden"
    )


def test_unbinding_creates_missing_imported_profile_and_cancels_only_pending_tasks(
    user_import_account, api
):
    account = user_import_account
    tasks = []
    for status in ("new", "waiting", "retry", "done", "error", "cancel"):
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            content=models.ImportUsersContent(),
            status=status,
        )
        task.insert()
        tasks.append(task)

    response = api.delete(
        f"/v1/messenger/external_accounts/{account.uuid}",
        user=account.owner_user_uuid,
        project=account.project_id,
        permissions=("workspace.external_account.delete",),
    )

    assert response.status_code == 204, response.text
    restored = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(account.uuid)}
    )
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
    assert restored.owner_user_uuid == uuid.uuid5(namespace, "user:7")
    assert restored.external_account_id == account.external_account_id
    assert "api_key" not in restored.settings
    profile = messenger_models.WorkspaceDirectoryUser.objects.get_one(
        filters={"uuid": filters.EQ(restored.owner_user_uuid)}
    )
    assert profile.source == "zulip"
    assert profile.email == account.settings["email"]
    assert profile.status == "offline"
    assert (
        messenger_models.WorkspaceUser.objects.get_one(
            filters={"uuid": filters.EQ(account.owner_user_uuid)}
        ).source
        == "iam"
    )
    assert [
        models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)}).status
        for task in tasks
    ] == ["cancel", "cancel", "cancel", "done", "error", "cancel"]


def test_first_connection_creates_hidden_profile_before_user_synchronization(
    user_import_account, monkeypatch, api
):
    account = user_import_account
    account.properties["external_account_id"].set_value_force(None)
    account.update()
    zulip = mock.Mock()
    zulip.check_connection.return_value = _user_data(7)
    zulip.get_users.return_value = [_user_data(7, full_name="Updated owner")]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.InitializeConnectionContent(),
    )
    task.insert()

    with contexts.Context().session_manager():
        task.execute()

    assert task.status == "done"
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
    user_uuid = uuid.uuid5(namespace, "user:7")
    profile = messenger_models.WorkspaceUser.objects.get_one(
        filters={"uuid": filters.EQ(user_uuid)}
    )
    assert profile.source == "hidden"
    assert profile.first_name == "User 7"
    assert profile.email == "user-7@example.com"
    assert (
        messenger_models.WorkspaceDirectoryUser.objects.get_one_or_none(
            filters={"uuid": filters.EQ(user_uuid)}
        )
        is None
    )
    assert _run_user_import(account).status == "done"
    profile = messenger_models.WorkspaceUser.objects.get_one(
        filters={"uuid": filters.EQ(user_uuid)}
    )
    assert profile.source == "hidden"
    assert profile.first_name == "Updated owner"
    assert (
        external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(account.uuid)}
        ).owner_user_uuid
        == account.owner_user_uuid
    )

    response = api.delete(
        f"/v1/messenger/external_accounts/{account.uuid}",
        user=account.owner_user_uuid,
        project=account.project_id,
        permissions=("workspace.external_account.delete",),
    )
    assert response.status_code == 204, response.text
    assert (
        external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(account.uuid)}
        ).owner_user_uuid
        == user_uuid
    )
    assert (
        messenger_models.WorkspaceDirectoryUser.objects.get_one(
            filters={"uuid": filters.EQ(user_uuid)}
        ).source
        == "zulip"
    )


def test_failed_unbinding_rolls_back_profile_visibility_and_account_ownership(
    pending_user_connection, api, monkeypatch
):
    imported, pending, task = pending_user_connection
    with contexts.Context().session_manager():
        task.execute()

    def fail(*args, **kwargs):
        raise RuntimeError("Cancellation failed")

    monkeypatch.setattr(models.Task, "cancel_pending_tasks", fail)
    response = api.delete(
        f"/v1/messenger/external_accounts/{imported.uuid}",
        user=pending.owner_user_uuid,
        project=pending.project_id,
        permissions=("workspace.external_account.delete",),
    )

    assert response.status_code == 500
    restored = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(imported.uuid)}
    )
    assert restored.owner_user_uuid == pending.owner_user_uuid
    assert restored.settings == pending.settings
    assert (
        messenger_models.WorkspaceUser.objects.get_one(
            filters={"uuid": filters.EQ(imported.owner_user_uuid)}
        ).source
        == "hidden"
    )


@pytest.mark.parametrize("scope", ("project", "provider", "host"))
def test_connection_does_not_claim_external_identity_from_another_scope(
    pending_user_connection, scope
):
    imported, pending, task = pending_user_connection
    if scope == "project":
        imported.properties["project_id"].set_value_force(uuid.uuid4())
    elif scope == "provider":
        imported.provider = "other"
    else:
        imported.properties["external_account_id"].set_value_force(
            "other.example.com:8"
        )
    imported.update()
    try:
        with contexts.Context().session_manager():
            task.execute()

        assert task.status == "done"
        assert task.external_account_uuid == pending.uuid
        assert (
            external_models.ExternalAccount.objects.get_one(
                filters={"uuid": filters.EQ(imported.uuid)}
            ).owner_user_uuid
            == imported.owner_user_uuid
        )
        assert (
            external_models.ExternalAccount.objects.get_one(
                filters={"uuid": filters.EQ(pending.uuid)}
            ).external_account_id
            == f"{pending.get_host()}:8"
        )
    finally:
        imported.delete()


@pytest.mark.pre_retirement_schema
def test_hidden_profile_directory_migration_can_be_applied_and_rolled_back(_database):
    migration = "0210-Hide-rebound-Zulip-profiles-from-the-user-directory-751a1a.py"
    with conftest.isolated_migration_database(
        "0209-Add-Zulip-channel-types-and-direct-account-references-6bdbf2.py",
        current_models=True,
    ):
        user = messenger_models.WorkspaceUser(
            username=f"hidden-{uuid.uuid4()}", source="hidden"
        )
        user.insert()
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        engine.apply_migration(migration)
        assert (
            messenger_models.WorkspaceDirectoryUser.objects.get_one_or_none(
                filters={"uuid": filters.EQ(user.uuid)}
            )
            is None
        )
        assert (
            messenger_models.WorkspaceUser.objects.get_one(
                filters={"uuid": filters.EQ(user.uuid)}
            ).source
            == "hidden"
        )
        engine.rollback_migration(migration)
        assert (
            messenger_models.WorkspaceDirectoryUser.objects.get_one(
                filters={"uuid": filters.EQ(user.uuid)}
            ).source
            == "zulip"
        )


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

    task = _run_user_import(account)
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
    assert pending == []
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


@pytest.mark.parametrize("include_realm_users", (False, True))
def test_user_import_always_creates_missing_system_bots(
    user_import_account, monkeypatch, include_realm_users
):
    account = user_import_account
    bots = [
        _user_data(
            user_id,
            full_name=name,
            email=email,
            is_bot=True,
            is_system_bot=True,
        )
        for user_id, name, email in (
            (1, "Email Gateway", "emailgateway@zulip.com"),
            (6, "Notification Bot", "notification-bot@zulip.com"),
        )
    ]
    active, inactive = _user_data(8), _user_data(9)
    del active["is_active"], inactive["is_active"]
    registration = {
        "result": "success",
        "queue_id": "test-queue",
        "realm_users": [active] if include_realm_users else [],
        "realm_non_active_users": [inactive] if include_realm_users else [],
        "cross_realm_bots": bots,
    }
    monkeypatch.setattr(
        client.ZulipClient,
        "_request",
        lambda self, method, path, **kwargs: (
            registration if method == "POST" else {"result": "success"}
        ),
    )

    assert _run_user_import(account).status == "done"
    imported = {row.external_account_id: row for row in _accounts(account)}
    assert len(imported) == (5 if include_realm_users else 3)
    for bot in bots:
        stored = imported[f"{account.get_host()}:{bot['user_id']}"]
        assert stored.disabled is False
        assert stored.settings == {
            "server_url": account.settings["server_url"],
            "email": bot["email"],
        }
        profile = messenger_models.WorkspaceUser.objects.get_one(
            filters={"uuid": filters.EQ(stored.owner_user_uuid)},
        )
        assert profile.first_name == bot["full_name"]
        assert profile.email == bot["email"]
        assert profile.source == "zulip"
        assert profile.disabled is False
        assert profile.status == "offline"
    if include_realm_users:
        assert imported[f"{account.get_host()}:8"].disabled is False
        assert imported[f"{account.get_host()}:9"].disabled is True

    assert _run_user_import(account).status == "done"
    assert {row.uuid: row.updated_at for row in _accounts(account)} == {
        row.uuid: row.updated_at for row in imported.values()
    }
    missing = imported[f"{account.get_host()}:6"]
    missing.delete()
    assert _run_user_import(account).status == "done"
    restored = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(missing.uuid)},
    )
    assert restored.owner_user_uuid == missing.owner_user_uuid
    assert restored.external_account_id == missing.external_account_id
    assert restored.settings == missing.settings


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
    user_import_account, db, monkeypatch, scheduled_reconciliation
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
    check_client.check_connection.return_value = _user_data()
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


@pytest.mark.pre_retirement_schema
def test_external_account_identity_migration_and_lookup_index(_database):
    migration = "0206-Add-provider-account-identity-to-external-accounts-812e0e.py"
    with conftest.isolated_migration_database(
        "0205-Store-Zulip-message-content-timestamps-as-UTC-for-ORM-ee88ee.py",
        current_models=True,
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
